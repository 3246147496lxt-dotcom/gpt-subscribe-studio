"""One-call quick-pay orchestration.

The card is held only in the local coroutine frame. It is never copied into
the job record, order, vault, or logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import HTTPException

from gss_contracts.errors import DomainError, ErrorCode, UpstreamFailureCode
from gss_core.chatgpt import ChatGPTClient
from gss_core.checkout_diagnostics import safe_checkout_diagnostics
from gss_core.redaction import redact_sensitive

from ..schemas import BillingIn, OrderCreateRequest, QuickPayRequest
from ..routers import events as events_router, orders, protocol_cards
from .. import locks
from ..persistence import (
    QuickPayAdmissionBlocked, QuickPayAdmissionConflict, get_db_optional, load_checkout_diagnostics,
)
from . import webhooks
from .preflight import run_quick_pay_preflight
from .quick_pay_intent import quick_pay_intent_digest

logger = logging.getLogger("gss.quick_pay")

WORKER_ID = f"api-{os.getpid()}-{uuid.uuid4().hex[:6]}"
LEASE_SECONDS = 300.0


def max_concurrency() -> int:
    from gss_core import runtime_flags

    return max(1, min(32, runtime_flags.get_int("quick_pay_concurrency")))


def shutdown_drain_seconds() -> float:
    raw = os.environ.get("GSS_SHUTDOWN_DRAIN_SECONDS", "15").strip()
    try:
        return max(0.0, min(120.0, float(raw)))
    except ValueError:
        return 15.0


@dataclass
class QuickPayJob:
    id: str
    idempotency_key: str
    account_fingerprint: Optional[str] = None
    status: str = "queued"          # queued | running | succeeded | failed | unknown
    stage: str = "queued"
    priority: int = 0
    attempts: int = 0
    reconcile_attempts: int = 0
    last_reconciled_at: Optional[str] = None
    order_id: Optional[str] = None
    purchase_attempt_id: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    failure_code: Optional[str] = None
    message: Optional[str] = None
    timings: Dict[str, Any] = field(default_factory=dict)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    next_action: Optional[Dict[str, Any]] = None
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
_tasks: set[asyncio.Task] = set()
_draining: bool = False
_loop_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]]" = (
    weakref.WeakKeyDictionary()
)
_loop_account_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def _get_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limit = max_concurrency()
    cached = _loop_semaphores.get(loop)
    if cached is None or cached[0] != limit:
        sem = asyncio.Semaphore(limit)
        _loop_semaphores[loop] = (limit, sem)
        return sem
    return cached[1]


def _get_account_lock(fingerprint: Optional[str]) -> Optional[asyncio.Lock]:
    if not fingerprint:
        return None
    loop = asyncio.get_running_loop()
    table = _loop_account_locks.get(loop)
    if table is None:
        table = {}
        _loop_account_locks[loop] = table
    lock = table.get(fingerprint)
    if lock is None:
        lock = asyncio.Lock()
        table[fingerprint] = lock
    return lock


def is_draining() -> bool:
    return _draining


def begin_shutdown() -> None:
    global _draining
    _draining = True


def reset_shutdown() -> None:
    global _draining
    _draining = False


async def drain_quick_pay(timeout_s: Optional[float] = None, *, keep_draining: bool = True) -> int:
    """Stop accepting new quick-pay jobs and wait for in-flight tasks to settle."""
    begin_shutdown()
    try:
        pending = [task for task in list(_tasks) if not task.done()]
        if not pending:
            return 0
        budget = shutdown_drain_seconds() if timeout_s is None else max(0.0, float(timeout_s))
        logger.info("quick_pay_shutdown_drain_start pending=%d timeout_s=%.1f", len(pending), budget)
        if budget > 0:
            _done, still_pending = await asyncio.wait(pending, timeout=budget)
        else:
            still_pending = set(pending)
        if still_pending:
            logger.warning("quick_pay_shutdown_drain_timeout remaining=%d", len(still_pending))
        else:
            logger.info("quick_pay_shutdown_drain_complete")
        return len(still_pending)
    finally:
        if not keep_draining:
            reset_shutdown()


def queue_stats() -> dict[str, Any]:
    db = get_db_optional()
    counts = {"queued": 0, "running": 0}
    if db is not None:
        try:
            counts = db.queue_stats()
        except sqlite3.Error:
            pass
    return {
        "queued": counts["queued"],
        "running": counts["running"],
        "max_concurrency": max_concurrency(),
        "draining": _draining,
    }


def list_queue_jobs(limit: int = 20) -> list[dict[str, Any]]:
    db = get_db_optional()
    if db is None:
        return []
    try:
        rows = db.list_queue_jobs(limit=limit)
    except sqlite3.Error:
        return []
    items: list[dict[str, Any]] = []
    for row in rows:
        pos = None
        if row.get("status") == "queued":
            try:
                pos = db.queue_position(row["id"])
            except sqlite3.Error:
                pos = None
        try:
            timings_raw = json.loads(row.get("timings_json") or "{}")
            timings = {str(k): int(v) for k, v in timings_raw.items()} if isinstance(timings_raw, dict) else {}
        except (TypeError, ValueError):
            timings = {}
        items.append({
            "job_id": row["id"],
            "status": row["status"],
            "stage": row["stage"],
            "queue_position": pos,
            "attempts": int(row.get("attempts") or 0),
            "order_id": row.get("order_id"),
            "amount": row.get("amount"),
            "currency": row.get("currency"),
            "failure_code": row.get("failure_code"),
            "message": row.get("message"),
            "timings_ms": timings,
            "created_at": row["created_at"],
            "finished_at": row.get("finished_at"),
        })
    return items


def _safe_message(value: Any) -> str:
    return redact_sensitive(str(value or ""))[:300]


# Only these local contract values may enter failure_code (and hence the log).
_JOB_ERROR_CODES = {code.value for group in (ErrorCode, UpstreamFailureCode) for code in group} | {
    "payment_unknown_lock", "BILLING_PROFILE_UNAVAILABLE", "token_revoked",
    "payment_state_unavailable", "service_draining", "server_draining",
    "preflight_failed", "unsupported_country", "invalid_request",
    "billing_country_mismatch",
}
_PUBLIC_CHECKOUT_REFUSALS = {
    "会话已失效，已停止建单。", "上游报告异常流量，已停止建单。", "该账号已有订阅，已停止建单。",
    "服务停机排空，未提交建单与付款，可安全重试。",
    "存在未核销的 unknown 订单，请先人工核对账单",
    "所选出口地区与固定核验账单国家不一致，请保持两者一致后再提交。",
}


def _job_error(exc: Exception) -> tuple[str, str]:
    # An exception's message can contain an unlabelled token/PAN; regex
    # redaction cannot identify those reliably. Do not store arbitrary prose.
    code, message = None, None
    if isinstance(exc, DomainError):
        code, message = exc.code, exc.message
    elif isinstance(exc, HTTPException):
        if isinstance(exc.detail, dict):
            code, message = exc.detail.get("code"), exc.detail.get("message")
        elif isinstance(exc.detail, str) and (
            "所选出口与本渠道的固定账单不兼容" in exc.detail
            or "结账账单国家与已核验账单不一致" in exc.detail
        ):
            code = "billing_country_mismatch"
            message = "所选出口地区与固定核验账单国家不一致，请保持两者一致后再提交。"
    code = str(code) if isinstance(code, str) and code in _JOB_ERROR_CODES else "quick_pay_failed"
    if not isinstance(message, str) or message not in _PUBLIC_CHECKOUT_REFUSALS:
        message = f"请求失败（{code}），请查看作业诊断。"
    return code, message


def get_job(job_id: str) -> Optional[QuickPayJob]:
    # Only a local dispatcher owns a mutable cached job. Do not cache another
    # worker's read-only snapshot: it may still be progressing there.
    if job_id in _jobs:
        return _jobs[job_id]
    db = get_db_optional()
    row = db.get_quick_pay_by_id(job_id) if db is not None else None
    return _from_db_job(row) if row is not None else None


def get_job_by_key(key: str) -> Optional[QuickPayJob]:
    """Read-only lookup; this is never an admission or dispatch decision."""
    db = get_db_optional()
    row = db.get_quick_pay_by_key(key) if db is not None else None
    return (_jobs.get(row["id"]) or _from_db_job(row)) if row is not None else None


def _safe_next_action(raw: Any) -> Optional[Dict[str, Any]]:
    """Allowlist only safe browser handoff fields (https redirect URL + type/message)."""
    if not isinstance(raw, dict):
        return None
    out: Dict[str, Any] = {}
    action_type = str(raw.get("type") or "").strip()
    if action_type:
        out["type"] = action_type[:64]
    url_candidate = str(raw.get("redirect_url") or raw.get("url") or raw.get("next_action_url") or "").strip()
    if url_candidate.startswith("https://") and len(url_candidate) <= 2048:
        out["redirect_url"] = url_candidate
    msg = redact_sensitive(str(raw.get("message") or "")).strip()
    if msg:
        out["message"] = msg[:300]
    return out or None


def _publish_sse(job: QuickPayJob) -> None:
    try:
        from ..routers.quick_pay import _out
        snapshot = _out(job, admitted=True).model_dump()
        events_router.publish_quick_pay(job.id, snapshot)
    except Exception:  # noqa: BLE001
        pass


def _notify_webhook(job: QuickPayJob, event_name: Optional[str] = None) -> None:
    event = event_name or job.status
    if event not in webhooks.SUPPORTED_EVENTS:
        return
    next_url = ""
    if isinstance(job.next_action, dict):
        next_url = str(job.next_action.get("redirect_url") or "")
    webhooks.dispatch_event(event, {
        "job_id": job.id,
        "order_id": job.order_id,
        "status": job.status,
        "stage": job.stage,
        "failure_code": job.failure_code,
        "amount": job.amount,
        "currency": job.currency,
        "lock_id": job.lock_id,
        "message": job.message,
        "next_action_url": next_url,
    })


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
    _publish_sse(job)


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
    try:
        next_action_raw = json.loads(row.get("next_action_json") or "{}")
    except (TypeError, ValueError):
        next_action_raw = None
    return QuickPayJob(
        id=row["id"], idempotency_key=row["idempotency_key"],
        account_fingerprint=row.get("account_fingerprint"), status=row["status"],
        stage=row["stage"], priority=int(row.get("priority") or 0),
        attempts=int(row.get("attempts") or 0),
        reconcile_attempts=int(row.get("reconcile_attempts") or 0),
        last_reconciled_at=row.get("last_reconciled_at"),
        order_id=row.get("order_id"),
        purchase_attempt_id=row.get("purchase_attempt_id"),
        amount=row.get("amount"), currency=row.get("currency"),
        failure_code=row.get("failure_code"), message=row.get("message"),
        lock_id=row.get("lock_id"), timings=json.loads(row.get("timings_json") or "{}"),
        diagnostics=load_checkout_diagnostics(row.get("diagnostics_json")),
        next_action=_safe_next_action(next_action_raw),
        locked_by=row.get("locked_by"),
        started_at=0, finished_at=None,
    )


def _capture_checkout_failure(job: QuickPayJob, exc: Exception) -> None:
    detail = exc.detail if isinstance(exc, (DomainError, HTTPException)) else None
    incoming = {}
    if isinstance(detail, dict):
        incoming = safe_checkout_diagnostics(detail.get("detail")) or safe_checkout_diagnostics(detail)
    # Capture the workflow stage *before* transitioning to "failed". If there
    # was no HTTP response, retain where we failed without inventing wire data.
    incoming.setdefault("stage", safe_checkout_diagnostics({"stage": job.stage}).get("stage", "failed"))
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


def _reserve_job(payload: QuickPayRequest, idempotency_key: str,
                 account_fingerprint: str) -> tuple[QuickPayJob, bool]:
    """A committed SQLite reservation is the only permission to dispatch."""
    db = get_db_optional()
    unavailable = {"code": "quick_pay_unavailable", "message": "暂时无法确认作业受理状态，请保留原幂等键。"}
    if db is None:
        raise HTTPException(503, detail=unavailable)
    if _draining:
        try:
            existing = db.get_quick_pay_by_key(idempotency_key)
        except sqlite3.Error:
            raise HTTPException(503, detail=unavailable) from None
        if existing is None:
            raise HTTPException(503, detail={
                "code": "service_draining",
                "message": "服务正在重启排空，暂不接收新任务，请稍后重试。",
            })
    job = QuickPayJob(id=uuid.uuid4().hex, idempotency_key=idempotency_key,
                      account_fingerprint=account_fingerprint or None)
    intent = quick_pay_intent_digest(payload, account_fingerprint)
    try:
        row, created = db.reserve_quick_pay(job, intent)
    except QuickPayAdmissionConflict:
        raise HTTPException(409, detail={
            "code": "idempotency_conflict",
            "message": "无法将此幂等键与本次请求匹配，未再次提交；如有原作业号，请查询其状态。",
        }) from None
    except QuickPayAdmissionBlocked as exc:
        raise HTTPException(409, detail={
            "code": "payment_unknown_lock",
            "message": "存在未核销的 unknown 订单，请先人工核对账单",
            **locks.lock_payload(exc.lock),
        }) from None
    except sqlite3.Error:
        # Even when the outcome of COMMIT cannot be determined, do not send
        # anything upstream or disclose SQLite error text / request material.
        logger.warning("quick_pay_admission_unavailable")
        raise HTTPException(503, detail=unavailable) from None
    if not created:
        return _jobs.get(row["id"]) or _from_db_job(row), False
    _jobs[job.id] = job
    return job, True


def start_quick_pay(payload: QuickPayRequest, idempotency_key: str,
                    account_fingerprint: str = "") -> QuickPayJob:
    job, created = _reserve_job(payload, idempotency_key, account_fingerprint)
    if not created:  # Includes an existing queued job, even after a restart.
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
            _event(job, "auto_renew_canceled", "下期自动续费已关闭")
            _publish_sse(job)
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

    account_lock = _get_account_lock(job.account_fingerprint)
    sem = _get_semaphore()

    async def _run_within_slot() -> QuickPayJob:
        if job.status != "queued":
            return job
        if _draining:
            job.status = "failed"
            job.stage = "failed"
            job.failure_code = "server_draining"
            job.message = "服务停机排空，未提交建单与付款，可安全重试。"
            job.diagnostics = {"stage": "draining"}
            job.finished_at = time.time()
            _event(job, "failed", job.message)
            _persist_job(job)
            return job

        job.status = "running"
        job.attempts += 1
        job.locked_by = WORKER_ID
        job.lease_until = time.time() + LEASE_SECONDS
        run_started = time.monotonic()
        if job.started_at > 1_000_000_000:
            job.timings["queue_ms"] = max(0, int((time.time() - job.started_at) * 1000))

        # Pre-flight deterministic gate before any order/checkout creation.
        preflight_started = time.monotonic()
        _set_stage(job, "preflight", "正在执行前置自动体检")
        blocked = run_quick_pay_preflight(
            plan=payload.plan,
            exit_country=payload.exit_country,
            billing_country=payload.billing_country,
            session=payload.session,
            account_fingerprint=job.account_fingerprint or "",
        )
        job.timings["preflight_ms"] = max(0, int((time.monotonic() - preflight_started) * 1000))
        if blocked is not None:
            code, reason = blocked
            job.status = "failed"
            job.stage = "failed"
            job.failure_code = code if code in _JOB_ERROR_CODES else "preflight_failed"
            job.message = reason
            job.diagnostics = {"stage": "preflight"}
            job.locked_by = None
            job.lease_until = None
            job.finished_at = time.time()
            job.timings["total_ms"] = int((time.monotonic() - run_started) * 1000)
            _event(job, "failed", job.message)
            _persist_job(job)
            return job
        _set_stage(job, "preflight_passed", "前置体检通过")

        _set_stage(job, "creating_order", "已接受任务，开始建单")
        _event(job, "running", job.stage)
        stage_started: Dict[str, float] = {"creating_order": time.monotonic()}
        try:
            create_kwargs: Dict[str, Any] = {
                "plan": payload.plan,
                "session": payload.session,
                "mode": payload.mode,
                "promo_code": payload.promo_code,
                "promo_campaign_id": payload.promo_campaign_id,
                "workspace_name": payload.workspace_name,
                "seat_quantity": payload.seat_quantity,
                "price_interval": payload.price_interval,
            }
            fields_set = getattr(payload, "model_fields_set", None)
            if (fields_set is None and getattr(payload, "billing_country", None) is not None) or (
                fields_set is not None and "billing_country" in fields_set
            ):
                create_kwargs["billing_country"] = payload.billing_country
            if (fields_set is None and getattr(payload, "exit_country", None) is not None) or (
                fields_set is not None and "exit_country" in fields_set
            ):
                create_kwargs["exit_country"] = payload.exit_country
            create_payload = OrderCreateRequest(**create_kwargs)
            def progress(stage: str) -> None:
                now = time.monotonic()
                if stage in {"reissuing", "session_refresh", "browser_decode", "browser_decoded", "creating_checkout"}:
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
                job.locked_by = None
                job.lease_until = None
                job.finished_at = time.time()
                job.timings["total_ms"] = int((time.monotonic() - run_started) * 1000)
                _event(job, "succeeded", job.message)
                _persist_job(job)
                return job

            job.stage = "confirming_payment"
            try:
                db = get_db_optional()
                if db is None:
                    raise sqlite3.DatabaseError("job storage unavailable")
                db.save_job(job)
            except Exception:
                job.stage = "checkout_created"
                raise HTTPException(503, detail={"code": "payment_state_unavailable"}) from None
            _event(job, job.stage, "正在提交卡片并确认支付")
            _publish_sse(job)
            payment_started = time.monotonic()
            order_billing = BillingIn(**order.billing) if getattr(order, "billing", None) else None
            resolved_billing = payload.billing or order_billing
            normalized_plan = orders.normalize_plan_name(payload.plan)
            if normalized_plan == "plus":
                bind_payload = protocol_cards.BindProtocolRequest(
                    card=payload.card,
                    billing=resolved_billing,
                    expected_amount=str(order.amount or ""),
                    expected_currency=str(order.currency or ""),
                )
                result = await protocol_cards.bind_and_pay_protocol(order.id, bind_payload)
            else:
                proto_payload = orders.ProtocolPayRequest(
                    card=payload.card,
                    billing=resolved_billing,
                )
                proto_out = await orders.pay_order_protocol(order.id, proto_payload)
                result = proto_out.model_dump() if hasattr(proto_out, "model_dump") else dict(proto_out)
            job.timings["payment_ms"] = int((time.monotonic() - payment_started) * 1000)
            _set_stage(job, "verifying_delivery", "支付已提交，正在核验交付")
            status = str(result.get("payment_status") or "")
            state = str(result.get("state") or "")
            job.failure_code = str(result.get("code") or "") or None
            job.message = _safe_message(result.get("message") or result.get("status_summary"))
            if status == "succeeded" or state == "paid":
                delivered = result.get("delivery_status") == "delivered"
                job.status = "succeeded" if delivered else "paid_pending_delivery"
                job.stage = job.status
                job.next_action = None
                if not delivered:
                    job.message = "已付款，待核验订阅交付。请勿重复付款。"
                _persist_job(job)
                await _cancel_auto_renew(job, order_id=job.order_id or order.id, plan=payload.plan)
                job.stage = "auto_renew_canceled" if delivered and job.auto_renew_status == "canceled" else job.status
            elif state in {"requires_action", "challenge_required"} or status == "requires_action":
                job.status = "requires_action"
                job.stage = "requires_action"
                raw_action = result.get("next_action") or getattr(view, "next_action", None)
                job.next_action = _safe_next_action(raw_action)
                if not job.message:
                    job.message = "需要持卡人或本人在浏览器完成验证（3DS / 官方确认），完成后可点击只读核验。"
                _event(job, "requires_action", job.message)
            elif state in {"failed", "rejected", "card_declined"}:
                job.status = "failed"
            elif state == "unknown" or status == "processing":
                job.status = "unknown"
                _set_stage(job, "unknown", "扣款结果待核对，已锁定")
                held = locks.hold_order_unknown(
                    job.order_id or "", job_id=job.id, reason="unknown_payment",
                )
                job.lock_id = (held or {}).get("id")
            else:
                job.status = "failed"
            if job.status == "failed":
                job.diagnostics = safe_checkout_diagnostics(job.diagnostics) or {"stage": job.stage}
                job.stage = "failed"
            job.locked_by = None
            job.lease_until = None
            job.finished_at = time.time() if job.status != "requires_action" else None
            job.timings["total_ms"] = int((time.monotonic() - run_started) * 1000)
            if job.status != "requires_action":
                _event(job, job.status, job.message or "")
            _persist_job(job)
            _notify_webhook(job)
            return job
        except Exception as exc:  # noqa: BLE001
            _capture_checkout_failure(job, exc)
            job.status = "failed"
            job.stage = "failed"
            job.failure_code, job.message = _job_error(exc)
            job.locked_by = None
            job.lease_until = None
            job.finished_at = time.time()
            job.timings["total_ms"] = int((time.monotonic() - run_started) * 1000)
            _event(job, "failed", job.message or "")
            _persist_job(job)
            _notify_webhook(job)
            logger.warning(
                "quick_pay_failed job=%s order=%s code=%s type=%s",
                job.id, job.order_id or "-", job.failure_code, type(exc).__name__,
            )
            return job

    if account_lock is not None:
        async with account_lock:
            async with sem:
                return await _run_within_slot()
    async with sem:
        return await _run_within_slot()


__all__ = [
    "QuickPayJob", "start_quick_pay", "run_quick_pay", "get_job", "get_job_by_key",
    "begin_shutdown", "reset_shutdown", "is_draining", "drain_quick_pay",
    "queue_stats", "list_queue_jobs", "max_concurrency", "shutdown_drain_seconds",
]
