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
        **({} if debug else {"card": CardIn(number="4242424242424242", cvc="123", exp_month="11", exp_year="2028")}),
        billing=BillingIn(name="Payer", email="p@example.com", line1="1 Main",
                          city="Portland", state="OR", postal_code="97201", country="US"),
        mode=ModeIn(),
        debug_checkout_only=debug,
    )


def test_quick_pay_runs_create_then_pay(monkeypatch):
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
        return {"state": "paid", "payment_status": "succeeded", "code": None, "message": "ok"}

    monkeypatch.setattr(order_flow.orders, "_create_order", create_order)
    monkeypatch.setattr(order_flow.protocol_cards, "bind_and_pay_protocol", bind)
    job, _ = order_flow._reserve_job(_payload(), "quick-key", "test-owner")
    result = asyncio.run(order_flow.run_quick_pay(job.id, _payload(), "quick-key"))
    assert result.status == "succeeded"
    assert [c[0] for c in calls] == ["create", "bind"]
    assert calls[1][1] == "order-1"
    assert calls[1][2:] == ("982.14", "PHP")
    assert "create_order_ms" in result.timings
    assert "create_checkout_ms" in result.timings
    assert result.timings.get("total_ms", 0) >= 0


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
    assert "4242424242424242" not in repr(job)


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
        "card": {"number": "4242424242424242", "cvc": "123", "exp_month": "11", "exp_year": "2028"},
    })
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "idempotency_conflict"
