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
    for row in db.load_jobs():
        if row.get("status") != "running":
            continue
        stage = str(row.get("stage") or "")
        job_id = str(row.get("id") or "")
        order_id = str(row.get("order_id") or "")
        if stage in {"confirming_payment", "verifying_delivery"}:
            db.save_job(type("RecoveredJob", (), {
                "id": job_id, "idempotency_key": row.get("idempotency_key") or "",
                "account_fingerprint": row.get("account_fingerprint"),
                "order_id": order_id, "status": "unknown", "stage": "unknown",
                "failure_code": "crash_recovery", "message": "服务重启时支付阶段未完成，已锁定待人工核对",
                "amount": row.get("amount"), "currency": row.get("currency"),
                "purchase_attempt_id": row.get("purchase_attempt_id"),
                "timings": {}, "diagnostics": {}, "started_at": 0, "finished_at": None,
            })())
            hold_unknown(
                fingerprint=str(row.get("account_fingerprint") or ""),
                order_id=order_id, job_id=job_id, reason="crash_recovery",
            )
        else:
            db.add_event(stage="recovery", event="job_failed_safely", order_id=order_id,
                         job_id=job_id, code="crash_before_payment")


__all__ = [
    "account_fingerprint", "find_active_lock", "lock_payload", "hold_unknown",
    "hold_order_unknown", "release", "recover_incomplete_jobs",
]
