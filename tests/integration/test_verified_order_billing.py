"""Non-Team HTTP boundary: compare verified facts before writes; never repair a claim."""

from dataclasses import replace
import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from gss_api.main import create_app
from gss_api.routers import orders
from gss_api.store import store
from gss_core.vault import vault


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    pipeline = orders.build_pipeline()
    monkeypatch.setattr(orders, "build_pipeline", lambda: pipeline)
    store.clear()
    with TestClient(create_app()) as client:
        yield client, pipeline
    for order_id in store.orders:
        vault.drop(order_id)
    store.clear()


def create(client, **fields):
    return client.post("/api/orders", json={
        "plan": "plus", "billing_country": "US", "session": "fixture-session", **fields,
    })


@pytest.mark.parametrize("override", [
    None, {"billing": None}, {"billing": {}}, {"billing": {"line2": "private-marker"}},
    {"billing": {"name": "private-marker"}}, {"billing": {"line1": "private-marker"}},
    {"billing_address": "private-marker"}, {"billing_verified": True},
    {"mode": {"billing": {"line1": "private-marker"}}},
])
def test_create_rejects_before_side_effects(offline, verified_payer, monkeypatch, override):
    client, pipeline = offline
    profile = verified_payer()
    route = Mock(side_effect=AssertionError("proxy allocated for invalid billing"))
    monkeypatch.setattr(orders, "checkout_proxy_for", route)
    if override is None:
        monkeypatch.delenv("GSS_VERIFIED_BILLING_FILE")
        override, expected = {}, 503
    else:
        expected = 409
        if override.get("billing"):
            override = {"billing": {**profile, **override["billing"]}}
    reply = create(client, **override)
    assert reply.status_code == expected, reply.text
    assert "private-marker" not in reply.text
    assert store.orders == {} and store.jobs == {}
    assert not route.called and not pipeline.deps.checkout.calls and not pipeline.deps.payment.calls


@pytest.mark.parametrize("plan", ["plus", "go", "pro_lite", "pro", "pro_max"])
def test_order_carries_verified_snapshot_and_private_binding(offline, verified_us_payer, plan):
    client, pipeline = offline
    result = create(client, plan=plan, billing=verified_us_payer)
    assert result.status_code == 201, result.text
    order = result.json()
    oid = order["id"]
    assert order["billing_verified"] is True
    assert pipeline.deps.checkout.calls[0].verified_billing == verified_us_payer
    assert vault.get(oid).billing == store.view(oid).billing == order["billing"] == verified_us_payer
    binding = store.view(oid).billing_verification_id
    assert binding and binding not in result.text and "verification_ref" not in result.text


@pytest.mark.parametrize("endpoint", ["pay", "pay-protocol"])
@pytest.mark.parametrize("issue", ["changed_street", "extra_field", "shadowed_card_address", "missing_field", "changed_payer", "legacy", "vault_drift", "country_drift"])
def test_payment_checks_claim_binding_and_both_snapshots_before_writes(
    offline, verified_payer, monkeypatch, endpoint, issue
):
    client, pipeline = offline
    profile = verified_payer()
    result = create(client)
    assert result.status_code == 201, result.text
    oid = result.json()["id"]
    claim = dict(profile)
    view, secrets = store.view(oid), vault.get(oid)
    if issue == "changed_street":
        claim["line1"] = "private-marker"
    elif issue == "extra_field":
        claim["line2"] = "private-marker"
    elif issue == "missing_field":
        claim.pop("line1")
        claim["name"] = "private-marker"
    elif issue == "changed_payer":
        verified_payer(billing=profile, payer_id="different-payer")
    elif issue == "legacy":
        view.billing_verified, view.billing_verification_id = False, None
    elif issue == "vault_drift":
        secrets.billing = {**profile, "postal_code": "other"}
    elif issue == "country_drift":
        store.get_order(oid).billing_country = "JP"
    before_vault = dict(secrets.billing)
    submitted = {"payment_method_token" if endpoint == "pay" else "payment_method_id": "pm_offline", "billing": claim}
    if issue == "shadowed_card_address":
        submitted["card"] = {
            "number": "0"*16, "exp_month": "12", "exp_year": "33", "cvc": "000",
            "billing_details": {"line1": "private-marker"},
        }
    protocol = Mock(side_effect=AssertionError("payment started for invalid billing"))
    monkeypatch.setattr(orders, "PlusProtocol", protocol)
    response = client.post(f"/api/orders/{oid}/{endpoint}", json=submitted)
    assert response.status_code == (422 if issue == "missing_field" else 409), response.text
    assert "private-marker" not in response.text
    assert not pipeline.deps.payment.calls and not protocol.called
    assert store.view(oid).payment_status == "unpaid"
    assert view.billing == profile and secrets.billing == before_vault


@pytest.mark.parametrize("reply_billing", [None, {"line1": "private-marker"}])
def test_mismatched_checkout_echo_not_authorized_for_payment(offline, verified_us_payer, monkeypatch, reply_billing):
    client, pipeline = offline
    original = pipeline.deps.checkout.create_checkout

    async def checkout(req):
        result = await original(req)
        return replace(result, billing=reply_billing)

    monkeypatch.setattr(pipeline.deps.checkout, "create_checkout", checkout)
    response = create(client)
    assert response.status_code == 409
    assert "private-marker" not in response.text
    oid, = store.orders
    state = client.get(f"/api/orders/{oid}").json()
    assert state["status"] == "rejected" and not state["billing_verified"] and state["billing"] is None
    assert not pipeline.deps.payment.calls


def test_validated_address_may_be_confirmed_but_never_substituted(offline, verified_us_payer):
    client, pipeline = offline
    result = create(client)
    assert result.status_code == 201
    oid = result.json()["id"]
    response = client.post(f"/api/orders/{oid}/pay", json={
        "payment_method_token": "offline-verified-instrument", "billing": dict(verified_us_payer),
    })
    assert response.status_code == 200, response.text
    assert len(pipeline.deps.payment.calls) == 1
    assert pipeline.deps.payment.calls[0].billing == verified_us_payer

@pytest.mark.parametrize("endpoint,field", [("pay", "payment_method_token"), ("pay-protocol", "payment_method_id")])
@pytest.mark.parametrize("token", ["pm_external", "ctoken_external"])
def test_external_instrument_refused_even_when_claim_and_fake_flag_match(
    offline, verified_us_payer, endpoint, field, token, monkeypatch
):
    client, pipeline = offline
    oid = create(client).json()["id"]
    protocol = Mock(side_effect=AssertionError("unverified external payment reached a client"))
    monkeypatch.setattr(orders, "PlusProtocol", protocol)
    reply = client.post(f"/api/orders/{oid}/{endpoint}", json={field: token, "billing": verified_us_payer})
    assert reply.status_code == 409 and token not in reply.text
    assert store.view(oid).payment_status == "unpaid"
    assert not protocol.called and not pipeline.deps.payment.calls


@pytest.mark.parametrize("token", ["pm_external", "ctoken_external", "offline-verified-instrument"])
def test_non_simulated_channel_rejects_opaque_on_create_and_pay(
    offline, verified_us_payer, monkeypatch, tmp_path, token
):
    client, pipeline = offline
    # Create an unpaid fixture order before moving to an untrusted channel boundary.
    oid = create(client).json()["id"]
    monkeypatch.setenv("GSS_DATA_DIR", str(tmp_path / "claims"))
    upstream = Mock()
    monkeypatch.setattr(pipeline.deps, "payment", upstream)
    checkout = Mock(side_effect=AssertionError("unverified combined purchase reached provider"))
    monkeypatch.setattr(pipeline, "create_checkout", checkout)
    body = {"billing_country": "US", "plan": "plus", "session": "fixture-session",
            "payment_method_token": token, "billing": dict(verified_us_payer)}
    create_result = client.post("/api/orders", json=body, headers={"Idempotency-Key": "opaque-fixture"})
    pay_result = client.post(f"/api/orders/{oid}/pay", json={
        "payment_method_token": token, "billing": dict(verified_us_payer),
    })
    assert create_result.status_code == pay_result.status_code == 409
    assert not (tmp_path / "claims" / "purchase-claims.sqlite3").exists()
    assert set(store.orders) == {oid} and store.view(oid).payment_status == "unpaid"
    assert not checkout.called and not upstream.mock_calls

def test_simulated_combined_purchase_cannot_reach_session_client(offline, verified_us_payer, monkeypatch):
    from unittest.mock import AsyncMock

    client, pipeline = offline
    guard = AsyncMock()
    monkeypatch.setattr(orders, "_guard_refresh_egress", guard)
    reply = client.post("/api/orders", headers={"Idempotency-Key": "no-refresh"}, json={
        "plan": "plus", "billing_country": "US", "exit_country": "US",
        "payment_method_token": "offline-verified-instrument",
        "session": json.dumps({"sessionToken": "private-session", "accessToken": "private-access",
                               "statsigContext": {"country": "JP"}}),
    })
    assert reply.status_code == 409 and "private-session" not in reply.text
    guard.assert_not_awaited()
    assert not store.orders and not pipeline.deps.checkout.calls



def test_omitted_country_is_loaded_from_attestation_not_a_us_default(offline, verified_payer, monkeypatch, tmp_path):
    monkeypatch.setenv("GSS_DATA_DIR", str(tmp_path))
    client, pipeline = offline
    profile = verified_payer("DE")
    body = {"plan": "plus", "session": "fixture-session"}
    headers = {"Idempotency-Key": "server-chooses-country"}
    created = client.post("/api/orders", json=body, headers=headers)
    assert created.status_code == 201, created.text
    # 账单来自核验记录（DE）；出口没有指定，可以自由落点，不再被账单国绑架。
    assert created.json()["billing_country"] == "DE"
    assert created.json()["billing"] == profile
    request, = pipeline.deps.checkout.calls
    assert request.billing_country == "DE"
    assert request.verified_billing == profile
    # Same request after a lost response: original order readable, no new write.
    monkeypatch.delenv("GSS_VERIFIED_BILLING_FILE")
    replay = client.post("/api/orders", json=body, headers=headers)
    assert replay.status_code == 201 and replay.json()["id"] == created.json()["id"]
    assert replay.json()["billing"] is None and replay.json()["billing_verified"] is False
    assert len(pipeline.deps.checkout.calls) == 1


@pytest.mark.parametrize("fields,expected", [
    ({"billing_country": "private-marker"}, 400), ({"exit_country": "private-marker"}, 400),
    ({"billing_country": None}, 409), ({"billing_country": ""}, 409), ({"exit_country": "  "}, 409),
    ({}, 503),
])
def test_invalid_regions_are_rejected_without_side_effects(offline, verified_payer, fields, expected):
    client, pipeline = offline
    if fields:
        verified_payer("DE")
    response = client.post("/api/orders", json={"plan": "go", "session": "fixture-session", **fields})
    assert response.status_code == expected, response.text
    assert "private-marker" not in response.text
    assert not store.orders and not pipeline.deps.checkout.calls and not pipeline.deps.payment.calls


@pytest.mark.parametrize("fields", [
    {"billing_country": "JP"}, {"exit_country": "JP"},
])
def test_free_exit_never_rewrites_the_fixed_billing(offline, verified_payer, fields):
    client, pipeline = offline
    profile = verified_payer("DE")
    response = client.post("/api/orders", json={"plan": "go", "session": "fixture-session", **fields})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["billing_country"] == "DE" and body["billing"] == profile
    request, = pipeline.deps.checkout.calls
    assert request.billing_country == "DE"
    assert request.verified_billing == profile


@pytest.mark.parametrize("fields", [{}, {"billing_country": None}])
def test_team_still_requires_explicit_region(offline, fields):
    client, pipeline = offline
    result = client.post("/api/orders", json={"plan": "team", "session": "fixture-session", **fields})
    assert result.status_code == 422
    assert not store.orders and not pipeline.deps.checkout.calls


def test_existing_custom_checkout_tax_observation_stops_before_confirmation(offline, verified_us_payer, monkeypatch):
    from types import SimpleNamespace
    from gss_core.plus_protocol import PlusProtocol

    api, pipeline = offline
    oid = create(api).json()["id"]
    view = store.view(oid)
    view.checkout_session_id = "cs_test_fixture"
    names = []
    class Stub(PlusProtocol):
        def __init__(self, **kwargs):
            self.checkout_id, self.publishable_key = "cs_test_fixture", "pk_test_fixture"
            self.checkout_url, self.billing, self.currency, self.timeout = kwargs["checkout_url"], kwargs["billing"], "USD", 1
            self.client = SimpleNamespace(mint_sentinel=lambda **_: None, _route_headers=lambda *a, **kw: {},
                                          http=SimpleNamespace(post=self.post))
        def post(self, url, **kwargs):
            names.append(url)
            return SimpleNamespace(status_code=200, json=lambda: {"billing_address": {"state": "other state"}})
        def _warmup(self):
            self._warm_taxes()
        def _confirm_with_ctoken(self, *args):
            pytest.fail("conflicting tax response reached payment confirmation")
        def close(self):
            pass
    monkeypatch.setattr(orders, "PlusProtocol", Stub)
    response = api.post(f"/api/orders/{oid}/pay-protocol", json={
        "card": {"number": "0"*16, "cvc": "000", "exp_month": "12", "exp_year": "33"},
        "billing": verified_us_payer,
    })
    assert response.status_code == 409 and "other state" not in response.text
    assert len(names) == 1 and names[0].endswith("/checkout/taxes")
    assert store.view(oid).payment_status == "unpaid" and not pipeline.deps.payment.calls
