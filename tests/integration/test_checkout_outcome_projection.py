"""The self-service hint is a read-only conclusion, never a new checkout."""
from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gss_api import persistence
from gss_api.persistence import Persistence
from gss_api.routers import quick_pay
from gss_api.services import order_flow
from gss_api.services.order_flow import QuickPayJob
from gss_api.store import OrderView
from gss_contracts import Mode, Order, OrderStatus, Plan


@pytest.fixture
def evidence(tmp_path):
    path = str(tmp_path / "evidence.db")
    d = Persistence(path)
    o = Order(id="order-e", idempotency_key="once", purchase_attempt_id="pa-fixture",
              plan=Plan.PLUS, billing_country="US", mode=Mode(), status=OrderStatus.REJECTED,
              amount=Decimal("0.00"))
    v = OrderView(failure_code="unusual_activity")
    j = QuickPayJob(id="job-e", order_id=o.id, status="failed", stage="failed",
                    idempotency_key="once", account_fingerprint="same-account",
                    failure_code="checkout_upstream_error", diagnostics={"upstream_status": 400})
    d.save_order(o, v, account_fingerprint=j.account_fingerprint)
    d.save_job(j)
    for stage in ("creating_order", "reissuing", "creating_checkout", "failed"):
        d.add_event(job_id=j.id, stage=stage, event="failed" if stage == "failed" else "running")
    try:
        yield d, j, o, v, path
    finally:
        d.close()


def test_hint_survives_restart_and_does_not_change_the_order(evidence, monkeypatch):
    d, j, o, _, path = evidence
    assert d.rejected_before_payment(j.id, o.id)
    fresh = Persistence(path)
    monkeypatch.setattr(persistence, "_db", fresh)
    monkeypatch.setattr(order_flow, "_jobs", {})
    monkeypatch.setattr(order_flow, "_by_key", {})
    app = FastAPI()
    app.include_router(quick_pay.router)
    try:
        before = fresh._conn.total_changes
        with TestClient(app) as http:
            r = http.get("/api/payments/quick-pay/" + j.id)
            assert r.status_code == 200
            payload = r.json()
            assert payload["checkout_outcome"] == "rejected_before_payment"
            assert payload["status"] == "failed"
            assert fresh._conn.total_changes == before
    finally:
        fresh.close()


@pytest.mark.parametrize("sql", [
    "UPDATE quick_pay_jobs SET status='unknown'", "UPDATE quick_pay_jobs SET lock_id='lock-test'",
    "UPDATE quick_pay_jobs SET order_id=NULL", "UPDATE quick_pay_jobs SET account_fingerprint='different'",
    "UPDATE quick_pay_jobs SET purchase_attempt_id='later-phase'",
    "UPDATE quick_pay_jobs SET failure_code='already_subscribed'",
    "UPDATE orders SET failure_code='already_subscribed'", "UPDATE orders SET status='unknown'",
    "UPDATE orders SET payment_status='processing'", "UPDATE orders SET payment_intent_id='configured'",
    "UPDATE orders SET checkout_session_id='configured'", "UPDATE orders SET delivery_status='delivered'",
    """UPDATE orders SET view_json='{"receipt":{"status":"paid"}}'""",
    """UPDATE orders SET view_json='{"checkout_url":"https://example.invalid/checkout"}'""",
    """UPDATE quick_pay_jobs SET diagnostics_json='{"upstream_status":502}'""",
    """UPDATE quick_pay_jobs SET diagnostics_json='{}'""",
    "DELETE FROM order_events WHERE stage='creating_checkout'",
    "UPDATE order_events SET stage='confirming_payment' WHERE stage='reissuing'",
    "UPDATE order_events SET stage='unrecognized_stage' WHERE stage='reissuing'",
    "DELETE FROM order_events WHERE stage='failed'",
])
def test_conflicting_or_insufficient_evidence_never_invites_payment(evidence, sql):
    d, j, o, _, _ = evidence
    d._conn.execute(sql)
    assert not d.rejected_before_payment(j.id, o.id)


def test_locked_account_or_new_attempt_supersedes_past_handoff(evidence):
    d, j, o, _, _ = evidence
    d.acquire_lock(lock_id="other-lock", lock_key="account:same-account", lock_type="account", reason="unknown")
    assert not d.rejected_before_payment(j.id, o.id)
    d.release_lock("other-lock", released_by="operator", note="synthetic-test")
    assert d.rejected_before_payment(j.id, o.id)
    d.save_job(replace(j, id="other-job", idempotency_key="another", order_id=None, stage="queued", status="queued"))
    assert not d.rejected_before_payment(j.id, o.id)
