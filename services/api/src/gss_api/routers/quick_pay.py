"""One-call quick-pay endpoint and progress polling."""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from ..schemas import QuickPayJobOut, QuickPayRequest
from ..services.order_flow import get_job, get_job_by_key, start_quick_pay
from .. import locks
from .orders import mask_sid
from ..persistence import get_db_optional

router = APIRouter(prefix="/api/payments", tags=["quick-pay"])


def _out(job) -> QuickPayJobOut:
    db = get_db_optional()
    events = db.list_events(job_id=job.id) if db is not None else []
    return QuickPayJobOut(
        job_id=job.id,
        lock_id=getattr(job, "lock_id", None),
        timings_ms={str(k): int(v) for k, v in (getattr(job, "timings", {}) or {}).items()},
        events=events,
        status=job.status,
        stage=job.stage,
        order_id=job.order_id,
        purchase_attempt_id=job.purchase_attempt_id,
        amount=job.amount,
        currency=job.currency,
        failure_code=job.failure_code,
        message=job.message,
        auto_renew_status=getattr(job, "auto_renew_status", "disabled"),
        auto_renew_canceled_at=getattr(job, "auto_renew_canceled_at", None),
        auto_renew_error=getattr(job, "auto_renew_error", None),
        proxy_rotations=getattr(job, "proxy_rotations", 0),
        proxy_last_sid=mask_sid(getattr(job, "proxy_last_sid", None)),
        proxy_last_exit_ip=getattr(job, "proxy_last_exit_ip", None),
    )


@router.post("/quick-pay", response_model=QuickPayJobOut, status_code=202)
async def quick_pay(
    payload: QuickPayRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> QuickPayJobOut:
    key = (idempotency_key or "").strip()
    if not key:
        raise HTTPException(400, detail={"code": "idempotency_conflict",
                                         "message": "quick-pay 必须提供 Idempotency-Key"})
    if len(key) > 255:
        raise HTTPException(400, detail={"code": "idempotency_conflict",
                                         "message": "Idempotency-Key 长度不能超过 255"})
    existing = get_job_by_key(key)
    if existing is not None:
        return _out(existing)
    fingerprint = locks.account_fingerprint(payload.session)
    held = locks.find_active_lock(fingerprint)
    if held is not None:
        raise HTTPException(409, detail={
            "code": "payment_unknown_lock",
            "message": "存在未核销的 unknown 订单，请先人工核对账单",
            **locks.lock_payload(held),
        })
    job = start_quick_pay(payload, key, fingerprint)
    return _out(job)


@router.get("/quick-pay/{job_id}", response_model=QuickPayJobOut)
async def quick_pay_status(job_id: str) -> QuickPayJobOut:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "quick-pay 任务不存在"})
    return _out(job)
