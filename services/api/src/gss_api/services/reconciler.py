"""Read-only post-payment reconciler for paid_pending_delivery and unknown orders.

Financial safety invariants:
1. Strictly read-only: never submits a new charge, checkout, or confirmation.
2. Never uses generic account subscription activity to resolve an unknown charge.
3. Never releases a held unknown-payment lock as "no charge"; only resolves a held
   lock to ``resolved`` after the authoritative order record confirms both
   ``payment_status == "succeeded"`` and active/delivered entitlement.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import weakref
from datetime import datetime, timezone
from typing import Any, Optional

from gss_contracts import OrderStatus, Plan
from gss_core.vault import vault

from .. import locks
from ..persistence import get_db_optional
from ..routers import orders, protocol_cards
from . import order_flow, webhooks

logger = logging.getLogger("gss.reconciler")

BACKOFF_SECONDS = (15.0, 30.0, 60.0, 120.0, 180.0)

_bg_task: Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None
_last_run_at: Optional[str] = None
_total_runs: int = 0
_resolved_count: int = 0
_loop_order_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def reconciler_enabled() -> bool:
    from gss_core import runtime_flags

    return runtime_flags.get_bool("reconciler_enabled")


def reconciler_interval_s() -> float:
    raw = os.environ.get("GSS_RECONCILER_INTERVAL_S", "15").strip()
    try:
        return max(2.0, min(600.0, float(raw)))
    except ValueError:
        return 15.0


def reconciler_max_attempts() -> int:
    raw = os.environ.get("GSS_RECONCILER_MAX_ATTEMPTS", "5").strip()
    try:
        return max(1, min(20, int(raw)))
    except ValueError:
        return 5


def _order_lock(order_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    table = _loop_order_locks.get(loop)
    if table is None:
        table = {}
        _loop_order_locks[loop] = table
    lock = table.get(order_id)
    if lock is None:
        lock = asyncio.Lock()
        table[order_id] = lock
    return lock


def _backoff_ready(attempts: int, last_reconciled_at: Optional[str]) -> bool:
    if attempts <= 0 or not last_reconciled_at:
        return True
    try:
        last_dt = datetime.fromisoformat(last_reconciled_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
    return (datetime.now(timezone.utc) - last_dt).total_seconds() >= delay


def _load_or_attach_job(job_id: Optional[str], order_id: Optional[str]) -> Optional[order_flow.QuickPayJob]:
    if job_id:
        existing = order_flow.get_job(job_id)
        if existing is not None:
            return existing
    db = get_db_optional()
    if db is not None and order_id:
        row = db.get_job_by_order_id(order_id)
        if row is not None:
            return order_flow._jobs.get(row["id"]) or order_flow._from_db_job(row)
    return None


def _resolve_held_lock_if_confirmed(order_id: str, job: Optional[order_flow.QuickPayJob]) -> Optional[str]:
    db = get_db_optional()
    if db is None:
        return None
    lock_id = (job.lock_id if job else None) or ""
    if not lock_id:
        view = orders.store.views.get(order_id)
        fp = (view.account_fingerprint if view else None) or (job.account_fingerprint if job else None) or ""
        held = locks.find_active_lock(fp) if fp else None
        if held and held.get("order_id") == order_id:
            lock_id = str(held.get("id") or "")
    if not lock_id:
        return None
    ok, _msg, updated = locks.resolve_paid(
        lock_id,
        note="只读对账确认原订单扣款成功且订阅已交付，自动核销锁。",
        operator="reconciler",
    )
    if ok and updated:
        return str(updated.get("state") or "resolved")
    row = db.get_lock(lock_id)
    return str(row.get("state")) if row else None


def _record_event(order_id: str, job_id: str, *, event: str, code: str, message: str) -> None:
    db = get_db_optional()
    if db is None:
        return
    try:
        db.add_event(
            stage="reconcile",
            event=event,
            order_id=order_id,
            job_id=job_id,
            code=code,
            message=message,
        )
    except Exception:  # noqa: BLE001
        logger.warning("reconcile_event_write_failed order=%s", order_id)


async def reconcile_order(
    order_id: str,
    *,
    job_id: Optional[str] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run one strictly read-only reconciliation pass for an order/job pair."""
    global _resolved_count
    async with _order_lock(order_id):
        order = orders.store.get_order(order_id)
        job = _load_or_attach_job(job_id, order_id)
        if order is None:
            return {
                "order_id": order_id,
                "job_id": job.id if job else job_id,
                "outcome": "order_not_found",
                "changed": False,
                "message": "订单不存在，无法对账。",
            }

        view = orders.store.view(order_id)
        attempts = int(job.reconcile_attempts if job else 0)
        if not force and job is not None:
            if attempts >= reconciler_max_attempts():
                return {
                    "order_id": order_id,
                    "job_id": job.id,
                    "outcome": "max_attempts_reached",
                    "changed": False,
                    "order_status": str(order.status),
                    "payment_status": view.payment_status,
                    "delivery_status": view.delivery_status,
                    "job_status": job.status,
                    "reconcile_attempts": attempts,
                    "message": "已达自动对账次数上限，保留当前状态待人工核对。",
                }
            if not _backoff_ready(attempts, job.last_reconciled_at):
                return {
                    "order_id": order_id,
                    "job_id": job.id,
                    "outcome": "backoff_waiting",
                    "changed": False,
                    "order_status": str(order.status),
                    "payment_status": view.payment_status,
                    "delivery_status": view.delivery_status,
                    "job_status": job.status,
                    "reconcile_attempts": attempts,
                    "message": "距上次对账时间过短，等待退避窗口。",
                }

        prev_order_status = str(order.status)
        prev_job_status = job.status if job else None
        now_iso = _utcnow_iso()
        if job is not None:
            job.reconcile_attempts = attempts + 1
            job.last_reconciled_at = now_iso

        # 1) Already confirmed paid and active/delivered locally -> converge job, auto-renew, and lock.
        if order.status in {OrderStatus.ACTIVE, OrderStatus.DELIVERED} and view.payment_status == "succeeded":
            changed = False
            if job is not None and job.status != "succeeded":
                job.status = "succeeded"
                job.stage = "succeeded"
                job.failure_code = None
                job.message = "对账确认订阅已生效交付。"
                job.finished_at = job.finished_at or time.time()
                changed = True
                if view.auto_cancel_renew and view.auto_renew_status not in {"canceled", "disabled"}:
                    await order_flow._cancel_auto_renew(job, order_id=order_id, plan=str(order.plan))
                    if job.auto_renew_status == "canceled":
                        job.stage = "auto_renew_canceled"
                order_flow._persist_job(job)
            lock_state = _resolve_held_lock_if_confirmed(order_id, job)
            if lock_state == "resolved":
                changed = True
            if changed:
                _resolved_count += 1
                _record_event(
                    order_id, job.id if job else "",
                    event="succeeded", code="reconcile_resolved",
                    message="只读对账收敛：订单已确认付款并交付。",
                )
            return {
                "order_id": order_id,
                "job_id": job.id if job else None,
                "outcome": "resolved_delivered",
                "changed": changed,
                "order_status": str(order.status),
                "payment_status": view.payment_status,
                "delivery_status": view.delivery_status,
                "job_status": job.status if job else None,
                "lock_state": lock_state,
                "reconcile_attempts": job.reconcile_attempts if job else 1,
                "message": "订单已确认付款并交付完成。",
            }

        # 2) Paid but entitlement verification was delayed -> read-only delivery verification.
        if view.payment_status == "succeeded":
            secrets = vault.get(order_id)
            if secrets is None:
                if job is not None:
                    order_flow._persist_job(job)
                _record_event(
                    order_id, job.id if job else "",
                    event="pending", code="vault_expired",
                    message="内存会话凭证已过有效期，无法自动核验订阅，请人工确认账号订阅状态。",
                )
                return {
                    "order_id": order_id,
                    "job_id": job.id if job else None,
                    "outcome": "manual_review_required",
                    "changed": False,
                    "order_status": str(order.status),
                    "payment_status": view.payment_status,
                    "delivery_status": view.delivery_status,
                    "job_status": job.status if job else None,
                    "reconcile_attempts": job.reconcile_attempts if job else 1,
                    "message": "订单已确认付款，但原会话凭证已过期，请通过「账号详情」人工核对订阅交付。",
                }
            delivered = await orders._verify_after_payment(order, view)
            order = orders.store.get_order(order_id) or order
            view = orders.store.view(order_id)
            if delivered:
                if job is not None:
                    job.status = "succeeded"
                    job.stage = "succeeded"
                    job.failure_code = None
                    job.message = "只读对账确认订阅已生效交付。"
                    job.finished_at = job.finished_at or time.time()
                    order_flow._persist_job(job)
                    if view.auto_cancel_renew and view.auto_renew_status not in {"canceled", "disabled"}:
                        await order_flow._cancel_auto_renew(job, order_id=order_id, plan=str(order.plan))
                        if job.auto_renew_status == "canceled":
                            job.stage = "auto_renew_canceled"
                        order_flow._persist_job(job)
                lock_state = _resolve_held_lock_if_confirmed(order_id, job)
                _resolved_count += 1
                _record_event(
                    order_id, job.id if job else "",
                    event="succeeded", code="reconcile_delivered",
                    message="只读对账核验通过，订单已推进至 delivered。",
                )
                if job is not None:
                    order_flow._notify_webhook(job, "reconcile_resolved")
                return {
                    "order_id": order_id,
                    "job_id": job.id if job else None,
                    "outcome": "resolved_delivered",
                    "changed": True,
                    "order_status": str(order.status),
                    "payment_status": view.payment_status,
                    "delivery_status": view.delivery_status,
                    "job_status": job.status if job else None,
                    "lock_state": lock_state,
                    "reconcile_attempts": job.reconcile_attempts if job else 1,
                    "message": "只读对账核验通过，订阅已确认交付。",
                }
            if job is not None:
                order_flow._persist_job(job)
            _record_event(
                order_id, job.id if job else "",
                event="pending", code="delivery_still_pending",
                message="只读核验暂未观测到订阅生效，保留已付款待交付状态。",
            )
            return {
                "order_id": order_id,
                "job_id": job.id if job else None,
                "outcome": "still_pending_delivery",
                "changed": False,
                "order_status": str(order.status),
                "payment_status": view.payment_status,
                "delivery_status": view.delivery_status,
                "job_status": job.status if job else None,
                "reconcile_attempts": job.reconcile_attempts if job else 1,
                "message": "已付款，上游订阅暂未同步生效，将在下个退避窗口继续只读复核。",
            }

        # 3) Unknown or requires_action -> strictly read-only original protocol status probe if vault is live.
        if order.status in {OrderStatus.UNKNOWN, OrderStatus.REQUIRES_ACTION} or (
            job is not None and job.status in {"unknown", "requires_action"}
        ):
            is_action_wait = order.status is OrderStatus.REQUIRES_ACTION or (
                job is not None and job.status == "requires_action"
            )
            secrets = vault.get(order_id)
            if secrets is None:
                if job is not None:
                    order_flow._persist_job(job)
                _record_event(
                    order_id, job.id if job else "",
                    event="requires_action" if is_action_wait else "unknown",
                    code="vault_expired",
                    message="原支付上下文已过内存有效期，请人工核对银行/Stripe 账单后处理。",
                )
                return {
                    "order_id": order_id,
                    "job_id": job.id if job else None,
                    "outcome": "manual_review_required",
                    "changed": False,
                    "order_status": str(order.status),
                    "payment_status": view.payment_status,
                    "delivery_status": view.delivery_status,
                    "job_status": job.status if job else None,
                    "lock_state": None if is_action_wait else "held",
                    "reconcile_attempts": job.reconcile_attempts if job else 1,
                    "message": "原支付上下文已过期，请人工核对账单或订阅状态。",
                }
            try:
                if order.plan is Plan.TEAM:
                    await orders.poll_team(order_id)
                elif order.plan is Plan.PLUS:
                    await protocol_cards.get_bind_protocol_status(order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("reconcile_probe_failed order=%s type=%s", order_id, type(exc).__name__)

            order = orders.store.get_order(order_id) or order
            view = orders.store.view(order_id)
            if view.payment_status == "succeeded":
                delivered = order.status in {OrderStatus.ACTIVE, OrderStatus.DELIVERED}
                if not delivered:
                    delivered = await orders._verify_after_payment(order, view)
                    order = orders.store.get_order(order_id) or order
                    view = orders.store.view(order_id)
                if job is not None:
                    job.status = "succeeded" if delivered else "paid_pending_delivery"
                    job.stage = job.status
                    job.next_action = None
                    job.failure_code = None
                    job.message = "只读对账确认原订单已扣款并交付。" if delivered else "只读对账确认已扣款，等待订阅交付。"
                    if delivered:
                        job.finished_at = job.finished_at or time.time()
                    order_flow._persist_job(job)
                    if delivered and view.auto_cancel_renew and view.auto_renew_status not in {"canceled", "disabled"}:
                        await order_flow._cancel_auto_renew(job, order_id=order_id, plan=str(order.plan))
                        if job.auto_renew_status == "canceled":
                            job.stage = "auto_renew_canceled"
                        order_flow._persist_job(job)
                lock_state = _resolve_held_lock_if_confirmed(order_id, job) if delivered else (None if is_action_wait else "held")
                if delivered:
                    _resolved_count += 1
                    if job is not None:
                        order_flow._notify_webhook(job, "reconcile_resolved")
                _record_event(
                    order_id, job.id if job else "",
                    event="succeeded" if delivered else "paid_pending_delivery",
                    code="reconcile_confirmed_paid",
                    message="只读对账确认原单已付款。",
                )
                return {
                    "order_id": order_id,
                    "job_id": job.id if job else None,
                    "outcome": "resolved_delivered" if delivered else "confirmed_paid_pending_delivery",
                    "changed": True,
                    "order_status": str(order.status),
                    "payment_status": view.payment_status,
                    "delivery_status": view.delivery_status,
                    "job_status": job.status if job else None,
                    "lock_state": lock_state,
                    "reconcile_attempts": job.reconcile_attempts if job else 1,
                    "message": "只读对账确认原单已付款并交付。" if delivered else "只读对账确认原单已付款，等待订阅交付。",
                }

            # Even if upstream probe reports failed, keep held lock for explicit human verification.
            if job is not None:
                order_flow._persist_job(job)
            if is_action_wait and view.payment_status != "failed":
                _record_event(
                    order_id, job.id if job else "",
                    event="requires_action", code="still_requires_action",
                    message="仍在等待持卡人/本人完成验证。",
                )
                return {
                    "order_id": order_id,
                    "job_id": job.id if job else None,
                    "outcome": "still_requires_action",
                    "changed": False,
                    "order_status": str(order.status),
                    "payment_status": view.payment_status,
                    "delivery_status": view.delivery_status,
                    "job_status": job.status if job else None,
                    "lock_state": None,
                    "reconcile_attempts": job.reconcile_attempts if job else 1,
                    "message": "尚未检测到验证完成，请在浏览器完成验证后再次只读核验。",
                }
            outcome = "upstream_reports_unpaid_manual_unlock" if view.payment_status == "failed" else "still_unknown"
            msg = (
                "原协议状态显示未扣款失败，按安全策略仍保留资金锁，请在核对账单后手动解锁。"
                if outcome == "upstream_reports_unpaid_manual_unlock"
                else "扣款结果仍待确认，已保留资金锁，未发起重复扣款。"
            )
            _record_event(
                order_id, job.id if job else "",
                event="unknown", code=outcome, message=msg,
            )
            return {
                "order_id": order_id,
                "job_id": job.id if job else None,
                "outcome": outcome,
                "changed": str(order.status) != prev_order_status or (job is not None and job.status != prev_job_status),
                "order_status": str(order.status),
                "payment_status": view.payment_status,
                "delivery_status": view.delivery_status,
                "job_status": job.status if job else None,
                "lock_state": None if is_action_wait else "held",
                "reconcile_attempts": job.reconcile_attempts if job else 1,
                "message": msg,
            }

        return {
            "order_id": order_id,
            "job_id": job.id if job else None,
            "outcome": "not_reconcilable",
            "changed": False,
            "order_status": str(order.status),
            "payment_status": view.payment_status,
            "delivery_status": view.delivery_status,
            "job_status": job.status if job else None,
            "reconcile_attempts": attempts,
            "message": f"当前订单状态为 {order.status}，无需自动对账。",
        }


async def reconcile_job(job_id: str, *, force: bool = False) -> dict[str, Any]:
    job = order_flow.get_job(job_id)
    if job is None:
        return {"job_id": job_id, "outcome": "job_not_found", "changed": False, "message": "作业不存在。"}
    if not job.order_id:
        return {
            "job_id": job_id,
            "outcome": "no_order_linked",
            "changed": False,
            "job_status": job.status,
            "message": "该作业尚未关联订单，无法执行支付对账。",
        }
    return await reconcile_order(job.order_id, job_id=job.id, force=force)


async def reconcile_lock(lock_id: str, *, force: bool = True) -> dict[str, Any]:
    db = get_db_optional()
    row = db.get_lock(lock_id) if db else None
    if row is None:
        return {"outcome": "lock_not_found", "changed": False, "message": "资金锁不存在。"}
    order_id = row.get("order_id") or ""
    if not order_id:
        return {
            "job_id": row.get("job_id"),
            "outcome": "no_order_linked",
            "changed": False,
            "lock_state": row.get("state"),
            "message": "该资金锁未关联订单号，请核对账单后手动解锁。",
        }
    result = await reconcile_order(order_id, job_id=row.get("job_id"), force=force)
    latest_lock = db.get_lock(lock_id) if db else None
    if latest_lock:
        result["lock_state"] = latest_lock.get("state")
    return result


async def run_once(limit: int = 20) -> list[dict[str, Any]]:
    global _last_run_at, _total_runs
    db = get_db_optional()
    _last_run_at = _utcnow_iso()
    _total_runs += 1
    if db is None:
        return []
    candidates = db.list_reconcilable_jobs(limit=limit)
    results: list[dict[str, Any]] = []
    for row in candidates:
        order_id = row.get("order_id") or ""
        if not order_id:
            continue
        try:
            res = await reconcile_order(order_id, job_id=row["id"], force=False)
            results.append(res)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconcile_pass_error job=%s type=%s", row.get("id"), type(exc).__name__)
    return results


async def _loop() -> None:
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=reconciler_interval_s())
            break
        except asyncio.TimeoutError:
            pass
        if order_flow.is_draining():
            break
        try:
            await run_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconciler_loop_error type=%s", type(exc).__name__)


def start() -> None:
    global _bg_task, _stop_event
    if not reconciler_enabled():
        return
    if _bg_task is not None and not _bg_task.done():
        return
    _stop_event = asyncio.Event()
    _bg_task = asyncio.create_task(_loop(), name="gss-readonly-reconciler")


async def stop() -> None:
    global _bg_task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    task = _bg_task
    _bg_task = None
    _stop_event = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def status() -> dict[str, Any]:
    db = get_db_optional()
    pending = 0
    if db is not None:
        try:
            pending = len(db.list_reconcilable_jobs(limit=100))
        except Exception:  # noqa: BLE001
            pending = 0
    return {
        "enabled": reconciler_enabled(),
        "running": _bg_task is not None and not _bg_task.done(),
        "interval_s": reconciler_interval_s(),
        "max_attempts": reconciler_max_attempts(),
        "pending_candidates": pending,
        "last_run_at": _last_run_at,
        "total_runs": _total_runs,
        "resolved_count": _resolved_count,
    }


__all__ = [
    "reconcile_order", "reconcile_job", "reconcile_lock", "run_once",
    "start", "stop", "status", "reconciler_enabled", "reconciler_interval_s",
    "reconciler_max_attempts",
]
