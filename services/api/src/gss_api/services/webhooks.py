"""Safe notification dispatcher (Webhook + Telegram Bot) and config store.

Security guarantees:
- Never sends session tokens, card numbers, billing addresses, or proxy passwords.
- Rejects non-HTTP(S) URLs and cloud metadata hosts (SSRF protection).
- Persists config in ``$GSS_CONFIG_DIR/webhooks.json`` with ``0600`` mode
  and never echoes the HMAC secret or Telegram Bot Token back to API callers.
- Dispatches asynchronously off the critical path with bounded timeout & retry.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit
import uuid

import httpx

from gss_core.redaction import redact_sensitive

logger = logging.getLogger("gss.webhooks")

SUPPORTED_EVENTS = (
    "succeeded",
    "failed",
    "unknown",
    "requires_action",
    "paid_pending_delivery",
    "reconcile_resolved",
    "manual_verdict",
)

_BLOCKED_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.goog",
    "100.100.100.200",
}

_EVENT_TITLES = {
    "succeeded": "订阅充值成功",
    "failed": "订阅任务未完成",
    "unknown": "警告：扣款结果未知，账号已加锁",
    "requires_action": "需要人工/持卡人验证（3DS 或官方确认）",
    "paid_pending_delivery": "已扣款，等待订阅交付核验",
    "reconcile_resolved": "自动对账已确认交付并核销",
    "manual_verdict": "人工判定已执行",
    "webhook_test": "告警通知测试",
}

_EVENT_ICONS = {
    "succeeded": "✅",
    "failed": "❌",
    "unknown": "⚠️",
    "requires_action": "🔐",
    "paid_pending_delivery": "⏳",
    "reconcile_resolved": "🔄",
    "manual_verdict": "🛠️",
    "webhook_test": "🔔",
}

_PLAN_DISPLAY = {
    "go": "Go",
    "plus": "Plus",
    "pro_lite": "Pro100",
    "prolite": "Pro100",
    "pro": "Pro200",
    "pro_max": "Pro500",
    "promax": "Pro500",
    "team": "Team",
}

_SOURCE_DISPLAY = {
    "quick_pay": "一键充值",
    "public_portal": "自助门户",
    "open_api": "开放 API",
    "protocol_step": "分步协议",
    "orders_step": "分步下单",
    "reconciler": "自动对账",
    "manual_verdict": "人工判定",
}

_AUTO_RENEW_DISPLAY = {
    "canceled": "已自动关闭",
    "pending": "关闭中",
    "failed": "关闭失败（建议人工核查）",
    "skipped": "已跳过",
    "disabled": "",
}

_DEFAULT_TELEGRAM_API_BASE = "https://api.telegram.org"
_TG_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{20,128}$")
_TG_CHAT_ID_RE = re.compile(r"^(?:-?\d{5,20}|@[A-Za-z][A-Za-z0-9_]{3,63})$")

# Suppresses inner direct-order alerts while run_quick_pay owns the order lifecycle.
_SUPPRESS_DIRECT_ALERTS: ContextVar[bool] = ContextVar("gss_suppress_direct_alerts", default=False)


@contextmanager
def suppress_direct_order_alerts() -> Iterator[None]:
    token = _SUPPRESS_DIRECT_ALERTS.set(True)
    try:
        yield
    finally:
        _SUPPRESS_DIRECT_ALERTS.reset(token)


def direct_order_alerts_suppressed() -> bool:
    return bool(_SUPPRESS_DIRECT_ALERTS.get())


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _allow_http() -> bool:
    return os.environ.get("GSS_WEBHOOK_ALLOW_HTTP", "").strip().lower() in {"1", "true", "yes", "on"}


def validate_webhook_url(raw_url: str) -> str:
    url = str(raw_url or "").strip()
    if not url:
        return ""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme == "http":
        if not _allow_http():
            raise ValueError("Webhook 地址必须使用 https://（本地调试可设置 GSS_WEBHOOK_ALLOW_HTTP=1）")
    elif scheme != "https":
        raise ValueError("Webhook 地址仅支持 https:// 协议")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("Webhook 地址缺少有效主机名")
    if host in _BLOCKED_HOSTS:
        raise ValueError("Webhook 地址不允许指向元数据保留主机")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ValueError("Webhook 地址不允许指向链路本地或保留 IP")
    except ValueError as exc:
        if "Webhook" in str(exc):
            raise
    return url


def validate_telegram_token(raw_token: str) -> str:
    token = str(raw_token or "").strip()
    if not token:
        return ""
    if not _TG_TOKEN_RE.match(token):
        raise ValueError("Telegram Bot Token 格式无效（应为类似 123456789:AA... 的 BotFather 令牌）")
    return token


def validate_telegram_chat_id(raw_chat_id: str) -> str:
    chat_id = str(raw_chat_id or "").strip()
    if not chat_id:
        return ""
    if not _TG_CHAT_ID_RE.match(chat_id):
        raise ValueError("Telegram Chat ID 格式无效（应为数字 ID、-100 开头的群组 ID 或 @频道名）")
    return chat_id


def validate_telegram_api_base(raw_base: str) -> str:
    cleaned = str(raw_base or "").strip().rstrip("/")
    if not cleaned:
        return _DEFAULT_TELEGRAM_API_BASE
    validated = validate_webhook_url(cleaned)
    return validated.rstrip("/")


def validate_telegram_thread_id(raw_thread_id: Optional[int | str]) -> Optional[int]:
    if raw_thread_id is None:
        return None
    text = str(raw_thread_id).strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError as exc:
        raise ValueError("Telegram 话题 ID（message_thread_id）必须为正整数") from exc
    if value <= 0:
        raise ValueError("Telegram 话题 ID（message_thread_id）必须为正整数")
    return value


def mask_webhook_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or ""
    if len(path) > 18:
        path = path[:10] + "..." + path[-6:]
    query_hint = "?***" if parsed.query else ""
    return f"{parsed.scheme}://{host}{port}{path}{query_hint}"


def mask_telegram_token(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""
    if ":" in raw:
        bot_id, secret = raw.split(":", 1)
        tail = secret[-4:] if len(secret) >= 8 else ""
        return f"{bot_id}:***{tail}"
    return "***"


def mask_email(raw: str) -> str:
    text = str(raw or "").strip()
    if not text or "@" not in text:
        return ""
    local, domain = text.split("@", 1)
    if not local or not domain:
        return ""
    head = local[:2] if len(local) >= 3 else local[:1]
    return f"{head}***@{domain}"


def format_plan_display(plan: str) -> str:
    raw = str(plan or "").strip()
    if not raw:
        return ""
    return _PLAN_DISPLAY.get(raw.lower(), raw)


def _strip_bot_token(text: str, token: str) -> str:
    cleaned = redact_sensitive(str(text or ""))
    if token:
        cleaned = cleaned.replace(token, "[REDACTED]")
    return cleaned


@dataclass
class WebhookConfig:
    enabled: bool = False
    url: str = ""
    secret: str = ""
    events: list[str] = field(default_factory=lambda: list(SUPPORTED_EVENTS))
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_thread_id: Optional[int] = None
    telegram_api_base: str = _DEFAULT_TELEGRAM_API_BASE
    telegram_silent: bool = False
    telegram_bot_commands_enabled: bool = True
    telegram_digest_enabled: bool = True
    telegram_digest_hour: int = 22
    telegram_streak_threshold: int = 3
    telegram_disk_warn_mb: int = 1024

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def public(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "url_masked": mask_webhook_url(self.url),
            "has_url": bool(self.url),
            "has_secret": bool(self.secret),
            "events": [e for e in self.events if e in SUPPORTED_EVENTS],
            "supported_events": list(SUPPORTED_EVENTS),
            "telegram_enabled": self.telegram_enabled,
            "telegram_has_token": bool(self.telegram_bot_token),
            "telegram_token_masked": mask_telegram_token(self.telegram_bot_token),
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_thread_id": self.telegram_thread_id,
            "telegram_api_base": self.telegram_api_base or _DEFAULT_TELEGRAM_API_BASE,
            "telegram_silent": self.telegram_silent,
            "telegram_configured": self.telegram_configured,
            "telegram_bot_commands_enabled": self.telegram_bot_commands_enabled,
            "telegram_digest_enabled": self.telegram_digest_enabled,
            "telegram_digest_hour": self.telegram_digest_hour,
            "telegram_streak_threshold": self.telegram_streak_threshold,
            "telegram_disk_warn_mb": self.telegram_disk_warn_mb,
        }


class WebhookManager:
    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._explicit_path = config_path
        self._lock = threading.RLock()
        self._deliveries: deque[dict[str, Any]] = deque(maxlen=50)
        self._tasks: set[asyncio.Task] = set()
        self._recent_keys: dict[tuple[str, str], float] = {}

    def _path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        config_dir = Path(os.environ.get("GSS_CONFIG_DIR", "config"))
        return config_dir / "webhooks.json"

    def load(self) -> WebhookConfig:
        with self._lock:
            path = self._path()
            if not path.is_file():
                return WebhookConfig()
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return WebhookConfig()
            if not isinstance(data, dict):
                return WebhookConfig()
            events_raw = data.get("events")
            if isinstance(events_raw, list):
                events = [str(e) for e in events_raw if str(e) in SUPPORTED_EVENTS]
            else:
                events = list(SUPPORTED_EVENTS)
            thread_raw = data.get("telegram_thread_id")
            try:
                thread_id = validate_telegram_thread_id(thread_raw)
            except ValueError:
                thread_id = None
            def _int_opt(key: str, default: int, lo: int, hi: int) -> int:
                raw = data.get(key)
                if raw is None or raw == "":
                    return default
                try:
                    return max(lo, min(hi, int(raw)))
                except (TypeError, ValueError):
                    return default

            return WebhookConfig(
                enabled=bool(data.get("enabled")),
                url=str(data.get("url") or "").strip(),
                secret=str(data.get("secret") or ""),
                events=events,
                telegram_enabled=bool(data.get("telegram_enabled")),
                telegram_bot_token=str(data.get("telegram_bot_token") or "").strip(),
                telegram_chat_id=str(data.get("telegram_chat_id") or "").strip(),
                telegram_thread_id=thread_id,
                telegram_api_base=str(data.get("telegram_api_base") or "").strip().rstrip("/") or _DEFAULT_TELEGRAM_API_BASE,
                telegram_silent=bool(data.get("telegram_silent")),
                telegram_bot_commands_enabled=bool(data.get("telegram_bot_commands_enabled", True)),
                telegram_digest_enabled=bool(data.get("telegram_digest_enabled", True)),
                telegram_digest_hour=_int_opt("telegram_digest_hour", 22, 0, 23),
                telegram_streak_threshold=_int_opt("telegram_streak_threshold", 3, 0, 20),
                telegram_disk_warn_mb=_int_opt("telegram_disk_warn_mb", 1024, 0, 51200),
            )

    def save(
        self,
        *,
        enabled: Optional[bool] = None,
        url: Optional[str] = None,
        secret: Optional[str] = None,
        clear_secret: bool = False,
        events: Optional[list[str]] = None,
        telegram_enabled: Optional[bool] = None,
        telegram_bot_token: Optional[str] = None,
        clear_telegram_token: bool = False,
        telegram_chat_id: Optional[str] = None,
        telegram_thread_id: Optional[int | str] = None,
        clear_telegram_thread_id: bool = False,
        telegram_api_base: Optional[str] = None,
        telegram_silent: Optional[bool] = None,
        telegram_bot_commands_enabled: Optional[bool] = None,
        telegram_digest_enabled: Optional[bool] = None,
        telegram_digest_hour: Optional[int] = None,
        telegram_streak_threshold: Optional[int] = None,
        telegram_disk_warn_mb: Optional[int] = None,
    ) -> WebhookConfig:
        with self._lock:
            current = self.load()
            next_enabled = current.enabled if enabled is None else bool(enabled)
            next_url = current.url if url is None else (validate_webhook_url(url) if url.strip() else "")
            if next_enabled and not next_url:
                raise ValueError("启用 Webhook 前请先填写有效的 Webhook URL")
            if clear_secret:
                next_secret = ""
            elif secret is not None and secret != "":
                next_secret = secret.strip()
            else:
                next_secret = current.secret

            next_tg_enabled = current.telegram_enabled if telegram_enabled is None else bool(telegram_enabled)
            if clear_telegram_token:
                next_tg_token = ""
            elif telegram_bot_token is not None and telegram_bot_token.strip() != "":
                next_tg_token = validate_telegram_token(telegram_bot_token)
            else:
                next_tg_token = current.telegram_bot_token

            if telegram_chat_id is None:
                next_tg_chat_id = current.telegram_chat_id
            elif telegram_chat_id.strip() == "":
                next_tg_chat_id = ""
            else:
                next_tg_chat_id = validate_telegram_chat_id(telegram_chat_id)

            if clear_telegram_thread_id:
                next_tg_thread_id = None
            elif telegram_thread_id is not None:
                next_tg_thread_id = validate_telegram_thread_id(telegram_thread_id)
            else:
                next_tg_thread_id = current.telegram_thread_id

            if telegram_api_base is None:
                next_tg_api_base = current.telegram_api_base or _DEFAULT_TELEGRAM_API_BASE
            else:
                next_tg_api_base = validate_telegram_api_base(telegram_api_base)

            next_tg_silent = current.telegram_silent if telegram_silent is None else bool(telegram_silent)
            next_tg_cmds = (
                current.telegram_bot_commands_enabled
                if telegram_bot_commands_enabled is None
                else bool(telegram_bot_commands_enabled)
            )
            next_tg_digest = (
                current.telegram_digest_enabled
                if telegram_digest_enabled is None
                else bool(telegram_digest_enabled)
            )
            if telegram_digest_hour is None:
                next_tg_hour = current.telegram_digest_hour
            else:
                if not (0 <= int(telegram_digest_hour) <= 23):
                    raise ValueError("每日简报推送小时须在 0 到 23 之间（北京时间）")
                next_tg_hour = int(telegram_digest_hour)
            if telegram_streak_threshold is None:
                next_tg_streak = current.telegram_streak_threshold
            else:
                if not (0 <= int(telegram_streak_threshold) <= 20):
                    raise ValueError("连续失败预警阈值须在 0 到 20 之间（0 表示关闭）")
                next_tg_streak = int(telegram_streak_threshold)
            if telegram_disk_warn_mb is None:
                next_tg_disk = current.telegram_disk_warn_mb
            else:
                if not (0 <= int(telegram_disk_warn_mb) <= 51200):
                    raise ValueError("磁盘余量预警阈值须在 0 到 51200 MB 之间（0 表示关闭）")
                next_tg_disk = int(telegram_disk_warn_mb)

            if next_tg_enabled and (not next_tg_token or not next_tg_chat_id):
                raise ValueError("启用 Telegram 通知前请先填写 Bot Token 与 Chat ID")

            if events is None:
                next_events = current.events or list(SUPPORTED_EVENTS)
            else:
                next_events = [str(e) for e in events if str(e) in SUPPORTED_EVENTS]

            cfg = WebhookConfig(
                enabled=next_enabled,
                url=next_url,
                secret=next_secret,
                events=next_events,
                telegram_enabled=next_tg_enabled,
                telegram_bot_token=next_tg_token,
                telegram_chat_id=next_tg_chat_id,
                telegram_thread_id=next_tg_thread_id,
                telegram_api_base=next_tg_api_base,
                telegram_silent=next_tg_silent,
                telegram_bot_commands_enabled=next_tg_cmds,
                telegram_digest_enabled=next_tg_digest,
                telegram_digest_hour=next_tg_hour,
                telegram_streak_threshold=next_tg_streak,
                telegram_disk_warn_mb=next_tg_disk,
            )
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            payload = json.dumps(
                {
                    "enabled": cfg.enabled,
                    "url": cfg.url,
                    "secret": cfg.secret,
                    "events": cfg.events,
                    "telegram_enabled": cfg.telegram_enabled,
                    "telegram_bot_token": cfg.telegram_bot_token,
                    "telegram_chat_id": cfg.telegram_chat_id,
                    "telegram_thread_id": cfg.telegram_thread_id,
                    "telegram_api_base": cfg.telegram_api_base,
                    "telegram_silent": cfg.telegram_silent,
                    "telegram_bot_commands_enabled": cfg.telegram_bot_commands_enabled,
                    "telegram_digest_enabled": cfg.telegram_digest_enabled,
                    "telegram_digest_hour": cfg.telegram_digest_hour,
                    "telegram_streak_threshold": cfg.telegram_streak_threshold,
                    "telegram_disk_warn_mb": cfg.telegram_disk_warn_mb,
                },
                ensure_ascii=False,
                indent=2,
            )
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return cfg

    def list_deliveries(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._deliveries)

    def _record_delivery(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._deliveries.appendleft(record)

    @staticmethod
    def build_payload(event: str, data: dict[str, Any]) -> dict[str, Any]:
        safe_keys = (
            "job_id", "order_id", "status", "stage", "failure_code",
            "amount", "currency", "lock_id", "next_action_url",
            "plan", "exit_country", "source", "auto_renew_status",
            "elapsed_s", "operator", "previous_status", "verdict",
        )
        clean_data: dict[str, Any] = {}
        for key in safe_keys:
            val = data.get(key)
            if val is not None and val != "":
                clean_data[key] = redact_sensitive(str(val))[:300]
        raw_email = data.get("account_email")
        if raw_email:
            masked = mask_email(str(raw_email))
            if masked:
                clean_data["account_email"] = masked
        raw_msg = redact_sensitive(str(data.get("message") or ""))[:300]
        if raw_msg:
            clean_data["message"] = raw_msg
        title = _EVENT_TITLES.get(event, f"订阅工作台事件：{event}")
        summary_parts = [title]
        if clean_data.get("plan"):
            summary_parts.append(f"套餐: {format_plan_display(clean_data['plan'])}")
        if clean_data.get("order_id"):
            summary_parts.append(f"订单: {clean_data['order_id'][:12]}")
        if clean_data.get("job_id"):
            summary_parts.append(f"作业: {clean_data['job_id'][:12]}")
        if clean_data.get("amount"):
            summary_parts.append(f"金额: {clean_data['amount']} {clean_data.get('currency', '')}".strip())
        if clean_data.get("failure_code"):
            summary_parts.append(f"错误码: {clean_data['failure_code']}")
        if raw_msg:
            summary_parts.append(raw_msg)
        text = " | ".join(summary_parts)
        return {
            "source": "gpt-subscribe-studio",
            "event": event,
            "title": title,
            "text": text,
            "msg_type": "text",
            "content": {"text": text},
            "timestamp": _utcnow_iso(),
            "data": clean_data,
        }

    @classmethod
    def build_telegram_html(cls, event: str, data: dict[str, Any]) -> str:
        payload = cls.build_payload(event, data)
        clean = payload["data"]
        icon = _EVENT_ICONS.get(event, "ℹ️")
        title = html.escape(payload["title"], quote=False)
        lines = [f"{icon} <b>{title}</b>"]

        meta_bits: list[str] = []
        if clean.get("plan"):
            meta_bits.append(f"套餐 <code>{html.escape(format_plan_display(clean['plan']), quote=False)}</code>")
        if clean.get("exit_country"):
            meta_bits.append(f"出口 <code>{html.escape(str(clean['exit_country']).upper(), quote=False)}</code>")
        if clean.get("source"):
            src = _SOURCE_DISPLAY.get(str(clean["source"]), str(clean["source"]))
            meta_bits.append(f"入口 <code>{html.escape(src, quote=False)}</code>")
        if meta_bits:
            lines.append("• " + " · ".join(meta_bits))

        if clean.get("account_email"):
            lines.append(f"• 账号：<code>{html.escape(clean['account_email'], quote=False)}</code>")

        money_bits: list[str] = []
        if clean.get("amount"):
            curr = str(clean.get("currency") or "").upper()
            amt_txt = f"{curr} {clean['amount']}".strip()
            money_bits.append(f"金额 <code>{html.escape(amt_txt, quote=False)}</code>")
        if clean.get("elapsed_s"):
            money_bits.append(f"耗时 <code>{html.escape(str(clean['elapsed_s']), quote=False)}s</code>")
        if money_bits:
            lines.append("• " + " · ".join(money_bits))

        id_bits: list[str] = []
        if clean.get("order_id"):
            id_bits.append(f"订单 <code>{html.escape(clean['order_id'][:12], quote=False)}</code>")
        if clean.get("job_id"):
            id_bits.append(f"作业 <code>{html.escape(clean['job_id'][:12], quote=False)}</code>")
        if id_bits:
            lines.append("• " + " · ".join(id_bits))

        if clean.get("verdict"):
            prev_s = clean.get("previous_status") or "-"
            curr_s = clean.get("status") or clean.get("verdict")
            op = clean.get("operator") or "admin"
            lines.append(
                f"• 判定：<code>{html.escape(str(prev_s), quote=False)}</code> → "
                f"<code>{html.escape(str(curr_s), quote=False)}</code>（操作人 {html.escape(str(op), quote=False)}）"
            )

        if clean.get("failure_code"):
            lines.append(f"• 错误码：<code>{html.escape(clean['failure_code'], quote=False)}</code>")

        ar_text = _AUTO_RENEW_DISPLAY.get(str(clean.get("auto_renew_status") or ""), "")
        if ar_text:
            lines.append(f"• 自动续费：{html.escape(ar_text, quote=False)}")

        if clean.get("lock_id"):
            lines.append(f"• 资金锁：<code>{html.escape(clean['lock_id'][:16], quote=False)}</code>")

        if clean.get("message"):
            lines.append(f"• 摘要：{html.escape(clean['message'], quote=False)}")

        next_url = str(clean.get("next_action_url") or "")
        if next_url.startswith("https://"):
            lines.append(f"• 验证链接：{html.escape(next_url, quote=False)}")

        return "\n".join(lines)

    @staticmethod
    def sign_body(secret: str, body_bytes: bytes) -> str:
        digest = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    async def deliver(self, cfg: WebhookConfig, event: str, data: dict[str, Any], *, max_attempts: int = 2) -> dict[str, Any]:
        payload = self.build_payload(event, data)
        body_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "gpt-subscribe-studio-webhook/1.0",
            "X-GSS-Event": event,
        }
        if cfg.secret:
            headers["X-GSS-Signature-256"] = self.sign_body(cfg.secret, body_bytes)

        started = time.perf_counter()
        status_code: Optional[int] = None
        last_error: Optional[str] = None
        attempts = 0
        ok = False
        for attempt in range(max(1, max_attempts)):
            attempts = attempt + 1
            try:
                async with httpx.AsyncClient(timeout=6.0, follow_redirects=False, trust_env=False) as client:
                    resp = await client.post(cfg.url, content=body_bytes, headers=headers)
                    status_code = int(resp.status_code)
                    if 200 <= status_code < 300:
                        ok = True
                        last_error = None
                        break
                    last_error = f"HTTP {status_code}"
            except Exception as exc:  # noqa: BLE001
                last_error = redact_sensitive(type(exc).__name__)
            if attempt + 1 < max_attempts:
                await asyncio.sleep(1.5)

        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        record = {
            "id": uuid.uuid4().hex[:12],
            "channel": "webhook",
            "event": event,
            "ok": ok,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms,
            "attempts": attempts,
            "error": last_error,
            "created_at": _utcnow_iso(),
        }
        self._record_delivery(record)
        if not ok:
            logger.warning("webhook_delivery_failed event=%s status=%s err=%s", event, status_code, last_error)
        return record

    @staticmethod
    def build_telegram_reply_markup(
        cfg: WebhookConfig,
        event: str,
        data: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        if not cfg.telegram_bot_commands_enabled:
            return None
        order_id = str(data.get("order_id") or "").strip()
        if event == "webhook_test":
            return {
                "inline_keyboard": [[
                    {"text": "📊 今日状态", "callback_data": "st:"},
                    {"text": "📋 最近订单", "callback_data": "ol:"},
                ]]
            }
        if not order_id or len(order_id) > 56:
            return None
        ar_status = str(data.get("auto_renew_status") or "").strip().lower()
        if event in {"unknown", "requires_action", "paid_pending_delivery"}:
            return {
                "inline_keyboard": [[
                    {"text": "🔍 订单详情", "callback_data": f"od:{order_id}"},
                    {"text": "🔄 只读对账", "callback_data": f"rc:{order_id}"},
                ]]
            }
        if event in {"succeeded", "reconcile_resolved"} and ar_status in {"failed", "manual_required", "pending"}:
            return {
                "inline_keyboard": [[
                    {"text": "🔍 订单详情", "callback_data": f"od:{order_id}"},
                    {"text": "🛑 重试关闭续费", "callback_data": f"ar:{order_id}"},
                ]]
            }
        return {
            "inline_keyboard": [[
                {"text": "🔍 订单详情", "callback_data": f"od:{order_id}"},
                {"text": "📊 今日状态", "callback_data": "st:"},
            ]]
        }

    async def deliver_telegram(
        self,
        cfg: WebhookConfig,
        event: str,
        data: dict[str, Any],
        *,
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        api_base = (cfg.telegram_api_base or _DEFAULT_TELEGRAM_API_BASE).rstrip("/")
        endpoint = f"{api_base}/bot{cfg.telegram_bot_token}/sendMessage"
        body_obj: dict[str, Any] = {
            "chat_id": cfg.telegram_chat_id,
            "text": self.build_telegram_html(event, data),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": bool(cfg.telegram_silent),
        }
        if cfg.telegram_thread_id is not None:
            body_obj["message_thread_id"] = int(cfg.telegram_thread_id)
        reply_markup = self.build_telegram_reply_markup(cfg, event, data)
        if reply_markup is not None:
            body_obj["reply_markup"] = reply_markup
        body_bytes = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "gpt-subscribe-studio-telegram/1.0",
        }

        started = time.perf_counter()
        status_code: Optional[int] = None
        last_error: Optional[str] = None
        attempts = 0
        ok = False
        for attempt in range(max(1, max_attempts)):
            attempts = attempt + 1
            try:
                async with httpx.AsyncClient(timeout=8.0, follow_redirects=False, trust_env=False) as client:
                    resp = await client.post(endpoint, content=body_bytes, headers=headers)
                    status_code = int(resp.status_code)
                    tg_ok = True
                    tg_desc = ""
                    try:
                        parsed = resp.json() if hasattr(resp, "json") else None
                        if isinstance(parsed, dict):
                            if "ok" in parsed:
                                tg_ok = bool(parsed.get("ok"))
                            if parsed.get("description"):
                                tg_desc = _strip_bot_token(str(parsed.get("description")), cfg.telegram_bot_token)[:140]
                    except Exception:  # noqa: BLE001
                        pass
                    if 200 <= status_code < 300 and tg_ok:
                        ok = True
                        last_error = None
                        break
                    last_error = f"HTTP {status_code}: {tg_desc}" if tg_desc else f"HTTP {status_code}"
                    # Do not retry deterministic 4xx Telegram errors (invalid token/chat_id/markdown).
                    if 400 <= status_code < 500 and status_code != 429:
                        break
            except Exception as exc:  # noqa: BLE001
                last_error = _strip_bot_token(type(exc).__name__, cfg.telegram_bot_token)
            if attempt + 1 < max_attempts:
                await asyncio.sleep(1.5)

        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        record = {
            "id": uuid.uuid4().hex[:12],
            "channel": "telegram",
            "event": event,
            "ok": ok,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms,
            "attempts": attempts,
            "error": last_error,
            "created_at": _utcnow_iso(),
        }
        self._record_delivery(record)
        if not ok:
            logger.warning("telegram_delivery_failed event=%s status=%s err=%s", event, status_code, last_error)
        return record

    def _should_dedupe(self, event: str, data: dict[str, Any]) -> bool:
        order_id = str(data.get("order_id") or "").strip()
        if not order_id or event in {"webhook_test", "manual_verdict"}:
            return False
        now = time.monotonic()
        key = (order_id, event)
        with self._lock:
            expired = [k for k, ts in self._recent_keys.items() if now - ts > 15.0]
            for k in expired:
                self._recent_keys.pop(k, None)
            prev = self._recent_keys.get(key)
            if prev is not None and now - prev <= 15.0:
                return True
            self._recent_keys[key] = now
        return False

    def dispatch(self, event: str, data: dict[str, Any]) -> bool:
        """Schedule asynchronous delivery to every enabled channel. Never blocks caller."""
        cfg = self.load()
        if event not in cfg.events:
            return False
        want_webhook = bool(cfg.enabled and cfg.url)
        want_telegram = bool(cfg.telegram_enabled and cfg.telegram_configured)
        if not want_webhook and not want_telegram:
            return False
        if self._should_dedupe(event, data):
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        scheduled = False
        if want_webhook:
            task = loop.create_task(self.deliver(cfg, event, data, max_attempts=2))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            scheduled = True
        if want_telegram:
            task = loop.create_task(self.deliver_telegram(cfg, event, data, max_attempts=2))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            scheduled = True
            try:
                from . import telegram_bot
                telegram_bot.on_order_event(cfg, event, data)
            except Exception:  # noqa: BLE001
                pass
        return scheduled

    async def send_test(self, channel: str = "all") -> dict[str, Any]:
        cfg = self.load()
        target = str(channel or "all").strip().lower()
        if target not in {"all", "webhook", "telegram"}:
            target = "all"
        test_data = {
            "status": "ok",
            "stage": "test",
            "plan": "plus",
            "exit_country": "PH",
            "source": "quick_pay",
            "message": "这是一条来自 gpt-subscribe-studio 的测试告警消息（不含任何敏感数据）。",
        }
        deliveries: list[dict[str, Any]] = []

        if target in {"all", "webhook"}:
            if cfg.url:
                deliveries.append(await self.deliver(cfg, "webhook_test", test_data, max_attempts=1))
            elif target == "webhook":
                return {
                    "ok": False,
                    "status_code": None,
                    "elapsed_ms": 0,
                    "error": "请先保存有效的 Webhook URL",
                    "delivery": None,
                    "deliveries": [],
                }

        if target in {"all", "telegram"}:
            if cfg.telegram_configured:
                deliveries.append(await self.deliver_telegram(cfg, "webhook_test", test_data, max_attempts=1))
            elif target == "telegram":
                return {
                    "ok": False,
                    "status_code": None,
                    "elapsed_ms": 0,
                    "error": "请先保存有效的 Telegram Bot Token 与 Chat ID",
                    "delivery": None,
                    "deliveries": [],
                }

        if not deliveries:
            return {
                "ok": False,
                "status_code": None,
                "elapsed_ms": 0,
                "error": "请先保存 Telegram 或 Webhook 配置后再测试",
                "delivery": None,
                "deliveries": [],
            }

        all_ok = all(item["ok"] for item in deliveries)
        primary = deliveries[0] if len(deliveries) == 1 else next((d for d in deliveries if not d["ok"]), deliveries[0])
        errors = [f"{d['channel']}: {d['error']}" for d in deliveries if not d["ok"] and d.get("error")]
        return {
            "ok": all_ok,
            "status_code": primary.get("status_code"),
            "elapsed_ms": sum(int(d.get("elapsed_ms") or 0) for d in deliveries),
            "error": "；".join(errors) if errors else None,
            "delivery": primary,
            "deliveries": deliveries,
        }

    async def drain(self, timeout_s: float = 5.0) -> int:
        pending = [t for t in list(self._tasks) if not t.done()]
        if not pending:
            return 0
        _done, still = await asyncio.wait(pending, timeout=max(0.0, timeout_s))
        return len(still)

    def health_summary(self) -> dict[str, Any]:
        cfg = self.load()
        webhook_ready = bool(cfg.enabled and cfg.url)
        telegram_ready = bool(cfg.telegram_enabled and cfg.telegram_configured)
        return {
            "enabled": bool(cfg.enabled or cfg.telegram_enabled),
            "configured": bool(cfg.url or cfg.telegram_configured),
            "webhook_enabled": cfg.enabled,
            "webhook_configured": bool(cfg.url),
            "telegram_enabled": cfg.telegram_enabled,
            "telegram_configured": cfg.telegram_configured,
            "active_channels": [
                name
                for name, ready in (("telegram", telegram_ready), ("webhook", webhook_ready))
                if ready
            ],
            "subscribed_events": len(cfg.events),
            "recent_deliveries": len(self._deliveries),
        }


manager = WebhookManager()


def dispatch_event(event: str, data: dict[str, Any]) -> bool:
    return manager.dispatch(event, data)


async def drain_webhooks(timeout_s: float = 5.0) -> int:
    return await manager.drain(timeout_s=timeout_s)


__all__ = [
    "SUPPORTED_EVENTS",
    "WebhookConfig",
    "WebhookManager",
    "manager",
    "dispatch_event",
    "drain_webhooks",
    "validate_webhook_url",
    "validate_telegram_token",
    "validate_telegram_chat_id",
    "validate_telegram_api_base",
    "validate_telegram_thread_id",
    "mask_webhook_url",
    "mask_telegram_token",
    "mask_email",
    "format_plan_display",
    "suppress_direct_order_alerts",
    "direct_order_alerts_suppressed",
]
