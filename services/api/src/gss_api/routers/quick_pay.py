"""One-call quick-pay endpoint and progress polling."""
from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Header, HTTPException

from ..schemas import QuickPayJobOut, QuickPayRequest
from ..services.order_flow import get_job, start_quick_pay
from .. import locks
from .orders import mask_sid
from ..persistence import get_db_optional

router = APIRouter(prefix="/api/payments", tags=["quick-pay"])


def _status_unavailable() -> HTTPException:
    return HTTPException(503, detail={"code": "quick_pay_status_unavailable",
                                      "message": "暂时无法读取作业状态，请保留原作业号。"})


def _out(job, *, admitted: bool = False) -> QuickPayJobOut:
    db = get_db_optional()
    try:
        events = db.list_events(job_id=job.id) if db is not None else []
        rejected = (db is not None and job.status == job.stage == "failed"
                    and not job.lock_id and job.order_id and db.rejected_before_payment(job.id, job.order_id))
    except sqlite3.Error:
        if not admitted:
            raise _status_unavailable() from None
        # POST already committed: an optional event read cannot revoke the
        # receipt or suggest another submission. Do not infer an outcome.
        logging.getLogger(__name__).warning("quick_pay_receipt_projection_unavailable")
        events, rejected = [], False
    return QuickPayJobOut(
        job_id=job.id,
        checkout_outcome="rejected_before_payment" if rejected else None,
        lock_id=getattr(job, "lock_id", None),
        timings_ms={str(k): int(v) for k, v in (getattr(job, "timings", {}) or {}).items()},
        diagnostics=dict(getattr(job, "diagnostics", {}) or {}),
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
    fingerprint = locks.account_fingerprint(payload.session)
    job = start_quick_pay(payload, key, fingerprint)
    return _out(job, admitted=True)


@router.get("/quick-pay/{job_id}", response_model=QuickPayJobOut)
async def quick_pay_status(job_id: str) -> QuickPayJobOut:
    if get_db_optional() is None:
        raise _status_unavailable()
    try:
        job = get_job(job_id)
    except sqlite3.Error:
        raise _status_unavailable() from None
    if job is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "quick-pay 任务不存在"})
    return _out(job)
