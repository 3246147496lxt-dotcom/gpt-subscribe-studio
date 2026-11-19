"""Legacy saved-card writes fail closed. Historical status/receipts remain queryable offline."""
import asyncio
import json

import httpx
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.store import store
from gss_contracts import OrderStatus, SubscriptionInfo
from gss_core.vault import vault


@pytest.fixture
def flow(monkeypatch, verified_payer):
    verified_payer()
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    store.clear()
    api = TestClient(create_app())
    created = api.post("/api/orders", json={"plan": "plus", "billing_country": "US", "session": "fixture-session"})
    assert created.status_code == 201, created.text
    oid = created.json()["id"]
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    state = {
        "account": "account_fixture", "key": "pk_test_saved", "setup_status": "requires_payment_method",
        "intent_id": "seti_fixture", "customer": "cus_fixture", "method_customer": "cus_fixture",
        "method_id": "pm_fixture", "visible": True, "closed": 0, "calls": [],
    }

    class Client:
        timeout = 30.0
        def __init__(self):
            self.account_id = state["account"]
            self.access_token = "fixture-original-token"
            self.session_token = "fixture-session-cookie"
            self.device_id = "fixture-device"
            self.oai_session_id = "fixture-session-id"
            self.extra_cookies = {"fixture_cookie": "fixture_value"}
            self.http = self
        def refresh_session(self):
            state["calls"].append("refresh_session")
            if state.get("renew_session"):
                self.access_token = "refreshed-fixture-token"
                return True
            return False
        def hydrate(self):
            pass
        def prepare_setup_intent(self):
            pytest.fail("disabled saved-card route called financial provider")
        def retrieve_stripe_intent(self, **kwargs):
            state["calls"].append("retrieve")
            assert kwargs["intent_id"] == state.get("expected_setup_id", "seti_fixture")
            return {"id": state["intent_id"], "status": state["setup_status"], "customer": state["customer"], "payment_method": state["method_id"]}
        def _route_headers(self, route):
            return {}
        def get(self, url, **kwargs):
            state["calls"].append("list")
            assert url.endswith("/backend-api/payments/payment_methods")
            assert kwargs["params"] == {"account_id": "account_fixture"}
            methods = [{"id": "pm_fixture", "type": state.get("method_type", "card"), "customer": state["method_customer"], "card": {"brand": "visa", "last4": "4242"}}] if state["visible"] else []
            return SimpleNamespace(status_code=200, json=lambda: {"data": methods})
        def create_checkout(self, **kwargs):
            state["calls"].append(("checkout", kwargs))
            pytest.fail("disabled saved-card route created a checkout")
        def subscription_payload(self):
            assert self.access_token == "refreshed-fixture-token"
            assert self.account_id == "account_fixture"
            state["calls"].append("real_adapter_subscription_payload")
            return {"plan_type": "plus", "status": "active"}
        def close(self):
            state["closed"] += 1

    monkeypatch.setattr("gss_api.routers.saved_cards._client", lambda secrets: Client())
    def from_raw(raw, proxy=None):
        instance = Client()
        try:
            payload = json.loads(raw)
            instance.access_token = payload["accessToken"]
            if instance.access_token == "refreshed-fixture-token":
                instance.account_id = "account_fixture"
        except (ValueError, KeyError):
            pass
        return instance
    monkeypatch.setattr("gss_api.routers.orders.ChatGPTClient.from_raw_session", from_raw)
    async def verify(**kwargs):
        parsed = json.loads(kwargs["session"])
        assert parsed["accessToken"] == state.get("expected_verification_token", "fixture-original-token")
        state["verified_session"] = parsed
        state["calls"].append("verify_subscription")
        return SubscriptionInfo(active=bool(state.get("verify_active")), channel="fixture")
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline", lambda: SimpleNamespace(verify=verify))
    class NoRealPayment:
        def __init__(self, **kwargs):
            raise AssertionError("offline test attempted to construct a real payment client")
    monkeypatch.setattr("gss_api.routers.orders.PlusProtocol", NoRealPayment)
    root = f"/api/orders/{oid}/saved-card"
    yield api, oid, root, state
    vault.drop(oid)
    store.clear()


def historical_setup(flow):
    """In-process records created before the single-payer policy took effect."""
    secrets = vault.get(flow[1])
    secrets.saved_card_context = {
        "account_id": "account_fixture", "publishable_key": "pk_test_saved",
        "setup_intent_id": "seti_fixture", "client_secret": "seti_fixture_secret_fixture",
        "prepare_started": True, "state": "requires_payment_method",
    }
    return secrets.saved_card_context


def saved(flow):
    historical_setup(flow)
    flow[3]["setup_status"] = "succeeded"


def historical_ready(flow):
    saved(flow)
    vault.get(flow[1]).saved_card_context.update(
        state="checkout_ready", payment_method_id="pm_fixture", card={"brand": "visa", "last4": "4242"},
        refreshed_checkout_id=store.view(flow[1]).checkout_session_id, customer_id="cus_fixture",
    )


def test_initial_status_does_not_create_setup(flow):
    api, _, root, state = flow
    assert api.get(root).json() == {"state": "not_started"}
    assert state["calls"] == []


@pytest.mark.parametrize("cached", [False, True])
def test_prepare_never_issues_or_releases_unscoped_secret(flow, cached):
    api, oid, root, state = flow
    if cached:
        historical_setup(flow)
    for _ in range(2):
        response = api.post(root + "/prepare")
        assert response.status_code == 409
        assert "seti_fixture_secret" not in response.text
    assert not state["calls"]
    assert bool(vault.get(oid).saved_card_context) == cached


@pytest.mark.parametrize("status", ["requires_payment_method", "requires_action", "requires_confirmation", "processing", "canceled"])
def test_incomplete_setup_never_refreshes_checkout(flow, status):
    historical_setup(flow)
    api, oid, root, state = flow
    old_url = store.view(oid).checkout_url
    state["setup_status"] = status
    assert api.get(root).json()["state"] == status
    assert api.post(root + "/refresh-checkout").status_code == 409
    assert store.view(oid).checkout_url == old_url
    assert not any(isinstance(call, tuple) for call in state["calls"])
    assert store.get_order(oid).status is OrderStatus.AWAITING_PAYMENT


def test_browser_success_flag_cannot_override_server_state(flow):
    historical_setup(flow)
    api, _, root, _ = flow
    result = api.post(root + "/refresh-checkout", json={"paid": True, "setup_status": "succeeded", "payment_method_id": "pm_other"})
    assert result.status_code == 409


def test_succeeded_setup_must_be_visible_on_same_account(flow):
    saved(flow)
    api, _, root, state = flow
    state["visible"] = False
    assert api.get(root).json()["state"] == "sync_pending"
    assert api.post(root + "/refresh-checkout").status_code == 409
    state["visible"] = True
    response = api.get(root)
    assert response.json()["state"] == "saved"
    assert response.json()["card"] == {"brand": "visa", "last4": "4242"}
    assert "secret" not in response.text
    assert "cus_fixture" not in response.text


@pytest.mark.parametrize("field,value", [("account", "other_account"), ("customer", ""), ("method_customer", "cus_other"), ("intent_id", "seti_other")])
def test_mismatched_context_never_counts_as_saved(flow, field, value):
    saved(flow)
    api, _, root, state = flow
    state[field] = value
    assert api.get(root).status_code == 400
    assert api.post(root + "/refresh-checkout").status_code == 409
    assert not any(isinstance(call, tuple) for call in state["calls"])


def test_reading_completed_refresh_returns_original_order_without_writes(flow):
    historical_ready(flow)
    api, oid, root, state = flow
    first = api.post(root + "/refresh-checkout")
    assert first.status_code == 200, first.text
    assert first.json()["order"]["id"] == oid
    assert store.view(oid).checkout_url and first.json()["order"]["checkout_url"] is None
    assert first.json()["order"]["status"] == "awaiting_payment"
    assert "payment_method_id" not in first.json() and first.json()["card"] == {"brand": "visa", "last4": "4242"}
    assert api.post(root + "/refresh-checkout").json() == first.json()
    assert not state["calls"]
    assert api.get(root).json()["state"] == "checkout_ready"
    assert vault.get(oid).session == "fixture-session"


@pytest.mark.parametrize("partial_cache", [False, True])
def test_new_saved_refresh_cannot_use_opaque_method_or_replace_order(flow, partial_cache):
    saved(flow)
    api, oid, root, state = flow
    assert api.get(root).json()["state"] == "saved"  # same account != verified billing
    context = vault.get(oid).saved_card_context
    if partial_cache:
        context["refreshed_checkout_id"] = "different_checkout"
    old_url, old_amount = store.view(oid).checkout_url, store.view(oid).amount
    before = list(state["calls"])
    for _ in range(2):
        response = api.post(root + "/refresh-checkout", json={"billing": store.view(oid).billing})
        assert response.status_code == 409
    assert store.view(oid).checkout_url == old_url and store.view(oid).amount == old_amount
    assert context["state"] == "saved"
    assert state["calls"] == before


def test_historical_prepare_unknown_is_not_replayed(flow):
    api, oid, root, state = flow
    vault.get(oid).saved_card_context = {"prepare_started": True}
    assert api.post(root + "/prepare").status_code == 409
    assert api.get(root).json()["state"] == "prepare_unknown"
    assert not state["calls"]


def test_payment_submission_waits_for_setup_resolution(flow):
    historical_setup(flow)
    api, oid, _, _ = flow
    body = {"payment_method_id": "pm_fixture", "billing": dict(store.view(oid).billing)}
    assert api.post(f"/api/orders/{oid}/pay-protocol", json=body).status_code == 409
    assert api.post(f"/api/orders/{oid}/pay", json={"payment_method_token": "pm_fixture"}).status_code == 409


def test_expired_order_cannot_create_setup(flow):
    api, oid, root, state = flow
    vault.drop(oid)
    assert api.post(root + "/prepare").status_code == 401
    assert state["calls"] == []


def test_non_card_method_is_not_exposed_as_saved_card(flow):
    saved(flow)
    api, _, root, state = flow
    state["method_type"] = "us_bank_account"
    assert api.get(root).status_code == 400
    assert api.post(root + "/refresh-checkout").status_code == 409


def test_canceled_setup_cannot_open_a_new_unscoped_setup(flow):
    historical_setup(flow)
    api, _, root, state = flow
    state["setup_status"] = "canceled"
    assert api.get(root).json()["state"] == "canceled"
    before = list(state["calls"])
    assert api.post(root + "/prepare").status_code == 409
    assert state["calls"] == before


def test_setup_status_still_rechecks_account_on_session_renewal(flow, monkeypatch):
    saved(flow)
    api, _, root, state = flow
    state["account"] = "expired_account_snapshot"
    state["renew_session"] = True
    monkeypatch.setattr("gss_core.saved_cards.jwt_account_id", lambda token: "account_fixture")
    assert api.get(root).json()["state"] == "saved"
    assert state["calls"].index("refresh_session") < state["calls"].index("retrieve")
    before = list(state["calls"])
    assert api.post(root + "/refresh-checkout").status_code == 409
    assert state["calls"] == before


def test_verify_during_setup_preserves_awaiting_payment(flow):
    historical_setup(flow)
    api, oid, root, state = flow
    state["setup_status"] = "requires_action"
    assert api.get(root).json()["state"] == "requires_action"
    assert api.post(f"/api/orders/{oid}/verify?paid=true").status_code == 409
    assert store.get_order(oid).status is OrderStatus.AWAITING_PAYMENT
    assert api.get(root).status_code == 200
    assert "verify_subscription" not in state["calls"]


@pytest.mark.parametrize("paid", [False, True])
def test_verify_before_official_payment_never_closes_refreshed_checkout(flow, paid):
    historical_ready(flow)
    api, oid, root, _ = flow
    assert api.post(root + "/refresh-checkout").status_code == 200
    response = api.post(f"/api/orders/{oid}/verify?paid={str(paid).lower()}")
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_payment"
    assert response.json()["subscription"]["active"] is False
    assert api.get(root).json()["state"] == "checkout_ready"
    assert vault.get(oid) is not None


def test_account_activation_alone_does_not_deliver_saved_checkout(flow):
    historical_ready(flow)
    api, oid, root, state = flow
    assert api.post(root + "/refresh-checkout").status_code == 200
    state["verify_active"] = True
    result = api.post(f"/api/orders/{oid}/verify")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "unknown"
    assert result.json()["subscription"] is None  # do not project an unrelated plan as delivery
    assert result.json()["payment_status"] != "succeeded"
    assert vault.get(oid) is not None
    assert api.post(f"/api/orders/{oid}/verify").json()["status"] == "unknown"
    assert state["calls"].count("verify_subscription") == 1


def test_historical_refresh_and_verify_serialize_and_never_create_checkout(flow):
    from gss_api.routers.orders import _team_locks
    historical_ready(flow)
    api, oid, root, state = flow
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://testserver") as client:
            lock = _team_locks.setdefault(oid, asyncio.Lock())
            async with lock:
                refresh = asyncio.create_task(client.post(root + "/refresh-checkout"))
                verify = asyncio.create_task(client.post(f"/api/orders/{oid}/verify"))
                await asyncio.sleep(0)
                assert not verify.done() and not refresh.done()
            refreshed, checked = await asyncio.gather(refresh, verify)
            assert refreshed.status_code == checked.status_code == 200
            assert checked.json()["status"] == "awaiting_payment"
    asyncio.run(run())
    assert store.get_order(oid).status is OrderStatus.AWAITING_PAYMENT
    assert vault.get(oid).saved_card_context["state"] == "checkout_ready"
    assert not any(isinstance(call, tuple) for call in state["calls"])


def test_final_verify_uses_renewed_session_and_preserves_account_context(flow, monkeypatch):
    historical_ready(flow)
    api, oid, root, state = flow
    assert api.post(root + "/refresh-checkout").status_code == 200
    state["account"] = "expired_account_snapshot"
    state["renew_session"] = True
    state["expected_verification_token"] = "refreshed-fixture-token"
    state["verify_active"] = True
    monkeypatch.setattr("gss_core.saved_cards.jwt_account_id", lambda token: "account_fixture")
    checked = api.post(f"/api/orders/{oid}/verify")
    assert checked.status_code == 200, checked.text
    assert checked.json()["status"] == "unknown"  # correct account, but no receipt for this order
    assert state["verified_session"]["accessToken"] == "refreshed-fixture-token"
    assert state["verified_session"]["cookies"] == {"fixture_cookie": "fixture_value"}


def test_final_verify_rejects_changed_account_before_subscription_lookup(flow):
    historical_ready(flow)
    api, oid, root, state = flow
    assert api.post(root + "/refresh-checkout").status_code == 200
    state["account"] = "other_account"
    response = api.post(f"/api/orders/{oid}/verify?paid=true")
    assert response.status_code == 400
    assert "verify_subscription" not in state["calls"]
    assert store.get_order(oid).status is OrderStatus.AWAITING_PAYMENT


def test_final_verify_real_account_adapter_receives_refreshed_credentials(flow, monkeypatch):
    from gss_account.adapter import UpstreamAccountAdapter
    historical_ready(flow)
    api, oid, root, state = flow
    assert api.post(root + "/refresh-checkout").status_code == 200
    state["account"] = "expired_account_snapshot"
    state["renew_session"] = True
    monkeypatch.setattr("gss_core.saved_cards.jwt_account_id", lambda token: "account_fixture")
    async def real_verify(*, session):
        return await UpstreamAccountAdapter().subscription_status(session)
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline", lambda: SimpleNamespace(verify=real_verify))
    response = api.post(f"/api/orders/{oid}/verify")
    assert response.status_code == 200, response.text
    assert response.json()["subscription"] is None
    assert response.json()["status"] == "unknown"  # account status is not an order receipt
    assert state["calls"].count("real_adapter_subscription_payload") == 1
