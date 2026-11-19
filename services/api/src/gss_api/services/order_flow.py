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

logger = logging.getLogger("gss.quick_pay")


@dataclass
class QuickPayJob:
    id: str
    idempotency_key: str
    status: str = "queued"          # queued | running | succeeded | failed | unknown
    stage: str = "queued"
    order_id: Optional[str] = None
    purchase_attempt_id: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    failure_code: Optional[str] = None
    message: Optional[str] = None
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
    return _jobs.get(job_id)


def get_job_by_key(key: str) -> Optional[QuickPayJob]:
    job_id = _by_key.get(key)
    return _jobs.get(job_id) if job_id else None


def _new_job(idempotency_key: str) -> QuickPayJob:
    existing = get_job_by_key(idempotency_key)
    if existing is not None:
        return existing
    job = QuickPayJob(id=uuid.uuid4().hex, idempotency_key=idempotency_key)
    _jobs[job.id] = job
    _by_key[idempotency_key] = job.id
    return job


def start_quick_pay(payload: QuickPayRequest, idempotency_key: str) -> QuickPayJob:
    """Reserve a job; the caller schedules run_quick_pay."""
    job = _new_job(idempotency_key)
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
            progress=lambda stage: setattr(job, "stage", stage),
        )
        job.order_id = order.id
        job.purchase_attempt_id = order.purchase_attempt_id
        job.amount = order.amount
        job.currency = order.currency
        job.stage = "checkout_created"
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
        else:
            job.status = "failed"
            job.stage = "failed"
        job.finished_at = time.time()
        return job
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.stage = "failed"
        job.failure_code = _error_code(exc)
        job.message = _error_message(exc)
        job.finished_at = time.time()
        logger.warning(
            "quick_pay_failed job=%s order=%s code=%s type=%s",
            job.id, job.order_id or "-", job.failure_code, type(exc).__name__,
        )
        return job
    finally:
        # Drop the only card-bearing reference as soon as the flow exits.
        card = None


__all__ = ["QuickPayJob", "start_quick_pay", "run_quick_pay", "get_job", "get_job_by_key"]
