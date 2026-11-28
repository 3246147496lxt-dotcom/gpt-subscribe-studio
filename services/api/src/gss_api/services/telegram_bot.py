"""Interactive Telegram Bot service: slash commands, inline action buttons,
daily Beijing-time digest, consecutive-failure streak alerts, and low-disk alerts.

Security & financial safety guarantees:
- Strictly allows ONLY the configured ``telegram_chat_id``; all other chats/users
  are silently ignored.
- Never echoes Bot Token, sessions, card numbers, CVC, or billing addresses.
- Inline actions are strictly bounded to safe operations:
  * ``od:<id>`` read-only order detail lookup
  * ``rc:<id>`` strictly read-only reconciliation (never submits a new charge)
  * ``ar:<id>`` post-payment auto-renew cancellation retry (reuses original vault only)
"""
from __future__ import annotations

import asyncio
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
import html
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Optional

from fastapi import HTTPException
import httpx

from gss_core.redaction import redact_sensitive

from ..persistence import get_db_optional, resolve_db_path
from . import webhooks

logger = logging.getLogger("gss.telegram_bot")

BEIJING = timezone(timedelta(hours=8))
_DEFAULT_TELEGRAM_API_BASE = "https://api.telegram.org"
_ORDER_PREFIX_RE = re.compile(r"^[0-9a-fA-F-]{4,48}$")

_BOT_COMMANDS = [
    {"command": "status", "description": "今日统计、成功率、队列与磁盘余量"},
    {"command": "orders", "description": "查看最近订单列表（默认 8 单）"},
    {"command": "order", "description": "查单详情，用法 /order 单号前缀"},
    {"command": "locks", "description": "查看当前活跃资金锁"},
    {"command": "disk", "description": "查看数据库体积与磁盘余量"},
    {"command": "digest", "description": "立即发送今日运行简报"},
    {"command": "help", "description": "查看机器人指令与快捷菜单"},
]

_STATUS_ICON = {
    "delivered": "✅",
    "active": "✅",
    "failed": "❌",
    "rejected": "❌",
    "unknown": "⚠️",
    "requires_action": "🔐",
    "verifying": "⏳",
    "paying": "⏳",
    "awaiting_payment": "💤",
    "pending_checkout": "💤",
    "refunded": "↩️",
}

_AUTO_RENEW_LABEL = {
    "canceled": "已自动关闭",
    "pending": "待关闭",
    "canceling_auto_renew": "关闭中",
    "failed": "关闭失败",
    "manual_required": "需人工关闭（会话已过期）",
    "skipped": "已跳过",
    "disabled": "未开启",
}

_bg_task: Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None
_state_lock = threading.RLock()
_streak_orders: deque[dict[str, str]] = deque(maxlen=20)
_streak_seen_ids: set[str] = set()
_last_disk_alert_mono: float = 0.0
_registered_token_hash: str = ""
_last_poll_ok_at: Optional[str] = None
_last_poll_error: Optional[str] = None
_processed_updates: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bj_now(now_utc: Optional[datetime] = None) -> datetime:
    base = now_utc or _utcnow()
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(BEIJING)


def _fmt_bj(iso_str: Optional[str]) -> str:
    raw = str(iso_str or "").strip()
    if not raw:
        return "-"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BEIJING).strftime("%m-%d %H:%M")
    except ValueError:
        return raw[:16]


def _esc(val: Any) -> str:
    return html.escape(redact_sensitive(str(val or "")), quote=False)


def _state_file() -> Path:
    config_dir = Path(os.environ.get("GSS_CONFIG_DIR", "config"))
    return config_dir / "telegram-bot-state.json"


def _load_bot_state() -> dict[str, Any]:
    with _state_lock:
        path = _state_file()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


def _save_bot_state(state: dict[str, Any]) -> None:
    with _state_lock:
        path = _state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        body = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _data_disk_info() -> dict[str, Any]:
    db_path = Path(resolve_db_path()) if resolve_db_path() != ":memory:" else Path.cwd()
    target = db_path.parent if db_path.parent.exists() else Path.cwd()
    free_mb = 0
    total_mb = 0
    used_pct = 0.0
    try:
        usage = shutil.disk_usage(target)
        free_mb = int(usage.free // (1024 * 1024))
        total_mb = int(usage.total // (1024 * 1024))
        if usage.total > 0:
            used_pct = round(usage.used * 100.0 / usage.total, 1)
    except OSError:
        pass
    db = get_db_optional()
    db_stats = db.maintenance_stats() if db is not None else {}
    return {
        "mount_path": str(target),
        "free_mb": free_mb,
        "total_mb": total_mb,
        "used_pct": used_pct,
        "db_bytes": int(db_stats.get("db_bytes") or 0),
        "wal_bytes": int(db_stats.get("wal_bytes") or 0),
    }


def _today_bj_utc_bounds(now_utc: Optional[datetime] = None) -> tuple[str, str, str]:
    bj = _bj_now(now_utc)
    start_bj = bj.replace(hour=0, minute=0, second=0, microsecond=0)
    end_bj = start_bj + timedelta(days=1)
    return (
        start_bj.astimezone(timezone.utc).isoformat(),
        end_bj.astimezone(timezone.utc).isoformat(),
        start_bj.strftime("%Y-%m-%d"),
    )


def collect_period_stats(start_utc_iso: str, end_utc_iso: str) -> dict[str, Any]:
    db = get_db_optional()
    if db is None:
        return {
            "total": 0, "delivered": 0, "failed": 0, "unknown": 0, "pending": 0,
            "success_rate": None, "paid_by_currency": {}, "top_errors": [],
            "auto_renew_canceled": 0, "auto_renew_failed": 0,
        }
    with db._lock:
        rows = db._conn.execute(
            """SELECT o.id, o.status, o.payment_status, o.delivery_status,
                      o.amount, o.currency, o.failure_code, o.auto_renew_status,
                      j.status AS job_status, j.failure_code AS job_failure_code
               FROM orders o
               LEFT JOIN quick_pay_jobs j ON j.order_id = o.id
               WHERE o.created_at >= ? AND o.created_at < ?
               ORDER BY o.created_at DESC""",
            (start_utc_iso, end_utc_iso),
        ).fetchall()

    seen: set[str] = set()
    total = delivered = failed = unknown = pending = 0
    ar_canceled = ar_failed = 0
    paid_by_currency: dict[str, float] = {}
    err_counter: Counter[str] = Counter()

    for r in rows:
        oid = str(r["id"])
        if oid in seen:
            continue
        seen.add(oid)
        total += 1
        st = str(r["status"] or "").lower()
        ps = str(r["payment_status"] or "").lower()
        js = str(r["job_status"] or "").lower()
        ar = str(r["auto_renew_status"] or "").lower()
        if ar == "canceled":
            ar_canceled += 1
        elif ar in {"failed", "manual_required"}:
            ar_failed += 1

        if st in {"delivered", "active"}:
            delivered += 1
            try:
                amt = float(r["amount"] or 0)
            except (TypeError, ValueError):
                amt = 0.0
            if amt > 0:
                curr = str(r["currency"] or "USD").upper()
                paid_by_currency[curr] = round(paid_by_currency.get(curr, 0.0) + amt, 2)
        elif st == "unknown" or js == "unknown":
            unknown += 1
            code = str(r["failure_code"] or r["job_failure_code"] or "unknown").strip()
            if code:
                err_counter[code] += 1
        elif st in {"failed", "rejected"} or js == "failed":
            failed += 1
            code = str(r["failure_code"] or r["job_failure_code"] or st or "failed").strip()
            if code:
                err_counter[code] += 1
        elif ps == "succeeded" or st in {"verifying", "paying", "requires_action", "awaiting_payment"}:
            pending += 1

    settled = delivered + failed + unknown
    rate = round(delivered * 100.0 / settled, 1) if settled > 0 else None
    return {
        "total": total,
        "delivered": delivered,
        "failed": failed,
        "unknown": unknown,
        "pending": pending,
        "success_rate": rate,
        "paid_by_currency": paid_by_currency,
        "top_errors": err_counter.most_common(5),
        "auto_renew_canceled": ar_canceled,
        "auto_renew_failed": ar_failed,
    }


def list_recent_orders(limit: int = 8) -> list[dict[str, Any]]:
    db = get_db_optional()
    if db is None:
        return []
    lim = max(1, min(20, int(limit)))
    with db._lock:
        rows = db._conn.execute(
            """SELECT o.id, o.plan, o.status, o.payment_status, o.delivery_status,
                      o.amount, o.currency, o.exit_country, o.account_email,
                      o.failure_code, o.last_error, o.auto_cancel_renew, o.auto_renew_status,
                      o.created_at, j.status AS job_status, j.failure_code AS job_failure_code,
                      j.message AS job_message
               FROM orders o
               LEFT JOIN quick_pay_jobs j ON j.order_id = o.id
               ORDER BY o.created_at DESC
               LIMIT ?""",
            (lim * 2,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        oid = str(r["id"])
        if oid in seen:
            continue
        seen.add(oid)
        out.append(dict(r))
        if len(out) >= lim:
            break
    return out


def find_orders_by_prefix(prefix: str) -> list[dict[str, Any]]:
    cleaned = str(prefix or "").strip()
    if not _ORDER_PREFIX_RE.match(cleaned):
        return []
    db = get_db_optional()
    if db is None:
        return []
    with db._lock:
        rows = db._conn.execute(
            """SELECT o.id, o.plan, o.status, o.payment_status, o.delivery_status,
                      o.amount, o.currency, o.exit_country, o.billing_country, o.account_email,
                      o.failure_code, o.last_error, o.auto_cancel_renew, o.auto_renew_status,
                      o.auto_renew_error, o.created_at, o.finished_at,
                      j.id AS job_id, j.status AS job_status, j.stage AS job_stage,
                      j.failure_code AS job_failure_code, j.message AS job_message, j.lock_id
               FROM orders o
               LEFT JOIN quick_pay_jobs j ON j.order_id = o.id
               WHERE o.id = ? OR o.id LIKE ?
               ORDER BY o.created_at DESC
               LIMIT 5""",
            (cleaned, f"{cleaned}%"),
        ).fetchall()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        oid = str(r["id"])
        if oid in seen:
            continue
        seen.add(oid)
        out.append(dict(r))
    return out


# ── Message formatters & inline keyboards ────────────────────────────────────

def _main_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "📊 今日状态", "callback_data": "st:"},
                {"text": "📋 最近订单", "callback_data": "ol:"},
            ],
            [
                {"text": "🔒 活跃资金锁", "callback_data": "lk:"},
                {"text": "💾 磁盘与健康", "callback_data": "dk:"},
            ],
            [
                {"text": "📰 立即发今日简报", "callback_data": "dg:"},
            ],
        ]
    }


def format_help_message() -> tuple[str, dict[str, Any]]:
    text = "\n".join([
        "🤖 <b>落雪订阅助手 · 指令菜单</b>",
        "",
        "• /status — 今日统计、成功率、队列与磁盘余量",
        "• /orders [数量] — 最近订单列表（默认 8 单，最多 20）",
        "• /order &lt;单号前缀&gt; — 查单详情并挂操作按钮",
        "• /locks — 查看活跃资金锁",
        "• /disk — 数据库体积与磁盘余量",
        "• /digest — 立即生成并发送今日运行简报",
        "",
        "💡 所有告警卡片底部按钮可直接一键「只读对账」或「重试关闭续费」，绝不重复扣款。",
    ])
    return text, _main_menu_keyboard()


def format_status_message(now_utc: Optional[datetime] = None) -> tuple[str, dict[str, Any]]:
    from . import order_flow, reconciler

    start_iso, end_iso, date_bj = _today_bj_utc_bounds(now_utc)
    today = collect_period_stats(start_iso, end_iso)
    db = get_db_optional()
    all_time = db.order_summary_stats() if db is not None else {
        "total": 0, "delivered": 0, "paid_pending_delivery": 0, "unknown": 0, "failed": 0, "active_locks": 0,
    }
    q = order_flow.queue_stats()
    rec = reconciler.status()
    disk = _data_disk_info()

    rate_txt = f"{today['success_rate']}%" if today["success_rate"] is not None else "-"
    paid_parts = [f"{curr} {amt:g}" for curr, amt in sorted(today["paid_by_currency"].items())]
    paid_txt = " / ".join(paid_parts) if paid_parts else "0"

    lines = [
        f"📊 <b>运行状态（北京时间 {date_bj}）</b>",
        "",
        f"<b>今日订单</b>：共 <code>{today['total']}</code> 单 · 成功 <code>{today['delivered']}</code> · 失败 <code>{today['failed']}</code> · 未知 <code>{today['unknown']}</code>",
        f"• 今日成功率：<code>{rate_txt}</code> · 成交额 <code>{_esc(paid_txt)}</code>",
        f"• 自动关续费：已关闭 <code>{today['auto_renew_canceled']}</code> · 异常 <code>{today['auto_renew_failed']}</code>",
        "",
        f"<b>累计账本</b>：共 <code>{all_time['total']}</code> 单（成功 <code>{all_time['delivered']}</code> · 待核验 <code>{all_time['paid_pending_delivery']}</code> · 未知 <code>{all_time['unknown']}</code>）",
        f"• 活跃资金锁：<code>{all_time['active_locks']}</code> 把",
        f"• 执行队列：运行 <code>{q['running']}/{q['max_concurrency']}</code> · 排队 <code>{q['queued']}</code>",
        f"• 自动对账器：{'运行中' if rec['running'] else '未运行'}（已核销 <code>{rec['resolved_count']}</code>）",
        f"• 数据盘余量：<code>{disk['free_mb']} MB</code>（已用 {disk['used_pct']}%）",
    ]
    kb = {
        "inline_keyboard": [
            [
                {"text": "🔄 刷新状态", "callback_data": "st:"},
                {"text": "📋 最近订单", "callback_data": "ol:"},
            ],
            [
                {"text": "🔒 活跃资金锁", "callback_data": "lk:"},
                {"text": "📰 今日简报", "callback_data": "dg:"},
            ],
        ]
    }
    return "\n".join(lines), kb


def format_orders_message(limit: int = 8) -> tuple[str, dict[str, Any]]:
    rows = list_recent_orders(limit=limit)
    if not rows:
        return "📋 <b>最近订单</b>\n\n暂无订单记录。", _main_menu_keyboard()

    lines = [f"📋 <b>最近 {len(rows)} 笔订单</b>（点按钮看详情）", ""]
    buttons: list[list[dict[str, str]]] = []
    row_buf: list[dict[str, str]] = []

    for idx, r in enumerate(rows):
        oid = str(r["id"])
        short_id = oid[:8]
        st = str(r["status"] or "").lower()
        js = str(r["job_status"] or "").lower()
        eff_st = js if st == "awaiting_payment" and js in {"failed", "unknown"} else st
        icon = _STATUS_ICON.get(eff_st, "•")
        plan = webhooks.format_plan_display(str(r["plan"] or ""))
        email = webhooks.mask_email(str(r["account_email"] or ""))
        amt = f"{str(r['currency'] or '').upper()} {r['amount']}".strip() if r.get("amount") else "-"
        code = str(r["failure_code"] or r["job_failure_code"] or "").strip()
        ts = _fmt_bj(r.get("created_at"))
        tail = f" · <code>{_esc(code)}</code>" if code and eff_st not in {"delivered", "active"} else ""
        email_bit = f" · {_esc(email)}" if email else ""
        lines.append(
            f"{icon} <code>{short_id}</code> <b>{_esc(plan)}</b> · {_esc(amt)}{email_bit}{tail} ({ts})"
        )
        if idx < 6:
            row_buf.append({"text": f"{icon} {short_id} {plan}", "callback_data": f"od:{oid}"})
            if len(row_buf) == 2:
                buttons.append(row_buf)
                row_buf = []
    if row_buf:
        buttons.append(row_buf)
    buttons.append([
        {"text": "🔄 刷新列表", "callback_data": "ol:"},
        {"text": "📊 今日状态", "callback_data": "st:"},
    ])
    return "\n".join(lines), {"inline_keyboard": buttons}


def _order_action_keyboard(row: dict[str, Any]) -> dict[str, Any]:
    oid = str(row["id"])
    st = str(row.get("status") or "").lower()
    ps = str(row.get("payment_status") or "").lower()
    ds = str(row.get("delivery_status") or "").lower()
    ar = str(row.get("auto_renew_status") or "").lower()
    auto_cancel = bool(row.get("auto_cancel_renew"))

    action_row: list[dict[str, str]] = []
    can_reconcile = (
        st in {"unknown", "requires_action", "verifying"}
        or (ps == "succeeded" and ds != "delivered")
    )
    if can_reconcile:
        action_row.append({"text": "🔄 只读对账", "callback_data": f"rc:{oid}"})

    can_retry_ar = ps == "succeeded" and auto_cancel and ar not in {"canceled", "disabled"}
    if can_retry_ar:
        action_row.append({"text": "🛑 重试关闭续费", "callback_data": f"ar:{oid}"})

    rows: list[list[dict[str, str]]] = []
    if action_row:
        rows.append(action_row)
    rows.append([
        {"text": "🔄 刷新详情", "callback_data": f"od:{oid}"},
        {"text": "📋 最近订单", "callback_data": "ol:"},
    ])
    return {"inline_keyboard": rows}


def format_order_detail_message(prefix: str) -> tuple[str, dict[str, Any]]:
    cleaned = str(prefix or "").strip()
    if not cleaned:
        return "用法：<code>/order 单号前缀</code>（至少 4 位，如 <code>/order 3b79c362</code>）", _main_menu_keyboard()
    matches = find_orders_by_prefix(cleaned)
    if not matches:
        return f"未找到匹配 <code>{_esc(cleaned)}</code> 的订单。", _main_menu_keyboard()
    if len(matches) > 1 and matches[0]["id"] != cleaned:
        lines = [f"匹配到 {len(matches)} 笔订单，请选择："]
        btns = []
        for m in matches:
            oid = str(m["id"])
            plan = webhooks.format_plan_display(str(m.get("plan") or ""))
            lines.append(f"• <code>{oid[:12]}</code> {plan} ({_fmt_bj(m.get('created_at'))})")
            btns.append([{"text": f"🔍 {oid[:12]} {plan}", "callback_data": f"od:{oid}"}])
        return "\n".join(lines), {"inline_keyboard": btns}

    r = matches[0]
    oid = str(r["id"])
    st = str(r.get("status") or "").lower()
    icon = _STATUS_ICON.get(st, "•")
    plan = webhooks.format_plan_display(str(r.get("plan") or ""))
    email = webhooks.mask_email(str(r.get("account_email") or "")) or "-"
    exit_c = str(r.get("exit_country") or r.get("billing_country") or "-").upper()
    amt = f"{str(r.get('currency') or '').upper()} {r['amount']}".strip() if r.get("amount") else "-"
    ar = _AUTO_RENEW_LABEL.get(str(r.get("auto_renew_status") or "").lower(), str(r.get("auto_renew_status") or "-"))
    code = str(r.get("failure_code") or r.get("job_failure_code") or "").strip()
    err = str(r.get("last_error") or r.get("job_message") or r.get("auto_renew_error") or "").strip()

    lines = [
        f"{icon} <b>订单详情</b> <code>{oid[:12]}</code>",
        "",
        f"• 状态：<code>{_esc(st)}</code>（支付 <code>{_esc(r.get('payment_status') or '-')}</code> / 交付 <code>{_esc(r.get('delivery_status') or '-')}</code>）",
        f"• 套餐：<code>{_esc(plan)}</code> · 出口 <code>{_esc(exit_c)}</code> · 金额 <code>{_esc(amt)}</code>",
        f"• 账号：<code>{_esc(email)}</code>",
        f"• 自动续费：{_esc(ar)}",
        f"• 时间：{_fmt_bj(r.get('created_at'))}（北京时间）",
    ]
    if r.get("lock_id"):
        lines.append(f"• 资金锁：<code>{_esc(str(r['lock_id'])[:16])}</code>")
    if code:
        lines.append(f"• 错误码：<code>{_esc(code)}</code>")
    if err:
        lines.append(f"• 说明：{_esc(err[:220])}")

    db = get_db_optional()
    if db is not None:
        events = db.list_events(order_id=oid)[-3:]
        if events:
            lines.append("")
            lines.append("<b>最近轨迹</b>：")
            for ev in events:
                ev_msg = str(ev.get("message") or ev.get("code") or ev.get("event") or "")[:90]
                lines.append(f"  · {_fmt_bj(ev.get('created_at'))} [{_esc(ev.get('stage') or ev.get('event'))}] {_esc(ev_msg)}")

    return "\n".join(lines), _order_action_keyboard(r)


def format_locks_message() -> tuple[str, dict[str, Any]]:
    db = get_db_optional()
    if db is not None:
        held, total, _counts = db.list_locks(state="held", limit=10)
    else:
        held, total = [], 0
    if not held:
        return "🔒 <b>活跃资金锁</b>\n\n当前没有挂起（held）的资金锁。", _main_menu_keyboard()

    lines = [f"🔒 <b>活跃资金锁（共 {total} 把）</b>", ""]
    btns: list[list[dict[str, str]]] = []
    for item in held:
        lid = str(item.get("id") or "")
        oid = str(item.get("order_id") or "")
        reason = str(item.get("reason") or "-")
        ts = _fmt_bj(item.get("created_at"))
        lines.append(f"• 锁 <code>{_esc(lid[:12])}</code> · 订单 <code>{_esc(oid[:12] or '-')}</code> · {_esc(reason)} ({ts})")
        if oid:
            btns.append([
                {"text": f"🔍 订单 {oid[:8]}", "callback_data": f"od:{oid}"},
                {"text": f"🔄 只读对账 {oid[:8]}", "callback_data": f"rc:{oid}"},
            ])
    btns.append([{"text": "🔄 刷新资金锁", "callback_data": "lk:"}])
    return "\n".join(lines), {"inline_keyboard": btns}


def format_disk_message() -> tuple[str, dict[str, Any]]:
    disk = _data_disk_info()
    db_kb = round(disk["db_bytes"] / 1024.0, 1)
    wal_kb = round(disk["wal_bytes"] / 1024.0, 1)
    warn = " ⚠️ 偏低，建议在本地执行 ./scripts/deploy.sh --prune-old" if 0 < disk["free_mb"] < 1024 else ""
    lines = [
        "💾 <b>存储与磁盘余量</b>",
        "",
        f"• 数据盘余量：<code>{disk['free_mb']} MB</code> / <code>{disk['total_mb']} MB</code>（已用 {disk['used_pct']}%）{warn}",
        f"• 主库 gss.db：<code>{db_kb} KB</code> · WAL：<code>{wal_kb} KB</code>",
        f"• 挂载路径：<code>{_esc(disk['mount_path'])}</code>",
    ]
    kb = {
        "inline_keyboard": [[
            {"text": "🔄 刷新", "callback_data": "dk:"},
            {"text": "📊 今日状态", "callback_data": "st:"},
        ]]
    }
    return "\n".join(lines), kb


def format_daily_digest_message(now_utc: Optional[datetime] = None) -> tuple[str, dict[str, Any]]:
    start_iso, end_iso, date_bj = _today_bj_utc_bounds(now_utc)
    st = collect_period_stats(start_iso, end_iso)
    db = get_db_optional()
    summary = db.order_summary_stats() if db is not None else {"active_locks": 0}
    disk = _data_disk_info()

    rate_txt = f"{st['success_rate']}%" if st["success_rate"] is not None else "暂无结算单"
    paid_parts = [f"{curr} {amt:g}" for curr, amt in sorted(st["paid_by_currency"].items())]
    paid_txt = " / ".join(paid_parts) if paid_parts else "0"

    lines = [
        f"📰 <b>每日运行简报（{date_bj} · 北京时间）</b>",
        "",
        f"• 总订单：<code>{st['total']}</code> 单",
        f"• 成功交付：<code>{st['delivered']}</code> · 失败：<code>{st['failed']}</code> · 未知：<code>{st['unknown']}</code> · 进行中/待核验：<code>{st['pending']}</code>",
        f"• 今日成功率：<code>{rate_txt}</code>",
        f"• 今日成交额：<code>{_esc(paid_txt)}</code>",
        f"• 自动关续费：成功 <code>{st['auto_renew_canceled']}</code> · 异常 <code>{st['auto_renew_failed']}</code>",
        f"• 当前活跃资金锁：<code>{summary.get('active_locks', 0)}</code> 把",
        f"• 数据盘余量：<code>{disk['free_mb']} MB</code>（已用 {disk['used_pct']}%）",
    ]
    if st["top_errors"]:
        lines.append("")
        lines.append("<b>主要失败原因</b>：")
        for code, cnt in st["top_errors"][:3]:
            lines.append(f"  · <code>{_esc(code)}</code> × {cnt}")
    kb = {
        "inline_keyboard": [[
            {"text": "📊 实时状态", "callback_data": "st:"},
            {"text": "📋 最近订单", "callback_data": "ol:"},
        ]]
    }
    return "\n".join(lines), kb


# ── Telegram Bot HTTP transport ──────────────────────────────────────────────

async def _tg_call(
    cfg: webhooks.WebhookConfig,
    method: str,
    payload: dict[str, Any],
    *,
    timeout_s: float = 12.0,
) -> Optional[dict[str, Any]]:
    api_base = (cfg.telegram_api_base or _DEFAULT_TELEGRAM_API_BASE).rstrip("/")
    endpoint = f"{api_base}/bot{cfg.telegram_bot_token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False, trust_env=False) as client:
            resp = await client.post(endpoint, json=payload)
            data = resp.json() if hasattr(resp, "json") else None
            if isinstance(data, dict) and data.get("ok"):
                return data
            desc = ""
            if isinstance(data, dict) and data.get("description"):
                desc = str(data["description"]).replace(cfg.telegram_bot_token, "[REDACTED]")[:160]
            logger.warning("telegram_bot_api_error method=%s status=%s desc=%s", method, resp.status_code, desc)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_bot_transport_error method=%s type=%s", method, type(exc).__name__)
        return None


async def send_bot_message(
    cfg: webhooks.WebhookConfig,
    text: str,
    *,
    reply_markup: Optional[dict[str, Any]] = None,
    chat_id: Optional[str] = None,
    silent: Optional[bool] = None,
) -> bool:
    target_chat = str(chat_id or cfg.telegram_chat_id or "").strip()
    if not cfg.telegram_bot_token or not target_chat:
        return False
    body: dict[str, Any] = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": bool(cfg.telegram_silent if silent is None else silent),
    }
    if cfg.telegram_thread_id is not None and target_chat == cfg.telegram_chat_id:
        body["message_thread_id"] = int(cfg.telegram_thread_id)
    if reply_markup is not None:
        body["reply_markup"] = reply_markup
    res = await _tg_call(cfg, "sendMessage", body, timeout_s=10.0)
    return res is not None


async def _answer_callback(cfg: webhooks.WebhookConfig, callback_id: str, text: str = "") -> None:
    if not callback_id:
        return
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:180]
    await _tg_call(cfg, "answerCallbackQuery", payload, timeout_s=6.0)


# ── Command & inline callback handlers ───────────────────────────────────────

def _is_authorized_chat(cfg: webhooks.WebhookConfig, chat_id: Any) -> bool:
    if not cfg.telegram_chat_id or chat_id is None:
        return False
    return str(chat_id).strip() == str(cfg.telegram_chat_id).strip()


async def _run_reconcile_action(order_id: str) -> tuple[str, dict[str, Any]]:
    from . import reconciler

    matches = find_orders_by_prefix(order_id)
    full_id = matches[0]["id"] if matches else order_id
    try:
        res = await reconciler.reconcile_order(full_id, force=True)
    except Exception as exc:  # noqa: BLE001
        return f"❌ 只读对账执行异常（{_esc(type(exc).__name__)}）。", _main_menu_keyboard()
    outcome = str(res.get("outcome") or "-")
    msg = str(res.get("message") or "")
    icon = "✅" if outcome == "resolved_delivered" else "🔄"
    lines = [
        f"{icon} <b>只读对账结果</b>（<code>{_esc(full_id[:12])}</code>）",
        "",
        f"• 结果：<code>{_esc(outcome)}</code>",
        f"• 订单状态：<code>{_esc(res.get('order_status') or '-')}</code>（支付 <code>{_esc(res.get('payment_status') or '-')}</code> / 交付 <code>{_esc(res.get('delivery_status') or '-')}</code>）",
        f"• 说明：{_esc(msg)}",
    ]
    kb = {
        "inline_keyboard": [[
            {"text": "🔍 查看订单详情", "callback_data": f"od:{full_id}"},
            {"text": "📊 今日状态", "callback_data": "st:"},
        ]]
    }
    return "\n".join(lines), kb


async def _run_cancel_auto_renew_action(order_id: str) -> tuple[str, dict[str, Any]]:
    from ..routers import orders as orders_router

    matches = find_orders_by_prefix(order_id)
    full_id = matches[0]["id"] if matches else order_id
    try:
        out = await orders_router.cancel_auto_renew(full_id)
        ar = str(getattr(out, "auto_renew_status", "") or "")
        icon = "✅" if ar == "canceled" else "⚠️"
        detail = _AUTO_RENEW_LABEL.get(ar, ar or "-")
        err = str(getattr(out, "auto_renew_error", "") or "")
        lines = [
            f"{icon} <b>关闭自动续费执行结果</b>（<code>{_esc(full_id[:12])}</code>）",
            "",
            f"• 当前续费状态：<code>{_esc(detail)}</code>",
        ]
        if err:
            lines.append(f"• 说明：{_esc(err[:200])}")
    except HTTPException as exc:
        detail_obj = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        lines = [
            f"⚠️ <b>无法自动重试关闭续费</b>（<code>{_esc(full_id[:12])}</code>）",
            "",
            f"• 原因：{_esc(detail_obj.get('message') or str(exc.detail))}",
        ]
    except Exception as exc:  # noqa: BLE001
        lines = [
            f"❌ <b>关闭自动续费异常</b>（<code>{_esc(full_id[:12])}</code>）",
            "",
            f"• 错误类型：<code>{_esc(type(exc).__name__)}</code>",
        ]
    kb = {
        "inline_keyboard": [[
            {"text": "🔍 查看订单详情", "callback_data": f"od:{full_id}"},
            {"text": "📊 今日状态", "callback_data": "st:"},
        ]]
    }
    return "\n".join(lines), kb


async def handle_update(cfg: webhooks.WebhookConfig, update: dict[str, Any]) -> bool:
    """Process one Telegram Update (message command or inline button callback)."""
    global _processed_updates
    if not isinstance(update, dict):
        return False

    # 1) Inline button callback_query
    cb = update.get("callback_query")
    if isinstance(cb, dict):
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if not _is_authorized_chat(cfg, chat_id):
            return False
        cb_id = str(cb.get("id") or "")
        data = str(cb.get("data") or "").strip()
        _processed_updates += 1
        if data == "st:":
            await _answer_callback(cfg, cb_id, "已刷新运行状态")
            text, kb = format_status_message()
            return await send_bot_message(cfg, text, reply_markup=kb, chat_id=str(chat_id))
        if data == "ol:":
            await _answer_callback(cfg, cb_id, "已加载最近订单")
            text, kb = format_orders_message()
            return await send_bot_message(cfg, text, reply_markup=kb, chat_id=str(chat_id))
        if data == "lk:":
            await _answer_callback(cfg, cb_id, "已刷新资金锁")
            text, kb = format_locks_message()
            return await send_bot_message(cfg, text, reply_markup=kb, chat_id=str(chat_id))
        if data == "dk:":
            await _answer_callback(cfg, cb_id, "已刷新存储与磁盘")
            text, kb = format_disk_message()
            return await send_bot_message(cfg, text, reply_markup=kb, chat_id=str(chat_id))
        if data == "dg:":
            await _answer_callback(cfg, cb_id, "正在生成今日简报")
            text, kb = format_daily_digest_message()
            return await send_bot_message(cfg, text, reply_markup=kb, chat_id=str(chat_id))
        if data.startswith("od:"):
            oid = data[3:].strip()
            await _answer_callback(cfg, cb_id, "已加载订单详情")
            text, kb = format_order_detail_message(oid)
            return await send_bot_message(cfg, text, reply_markup=kb, chat_id=str(chat_id))
        if data.startswith("rc:"):
            oid = data[3:].strip()
            await _answer_callback(cfg, cb_id, "正在执行只读对账…")
            text, kb = await _run_reconcile_action(oid)
            return await send_bot_message(cfg, text, reply_markup=kb, chat_id=str(chat_id))
        if data.startswith("ar:"):
            oid = data[3:].strip()
            await _answer_callback(cfg, cb_id, "正在重试关闭自动续费…")
            text, kb = await _run_cancel_auto_renew_action(oid)
            return await send_bot_message(cfg, text, reply_markup=kb, chat_id=str(chat_id))
        await _answer_callback(cfg, cb_id, "未知操作")
        return False

    # 2) Slash command message
    msg = update.get("message")
    if not isinstance(msg, dict):
        return False
    chat_id = (msg.get("chat") or {}).get("id")
    if not _is_authorized_chat(cfg, chat_id):
        return False
    raw_text = str(msg.get("text") or "").strip()
    if not raw_text.startswith("/"):
        return False

    parts = raw_text.split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    _processed_updates += 1

    if cmd in {"/start", "/help"}:
        text, kb = format_help_message()
    elif cmd == "/status":
        text, kb = format_status_message()
    elif cmd == "/orders":
        try:
            lim = int(arg) if arg else 8
        except ValueError:
            lim = 8
        text, kb = format_orders_message(limit=lim)
    elif cmd == "/order":
        text, kb = format_order_detail_message(arg)
    elif cmd == "/locks":
        text, kb = format_locks_message()
    elif cmd == "/disk":
        text, kb = format_disk_message()
    elif cmd == "/digest":
        text, kb = format_daily_digest_message()
    else:
        text, kb = format_help_message()
    return await send_bot_message(cfg, text, reply_markup=kb, chat_id=str(chat_id))


# ── Consecutive-failure streak, daily digest & low-disk monitors ─────────────

def reset_runtime_monitors() -> None:
    global _last_disk_alert_mono, _registered_token_hash, _processed_updates
    with _state_lock:
        _streak_orders.clear()
        _streak_seen_ids.clear()
        _last_disk_alert_mono = 0.0
        _registered_token_hash = ""
        _processed_updates = 0


def on_order_event(cfg: webhooks.WebhookConfig, event: str, data: dict[str, Any]) -> bool:
    """Track consecutive failures and schedule a streak alert when threshold is reached."""
    if not cfg.telegram_enabled or not cfg.telegram_configured:
        return False
    threshold = int(cfg.telegram_streak_threshold or 0)
    if threshold <= 0:
        return False

    oid = str(data.get("order_id") or data.get("job_id") or "").strip()
    if event in {"succeeded", "reconcile_resolved"}:
        with _state_lock:
            _streak_orders.clear()
            _streak_seen_ids.clear()
        return False

    if event not in {"failed", "unknown"}:
        return False

    with _state_lock:
        if oid and oid in _streak_seen_ids:
            return False
        if oid:
            _streak_seen_ids.add(oid)
        _streak_orders.append({
            "order_id": oid[:12] or "-",
            "event": event,
            "code": str(data.get("failure_code") or event),
            "plan": webhooks.format_plan_display(str(data.get("plan") or "")),
        })
        count = len(_streak_orders)
        snapshot = list(_streak_orders)[-threshold:]

    if count < threshold or count % threshold != 0:
        return False

    lines = [
        f"🚨 <b>连续失败预警（已连续 {count} 单异常）</b>",
        "",
        f"最近 {len(snapshot)} 单异常：",
    ]
    for item in snapshot:
        plan_bit = f" {item['plan']}" if item["plan"] else ""
        lines.append(f"• <code>{_esc(item['order_id'])}</code>{_esc(plan_bit)} · <code>{_esc(item['code'])}</code>")
    lines.append("")
    lines.append("建议立刻检查：住宅代理节点连通性 / 卡片额度 / 账号风控状态。")
    kb = {
        "inline_keyboard": [[
            {"text": "📊 今日状态", "callback_data": "st:"},
            {"text": "📋 最近订单", "callback_data": "ol:"},
        ]]
    }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    task = loop.create_task(send_bot_message(cfg, "\n".join(lines), reply_markup=kb, silent=False))
    webhooks.manager._tasks.add(task)
    task.add_done_callback(webhooks.manager._tasks.discard)
    return True


async def check_daily_digest(cfg: webhooks.WebhookConfig, *, now_utc: Optional[datetime] = None) -> bool:
    if not cfg.telegram_enabled or not cfg.telegram_configured or not cfg.telegram_digest_enabled:
        return False
    bj = _bj_now(now_utc)
    target_hour = max(0, min(23, int(cfg.telegram_digest_hour)))
    if bj.hour < target_hour:
        return False
    today_str = bj.strftime("%Y-%m-%d")
    state = _load_bot_state()
    if state.get("last_digest_date") == today_str:
        return False
    text, kb = format_daily_digest_message(now_utc)
    ok = await send_bot_message(cfg, text, reply_markup=kb)
    if ok:
        state["last_digest_date"] = today_str
        state["last_digest_sent_at"] = _utcnow().isoformat()
        _save_bot_state(state)
    return ok


async def check_disk_warning(cfg: webhooks.WebhookConfig, *, now_mono: Optional[float] = None) -> bool:
    global _last_disk_alert_mono
    if not cfg.telegram_enabled or not cfg.telegram_configured:
        return False
    warn_mb = int(cfg.telegram_disk_warn_mb or 0)
    if warn_mb <= 0:
        return False
    now_m = time.monotonic() if now_mono is None else now_mono
    if _last_disk_alert_mono > 0 and (now_m - _last_disk_alert_mono) < 6 * 3600:
        return False
    disk = _data_disk_info()
    if disk["free_mb"] <= 0 or disk["free_mb"] >= warn_mb:
        return False
    text = "\n".join([
        "💾 <b>服务器磁盘余量预警</b>",
        "",
        f"• 当前剩余：<code>{disk['free_mb']} MB</code>（已用 {disk['used_pct']}%，低于阈值 {warn_mb} MB）",
        f"• 路径：<code>{_esc(disk['mount_path'])}</code>",
        "• 建议：在本地执行 <code>./scripts/deploy.sh --prune-old</code> 清理旧版镜像与缓存。",
    ])
    kb = {"inline_keyboard": [[{"text": "💾 查看存储详情", "callback_data": "dk:"}]]}
    ok = await send_bot_message(cfg, text, reply_markup=kb, silent=False)
    if ok:
        _last_disk_alert_mono = now_m
    return ok


# ── Background long-polling & scheduler loop ─────────────────────────────────

def polling_enabled() -> bool:
    raw = os.environ.get("GSS_TELEGRAM_BOT_POLLING", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


async def _ensure_commands_registered(cfg: webhooks.WebhookConfig) -> None:
    global _registered_token_hash
    token_sig = f"{cfg.telegram_api_base}:{cfg.telegram_bot_token[:12]}:{len(cfg.telegram_bot_token)}"
    if _registered_token_hash == token_sig:
        return
    res = await _tg_call(cfg, "setMyCommands", {"commands": _BOT_COMMANDS}, timeout_s=8.0)
    if res is not None:
        _registered_token_hash = token_sig


async def _poll_loop() -> None:
    global _last_poll_ok_at, _last_poll_error
    offset = 0
    backoff = 2.0
    last_housekeeping = 0.0

    # Initialize offset cleanly so we don't replay stale historical messages on container restart.
    init_done_for_token = ""

    while _stop_event is not None and not _stop_event.is_set():
        cfg = webhooks.manager.load()
        if not cfg.telegram_enabled or not cfg.telegram_configured:
            try:
                await asyncio.wait_for(_stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            continue

        now_m = time.monotonic()
        if now_m - last_housekeeping >= 60.0:
            last_housekeeping = now_m
            try:
                await check_daily_digest(cfg)
                await check_disk_warning(cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram_bot_housekeeping_error type=%s", type(exc).__name__)

        if not cfg.telegram_bot_commands_enabled:
            try:
                await asyncio.wait_for(_stop_event.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass
            continue

        await _ensure_commands_registered(cfg)
        api_base = (cfg.telegram_api_base or _DEFAULT_TELEGRAM_API_BASE).rstrip("/")
        token = cfg.telegram_bot_token

        try:
            if init_done_for_token != token:
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=False) as client:
                    r0 = await client.get(
                        f"{api_base}/bot{token}/getUpdates",
                        params={"timeout": 0, "limit": 50},
                    )
                    d0 = r0.json()
                    if isinstance(d0, dict) and d0.get("ok"):
                        items = d0.get("result") or []
                        if items:
                            offset = max(int(it.get("update_id") or 0) for it in items) + 1
                init_done_for_token = token

            async with httpx.AsyncClient(timeout=32.0, follow_redirects=False, trust_env=False) as client:
                resp = await client.get(
                    f"{api_base}/bot{token}/getUpdates",
                    params={
                        "timeout": 20,
                        "offset": offset,
                        "allowed_updates": json.dumps(["message", "callback_query"]),
                    },
                )
            data = resp.json()
            if resp.status_code == 200 and isinstance(data, dict) and data.get("ok"):
                _last_poll_ok_at = _utcnow().isoformat()
                _last_poll_error = None
                backoff = 2.0
                for upd in data.get("result") or []:
                    uid = int(upd.get("update_id") or 0)
                    if uid >= offset:
                        offset = uid + 1
                    try:
                        await handle_update(cfg, upd)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("telegram_bot_handle_error type=%s", type(exc).__name__)
            else:
                desc = ""
                if isinstance(data, dict) and data.get("description"):
                    desc = str(data["description"]).replace(token, "[REDACTED]")[:120]
                _last_poll_error = f"HTTP {resp.status_code}: {desc}" if desc else f"HTTP {resp.status_code}"
                logger.warning("telegram_bot_poll_failed err=%s", _last_poll_error)
                try:
                    await asyncio.wait_for(_stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(30.0, backoff * 2.0)
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            _last_poll_error = type(exc).__name__
            logger.debug("telegram_bot_poll_exception type=%s", _last_poll_error)
            try:
                await asyncio.wait_for(_stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(30.0, backoff * 2.0)


def start() -> bool:
    global _bg_task, _stop_event
    if not polling_enabled():
        return False
    if _bg_task is not None and not _bg_task.done():
        return True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _stop_event = asyncio.Event()
    _bg_task = loop.create_task(_poll_loop(), name="gss-telegram-bot")
    return True


async def stop() -> None:
    global _bg_task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _bg_task is not None and not _bg_task.done():
        _bg_task.cancel()
        try:
            await asyncio.wait_for(_bg_task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _bg_task = None
    _stop_event = None


def status() -> dict[str, Any]:
    cfg = webhooks.manager.load()
    running = bool(_bg_task is not None and not _bg_task.done())
    return {
        "running": running,
        "commands_enabled": bool(cfg.telegram_enabled and cfg.telegram_configured and cfg.telegram_bot_commands_enabled),
        "digest_enabled": bool(cfg.telegram_enabled and cfg.telegram_configured and cfg.telegram_digest_enabled),
        "digest_hour": int(cfg.telegram_digest_hour),
        "streak_threshold": int(cfg.telegram_streak_threshold),
        "disk_warn_mb": int(cfg.telegram_disk_warn_mb),
        "processed_updates": _processed_updates,
        "last_poll_ok_at": _last_poll_ok_at,
        "last_poll_error": _last_poll_error,
    }


__all__ = [
    "start",
    "stop",
    "status",
    "handle_update",
    "on_order_event",
    "check_daily_digest",
    "check_disk_warning",
    "reset_runtime_monitors",
    "format_help_message",
    "format_status_message",
    "format_orders_message",
    "format_order_detail_message",
    "format_locks_message",
    "format_disk_message",
    "format_daily_digest_message",
    "send_bot_message",
]
