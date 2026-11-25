"""One-call quick-pay endpoint and progress polling."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ..schemas import (
    BatchPreflightItemOut,
    BatchPreflightOut,
    BatchPreflightRequest,
    PreflightOut,
    PreflightRequest,
    QueueJobSummaryOut,
    QueueStatsOut,
    QueueStatusOut,
    QuickPayJobOut,
    QuickPayRequest,
    ReconcilerStatusOut,
    ReconcileResultOut,
)
from ..services.order_flow import (
    get_job,
    list_queue_jobs,
    queue_stats,
    start_quick_pay,
)
from ..services.preflight import run_preflight
from .. import locks
from .orders import mask_sid
from ..persistence import get_db_optional

router = APIRouter(prefix="/api/payments", tags=["quick-pay"])


def _status_unavailable() -> HTTPException:
    return HTTPException(503, detail={"code": "quick_pay_status_unavailable",
                                      "message": "暂时无法读取作业状态，请保留原作业号。"})


def _out(job, *, admitted: bool = False) -> QuickPayJobOut:
    db = get_db_optional()
    queue_position = None
    try:
        events = db.list_events(job_id=job.id) if db is not None else []
        rejected = (db is not None and job.status == job.stage == "failed"
                    and not job.lock_id and job.order_id and db.rejected_before_payment(job.id, job.order_id))
        if db is not None and job.status == "queued":
            queue_position = db.queue_position(job.id)
    except sqlite3.Error:
        if not admitted:
            raise _status_unavailable() from None
        # POST already committed: an optional event read cannot revoke the
        # receipt or suggest another submission. Do not infer an outcome.
        logging.getLogger(__name__).warning("quick_pay_receipt_projection_unavailable")
        events, rejected, queue_position = [], False, None
    return QuickPayJobOut(
        job_id=job.id,
        checkout_outcome="rejected_before_payment" if rejected else None,
        lock_id=getattr(job, "lock_id", None),
        timings_ms={str(k): int(v) for k, v in (getattr(job, "timings", {}) or {}).items()},
        diagnostics=dict(getattr(job, "diagnostics", {}) or {}),
        next_action=dict(getattr(job, "next_action", None) or {}) or None,
        events=events,
        status=job.status,
        stage=job.stage,
        queue_position=queue_position,
        attempts=int(getattr(job, "attempts", 0) or 0),
        reconcile_attempts=int(getattr(job, "reconcile_attempts", 0) or 0),
        last_reconciled_at=getattr(job, "last_reconciled_at", None),
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


@router.get("/queue", response_model=QueueStatusOut)
async def quick_pay_queue(limit: int = Query(default=20, ge=1, le=100)) -> QueueStatusOut:
    stats = QueueStatsOut(**queue_stats())
    jobs = [QueueJobSummaryOut(**item) for item in list_queue_jobs(limit=limit)]
    return QueueStatusOut(stats=stats, jobs=jobs)


@router.post("/preflight", response_model=PreflightOut)
async def quick_pay_preflight(payload: PreflightRequest, request: Request) -> PreflightOut:
    gateway_store = getattr(request.app.state, "gateway_store", None)
    stats = QueueStatsOut(**queue_stats())
    return await run_preflight(payload, queue_stats=stats, gateway_store=gateway_store)


@router.post("/preflight/batch", response_model=BatchPreflightOut)
async def quick_pay_preflight_batch(payload: BatchPreflightRequest, request: Request) -> BatchPreflightOut:
    """Batch read-only pre-flight checks (bounded concurrency, zero card input)."""
    started = time.perf_counter()
    gateway_store = getattr(request.app.state, "gateway_store", None)
    stats = QueueStatsOut(**queue_stats())
    sem = asyncio.Semaphore(4)

    async def check_one(idx: int, item) -> BatchPreflightItemOut:
        async with sem:
            req = PreflightRequest(
                plan=item.plan,
                billing_country=item.billing_country,
                exit_country=item.exit_country,
                session=item.session,
                check_proxy_connectivity=item.check_proxy_connectivity,
                check_account_upstream=item.check_account_upstream,
            )
            res = await run_preflight(req, queue_stats=stats, gateway_store=gateway_store)
            return BatchPreflightItemOut(
                index=idx,
                label=item.label.strip() or f"任务 #{idx + 1}",
                ok=res.ok,
                blocking_code=res.blocking_code,
                summary=res.summary,
                checks=res.checks,
                elapsed_ms=res.elapsed_ms,
            )

    results = list(await asyncio.gather(*(check_one(i, item) for i, item in enumerate(payload.items))))
    passed = sum(1 for r in results if r.ok)
    return BatchPreflightOut(
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
        items=results,
    )


@router.get("/reconciler", response_model=ReconcilerStatusOut)
async def quick_pay_reconciler_status() -> ReconcilerStatusOut:
    from ..services import reconciler
    return ReconcilerStatusOut(**reconciler.status())


@router.post("/quick-pay/{job_id}/reconcile", response_model=ReconcileResultOut)
async def quick_pay_reconcile(job_id: str) -> ReconcileResultOut:
    from ..services import reconciler
    result = await reconciler.reconcile_job(job_id, force=True)
    if result.get("outcome") == "job_not_found":
        raise HTTPException(404, detail={"code": "not_found", "message": "quick-pay 任务不存在"})
    return ReconcileResultOut(**result)


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
