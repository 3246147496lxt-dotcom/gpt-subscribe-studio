"""核验地址与渠道账单声明必须一致；保留合法订单的 Session 回归覆盖。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.routers import orders as orders_router
from gss_api.runtime import build_pipeline
from gss_api.store import store
from gss_core.chatgpt import ChatGPTClient
from gss_core.vault import vault

OLD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)
NEW_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.7871.189 Safari/537.36"
)


def _session(country: str = "US"):
    return json.dumps(
        {
            "accessToken": "old-access-token",
            "sessionToken": "core-session-token",
            "statsigContext": {
                "deviceId": "old-device-id",
                "userAgent": OLD_UA,
                "country": country,
            },
        }
    )


def _fake_reauth(monkeypatch):
    calls = []

    def fake(cls, raw, *, proxy, country):
        calls.append({"raw": raw, "proxy": proxy, "country": country})
        return SimpleNamespace(
            access_token="new-access-token",
            session_token="core-session-token",
            device_id="new-device-id",
            user_agent=NEW_UA,
            oai_session_id="new-session-id",
            extra_cookies={
                "__Secure-next-auth.session-token": "core-session-token",
                "oai-did": "new-device-id",
            },
            close=lambda: None,
        )

    monkeypatch.setattr(orders_router, "_guard_refresh_egress", AsyncMock())
    monkeypatch.setattr(ChatGPTClient, "reauthorize_for_country", classmethod(fake))
    return calls


def test_cross_country_reissues_session_and_uses_target_country(monkeypatch, verified_payer):
    profile = verified_payer(country="PH")
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: pipeline)
    reauth_calls = _fake_reauth(monkeypatch)
    store.clear()
    client = TestClient(create_app())

    created = client.post(
        "/api/orders",
        json={"plan": "plus", "billing_country": "PH", "exit_country": "PH", "session": _session("US")},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["billing_country"] == "PH"
    assert body["currency"] == "PHP"
    assert body["session_reissued_for_country"] is True
    assert pipeline.deps.checkout.calls[0].billing_country == "PH"
    assert reauth_calls and reauth_calls[0]["country"] == "PH"
    assert "core-session-token" in reauth_calls[0]["raw"]

    secrets = vault.get(body["id"])
    assert secrets is not None
    assert secrets.session_country == "US"
    assert secrets.exit_country == "PH"
    assert secrets.user_agent == NEW_UA
    assert secrets.device_id == "new-device-id"
    assert secrets.billing["country"] == "PH"
    assert "new-access-token" in secrets.session

    paid = client.post(
        f"/api/orders/{body['id']}/pay",
        json={
            "payment_method_token": "pm_fixture",
            "billing": {
                "name": "Fixture User",
                "line1": "1 Chome",
                "city": "Tokyo",
                "state": "",
                "postal_code": "150-0002",
                "country": "US",
            },
        },
    )
    assert paid.status_code == 409, paid.text
    assert not pipeline.deps.payment.calls
    assert secrets.billing == body["billing"] == profile
    vault.drop(body["id"])
    store.clear()


@pytest.mark.usefixtures("verified_us_payer")
def test_same_country_does_not_reissue(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: pipeline)
    reauth_calls = _fake_reauth(monkeypatch)
    store.clear()
    client = TestClient(create_app())

    created = client.post(
        "/api/orders",
        json={"plan": "plus", "billing_country": "US", "session": _session("US")},
    )
    assert created.status_code == 201, created.text
    assert created.json()["billing_country"] == "US"
    assert created.json()["session_reissued_for_country"] is False
    assert reauth_calls == []
    store.clear()


@pytest.mark.usefixtures("verified_us_payer")
def test_order_without_session_country_keeps_requested_billing(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: pipeline)
    store.clear()
    client = TestClient(create_app())

    created = client.post(
        "/api/orders",
        json={"plan": "plus", "billing_country": "US", "session": json.dumps({"accessToken": "tok"})},
    )
    assert created.status_code == 201, created.text
    assert created.json()["billing_country"] == "US"
    assert created.json()["currency"] == "USD"
    assert created.json()["session_reissued_for_country"] is False
    store.clear()


def test_cross_country_without_session_token_is_rejected(monkeypatch, verified_payer):
    verified_payer(country="PH")
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: pipeline)
    store.clear()
    client = TestClient(create_app())

    session = json.dumps(
        {
            "accessToken": "old-access-token",
            "statsigContext": {"deviceId": "old", "userAgent": OLD_UA, "country": "US"},
        }
    )
    created = client.post(
        "/api/orders", json={"plan": "plus", "billing_country": "PH", "exit_country": "PH", "session": session}
    )
    assert created.status_code == 401
    store.clear()


@pytest.mark.usefixtures("verified_us_payer")
def test_strict_mode_rejects_split_exit_before_refresh(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    monkeypatch.setenv("GSS_REQUIRE_EXIT_BILLING_MATCH", "1")
    pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: pipeline)
    reauth = _fake_reauth(monkeypatch)
    store.clear()
    response = TestClient(create_app()).post("/api/orders", json={
        "plan": "plus", "billing_country": "US", "exit_country": "PH", "session": _session("US"),
    })
    assert response.status_code == 409
    assert not reauth and not pipeline.deps.checkout.calls
    assert not store.orders and not store.jobs


@pytest.mark.usefixtures("verified_us_payer")
def test_split_exit_and_fixed_billing_allowed_by_default(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: pipeline)
    _fake_reauth(monkeypatch)
    store.clear()
    created = TestClient(create_app()).post("/api/orders", json={
        "plan": "plus", "billing_country": "US", "exit_country": "PH", "session": _session("JP"),
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["billing_country"] == "US"
    assert body["exit_country"] == "PH"
    assert body["billing"]["country"] == "US"
    call = pipeline.deps.checkout.calls[0]
    assert call.billing_country == "US" and call.exit_country == "PH"
    store.clear()


@pytest.mark.parametrize("stage", ["checkout", "pay"])
@pytest.mark.parametrize("reissued", [False, True])
def test_failure_reports_session_state_without_credentials(monkeypatch, stage, reissued, verified_payer):
    verified_payer(country="PH")
    from gss_contracts.errors import DomainError, ErrorCode

    monkeypatch.setenv("GSS_USE_FAKE", "1")
    _fake_reauth(monkeypatch)
    store.clear()
    client = TestClient(create_app())
    pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: pipeline)
    order = {"plan": "plus", "billing_country": "PH", "exit_country": "PH",
             "session": _session("US" if reissued else "PH")}
    order_id = None

    token, cookie = "fixture-access-value", "fixture-cookie-value"
    diagnostic = f"upstream: {{'accessToken': '{token}'}}; __Secure-next-auth.session-token.0={cookie}"
    if reissued:
        diagnostic += " opaque-fixture-credential"
    code = ErrorCode.CHECKOUT_UPSTREAM_ERROR if stage == "checkout" else ErrorCode.PAYMENT_DECLINED
    monkeypatch.setattr(pipeline, "create_checkout" if stage == "checkout" else "pay",
                        AsyncMock(side_effect=DomainError(code, diagnostic)))
    notify, log = AsyncMock(), Mock()
    monkeypatch.setattr(orders_router, "notify_order_update", notify)
    monkeypatch.setattr(orders_router.logger, "info", log)

    try:
        response = client.post("/api/orders", json=order)
        if stage == "pay":
            assert response.status_code == 201
            order_id = response.json()["id"]
            response = client.post(f"/api/orders/{order_id}/pay", json={"payment_method_token": "offline-verified-instrument"})
        else:
            order_id, = store.orders

        assert response.status_code == 400
        error = response.json()["detail"]
        assert error["code"] == code.value
        assert error["detail"] == ({"session_reissued_for_country": True} if reissued else None)
        view = store.view(order_id)
        assert view.last_error == error["message"]
        public_order = client.get(f"/api/orders/{order_id}")
        assert public_order.status_code == 200
        if stage == "pay":
            assert notify.await_args.args[2] == {"error": view.last_error}
        for channel in (response.text, public_order.text, str(log.call_args_list), str(notify.await_args_list)):
            for secret in (token, cookie, "opaque-fixture-credential", "new-access-token", "core-session-token"):
                assert secret not in channel
    finally:
        if order_id:
            vault.drop(order_id)
        store.clear()
