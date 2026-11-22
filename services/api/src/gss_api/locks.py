"""Account-level unknown-payment locks.

A lock is a financial safety mechanism, not a retry helper.  It is created only
after an ambiguous payment outcome and can be released only by an explicit
operator confirmation after the backend rechecks the recorded payment state.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .persistence import get_db_optional
from .store import store


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pepper() -> bytes:
    return os.environ.get("GSS_LOCK_PEPPER", "gss-local-lock-pepper-v1").encode()


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part.encode()))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def account_fingerprint(raw_session: str) -> str:
    """Stable HMAC fingerprint. The raw token never leaves this function."""
    try:
        from gss_core.session_token import parse_session_bundle
        bundle = parse_session_bundle(raw_session)
    except Exception:
        return ""
    claims = _jwt_claims(bundle.access_token)
    auth = claims.get("https://api.openai.com/auth")
    account_id = ""
    if isinstance(auth, dict):
        account_id = str(auth.get("chatgpt_account_id") or "")
    account_id = account_id or str(claims.get("sub") or "")
    if not account_id:
        return ""
    return hmac.new(_pepper(), account_id.encode(), hashlib.sha256).hexdigest()


def _lock_key(fingerprint: str) -> str:
    return f"account:{fingerprint}" if fingerprint else ""


def find_active_lock(fingerprint: str) -> Optional[dict[str, Any]]:
    db = get_db_optional()
    if db is None or not fingerprint:
        return None
    return db.find_active_lock(_lock_key(fingerprint))


def lock_payload(lock: dict[str, Any]) -> dict[str, Any]:
    return {
        "lock_id": lock.get("id"),
        "order_id": lock.get("order_id"),
        "job_id": lock.get("job_id"),
        "payment_intent_id": lock.get("payment_intent_id"),
        "reason": lock.get("reason"),
        "locked_since": lock.get("created_at"),
    }


def hold_unknown(*, fingerprint: str = "", order_id: str = "", job_id: str = "",
                 payment_intent_id: str = "", reason: str = "unknown_payment") -> Optional[dict[str, Any]]:
    db = get_db_optional()
    if db is None or not fingerprint:
        return None
    existing = find_active_lock(fingerprint)
    if existing is not None:
        return existing
    return db.acquire_lock(
        lock_id=str(uuid.uuid4()), lock_key=_lock_key(fingerprint),
        lock_type="account", reason=reason, order_id=order_id, job_id=job_id,
        payment_intent_id=payment_intent_id,
    )


def hold_order_unknown(order_id: str, *, job_id: str = "", reason: str = "unknown_payment") -> Optional[dict[str, Any]]:
    view = store.views.get(order_id)
    fingerprint = (view.account_fingerprint if view else "") or ""
    if not fingerprint:
        return None
    return hold_unknown(
        fingerprint=fingerprint, order_id=order_id, job_id=job_id,
        payment_intent_id=(view.payment_intent_id or "" if view else ""),
        reason=reason,
    )


def release(lock_id: str, *, confirm_no_charge: bool, note: str, operator: str = "operator") -> tuple[bool, str, Optional[dict[str, Any]]]:
    db = get_db_optional()
    if db is None:
        return False, "持久化未初始化", None
    lock = db.get_lock(lock_id)
    if lock is None:
        return False, "锁不存在", None
    if lock.get("state") != "held":
        return False, "锁已释放", lock
    if not confirm_no_charge:
        return False, "需要明确确认账单未扣款", lock
    note = (note or "").strip()
    if not note:
        return False, "解锁备注不能为空", lock
    order = store.get_order(lock.get("order_id") or "")
    view = store.views.get(lock.get("order_id") or "")
    # Re-check the locally recorded result before allowing release. A succeeded
    # payment must never be "released" into a new charge.
    if order is not None and (str(order.status) in {"active", "delivered"} or
                              (view is not None and view.payment_status == "succeeded")):
        return False, "订单已显示扣款成功，不能解锁；请走对账/退款流程", lock
    if not db.release_lock(lock_id, released_by=operator, note=note):
        return False, "锁状态已变化，请刷新后重试", db.get_lock(lock_id)
    db.add_event(
        stage="lock", event="lock_released", order_id=lock.get("order_id") or "",
        job_id=lock.get("job_id") or "", code="manual_release",
        metadata={"operator": operator, "note": note, "payment_intent_id": lock.get("payment_intent_id")},
    )
    return True, "已解锁", db.get_lock(lock_id)


def recover_incomplete_jobs() -> None:
    """On startup, crash-interrupted payment stages become unknown and locked."""
    db = get_db_optional()
    if db is None:
        return
    from .services.order_flow import _from_db_job

    for row in db.load_jobs():
        if row.get("status") not in {"queued", "running"}:
            continue
        job = _from_db_job(row)
        order_id = job.order_id or ""
        if job.stage in {"confirming_payment", "verifying_delivery", "unknown"}:
            held = hold_unknown(fingerprint=job.account_fingerprint or "", order_id=order_id,
                                job_id=job.id, reason="crash_recovery")
            job.lock_id = (held or {}).get("id")
            job.status = job.stage = "unknown"
            job.failure_code = "crash_recovery"
            job.message = "服务重启时支付阶段未完成，请人工核对。"
        else:
            job.status = job.stage = "failed"
            job.failure_code = "crash_before_payment"
            job.message = "作业在提交付款前中断，未自动重放。请保留作业编号并核对订单。"
        job.finished_at = datetime.now(timezone.utc).timestamp()
        db.save_job(job)
        db.save_idempotency(job.idempotency_key, "quick_pay", order_id=order_id,
                            job_id=job.id, state=job.status)
        db.add_event(stage="recovery", event=job.status, order_id=order_id,
                     job_id=job.id, code=job.failure_code)


__all__ = [
    "account_fingerprint", "find_active_lock", "lock_payload", "hold_unknown",
    "hold_order_unknown", "release", "recover_incomplete_jobs",
]
