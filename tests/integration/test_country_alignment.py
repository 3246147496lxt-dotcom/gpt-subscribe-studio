"""目标充值国家决定订单币种/地址/出口；跨区时重签发 session。"""

from __future__ import annotations

import json
from types import SimpleNamespace

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

    monkeypatch.setattr(ChatGPTClient, "reauthorize_for_country", classmethod(fake))
    return calls


def test_cross_country_reissues_session_and_uses_target_country(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: pipeline)
    reauth_calls = _fake_reauth(monkeypatch)
    store.clear()
    client = TestClient(create_app())

    created = client.post(
        "/api/orders",
        json={"plan": "plus", "billing_country": "PH", "session": _session("US")},
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
    assert paid.status_code == 200, paid.text
    assert paid.json()["billing"]["country"] == "PH"
    request = pipeline.deps.payment.calls[0]
    assert request.country == "PH"
    assert request.billing["country"] == "PH"
    vault.drop(body["id"])
    store.clear()


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


def test_cross_country_without_session_token_is_rejected(monkeypatch):
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
        "/api/orders", json={"plan": "plus", "billing_country": "PH", "session": session}
    )
    assert created.status_code == 401
    store.clear()
