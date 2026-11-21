"""One-call quick-pay orchestration.

The card is held only in the local coroutine frame. It is never copied into
the job record, order, vault, or logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import HTTPException

from gss_contracts.errors import DomainError
from gss_core.chatgpt import ChatGPTClient
from gss_core.checkout_diagnostics import safe_checkout_diagnostics
from gss_core.redaction import redact_sensitive

from ..schemas import BillingIn, OrderCreateRequest, QuickPayRequest
from ..routers import orders, protocol_cards
from .. import locks
from ..persistence import get_db_optional, load_checkout_diagnostics

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
    auto_renew_status: str = "disabled"
    auto_renew_canceled_at: Optional[str] = None
    auto_renew_error: Optional[str] = None
    proxy_rotations: int = 0
    proxy_last_sid: Optional[str] = None
    proxy_last_exit_ip: Optional[str] = None
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
        message = exc.detail.get("message")
        return _safe_message(message) if isinstance(message, str) and message else "请求失败"
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
        operation = "save_job"
        try:
            db.save_job(job)
            operation = "save_idempotency"
            db.save_idempotency(job.idempotency_key, "quick_pay", order_id=job.order_id or "",
                                job_id=job.id, state=job.status)
        except Exception:
            # Database errors may contain values. Never log the exception or retry payment.
            logger.warning("quick_pay_persistence_failed operation=%s", operation)


def _event(job: QuickPayJob, stage: str, message: str = "") -> None:
    db = get_db_optional()
    if db is not None:
        try:
            db.add_event(stage=stage, event=job.status, order_id=job.order_id or "",
                         job_id=job.id, message=message)
        except Exception:
            logger.warning("quick_pay_persistence_failed operation=add_event")


def _set_stage(job: QuickPayJob, stage: str, message: str = "", *, event: bool = True) -> None:
    if job.stage != stage and event:
        _event(job, stage, message)
    job.stage = stage
    _persist_job(job)


def _from_db_job(row: dict) -> QuickPayJob:
    return QuickPayJob(
        id=row["id"], idempotency_key=row["idempotency_key"],
        account_fingerprint=row.get("account_fingerprint"), status=row["status"],
        stage=row["stage"], order_id=row.get("order_id"),
        purchase_attempt_id=row.get("purchase_attempt_id"),
        amount=row.get("amount"), currency=row.get("currency"),
        failure_code=row.get("failure_code"), message=row.get("message"),
        lock_id=row.get("lock_id"), timings=json.loads(row.get("timings_json") or "{}"),
        diagnostics=load_checkout_diagnostics(row.get("diagnostics_json")),
        started_at=0, finished_at=None,
    )


def _capture_checkout_failure(job: QuickPayJob, exc: Exception) -> None:
    if isinstance(exc, DomainError):
        incoming = safe_checkout_diagnostics(exc.detail)
    elif isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        incoming = (safe_checkout_diagnostics(exc.detail.get("detail"))
                    or safe_checkout_diagnostics(exc.detail))
    else:
        incoming = {}
    # Do not combine fields/traces from different upstream requests.
    job.diagnostics = safe_checkout_diagnostics(job.diagnostics) or incoming

    # The order router sets this private attribute only after order creation.
    # Never accept an order identifier from upstream data or an HTTP payload.
    candidate = getattr(exc, "_checkout_order_id", None)
    if job.order_id or not isinstance(candidate, str) or len(candidate) != 36:
        return
    try:
        parsed = uuid.UUID(candidate)
    except ValueError:
        return
    if parsed.version == 4 and str(parsed) == candidate:
        order = orders.store.get_order(candidate)
        if order and job.idempotency_key and order.idempotency_key == job.idempotency_key:
            job.order_id = candidate


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


async def _cancel_auto_renew(job: QuickPayJob, *, order_id: str, plan: str) -> None:
    view = orders.store.view(order_id)
    if not view.auto_cancel_renew:
        view.auto_renew_status = "disabled"
        job.auto_renew_status = "disabled"
        return
    _set_stage(job, "canceling_auto_renew", "正在关闭下期自动续费")
    view.auto_renew_status = "canceling_auto_renew"
    orders.store.persist(order_id)
    secrets = orders.vault.get(order_id)
    if secrets is None:
        view.auto_renew_status = "manual_required"
        view.auto_renew_error = "vault_expired"
        job.auto_renew_status = "manual_required"
        job.auto_renew_error = "vault_expired"
        orders.store.persist(order_id)
        return
    plan_type = orders.normalize_plan_name(plan)
    last_error = ""
    attempts = 0
    for attempt in range(3):
        attempts = attempt + 1
        view.auto_renew_attempts = attempts
        try:
            client = await asyncio.to_thread(
                ChatGPTClient.from_raw_session, secrets.session, proxy=secrets.proxy,
            )
            try:
                await asyncio.to_thread(client.cancel_auto_renew, plan_type=plan_type)
            finally:
                client.close()
            view.auto_renew_status = "canceled"
            view.auto_renew_canceled_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            job.auto_renew_status = "canceled"
            job.auto_renew_canceled_at = view.auto_renew_canceled_at
            orders.store.persist(order_id)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = _safe_message(exc)
            if "未配置" in last_error or "Session" in last_error:
                break
            if attempt < 2:
                await asyncio.sleep(1 if attempt == 0 else 4)
    view.auto_renew_status = "manual_required"
    view.auto_renew_error = last_error or "cancel_failed"
    job.auto_renew_status = "manual_required"
    job.auto_renew_error = view.auto_renew_error
    orders.store.persist(order_id)


async def run_quick_pay(job_id: str, payload: QuickPayRequest, idempotency_key: str) -> QuickPayJob:
    job = _jobs.get(job_id)
    if job is None:
        raise LookupError("quick-pay job not found")
    if job.status != "queued":
        return job
    job.status = "running"
    run_started = time.monotonic()
    if job.started_at > 1_000_000_000:
        job.timings["queue_ms"] = max(0, int((time.time() - job.started_at) * 1000))
    _set_stage(job, "creating_order", "已接受任务，开始建单")
    _event(job, "running", job.stage)
    stage_started: Dict[str, float] = {"creating_order": run_started}
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
        def progress(stage: str) -> None:
            now = time.monotonic()
            if stage in {"reissuing", "browser_decode", "browser_decoded", "creating_checkout"}:
                stage_started[stage] = now
            if stage == "reissued":
                job.timings["reissue_ms"] = int((now - stage_started.get("reissuing", now)) * 1000)
            elif stage == "session_refreshed":
                job.timings["reissue_ms"] = int((now - stage_started.get("session_refresh", now)) * 1000)
            elif stage == "browser_decoded":
                job.timings["browser_decode_ms"] = int((now - stage_started.get("browser_decode", now)) * 1000)
            elif stage == "browser_decode_skipped":
                job.timings["browser_decode_ms"] = 0
            elif stage == "checkout_created":
                job.timings["create_checkout_ms"] = int((now - stage_started.get("creating_checkout", now)) * 1000)
                job.timings["create_order_ms"] = int((now - run_started) * 1000)
            _set_stage(job, stage)

        order = await orders._create_order(
            create_payload, idempotency_key, progress=progress,
        )
        job.order_id = order.id
        view = orders.store.view(order.id)
        view.account_fingerprint = job.account_fingerprint
        view.auto_cancel_renew = bool(payload.auto_cancel_renew)
        view.auto_renew_status = "pending" if payload.auto_cancel_renew else "disabled"
        job.auto_renew_status = view.auto_renew_status
        job.proxy_rotations = view.proxy_rotations
        job.proxy_last_sid = view.proxy_last_sid
        job.proxy_last_exit_ip = view.proxy_last_exit_ip
        orders.store.persist(order.id)
        job.purchase_attempt_id = order.purchase_attempt_id
        job.amount = order.amount
        job.currency = order.currency
        _set_stage(job, "checkout_created", "结账单已生成")
        if payload.debug_checkout_only:
            job.status = "succeeded"
            job.stage = "checkout_created"
            job.message = "仅建单完成，未提交卡"
            job.finished_at = time.time()
            job.timings["total_ms"] = int((time.monotonic() - run_started) * 1000)
            _event(job, "succeeded", job.message)
            _persist_job(job)
            return job

        _set_stage(job, "confirming_payment", "正在提交卡片并确认支付")
        payment_started = time.monotonic()
        order_billing = BillingIn(**order.billing) if getattr(order, "billing", None) else None
        bind_payload = protocol_cards.BindProtocolRequest(
            card=payload.card,
            billing=payload.billing or order_billing,
            expected_amount=str(order.amount or ""),
            expected_currency=str(order.currency or ""),
        )
        result = await protocol_cards.bind_and_pay_protocol(order.id, bind_payload)
        job.timings["payment_ms"] = int((time.monotonic() - payment_started) * 1000)
        _set_stage(job, "verifying_delivery", "支付已提交，正在核验交付")
        status = str(result.get("payment_status") or "")
        state = str(result.get("state") or "")
        job.failure_code = str(result.get("code") or "") or None
        job.message = _safe_message(result.get("message") or result.get("status_summary"))
        if status == "succeeded" or state == "paid":
            job.status = "succeeded"
            job.stage = "succeeded"
            await _cancel_auto_renew(job, order_id=job.order_id or order.id, plan=payload.plan)
            job.stage = "auto_renew_canceled" if job.auto_renew_status == "canceled" else "succeeded"
        elif state in {"failed", "rejected", "card_declined"}:
            job.status = "failed"
            job.stage = "failed"
        elif state == "unknown" or status == "processing":
            job.status = "unknown"
            _set_stage(job, "unknown", "扣款结果待核对，已锁定")
            held = locks.hold_order_unknown(
                job.order_id or "", job_id=job.id, reason="unknown_payment",
            )
            job.lock_id = (held or {}).get("id")
        else:
            job.status = "failed"
            job.stage = "failed"
        job.finished_at = time.time()
        job.timings["total_ms"] = int((time.monotonic() - run_started) * 1000)
        _event(job, job.status, job.message or "")
        _persist_job(job)
        return job
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.stage = "failed"
        job.failure_code = _error_code(exc)
        job.message = _error_message(exc)
        _capture_checkout_failure(job, exc)
        job.finished_at = time.time()
        job.timings["total_ms"] = int((time.monotonic() - run_started) * 1000)
        _event(job, "failed", job.message or "")
        _persist_job(job)
        logger.warning(
            "quick_pay_failed job=%s order=%s code=%s type=%s",
            job.id, job.order_id or "-", job.failure_code, type(exc).__name__,
        )
        return job


__all__ = ["QuickPayJob", "start_quick_pay", "run_quick_pay", "get_job", "get_job_by_key"]
