"""One-call quick-pay orchestration.

The card is held only in the local coroutine frame. It is never copied into
the job record, order, vault, or logs.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import HTTPException

from gss_contracts.errors import DomainError
from gss_core.redaction import redact_sensitive

from ..schemas import BillingIn, OrderCreateRequest, QuickPayRequest
from ..routers import orders, protocol_cards
from .. import locks
from ..persistence import get_db_optional

logger = logging.getLogger("gss.quick_pay")


@dataclass
class QuickPayJob:
    id: str
    idempotency_key: str
    account_fingerprint: Optional[str] = None
    status: str = "queued"          # queued | running | succeeded | failed | unknown
    stage: str = "queued"
    order_id: Optional[str] = None
    purchase_attempt_id: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    failure_code: Optional[str] = None
    message: Optional[str] = None
    timings: Dict[str, Any] = field(default_factory=dict)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    lock_id: Optional[str] = None
    locked_by: Optional[str] = None
    lease_until: Optional[float] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


_jobs: Dict[str, QuickPayJob] = {}
_by_key: Dict[str, str] = {}
_tasks: set[asyncio.Task] = set()


def _safe_message(value: Any) -> str:
    return redact_sensitive(str(value or ""))[:300]


def _error_code(exc: Exception) -> str:
    if isinstance(exc, DomainError):
        return str(exc.code)
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("code") or "quick_pay_failed")
    return "quick_pay_failed"


def _error_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        return _safe_message(exc.detail.get("message") or exc.detail)
    return _safe_message(exc)


def get_job(job_id: str) -> Optional[QuickPayJob]:
    job = _jobs.get(job_id)
    if job is not None:
        return job
    db = get_db_optional()
    if db is None:
        return None
    for row in db.load_jobs():
        if row.get("id") == job_id:
            job = _from_db_job(row)
            _jobs[job.id] = job
            _by_key[job.idempotency_key] = job.id
            return job
    return None


def get_job_by_key(key: str) -> Optional[QuickPayJob]:
    job_id = _by_key.get(key)
    if job_id:
        return _jobs.get(job_id)
    db = get_db_optional()
    if db is None:
        return None
    for row in db.load_jobs():
        if row.get("idempotency_key") == key:
            job = _from_db_job(row)
            _jobs[job.id] = job
            _by_key[key] = job.id
            return job
    return None


def _persist_job(job: QuickPayJob) -> None:
    db = get_db_optional()
    if db is not None:
        try:
            db.save_job(job)
            db.save_idempotency(job.idempotency_key, "quick_pay", order_id=job.order_id or "",
                                job_id=job.id, state=job.status)
        except Exception:
            pass


def _from_db_job(row: dict) -> QuickPayJob:
    return QuickPayJob(
        id=row["id"], idempotency_key=row["idempotency_key"],
        account_fingerprint=row.get("account_fingerprint"), status=row["status"],
        stage=row["stage"], order_id=row.get("order_id"),
        purchase_attempt_id=row.get("purchase_attempt_id"),
        amount=row.get("amount"), currency=row.get("currency"),
        failure_code=row.get("failure_code"), message=row.get("message"),
        lock_id=row.get("lock_id"), started_at=0, finished_at=None,
    )


def _new_job(idempotency_key: str, account_fingerprint: str = "") -> QuickPayJob:
    existing = get_job_by_key(idempotency_key)
    if existing is not None:
        return existing
    job = QuickPayJob(id=uuid.uuid4().hex, idempotency_key=idempotency_key,
                      account_fingerprint=account_fingerprint or None)
    _jobs[job.id] = job
    _by_key[idempotency_key] = job.id
    _persist_job(job)
    return job


def start_quick_pay(payload: QuickPayRequest, idempotency_key: str,
                    account_fingerprint: str = "") -> QuickPayJob:
    """Reserve a job; the caller schedules run_quick_pay."""
    job = _new_job(idempotency_key, account_fingerprint)
    if job.status != "queued" or job.stage != "queued":
        return job
    task = asyncio.create_task(run_quick_pay(job.id, payload, idempotency_key))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


async def run_quick_pay(job_id: str, payload: QuickPayRequest, idempotency_key: str) -> QuickPayJob:
    job = _jobs.get(job_id)
    if job is None:
        raise LookupError("quick-pay job not found")
    if job.status != "queued":
        return job
    job.status = "running"
    job.stage = "creating_order"
    _persist_job(job)
    card = payload.card.model_dump()  # local only; never assigned to job
    try:
        create_payload = OrderCreateRequest(
            plan=payload.plan,
            billing_country=payload.billing_country,
            exit_country=payload.exit_country,
            session=payload.session,
            mode=payload.mode,
            promo_code=payload.promo_code,
            promo_campaign_id=payload.promo_campaign_id,
            workspace_name=payload.workspace_name,
            seat_quantity=payload.seat_quantity,
            price_interval=payload.price_interval,
        )
        order = await orders._create_order(
            create_payload, idempotency_key,
            progress=lambda stage: (setattr(job, "stage", stage), _persist_job(job)),
        )
        job.order_id = order.id
        view = orders.store.view(order.id)
        view.account_fingerprint = job.account_fingerprint
        orders.store.persist(order.id)
        job.purchase_attempt_id = order.purchase_attempt_id
        job.amount = order.amount
        job.currency = order.currency
        job.stage = "checkout_created"
        _persist_job(job)
        if payload.debug_checkout_only:
            job.status = "succeeded"
            job.stage = "checkout_created"
            job.message = "仅建单完成，未提交卡"
            job.finished_at = time.time()
            return job

        job.stage = "confirming_payment"
        order_billing = BillingIn(**order.billing) if getattr(order, "billing", None) else None
        bind_payload = protocol_cards.BindProtocolRequest(
            card=payload.card,
            billing=payload.billing or order_billing,
            expected_amount=str(order.amount or ""),
            expected_currency=str(order.currency or ""),
        )
        result = await protocol_cards.bind_and_pay_protocol(order.id, bind_payload)
        job.stage = "verifying_delivery"
        status = str(result.get("payment_status") or "")
        state = str(result.get("state") or "")
        job.failure_code = str(result.get("code") or "") or None
        job.message = _safe_message(result.get("message") or result.get("status_summary"))
        if status == "succeeded" or state == "paid":
            job.status = "succeeded"
            job.stage = "succeeded"
        elif state in {"failed", "rejected", "card_declined"}:
            job.status = "failed"
            job.stage = "failed"
        elif state == "unknown" or status == "processing":
            job.status = "unknown"
            job.stage = "unknown"
            held = locks.hold_order_unknown(
                job.order_id or "", job_id=job.id, reason="unknown_payment",
            )
            job.lock_id = (held or {}).get("id")
        else:
            job.status = "failed"
            job.stage = "failed"
        job.finished_at = time.time()
        _persist_job(job)
        return job
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.stage = "failed"
        job.failure_code = _error_code(exc)
        job.message = _error_message(exc)
        job.finished_at = time.time()
        _persist_job(job)
        logger.warning(
            "quick_pay_failed job=%s order=%s code=%s type=%s",
            job.id, job.order_id or "-", job.failure_code, type(exc).__name__,
        )
        return job
    finally:
        # Drop the only card-bearing reference as soon as the flow exits.
        card = None


__all__ = ["QuickPayJob", "start_quick_pay", "run_quick_pay", "get_job", "get_job_by_key"]
