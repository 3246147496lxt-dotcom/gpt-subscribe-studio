"""独立开通闭环：Fake adapter 下 session + 卡标识 → delivered。"""

from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock, Mock

import pytest

os.environ["GSS_USE_FAKE"] = "1"

from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.store import store
from gss_core.vault import vault


def test_session_and_card_token_deliver() -> None:
    store.clear()
    vault.drop("unused")
    client = TestClient(create_app())

    created = client.post(
        "/api/orders",
        json={
            "plan": "plus",
            "billing_country": "US",
            "session": '{"accessToken":"fake-session"}',
            "mode": {
                "account_source": "customer_session",
                "card_source": "customer",
                "billing_mode": "standard",
            },
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "awaiting_payment"
    assert body["billing"]["country"] == "US"
    assert body["billing"]["line1"]
    assert body["currency"] == "USD"
    assert body["amount"] == "20.00"
    order_id = body["id"]

    paid = client.post(f"/api/orders/{order_id}/pay", json={"payment_method_token": "pm_fake"})
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "delivered"
    assert paid.json()["subscription"]["active"] is True


def test_promo_eligibility_is_read_only_and_returns_campaign() -> None:
    store.clear()
    client = TestClient(create_app())
    response = client.post(
        "/api/promo/eligibility",
        json={"session": '{"accessToken":"fake-session"}'},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plus_eligible"] is True
    assert body["campaigns"][0]["campaign_id"] == "plus-1-month-free"
    assert store.orders == {}


def test_plus_campaign_id_is_carried_into_promo_checkout(monkeypatch) -> None:
    store.clear()
    monkeypatch.setenv("GSS_ENABLE_PROMO", "1")
    client = TestClient(create_app())
    response = client.post(
        "/api/orders",
        json={
            "plan": "plus",
            "billing_country": "US",
            "session": '{"accessToken":"fake-session"}',
            "promo_campaign_id": "plus-3-months-50-pct-off",
            "mode": {
                "account_source": "customer_session",
                "card_source": "customer",
                "billing_mode": "promo",
                "risk_acknowledged": True,
            },
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["promo_campaign_id"] == "plus-3-months-50-pct-off"
    assert body["mode"]["billing_mode"] == "promo"
    assert body["amount"] == "0.00"


def test_team_hosted_checkout_requires_seats_and_returns_link() -> None:
    store.clear()
    client = TestClient(create_app())
    missing = client.post(
        "/api/orders",
        json={
            "plan": "team",
            "billing_country": "JP",
            "session": '{"accessToken":"fake-session"}',
            "mode": {"account_source": "customer_session", "card_source": "customer"},
        },
    )
    assert missing.status_code == 400
    assert "席位" in missing.text

    created = client.post(
        "/api/orders",
        json={
            "plan": "team",
            "billing_country": "JP",
            "session": '{"accessToken":"fake-session"}',
            "seat_quantity": 8,
            "promo_code": "TEAM50",
            "workspace_name": "Desk",
            "mode": {"account_source": "customer_session", "card_source": "customer"},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "awaiting_payment"
    assert body["checkout_kind"] == "hosted"
    assert body["seat_quantity"] == 8
    assert body["currency"] == "JPY"
    assert body["amount"] == "200"
    assert "pay.openai.com/c/pay/" in body["checkout_url"]

    verified = client.post(f"/api/orders/{body['id']}/verify")
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "awaiting_payment"
    assert verified.json()["payment_status"] == "unpaid"


@pytest.mark.parametrize("payment_country,state", [("US", "OR"), ("JP", "Tokyo")])
def test_team_egypt_checkout_aligns_payment_billing_to_order(monkeypatch, payment_country, state) -> None:
    """EG / EGP 创建 Team；支付地址国家被对齐到订单目标国家。"""
    from gss_api.runtime import build_pipeline
    pipeline = build_pipeline()
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline", lambda: pipeline)
    store.clear()
    client = TestClient(create_app())
    created = client.post(
        "/api/orders",
        json={
            "plan": "team", "billing_country": "EG", "session": '{"accessToken":"fake-session"}',
            "seat_quantity": 2, "workspace_name": "Desk",
            "mode": {"account_source": "customer_session", "card_source": "customer"},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["checkout_kind"] == "hosted"
    assert body["publishable_key"] == "pk_test_fake"
    assert body["currency"] == "EGP"
    assert pipeline.deps.checkout.calls[0].billing_country == "EG"
    address = {"name": "Fixture User", "line1": "Fixture Street", "city": "Fixture City",
               "state": state, "postal_code": "00000", "country": payment_country.lower()}
    paid = client.post(f"/api/orders/{body['id']}/pay",
                       json={"payment_method_token": "pm_fake", "billing": address})
    assert paid.status_code == 200, paid.text
    result = paid.json()
    # The generic fake account does not establish a Team entitlement.
    assert result["status"] == "verifying"
    assert result["payment_status"] == "succeeded"
    assert result["billing_country"] == "EG"
    assert result["currency"] == "EGP"
    assert result["billing"]["country"] == "EG"
    request = pipeline.deps.payment.calls[0]
    assert request.country == "EG"
    assert request.billing["country"] == "EG"
    assert request.billing["postal_code"]
    assert request.payment_method_token == "pm_fake"


def test_unknown_region_is_400() -> None:
    store.clear()
    client = TestClient(create_app())
    res = client.post(
        "/api/orders",
        json={
            "plan": "plus",
            "billing_country": "XX",
            "session": '{"accessToken":"fake-session"}',
            "mode": {"account_source": "customer_session", "card_source": "customer"},
        },
    )
    assert res.status_code == 400
    assert "不支持的地区" in res.text


def test_missing_session_is_401() -> None:
    store.clear()
    client = TestClient(create_app())
    res = client.post(
        "/api/orders",
        json={"plan": "plus", "billing_country": "US", "mode": {"account_source": "customer_session"}},
    )
    assert res.status_code == 401


def _create_us_order(client: TestClient) -> str:
    created = client.post(
        "/api/orders",
        json={
            "plan": "plus",
            "billing_country": "US",
            "session": '{"accessToken":"fake-session"}',
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_pay_uses_manual_billing_address() -> None:
    store.clear()
    client = TestClient(create_app())
    order_id = _create_us_order(client)
    manual = {
        "name": "Jane Roe",
        "line1": "1 Main St",
        "city": "Austin",
        "state": "TX",
        "postal_code": "73301",
        "country": "us",
    }

    paid = client.post(
        f"/api/orders/{order_id}/pay",
        json={"payment_method_token": "pm_fake", "billing": manual},
    )

    assert paid.status_code == 200, paid.text
    assert paid.json()["billing"]["line1"] == "1 Main St"
    assert paid.json()["billing"]["country"] == "US"


def test_pay_aligns_billing_to_order_country() -> None:
    store.clear()
    client = TestClient(create_app())
    order_id = _create_us_order(client)

    resp = client.post(
        f"/api/orders/{order_id}/pay",
        json={
            "payment_method_token": "pm_fake",
            "billing": {
                "name": "Jane Roe",
                "line1": "1 Chome",
                "city": "Tokyo",
                "state": "",
                "postal_code": "150-0002",
                "country": "JP",
            },
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "delivered"
    assert resp.json()["billing"]["country"] == "US"
    assert resp.json()["billing_country"] == "US"
    assert resp.json()["currency"] == "USD"


_JWE = (
    base64.urlsafe_b64encode(b'{"alg":"dir","enc":"A256GCM"}').decode().rstrip("=") + "..iv.cipher.tag"
)


def _order_with_session(client: TestClient, session: str):
    return client.post(
        "/api/orders",
        json={
            "plan": "plus",
            "billing_country": "US",
            "session": session,
            "mode": {
                "account_source": "customer_session",
                "card_source": "customer",
                "billing_mode": "standard",
            },
        },
    )


def test_session_token_only_is_exchanged_before_checkout(monkeypatch) -> None:
    from gss_core.chatgpt import ChatGPTClient

    seen = {}

    def fake_refresh(self) -> bool:
        seen["auth"] = "Authorization" in self._identity_headers()
        seen["session_token"] = self.session_token
        self.access_token = "exchanged-at"
        return True

    monkeypatch.setattr("gss_api.routers.orders._guard_refresh_egress", AsyncMock())
    monkeypatch.setattr(ChatGPTClient, "refresh_session", fake_refresh)
    store.clear()
    created = _order_with_session(TestClient(create_app()), _JWE)
    assert created.status_code == 201, created.text
    assert seen == {"auth": False, "session_token": _JWE}
    assert '"accessToken": "exchanged-at"' in vault.get(created.json()["id"]).session


def test_expired_session_token_is_rejected(monkeypatch) -> None:
    from gss_core.chatgpt import ChatGPTClient

    monkeypatch.setattr("gss_api.routers.orders._guard_refresh_egress", AsyncMock())
    monkeypatch.setattr(ChatGPTClient, "refresh_session", lambda self: False)
    store.clear()
    created = _order_with_session(TestClient(create_app()), _JWE)
    assert created.status_code == 401
    assert "session token" in created.json()["detail"]["message"]
    assert store.orders == {}


def test_sse_frames_are_valid_json() -> None:
    import json

    from gss_api.routers.events import _sse

    frame = _sse({"type": "init", "ok": True, "none": None, "msg": "已支付"})
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    assert json.loads(frame[len("data: "):]) == {"type": "init", "ok": True, "none": None, "msg": "已支付"}


def test_checkout_effective_proxy_is_kept_only_in_memory(monkeypatch):
    from dataclasses import replace
    from gss_api.runtime import build_pipeline
    pipeline = build_pipeline()
    original = pipeline.deps.checkout.create_checkout
    used = "http://fixture-host.invalid:3010"
    async def checkout(request):
        result = await original(request)
        return replace(result, effective_proxy=used)
    monkeypatch.setattr(pipeline.deps.checkout, "create_checkout", checkout)
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline", lambda: pipeline)
    store.clear()
    client = TestClient(create_app())
    r = client.post("/api/orders", json={"plan": "team", "billing_country": "EG", "seat_quantity": 2, "session": "fixture-session"})
    assert r.status_code == 201, r.text
    assert vault.get(r.json()["id"]).proxy == used
    assert "effective_proxy" not in r.json()
    assert used not in r.text


@pytest.mark.parametrize("checkout_country,billing_country,currency", [("EG", "US", "EGP"), ("US", "JP", "USD")])
def test_plus_input_payment_aligns_billing_to_checkout_country(monkeypatch, checkout_country, billing_country, currency):
    from gss_api.runtime import build_pipeline
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    pipeline = build_pipeline()
    monkeypatch.setattr("gss_api.routers.orders.build_pipeline", lambda: pipeline)
    store.clear()
    client = TestClient(create_app())
    created = client.post("/api/orders", json={"plan": "plus", "billing_country": checkout_country, "session": "fixture-session"})
    assert created.status_code == 201, created.text
    body = created.json()
    address = {"name": "Fixture User", "line1": "Fixture Street", "city": "Fixture City", "state": "OR", "postal_code": "97005", "country": billing_country}
    response = client.post(f"/api/orders/{body['id']}/pay", json={"payment_method_token": "pm_fixture", "billing": address})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "delivered"
    assert result["billing_country"] == checkout_country
    assert result["currency"] == currency
    assert result["billing"]["country"] == checkout_country
    request = pipeline.deps.payment.calls[0]
    assert request.country == checkout_country
    assert request.billing["country"] == checkout_country


@pytest.mark.parametrize("plan", ["plus", "team"])
@pytest.mark.parametrize("failure", ["domain", "transport", "unclassified"])
def test_unknown_payment_is_not_a_failure_and_cannot_be_resubmitted(monkeypatch, plan, failure):
    """A lost confirmation reply proves neither failure nor a safe second charge."""
    from gss_api.routers import orders
    from gss_contracts.errors import DomainError, ErrorCode

    store.clear()
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    client = TestClient(create_app())
    created = client.post("/api/orders", json={
        "plan": plan, "billing_country": "US", "seat_quantity": 2,
        "session": '{"accessToken":"fake-session"}',
    })
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]
    path = f"/api/orders/{order_id}"
    try:
        pipeline = orders.build_pipeline()
        from gss_contracts import PaymentResult, PaymentStatus
        if failure == "unclassified":
            pay = AsyncMock(return_value=PaymentResult(status=PaymentStatus.ERROR))
        else:
            exc = (DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "确认响应丢失")
                   if failure == "domain" else TimeoutError())
            pay = AsyncMock(side_effect=exc)
        verify = AsyncMock()
        monkeypatch.setattr(pipeline, "verify", verify)
        notify = AsyncMock()
        monkeypatch.setattr(pipeline, "pay", pay)
        monkeypatch.setattr(orders, "build_pipeline", lambda: pipeline)
        monkeypatch.setattr(orders, "notify_order_update", notify)
        request = {"payment_method_token": "pm_fake"}
        response = client.post(f"{path}/pay", json=request)
        repeated = client.post(f"{path}/pay", json=request)
        pay.assert_awaited_once()
        assert repeated.status_code == 409

        result = client.get(path)
        assert result.status_code == 200
        order = result.json()
        assert order["status"] == "unknown"
        assert order["payment_status"] in {"unknown", "processing"}
        assert order["receipt"]["payment_status"] == order["payment_status"]
        assert "支付失败" not in order["status_summary"] and "未扣款" not in order["status_summary"]
        assert response.status_code in {200, 202}
        assert response.json()["status"] == order["status"]
        assert response.json()["payment_status"] == order["payment_status"]
        assert notify.await_args.args[1].value == order["status"]
        reread = client.post(f"{path}/verify?paid=true")
        assert reread.status_code == 200
        assert reread.json()["status"] == "unknown"
        verify.assert_not_awaited()
        pay.assert_awaited_once()
    finally:
        vault.drop(order_id)
        store.clear()

@pytest.mark.parametrize("outcome", ["timeout", "ambiguous"])
def test_protocol_payment_unknown_blocks_all_further_confirmation(monkeypatch, outcome):
    """The second payment endpoint shares the same no-replay rule as /pay."""
    from gss_api.routers import orders
    from gss_core.stripe_protocol import PaymentOutcome

    monkeypatch.setenv("GSS_USE_FAKE", "1")
    store.clear()
    client = TestClient(create_app())
    created = client.post("/api/orders", json={
        "plan": "plus", "billing_country": "US", "session": "fake-session",
    })
    assert created.status_code == 201
    oid = created.json()["id"]
    path = f"/api/orders/{oid}"
    try:
        protocol = Mock()
        if outcome == "timeout":
            protocol.pay_from_url.side_effect = TimeoutError("private transport detail")
        else:
            protocol.pay_from_url.return_value = PaymentOutcome(state="unknown", message="未扣款")
        monkeypatch.setattr(orders, "PlusProtocol", lambda **kwargs: protocol)
        store.view(oid).checkout_kind = "custom"
        body = {"payment_method_id": "pm_fixture", "billing": created.json()["billing"]}
        resp = client.post(f"{path}/pay-protocol", json=body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "unknown"
        assert resp.json()["receipt"]["summary"] == "扣款结果待核对，请勿重复支付"
        assert client.post(f"{path}/pay-protocol", json=body).status_code == 409
        assert client.post(f"{path}/pay", json={"payment_method_token": "pm_fixture"}).status_code == 409
        assert client.post(f"{path}/verify").json()["status"] == "unknown"
        protocol.pay_from_url.assert_called_once()
    finally:
        vault.drop(oid)
        store.clear()
