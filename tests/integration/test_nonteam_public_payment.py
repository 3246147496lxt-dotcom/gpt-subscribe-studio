"""Offline HTTP response projections: receipts are not writable payment capabilities."""
import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.routers import saved_cards
from gss_api.store import store
from gss_contracts import OrderStatus
from gss_contracts.errors import DomainError, ErrorCode
from gss_core.vault import vault


@pytest.fixture
def existing(monkeypatch, verified_us_payer):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    store.clear()
    with TestClient(create_app()) as client:
        created = client.post("/api/orders", json={"plan": "plus", "billing_country": "US", "session": "fixture-session"})
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["checkout_url"] is None and body["customer_session_client_secret"] is None
        oid = body["id"]
        view = store.view(oid)
        view.checkout_url = "https://checkout.example.invalid/checkout-sensitive"
        view.confirm_return_url = view.checkout_url + "?confirm"
        view.customer_session_client_secret = "cuss_fixture_sensitive"
        yield client, f"/api/orders/{oid}", oid, view
        vault.drop(oid)
    store.clear()


AUTH = "https://hooks.stripe.com/redirect/authenticate/src_example"
@pytest.mark.parametrize("url,verified,expected", [
    (AUTH, True, AUTH), (AUTH, False, None),
    (AUTH + "?return_url=https://checkout.example.invalid", True, None),
    ("https://hooks.stripe.com.evil.invalid/redirect/authenticate/src_x", True, None),
    ("https://checkout.example.invalid/checkout-sensitive", True, None),
])
def test_requires_action_is_not_a_generic_checkout_or_reusable_secret(existing, url, verified, expected):
    client, route, oid, view = existing
    store.update_status(oid, OrderStatus.REQUIRES_ACTION)
    view.billing_verified = verified
    view.next_action = {"type": "3ds_redirect", "url": url, "checkout_url": view.checkout_url,
                        "client_secret": "pi_fixture_secret_sensitive", "nested": {"setup_intent": "secret-sensitive"}}
    view.quote_error = f"bad redirect {view.checkout_url}"
    body = client.get(route)
    assert body.status_code == 200 and "sensitive" not in body.text
    assert body.json()["next_action"] == ({"type": "3ds_redirect", "url": expected} if expected else {"type": "3ds_redirect"})
    assert body.json()["status"] == "requires_action"


def test_cached_protocol_and_nested_receipts_are_read_only_after_vault_expiry(existing):
    client, route, oid, view = existing
    store.update_status(oid, OrderStatus.DELIVERED)
    receipt = {"amount":"20", "currency":"USD", "payment_status":"succeeded", "editable_checkout":view.checkout_url}
    view.receipt = receipt
    view.protocol_saved_result = {
        "stage":"payment", "state":"paid", "payment_method_id":"pm_sensitive", "nested": {"url":view.checkout_url},
        "receipt": receipt, "next_action":{"type":"use_stripe_sdk","client_secret":"pi_x_secret_sensitive"},
    }
    vault.drop(oid)
    resp = client.get(route + "/bind-and-pay-protocol")
    assert resp.status_code == 200 and "sensitive" not in resp.text
    body = resp.json()
    assert body["state"] == "paid" and body["order"]["status"] == "delivered"
    assert body["receipt"] == body["order"]["receipt"] == {"amount":"20","currency":"USD","payment_status":"succeeded"}
    assert body["next_action"] == {"type":"use_stripe_sdk"}


def test_old_saved_card_inspection_and_cached_refresh_only_return_masked_record(existing, monkeypatch):
    client, route, oid, view = existing
    record = {"brand":"visa", "last4":"4242", "source":{"pay_url":"sensitive"}}
    secrets = vault.get(oid)
    secrets.saved_card_context = {"setup_intent_id":"seti_fixture", "client_secret":"sensitive",
                                  "refreshed_checkout_id":view.checkout_session_id, "payment_method_id":"pm_sensitive", "card":record}
    monkeypatch.setattr(saved_cards, "_inspect", lambda *_: {"state":"saved", "payment_method_id":"pm_sensitive",
                                                              "card":record, "client_secret":"sensitive"})
    for resp in (client.get(route + "/saved-card"), client.post(route + "/saved-card/refresh-checkout")):
        assert resp.status_code == 200 and "sensitive" not in resp.text
        assert "payment_method_id" not in resp.json() and resp.json()["card"] == {"brand":"visa","last4":"4242"}


def test_historical_read_errors_never_return_a_checkout_capability(existing, monkeypatch):
    client, route, oid, view = existing
    vault.get(oid).saved_card_context = {"client_secret": "private", "setup_intent_id": "seti_fixture"}

    def unreadable(*args):
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, f"original {view.checkout_url}")

    monkeypatch.setattr(saved_cards, "_inspect", unreadable)
    response = client.get(route + "/saved-card")
    assert response.status_code >= 400 and "sensitive" not in response.text


def test_rotating_payer_hides_old_address_and_action_but_not_paid_receipt(existing, verified_payer):
    client, route, oid, view = existing
    original = view.billing.copy()
    verified_payer(billing=original, payer_id="distinct-fixture-subject")
    view.payment_status = "succeeded"
    view.receipt = {"amount": "20", "currency": "USD", "payment_status": "succeeded"}
    response = client.get(route)
    assert response.status_code == 200
    order = response.json()
    assert order["billing_verified"] is False and order["billing"] is None
    assert order["receipt"]["payment_status"] == "succeeded"
    assert original["line1"] not in response.text
