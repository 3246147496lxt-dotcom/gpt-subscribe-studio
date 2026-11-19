"""Team prepare -> pm -> confirm -> challenge -> server GET poll, without network."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.store import store
from gss_core.vault import vault
from gss_contracts import OrderStatus, PaymentResult, PaymentStatus, SubscriptionInfo

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/team_hosted_success.json").read_text())
BILLING = {"name": "Fixture", "line1": "Fixture Street", "city": "Fixture City", "state": "OR", "postal_code": "00000", "country": "US"}


@pytest.fixture
def setup_team(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    store.clear()
    client = TestClient(create_app())
    r = client.post("/api/orders", json={"plan": "team", "billing_country": "EG", "seat_quantity": 2,
                                       "session": "fixture-session"})
    assert r.status_code == 201
    order_id = r.json()["id"]
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    calls = []
    class FakeClient:
        def close(self): pass
    class Hosted:
        def __init__(self, client): pass
        def prepare(self, **kw):
            calls.append(("prepare", kw))
            return {"context_id": "fixture-context", "session_id": kw["session_id"], "publishable_key": kw["key"],
                    "amount_minor": 115000, "currency": "egp", "eid": "NA", "billing": kw["billing"],
                    "prepared_at": time.time(), "checkout_config_id": "fixture-config-updated",
                    "elements_session_id": "elements_fixture", "client_session_id": "client_fixture",
                    "guid": "fixture-guid", "muid": "fixture-muid", "sid": "fixture-sid", "passive_captcha": {}}
        def poll(self, **kw):
            calls.append(("GET-poll", kw))
            return {**FIXTURE["poll"]["record"]["response"]["fields"], "payment_status": "paid"}
    monkeypatch.setattr("gss_api.routers.orders._team_client", lambda *a: FakeClient())
    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", Hosted)
    return client, order_id, calls


def test_prepare_requotes_before_pm_and_keeps_egp(setup_team):
    client, order_id, calls = setup_team
    r = client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING, "browser_timezone": "UTC"})
    assert r.status_code == 200, r.text
    assert r.json()["order"]["currency"] == "EGP"
    assert r.json()["order"]["amount"] == "1150"
    assert r.json()["order"]["billing_country"] == "EG"
    assert r.json()["context"]["checkout_config_id"] == "fixture-config-updated"
    assert "billing" not in r.json()["context"]
    assert calls[0][1]["billing"]["country"] == "EG"
    assert store.get_order(order_id).status is OrderStatus.AWAITING_PAYMENT


def test_verified_team_entitlement_delivers_and_clears_secrets(setup_team, monkeypatch):
    client, order_id, calls = setup_team
    verify = AsyncMock(return_value=SubscriptionInfo(active=True, raw_kind="team"))
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline", lambda: SimpleNamespace(verify=verify))
    store.update_status(order_id, OrderStatus.REQUIRES_ACTION)
    r = client.post(f"/api/orders/{order_id}/team/poll")
    assert r.status_code == 200, r.text
    assert r.json()["payment_status"] == "succeeded"
    assert client.post(f"/api/orders/{order_id}/verify").json()["status"] == "delivered"
    assert verify.await_count > 0
    assert vault.get(order_id) is None
    assert calls[0][0] == "GET-poll"
    # Duplicate poll after delivery must not require secrets or issue another upstream call.
    assert client.post(f"/api/orders/{order_id}/team/poll").json()["status"] == "delivered"
    assert len(calls) == 1


def test_client_paid_flag_never_overrides_team_poll(setup_team, monkeypatch):
    client, order_id, calls = setup_team
    class Pending:
        def __init__(self, client): pass
        def poll(self, **kw): return {"state": "active", "payment_object_status": "processing"}
    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", Pending)
    r = client.post(f"/api/orders/{order_id}/verify?paid=true")
    assert r.status_code == 200
    assert r.json()["status"] != "delivered"


def test_missing_context_cannot_submit_confirm(setup_team):
    client, order_id, _ = setup_team
    r = client.post(f"/api/orders/{order_id}/pay", json={"payment_method_token": "pm_fixture", "billing": BILLING})
    assert r.status_code == 409
    assert store.get_order(order_id).status is OrderStatus.AWAITING_PAYMENT


def test_mutated_billing_requires_prepare_again(setup_team):
    client, order_id, _ = setup_team
    client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING})
    r = client.post(f"/api/orders/{order_id}/pay", json={"payment_method_token": "pm_fixture", "team_context_id": "fixture-context",
                                                     "billing": {**BILLING, "country": "EG", "city": "Changed"}})
    assert r.status_code == 409


def test_duplicate_confirm_is_rejected_after_submission(setup_team):
    client, order_id, _ = setup_team
    store.update_status(order_id, OrderStatus.REQUIRES_ACTION)
    r = client.post(f"/api/orders/{order_id}/pay", json={"payment_method_token": "pm_fixture"})
    assert r.status_code == 409


def test_team_successful_confirm_is_idempotent_after_entitlement_verification(setup_team, monkeypatch):
    client, order_id, _ = setup_team
    client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING})
    confirms = []
    class Payment:
        async def pay(self, request):
            assert request.team_context["checkout_config_id"] == "fixture-config-updated"
            assert request.passive_captcha_token == "fixture-captcha"
            confirms.append(request)
            return PaymentResult(status=PaymentStatus.SUCCEEDED, provider_ref="cs_test_fixture")
    class Account:
        async def subscription_status(self, session):
            return SubscriptionInfo(active=True, raw_kind="team")
    from gss_orchestrator.pipeline import OrderPipeline, PipelineDeps
    pipeline = OrderPipeline(PipelineDeps(checkout=None, payment=Payment(), account=Account()))
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline", lambda: pipeline)
    body = {"payment_method_token": "pm_fixture", "team_context_id": "fixture-context", "billing": BILLING,
            "passive_captcha_token": "fixture-captcha"}
    r = client.post(f"/api/orders/{order_id}/pay", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["payment_status"] == "succeeded"
    assert client.post(f"/api/orders/{order_id}/verify").json()["status"] == "delivered"
    assert client.post(f"/api/orders/{order_id}/pay", json=body).json()["status"] == "delivered"
    assert len(confirms) == 1


def test_team_challenge_then_pending_poll_preserves_secret(setup_team, monkeypatch):
    client, order_id, _ = setup_team
    view = store.view(order_id)
    view.next_action = {"type": "use_stripe_sdk", "client_secret": "pi_fixture_secret_fixture"}
    store.update_status(order_id, OrderStatus.REQUIRES_ACTION)
    class Pending:
        def __init__(self, client): pass
        def poll(self, **kw): return {"payment_object_status": "requires_action", "state": "active"}
    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", Pending)
    r = client.post(f"/api/orders/{order_id}/team/poll")
    assert r.status_code == 200
    assert r.json()["status"] == "requires_action"
    assert r.json()["next_action"]["client_secret"] == "pi_fixture_secret_fixture"


def test_quote_failure_keeps_order_awaiting_and_reports_no_payment(setup_team, monkeypatch):
    from gss_contracts.errors import DomainError, ErrorCode
    client, order_id, _ = setup_team
    class FailedQuote:
        def __init__(self, client): pass
        def quote(self, **kw):
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "Stripe 初始化结账失败：订单代理连接超时；本步骤未提交付款确认")
    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", FailedQuote)
    r = client.post(f"/api/orders/{order_id}/team/quote", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "awaiting_payment"
    assert "未提交付款确认" in r.json()["quote_error"]


def test_quote_amount_does_not_require_full_prepare(setup_team, monkeypatch):
    client, order_id, _ = setup_team
    class QuoteOnly:
        def __init__(self, client): pass
        def quote(self, **kw): return {"currency": "egp", "amount_minor": 115000}
    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", QuoteOnly)
    r = client.post(f"/api/orders/{order_id}/team/quote", json={"billing": BILLING})
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == "1150" and r.json()["currency"] == "EGP"
    assert r.json()["status"] == "awaiting_payment"


def test_partial_prepare_quote_survives_later_error(setup_team, monkeypatch):
    from gss_contracts.errors import DomainError, ErrorCode
    client, order_id, _ = setup_team
    class LaterFailure:
        def __init__(self, client): pass
        def prepare(self, on_quote, **kw):
            on_quote({"currency": "egp", "amount_minor": 115000})
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "组件初始化失败，本步骤未提交付款确认")
    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", LaterFailure)
    r = client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING})
    assert r.status_code == 400
    got = client.get(f"/api/orders/{order_id}").json()
    assert got["amount"] == "1150" and got["status"] == "awaiting_payment"
    assert "未提交付款确认" in got["last_error"]


def test_server_side_card_is_tokenized_then_confirmed(setup_team, monkeypatch):
    from gss_orchestrator.pipeline import OrderPipeline, PipelineDeps
    client, order_id, calls = setup_team
    assert client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING}).status_code == 200
    tokenized = []

    class Hosted:
        def __init__(self, client): pass
        def tokenize(self, **kw):
            tokenized.append(kw)
            return "pm_server_fixture"

    class Payment:
        async def pay(self, request):
            assert request.payment_method_token == "pm_server_fixture"
            return PaymentResult(status=PaymentStatus.SUCCEEDED, provider_ref="cs_test_fixture")

    class Account:
        async def subscription_status(self, session):
            return SubscriptionInfo(active=True, raw_kind="team")

    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", Hosted)
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline",
                        lambda: OrderPipeline(PipelineDeps(checkout=None, payment=Payment(), account=Account())))
    body = {"billing": BILLING, "team_context_id": "fixture-context",
            "card": {"number": "4242424242424242", "cvc": "123", "exp_month": "12", "exp_year": "2030"}}  # release-check: ignore
    r = client.post(f"/api/orders/{order_id}/pay", json=body)
    assert r.status_code == 200, r.text
    assert tokenized and tokenized[0]["publishable_key"].startswith("pk_")
    assert tokenized[0]["card"]["number"] == "4242424242424242"  # release-check: ignore
    assert "4242424242424242" not in r.text and "123" not in r.text  # release-check: ignore


def test_card_and_payment_method_are_mutually_exclusive(setup_team, monkeypatch):
    client, order_id, _ = setup_team
    client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING})
    body = {"billing": BILLING, "team_context_id": "fixture-context", "payment_method_token": "pm_fixture",
            "card": {"number": "4242424242424242", "cvc": "123", "exp_month": "12", "exp_year": "2030"}}  # release-check: ignore
    assert client.post(f"/api/orders/{order_id}/pay", json=body).status_code == 400


def test_server_card_decline_keeps_order_awaiting_payment(setup_team, monkeypatch):
    from gss_contracts.errors import DomainError, ErrorCode
    client, order_id, _ = setup_team
    client.post(f"/api/orders/{order_id}/team/prepare", json={"billing": BILLING})

    class Hosted:
        def __init__(self, client): pass
        def tokenize(self, **kw):
            raise DomainError(ErrorCode.PAYMENT_DECLINED, "Your card was declined (card_declined)")

    monkeypatch.setattr("gss_api.routers.orders.TeamHostedClient", Hosted)
    body = {"billing": BILLING, "team_context_id": "fixture-context",
            # Public Stripe decline-test PAN, not a real card.
            "card": {"number": "4000000000000002", "cvc": "123", "exp_month": "12", "exp_year": "2030"}}  # release-check: ignore
    r = client.post(f"/api/orders/{order_id}/pay", json=body)
    assert r.status_code >= 400
    assert store.get_order(order_id).status is not OrderStatus.PAYING


@pytest.mark.parametrize("entry", ["pay", "team/poll"])
def test_team_paid_is_not_delivered_without_verified_team_entitlement(setup_team, monkeypatch, entry):
    client, order_id, _ = setup_team
    url = f"/api/orders/{order_id}"
    pay = AsyncMock(return_value=PaymentResult(status=PaymentStatus.SUCCEEDED, provider_ref="pi_fixture"))
    verify = AsyncMock(return_value=SubscriptionInfo(active=True, raw_kind="plus"))
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline",
                        lambda: SimpleNamespace(pay=pay, verify=verify))
    try:
        if entry == "pay":
            assert client.post(f"{url}/team/prepare", json={"billing": BILLING}).status_code == 200
            body = {"payment_method_token": "pm_fixture", "team_context_id": "fixture-context", "billing": BILLING}
            first = client.post(f"{url}/pay", json=body)
        else:
            store.update_status(order_id, OrderStatus.REQUIRES_ACTION)
            first = client.post(f"{url}/team/poll")
        assert first.status_code == 200, first.text

        def assert_waiting(result):
            # An active unrelated subscription is not evidence of a Team entitlement.
            assert result["status"] == "verifying"
            assert result["subscription"] is None
            assert result["plan_type"] == "team"
            assert result["payment_status"] == result["receipt"]["payment_status"] == "succeeded"
            assert result["delivery_status"] == result["receipt"]["delivery_status"] == "verify_delayed"
            assert result["status_summary"] == "已扣款，待核验"
            assert vault.get(order_id) is not None

        assert_waiting(first.json())
        assert_waiting(client.get(url).json())
        assert_waiting(client.post(f"{url}/verify").json())
        assert_waiting(client.post(f"{url}/team/poll").json())
        verify.return_value = SubscriptionInfo(active=True, raw_kind="team")
        last = client.post(f"{url}/verify")
        assert last.status_code == 200, last.text
        assert last.json()["status"] == last.json()["delivery_status"] == "delivered"
        assert last.json()["payment_status"] == "succeeded"
        assert vault.get(order_id) is None
        assert pay.await_count == (1 if entry == "pay" else 0)
    finally:
        vault.drop(order_id)


def test_paid_team_survives_verification_timeout_and_vault_expiry(setup_team, monkeypatch):
    client, order_id, _ = setup_team
    url = f"/api/orders/{order_id}"
    verify = AsyncMock(side_effect=TimeoutError("private-timeout-diagnostic"))
    pay = AsyncMock(side_effect=AssertionError("a status query must not submit another payment"))
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline", lambda: SimpleNamespace(verify=verify, pay=pay))
    try:
        def assert_waiting(response):
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["payment_status"] == body["receipt"]["payment_status"] == "succeeded"
            assert body["status"] != "delivered" and body["delivery_status"] != "delivered"
            assert body["status_summary"] == "已扣款，待核验"
            assert "private-timeout-diagnostic" not in response.text

        assert_waiting(client.post(f"{url}/team/poll"))
        assert_waiting(client.post(f"{url}/verify"))
        assert verify.await_count >= 1
        assert vault.get(order_id) is not None
        vault.drop(order_id)  # mimic idle expiry with the order and receipt still in RAM
        checks = verify.await_count
        assert_waiting(client.post(f"{url}/verify"))
        assert_waiting(client.post(f"{url}/team/poll"))
        assert_waiting(client.get(url))
        assert verify.await_count == checks
        pay.assert_not_awaited()
    finally:
        vault.drop(order_id)
