from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from gss_api import locks, persistence
from gss_api.persistence import Persistence
from gss_api.services.order_flow import QuickPayJob
from gss_api.store import InMemoryStore, OrderView
from gss_contracts import Mode, Order, OrderStatus, Plan


def _order(order_id="order-persist"):
    return Order(id=order_id, idempotency_key="idem-persist", purchase_attempt_id="pa_persist",
                 plan=Plan.PRO_LITE, billing_country="US", mode=Mode(),
                 status=OrderStatus.UNKNOWN, amount=Decimal("100.00"))


def test_roundtrip_persists_unknown_and_never_stores_secret_fields(tmp_path):
    db = Persistence(str(tmp_path / "gss.db"))
    order = _order()
    view = OrderView(payment_status="processing", payment_intent_id="pi_test_1",
                     account_fingerprint="fingerprint-1", exit_country="CL",
                     auto_cancel_renew=True, auto_renew_status="pending",
                     proxy_rotations=1, proxy_last_sid="abcd1234wxyz",
                     unknown_since="2026-11-20T00:00:00+00:00")
    db.save_order(order, view, account_fingerprint=view.account_fingerprint)
    rows = db.load_orders()
    assert len(rows) == 1
    loaded_order, loaded_view, fingerprint = rows[0]
    assert loaded_order.status is OrderStatus.UNKNOWN
    assert loaded_view.payment_status == "processing"
    assert loaded_view.payment_intent_id == "pi_test_1"
    assert loaded_view.exit_country == "CL"
    assert loaded_view.auto_cancel_renew is True
    assert loaded_view.auto_renew_status == "pending"
    assert loaded_view.proxy_rotations == 1
    assert fingerprint == "fingerprint-1"

    conn = sqlite3.connect(tmp_path / "gss.db")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
    assert not {"card", "cvc", "session", "session_token", "cookie"} & columns
    dump = "\n".join(conn.iterdump())
    assert "4242424242424242" not in dump  # release-check: ignore
    assert "super-secret-session" not in dump
    conn.close()
    db.close()


def test_persistent_store_rehydrates_unknown_order(tmp_path):
    db = Persistence(str(tmp_path / "gss.db"))
    order = _order("order-rehydrate")
    view = OrderView(payment_status="processing", account_fingerprint="fp-rehydrate")
    db.save_order(order, view, account_fingerprint="fp-rehydrate")
    store = InMemoryStore()
    store.attach_db(db)
    assert store.get_order("order-rehydrate").status is OrderStatus.UNKNOWN
    assert store.view("order-rehydrate").account_fingerprint == "fp-rehydrate"


def test_quick_pay_job_lock_roundtrip(tmp_path):
    db = Persistence(str(tmp_path / "gss.db"))
    job = QuickPayJob(id="job-1", idempotency_key="idem-job", account_fingerprint="fp-job",
                      status="unknown", stage="unknown", order_id="order-1",
                      amount="100.00", currency="USD", failure_code="payment_unknown_state",
                      lock_id="lock-1")
    db.save_job(job)
    row = db.load_jobs()[0]
    assert row["status"] == "unknown"
    assert row["lock_id"] == "lock-1"
    assert row["account_fingerprint"] == "fp-job"
    db.add_event(stage="checkout_created", event="succeeded", job_id="job-1",
                 message="结账单已生成")
    events = db.list_events(job_id="job-1")
    assert events and events[0]["stage"] == "checkout_created"
    assert events[0]["message"] == "结账单已生成"


def test_unknown_lock_blocks_and_manual_release_requires_confirmation(tmp_path, monkeypatch):
    db = Persistence(str(tmp_path / "gss.db"))
    monkeypatch.setattr(persistence, "_db", db)
    store = InMemoryStore()
    store.attach_db(db)
    monkeypatch.setattr(locks, "store", store)
    order = _order("order-lock")
    order.status = OrderStatus.UNKNOWN
    store.orders[order.id] = order
    view = OrderView(payment_status="processing", account_fingerprint="fp-lock",
                     payment_intent_id="pi_lock")
    store.views[order.id] = view
    lock = locks.hold_order_unknown(order.id)
    assert lock and locks.find_active_lock("fp-lock")

    ok, message, _ = locks.release(lock["id"], confirm_no_charge=False, note="checked", operator="tester")
    assert not ok
    ok, message, released = locks.release(lock["id"], confirm_no_charge=True, note="", operator="tester")
    assert not ok
    ok, message, released = locks.release(lock["id"], confirm_no_charge=True, note="checked statement", operator="tester")
    assert ok and released["state"] == "released"
    assert locks.find_active_lock("fp-lock") is None
    assert db.get_lock(lock["id"])["released_by"] == "tester"


def test_release_refuses_payment_that_is_already_succeeded(tmp_path, monkeypatch):
    db = Persistence(str(tmp_path / "gss.db"))
    monkeypatch.setattr(persistence, "_db", db)
    store = InMemoryStore()
    store.attach_db(db)
    monkeypatch.setattr(locks, "store", store)
    order = _order("order-paid-lock")
    order.status = OrderStatus.ACTIVE
    store.orders[order.id] = order
    store.views[order.id] = OrderView(payment_status="succeeded", account_fingerprint="fp-paid")
    lock = locks.hold_unknown(fingerprint="fp-paid", order_id=order.id)
    ok, message, _ = locks.release(lock["id"], confirm_no_charge=True, note="checked", operator="tester")
    assert not ok
    assert "成功" in message
    assert db.get_lock(lock["id"])["state"] == "held"
