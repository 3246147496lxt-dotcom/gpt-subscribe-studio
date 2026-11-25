"""Phase 2 tests: read-only reconciler, server-side order ledger, and locks console."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from gss_api import locks, main, persistence
from gss_api.routers import orders as orders_router
from gss_api.services import order_flow, reconciler
from gss_api.store import InMemoryStore, OrderView
from gss_contracts import Mode, Order, OrderStatus, Plan
from gss_core.vault import OrderSecrets, vault


@pytest.fixture(autouse=True)
def _isolated_phase2(monkeypatch):
    db = persistence.Persistence(":memory:")
    store = InMemoryStore()
    store.attach_db(db)
    monkeypatch.setattr(persistence, "_db", db)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(orders_router, "store", store)
    monkeypatch.setattr(locks, "store", store)
    monkeypatch.setattr(order_flow, "_jobs", {})
    monkeypatch.setattr(order_flow, "_tasks", set())
    monkeypatch.setenv("GSS_RECONCILER_ENABLED", "0")
    order_flow.reset_shutdown()
    yield db, store
    order_flow.reset_shutdown()
    db.close()


def _seed_order(
    store: InMemoryStore,
    *,
    order_id: str,
    status: OrderStatus,
    payment_status: str,
    delivery_status: str = "pending",
    plan: Plan = Plan.PLUS,
    fingerprint: str = "fp-phase2",
    auto_cancel_renew: bool = False,
):
    order = Order(
        id=order_id,
        idempotency_key=f"idem-{order_id}",
        purchase_attempt_id=f"pa-{order_id}",
        plan=plan,
        billing_country="US",
        mode=Mode(),
        status=status,
        amount=Decimal("20.00"),
    )
    view = OrderView(
        purchase_attempt_id=order.purchase_attempt_id,
        account_fingerprint=fingerprint,
        exit_country="US",
        currency="USD",
        amount="20.00",
        payment_status=payment_status,
        delivery_status=delivery_status,
        payment_intent_id=f"pi_{order_id}",
        auto_cancel_renew=auto_cancel_renew,
        auto_renew_status="pending" if auto_cancel_renew else "disabled",
    )
    store.orders[order.id] = order
    store.views[order.id] = view
    store.persist(order.id)
    return order, view


def test_reconciler_converges_paid_pending_delivery_and_cancels_auto_renew(monkeypatch, _isolated_phase2):
    db, store = _isolated_phase2
    order, view = _seed_order(
        store,
        order_id="ord-paid-delay",
        status=OrderStatus.VERIFYING,
        payment_status="succeeded",
        delivery_status="verify_delayed",
        auto_cancel_renew=True,
    )
    job = order_flow.QuickPayJob(
        id="job-paid-delay",
        idempotency_key="idem-ord-paid-delay",
        account_fingerprint="fp-phase2",
        status="paid_pending_delivery",
        stage="paid_pending_delivery",
        order_id=order.id,
        amount="20.00",
        currency="USD",
    )
    db.save_job(job)
    order_flow._jobs[job.id] = job
    vault.put(order.id, OrderSecrets(session="synthetic-session"))

    async def fake_verify(ord_obj, ord_view):
        ord_obj.status = OrderStatus.DELIVERED
        ord_view.delivery_status = "delivered"
        store.persist(ord_obj.id)
        return True

    cancel_mock = AsyncMock()
    monkeypatch.setattr(orders_router, "_verify_after_payment", fake_verify)
    monkeypatch.setattr(order_flow, "_cancel_auto_renew", cancel_mock)

    try:
        res = asyncio.run(reconciler.reconcile_job(job.id, force=True))
    finally:
        vault.drop(order.id)

    assert res["outcome"] == "resolved_delivered"
    assert res["changed"] is True
    assert res["job_status"] == "succeeded"
    assert res["order_status"] == "delivered"
    assert res["reconcile_attempts"] == 1
    cancel_mock.assert_awaited_once()
    persisted = db.get_quick_pay_by_id(job.id)
    assert persisted["status"] == "succeeded"
    assert persisted["reconcile_attempts"] == 1
    assert persisted["last_reconciled_at"] is not None


def test_reconciler_keeps_held_lock_when_unknown_vault_expired(_isolated_phase2):
    db, store = _isolated_phase2
    order, _view = _seed_order(
        store,
        order_id="ord-unk-expired",
        status=OrderStatus.UNKNOWN,
        payment_status="processing",
        fingerprint="fp-unk-1",
    )
    lock = locks.hold_order_unknown(order.id, job_id="job-unk-expired")
    job = order_flow.QuickPayJob(
        id="job-unk-expired",
        idempotency_key="idem-ord-unk-expired",
        account_fingerprint="fp-unk-1",
        status="unknown",
        stage="unknown",
        order_id=order.id,
        lock_id=lock["id"],
    )
    db.save_job(job)
    order_flow._jobs[job.id] = job
    vault.drop(order.id)

    res = asyncio.run(reconciler.reconcile_lock(lock["id"], force=True))
    assert res["outcome"] == "manual_review_required"
    assert res["changed"] is False
    assert res["lock_state"] == "held"
    assert db.get_lock(lock["id"])["state"] == "held"


def test_reconciler_resolves_unknown_lock_only_on_authoritative_paid_delivery(monkeypatch, _isolated_phase2):
    db, store = _isolated_phase2
    order, view = _seed_order(
        store,
        order_id="ord-unk-paid",
        status=OrderStatus.UNKNOWN,
        payment_status="processing",
        fingerprint="fp-unk-paid",
    )
    lock = locks.hold_order_unknown(order.id, job_id="job-unk-paid")
    job = order_flow.QuickPayJob(
        id="job-unk-paid",
        idempotency_key="idem-ord-unk-paid",
        account_fingerprint="fp-unk-paid",
        status="unknown",
        stage="unknown",
        order_id=order.id,
        lock_id=lock["id"],
    )
    db.save_job(job)
    order_flow._jobs[job.id] = job
    vault.put(order.id, OrderSecrets(session="synthetic-session"))

    async def fake_probe(oid):
        order.status = OrderStatus.DELIVERED
        view.payment_status = "succeeded"
        view.delivery_status = "delivered"
        store.persist(oid)
        return {"state": "paid"}

    monkeypatch.setattr(reconciler.protocol_cards, "get_bind_protocol_status", fake_probe)

    try:
        res = asyncio.run(reconciler.reconcile_lock(lock["id"], force=True))
    finally:
        vault.drop(order.id)

    assert res["outcome"] == "resolved_delivered"
    assert res["changed"] is True
    assert res["job_status"] == "succeeded"
    assert res["lock_state"] == "resolved"
    assert db.get_lock(lock["id"])["state"] == "resolved"
    assert locks.find_active_lock("fp-unk-paid") is None


def test_reconciler_keeps_held_lock_even_when_probe_reports_failed(monkeypatch, _isolated_phase2):
    db, store = _isolated_phase2
    order, view = _seed_order(
        store,
        order_id="ord-unk-fail",
        status=OrderStatus.UNKNOWN,
        payment_status="processing",
        fingerprint="fp-unk-fail",
    )
    lock = locks.hold_order_unknown(order.id, job_id="job-unk-fail")
    job = order_flow.QuickPayJob(
        id="job-unk-fail",
        idempotency_key="idem-ord-unk-fail",
        account_fingerprint="fp-unk-fail",
        status="unknown",
        stage="unknown",
        order_id=order.id,
        lock_id=lock["id"],
    )
    db.save_job(job)
    order_flow._jobs[job.id] = job
    vault.put(order.id, OrderSecrets(session="synthetic-session"))

    async def fake_probe_failed(oid):
        view.payment_status = "failed"
        store.persist(oid)
        return {"state": "card_declined"}

    monkeypatch.setattr(reconciler.protocol_cards, "get_bind_protocol_status", fake_probe_failed)

    try:
        res = asyncio.run(reconciler.reconcile_lock(lock["id"], force=True))
    finally:
        vault.drop(order.id)

    assert res["outcome"] == "upstream_reports_unpaid_manual_unlock"
    assert res["lock_state"] == "held"
    assert db.get_lock(lock["id"])["state"] == "held"


def test_reconciler_backoff_and_max_attempts(monkeypatch, _isolated_phase2):
    db, store = _isolated_phase2
    monkeypatch.setenv("GSS_RECONCILER_MAX_ATTEMPTS", "2")
    order, _view = _seed_order(
        store,
        order_id="ord-backoff",
        status=OrderStatus.UNKNOWN,
        payment_status="processing",
        fingerprint="fp-backoff",
    )
    job = order_flow.QuickPayJob(
        id="job-backoff",
        idempotency_key="idem-ord-backoff",
        account_fingerprint="fp-backoff",
        status="unknown",
        stage="unknown",
        order_id=order.id,
    )
    db.save_job(job)
    order_flow._jobs[job.id] = job

    first = asyncio.run(reconciler.run_once())
    assert len(first) == 1 and first[0]["outcome"] == "manual_review_required"

    # Immediate second run is skipped by backoff window.
    second = asyncio.run(reconciler.run_once())
    assert len(second) == 1 and second[0]["outcome"] == "backoff_waiting"

    # Exhaust attempts -> max_attempts_reached.
    job.reconcile_attempts = 2
    job.last_reconciled_at = "2020-01-01T00:00:00+00:00"
    db.save_job(job)
    third = asyncio.run(reconciler.run_once())
    assert len(third) == 1 and third[0]["outcome"] == "max_attempts_reached"


def test_ledger_locks_and_reconciler_endpoints(_isolated_phase2):
    db, store = _isolated_phase2
    _seed_order(store, order_id="ord-delivered-1", status=OrderStatus.DELIVERED, payment_status="succeeded", delivery_status="delivered")
    _seed_order(store, order_id="ord-failed-1", status=OrderStatus.FAILED, payment_status="failed", plan=Plan.TEAM)
    _seed_order(store, order_id="ord-unknown-1", status=OrderStatus.UNKNOWN, payment_status="processing", fingerprint="fp-api-lock")
    lock = locks.hold_order_unknown("ord-unknown-1", reason="unknown_payment")
    db.add_event(stage="creating_order", event="running", order_id="ord-delivered-1", message="建单完成")

    with TestClient(main.create_app()) as client:
        all_resp = client.get("/api/orders?limit=10&offset=0")
        assert all_resp.status_code == 200
        body = all_resp.json()
        assert body["total"] == 3
        assert body["stats"]["delivered"] == 1
        assert body["stats"]["failed"] == 1
        assert body["stats"]["unknown"] == 1
        assert body["stats"]["active_locks"] == 1

        filtered = client.get("/api/orders?status=succeeded")
        assert filtered.status_code == 200
        assert [item["id"] for item in filtered.json()["items"]] == ["ord-delivered-1"]

        searched = client.get("/api/orders?search=ord-failed")
        assert searched.status_code == 200
        assert [item["id"] for item in searched.json()["items"]] == ["ord-failed-1"]

        events_resp = client.get("/api/orders/ord-delivered-1/events")
        assert events_resp.status_code == 200
        assert len(events_resp.json()) == 1
        assert events_resp.json()[0]["stage"] == "creating_order"

        locks_resp = client.get("/api/payments/locks?state=held")
        assert locks_resp.status_code == 200
        locks_body = locks_resp.json()
        assert locks_body["total"] == 1
        assert locks_body["counts"]["held"] == 1
        assert locks_body["items"][0]["id"] == lock["id"]
        assert locks_body["items"][0]["order_id"] == "ord-unknown-1"
        assert locks_body["items"][0]["lock_key_prefix"].startswith("account:")

        rec_resp = client.post(f"/api/payments/locks/{lock['id']}/reconcile")
        assert rec_resp.status_code == 200
        assert rec_resp.json()["outcome"] == "manual_review_required"
        assert rec_resp.json()["lock_state"] == "held"

        status_resp = client.get("/api/payments/reconciler")
        assert status_resp.status_code == 200
        assert "max_attempts" in status_resp.json()

        health_resp = client.get("/api/health")
        assert health_resp.status_code == 200
        assert "reconciler" in health_resp.json()
