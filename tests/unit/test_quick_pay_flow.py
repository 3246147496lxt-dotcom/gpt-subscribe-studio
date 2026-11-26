import asyncio
from types import SimpleNamespace

import pytest

from gss_api.schemas import BillingIn, CardIn, ModeIn, QuickPayRequest
from gss_api.services import order_flow


@pytest.fixture(autouse=True)
def _clear_jobs(monkeypatch):
    from gss_api import persistence
    db = persistence.Persistence(":memory:")
    monkeypatch.setattr(persistence, "_db", db)
    monkeypatch.setattr(order_flow, "_jobs", {})
    yield
    db.close()


def _payload(debug=False):
    return QuickPayRequest(
        plan="plus",
        billing_country="US",
        exit_country="PH",
        session="session-fixture",
        **({} if debug else {"card": CardIn(number="4242424242424242", cvc="123", exp_month="11", exp_year="2028")}),  # release-check: ignore
        billing=BillingIn(name="Payer", email="p@example.com", line1="1 Main",
                          city="Portland", state="OR", postal_code="97201", country="US"),
        mode=ModeIn(),
        debug_checkout_only=debug,
    )


@pytest.mark.parametrize("delivery, expected", [("delivered", "succeeded"),
                                                ("verify_delayed", "paid_pending_delivery")])
def test_quick_pay_runs_create_then_pay(monkeypatch, delivery, expected):
    calls = []

    async def create_order(payload, key, progress=None):
        calls.append(("create", key))
        if progress:
            progress("reissuing")
            progress("reissued")
            progress("browser_decode")
            progress("browser_decoded")
            progress("creating_checkout")
            progress("checkout_created")
        return SimpleNamespace(id="order-1", purchase_attempt_id="pa_1", amount="982.14", currency="PHP")

    async def bind(order_id, payload):
        calls.append(("bind", order_id, payload.expected_amount, payload.expected_currency))
        return {"state": "paid", "payment_status": "succeeded", "delivery_status": delivery,
                "code": None, "message": "ok"}

    monkeypatch.setattr(order_flow.orders, "_create_order", create_order)
    monkeypatch.setattr(order_flow.protocol_cards, "bind_and_pay_protocol", bind)
    job, _ = order_flow._reserve_job(_payload(), "quick-key", "test-owner")
    result = asyncio.run(order_flow.run_quick_pay(job.id, _payload(), "quick-key"))
    assert result.status == expected
    assert [c[0] for c in calls] == ["create", "bind"]
    assert calls[1][1] == "order-1"
    assert calls[1][2:] == ("982.14", "PHP")
    assert "create_order_ms" in result.timings
    assert "create_checkout_ms" in result.timings
    assert result.timings.get("total_ms", 0) >= 0


def test_payment_does_not_start_when_checkpoint_write_fails(monkeypatch):
    import sqlite3
    from unittest.mock import AsyncMock
    from gss_api.persistence import get_db_optional

    order = SimpleNamespace(id="order-db-fault", purchase_attempt_id="pa_db", amount="20", currency="USD")
    monkeypatch.setattr(order_flow.orders, "_create_order", AsyncMock(return_value=order))
    bind = AsyncMock()
    monkeypatch.setattr(order_flow.protocol_cards, "bind_and_pay_protocol", bind)
    db = get_db_optional()
    save = db.save_job

    def fail_checkpoint(job):
        if job.stage == "confirming_payment":
            raise sqlite3.OperationalError("checkpoint failed")
        save(job)

    monkeypatch.setattr(db, "save_job", fail_checkpoint)
    job, _ = order_flow._reserve_job(_payload(), "db-fault", "test-owner")
    asyncio.run(order_flow.run_quick_pay(job.id, _payload(), "db-fault"))
    bind.assert_not_called()
    assert db.get_quick_pay_by_id(job.id)["failure_code"] == "payment_state_unavailable"


def test_quick_pay_card_is_not_stored_on_job(monkeypatch):
    async def create_order(payload, key, progress=None):
        return SimpleNamespace(id="order-2", purchase_attempt_id="pa_2", amount="982.14", currency="PHP")

    async def bind(order_id, payload):
        return {"state": "card_declined", "payment_status": "failed",
                "code": "card_declined", "message": "declined"}

    monkeypatch.setattr(order_flow.orders, "_create_order", create_order)
    monkeypatch.setattr(order_flow.protocol_cards, "bind_and_pay_protocol", bind)
    job, _ = order_flow._reserve_job(_payload(), "quick-key-2", "test-owner")
    asyncio.run(order_flow.run_quick_pay(job.id, _payload(), "quick-key-2"))
    assert job.status == "failed"
    assert job.failure_code == "card_declined"
    assert "4242424242424242" not in repr(job)  # release-check: ignore


def test_debug_checkout_only_never_calls_bind(monkeypatch):
    calls = []

    async def create_order(payload, key, progress=None):
        return SimpleNamespace(id="order-3", purchase_attempt_id="pa_3", amount="982.14", currency="PHP")

    async def bind(order_id, payload):
        calls.append("bind")
        return {}

    monkeypatch.setattr(order_flow.orders, "_create_order", create_order)
    monkeypatch.setattr(order_flow.protocol_cards, "bind_and_pay_protocol", bind)
    job, _ = order_flow._reserve_job(_payload(debug=True), "quick-key-3", "test-owner")
    result = asyncio.run(order_flow.run_quick_pay(job.id, _payload(debug=True), "quick-key-3"))
    assert result.status == "succeeded"
    assert result.stage == "checkout_created"
    assert calls == []


def test_quick_pay_requires_idempotency_key():
    from fastapi.testclient import TestClient
    from gss_api.main import create_app
    client = TestClient(create_app())
    response = client.post("/api/payments/quick-pay", json={
        "plan": "plus", "session": "s", "billing_country": "US", "exit_country": "PH",
        "billing": {"name": "P", "email": "p@example.com", "line1": "1 Main",
                    "city": "Portland", "state": "OR", "postal_code": "97201", "country": "US"},
        "card": {"number": "4242424242424242", "cvc": "123", "exp_month": "11", "exp_year": "2028"},  # release-check: ignore
    })
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "idempotency_conflict"


def test_quick_pay_omitted_billing_country_is_not_forwarded_as_null(monkeypatch):
    seen = {}

    async def create_order(payload, key, progress=None):
        seen["fields_set"] = set(payload.model_fields_set)
        seen["exit_country"] = payload.exit_country
        seen["billing_country"] = payload.billing_country
        return SimpleNamespace(id="order-omit", purchase_attempt_id="pa_omit", amount="982.14", currency="PHP")

    monkeypatch.setattr(order_flow.orders, "_create_order", create_order)
    req = QuickPayRequest(
        plan="plus",
        exit_country="PH",
        session="session-fixture",
        mode=ModeIn(),
        debug_checkout_only=True,
    )
    job, _ = order_flow._reserve_job(req, "omit-billing-key", "test-owner")
    result = asyncio.run(order_flow.run_quick_pay(job.id, req, "omit-billing-key"))
    assert result.status == "succeeded"
    assert "billing_country" not in seen["fields_set"]
    assert seen["billing_country"] is None
    assert seen["exit_country"] == "PH"


@pytest.mark.parametrize("plan_name", ["go", "pro_lite", "pro", "pro_max"])
def test_quick_pay_non_plus_plan_routes_to_pay_order_protocol(monkeypatch, plan_name):
    calls = []

    async def create_order(payload, key, progress=None):
        calls.append(("create", payload.plan))
        return SimpleNamespace(id=f"order-{plan_name}", purchase_attempt_id=f"pa-{plan_name}",
                               amount="20.00", currency="USD")

    async def bind_should_not_run(order_id, payload):
        calls.append(("bind_plus", order_id))
        raise AssertionError("non-Plus plan must not call Plus-only bind_and_pay_protocol")

    async def pay_protocol(order_id, payload):
        calls.append(("pay_protocol", order_id, payload.billing.country))
        return order_flow.orders.ProtocolPayOut(
            state="paid",
            payment_status="succeeded",
            delivery_status="delivered",
            message="ok",
        )

    monkeypatch.setattr(order_flow.orders, "_create_order", create_order)
    monkeypatch.setattr(order_flow.protocol_cards, "bind_and_pay_protocol", bind_should_not_run)
    monkeypatch.setattr(order_flow.orders, "pay_order_protocol", pay_protocol)

    base = _payload(debug=False)
    req = QuickPayRequest(
        plan=plan_name,
        exit_country="PH",
        session=base.session,
        card=base.card,
        billing=base.billing,
        mode=base.mode,
        debug_checkout_only=False,
    )
    job, _ = order_flow._reserve_job(req, f"key-{plan_name}", "test-owner")
    result = asyncio.run(order_flow.run_quick_pay(job.id, req, f"key-{plan_name}"))
    assert result.status == "succeeded"
    assert calls == [("create", plan_name), ("pay_protocol", f"order-{plan_name}", "US")]


def test_quick_pay_challenge_required_transitions_to_requires_action(monkeypatch):
    async def create_order(payload, key, progress=None):
        return SimpleNamespace(id="order-3ds", purchase_attempt_id="pa-3ds", amount="20.00", currency="USD")

    async def bind_challenge(order_id, payload):
        return {
            "state": "challenge_required",
            "payment_status": "processing",
            "code": None,
            "message": "需要完成验证",
            "next_action": {"type": "use_stripe_sdk"},
        }

    monkeypatch.setattr(order_flow.orders, "_create_order", create_order)
    monkeypatch.setattr(order_flow.protocol_cards, "bind_and_pay_protocol", bind_challenge)

    req = _payload(debug=False)
    job, _ = order_flow._reserve_job(req, "challenge-key", "test-owner")
    result = asyncio.run(order_flow.run_quick_pay(job.id, req, "challenge-key"))
    assert result.status == "requires_action"
    assert result.stage == "requires_action"
    assert result.lock_id is None


def test_orders_table_persists_exit_country_on_upsert():
    from gss_api.persistence import get_db_optional
    from gss_api.store import OrderView
    from gss_contracts import Mode, Order, OrderStatus, Plan

    db = get_db_optional()
    order = Order(
        idempotency_key="exit-persist-key",
        purchase_attempt_id="pa_exit_persist",
        plan=Plan.PLUS,
        billing_country="US",
        mode=Mode(),
        status=OrderStatus.CREATED,
    )
    view = OrderView()
    db.save_order(order, view)

    # Simulate remember() updating view.exit_country after initial insert.
    view.exit_country = "PH"
    order.status = OrderStatus.AWAITING_PAYMENT
    db.save_order(order, view)

    rows, total = db.list_orders_page(limit=10)
    assert total == 1
    assert rows[0]["exit_country"] == "PH"

    reloaded = db.get_order_by_id(order.id)
    assert reloaded is not None
    assert reloaded[1].exit_country == "PH"
