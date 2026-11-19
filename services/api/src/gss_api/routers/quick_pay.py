"""One-call quick-pay endpoint and progress polling."""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from ..schemas import QuickPayJobOut, QuickPayRequest
from ..services.order_flow import get_job, get_job_by_key, start_quick_pay

router = APIRouter(prefix="/api/payments", tags=["quick-pay"])


def _out(job) -> QuickPayJobOut:
    return QuickPayJobOut(
        job_id=job.id,
        status=job.status,
        stage=job.stage,
        order_id=job.order_id,
        purchase_attempt_id=job.purchase_attempt_id,
        amount=job.amount,
        currency=job.currency,
        failure_code=job.failure_code,
        message=job.message,
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
    job = start_quick_pay(payload, key)
    return _out(job)


@router.get("/quick-pay/{job_id}", response_model=QuickPayJobOut)
async def quick_pay_status(job_id: str) -> QuickPayJobOut:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "quick-pay 任务不存在"})
    return _out(job)
