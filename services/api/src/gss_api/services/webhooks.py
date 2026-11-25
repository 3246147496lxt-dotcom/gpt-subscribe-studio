"""Safe Webhook alert dispatcher and config store.

Security guarantees:
- Never sends session tokens, card numbers, billing addresses, or proxy passwords.
- Rejects non-HTTP(S) URLs and cloud metadata hosts (SSRF protection).
- Persists webhook config in ``$GSS_CONFIG_DIR/webhooks.json`` with ``0600`` mode
  and never echoes the HMAC secret back to API callers.
- Dispatches asynchronously off the critical path with bounded timeout & retry.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Optional
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
    "webhook_test": "Webhook 测试告警",
}


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


@dataclass
class WebhookConfig:
    enabled: bool = False
    url: str = ""
    secret: str = ""
    events: list[str] = field(default_factory=lambda: list(SUPPORTED_EVENTS))

    def public(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "url_masked": mask_webhook_url(self.url),
            "has_url": bool(self.url),
            "has_secret": bool(self.secret),
            "events": [e for e in self.events if e in SUPPORTED_EVENTS],
            "supported_events": list(SUPPORTED_EVENTS),
        }


class WebhookManager:
    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._explicit_path = config_path
        self._lock = threading.RLock()
        self._deliveries: deque[dict[str, Any]] = deque(maxlen=30)
        self._tasks: set[asyncio.Task] = set()

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
            return WebhookConfig(
                enabled=bool(data.get("enabled")),
                url=str(data.get("url") or "").strip(),
                secret=str(data.get("secret") or ""),
                events=events,
            )

    def save(
        self,
        *,
        enabled: bool,
        url: Optional[str] = None,
        secret: Optional[str] = None,
        clear_secret: bool = False,
        events: Optional[list[str]] = None,
    ) -> WebhookConfig:
        with self._lock:
            current = self.load()
            next_url = current.url if url is None else (validate_webhook_url(url) if url.strip() else "")
            if enabled and not next_url:
                raise ValueError("启用 Webhook 前请先填写有效的 Webhook URL")
            if clear_secret:
                next_secret = ""
            elif secret is not None and secret != "":
                next_secret = secret.strip()
            else:
                next_secret = current.secret
            if events is None:
                next_events = current.events or list(SUPPORTED_EVENTS)
            else:
                next_events = [str(e) for e in events if str(e) in SUPPORTED_EVENTS]
            cfg = WebhookConfig(
                enabled=bool(enabled),
                url=next_url,
                secret=next_secret,
                events=next_events,
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
        )
        clean_data: dict[str, Any] = {}
        for key in safe_keys:
            val = data.get(key)
            if val is not None and val != "":
                clean_data[key] = redact_sensitive(str(val))[:300]
        raw_msg = redact_sensitive(str(data.get("message") or ""))[:300]
        if raw_msg:
            clean_data["message"] = raw_msg
        title = _EVENT_TITLES.get(event, f"订阅工作台事件：{event}")
        summary_parts = [title]
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

    def dispatch(self, event: str, data: dict[str, Any]) -> bool:
        """Schedule asynchronous delivery if enabled and subscribed. Never blocks caller."""
        cfg = self.load()
        if not cfg.enabled or not cfg.url or event not in cfg.events:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        task = loop.create_task(self.deliver(cfg, event, data, max_attempts=2))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def send_test(self) -> dict[str, Any]:
        cfg = self.load()
        if not cfg.url:
            return {"ok": False, "status_code": None, "elapsed_ms": 0, "error": "请先保存有效的 Webhook URL", "delivery": None}
        record = await self.deliver(
            cfg,
            "webhook_test",
            {"status": "ok", "stage": "test", "message": "这是一条来自 gpt-subscribe-studio 的测试告警消息（不含任何敏感数据）。"},
            max_attempts=1,
        )
        return {
            "ok": record["ok"],
            "status_code": record["status_code"],
            "elapsed_ms": record["elapsed_ms"],
            "error": record["error"],
            "delivery": record,
        }

    async def drain(self, timeout_s: float = 5.0) -> int:
        pending = [t for t in list(self._tasks) if not t.done()]
        if not pending:
            return 0
        _done, still = await asyncio.wait(pending, timeout=max(0.0, timeout_s))
        return len(still)

    def health_summary(self) -> dict[str, Any]:
        cfg = self.load()
        return {
            "enabled": cfg.enabled,
            "configured": bool(cfg.url),
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
    "mask_webhook_url",
]
