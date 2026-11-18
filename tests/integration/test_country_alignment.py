"""statsigContext.country 存在时，订单国家/币种/地址三者一致。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.routers import orders as orders_router
from gss_api.runtime import build_pipeline
from gss_api.store import store
from gss_core.vault import vault

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.7444.60 Safari/537.36"
)


def _session(country: str = "JP"):
    return json.dumps(
        {
            "accessToken": "tok_fixture",
            "sessionToken": "sess_fixture",
            "statsigContext": {
                "deviceId": "did_fixture",
                "userAgent": UA,
                "country": country,
            },
        }
    )


def test_order_country_follows_session_and_payment_reuses_it(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: pipeline)
    store.clear()
    client = TestClient(create_app())

    created = client.post(
        "/api/orders",
        json={"plan": "plus", "billing_country": "US", "session": _session("JP")},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["billing_country"] == "JP"
    assert body["currency"] == "JPY"
    assert body["country_aligned_to_session"] is True
    assert pipeline.deps.checkout.calls[0].billing_country == "JP"

    secrets = vault.get(body["id"])
    assert secrets is not None
    assert secrets.session_country == "JP"
    assert secrets.exit_country == "JP"
    assert secrets.user_agent == UA
    assert secrets.device_id == "did_fixture"
    assert secrets.billing["country"] == "JP"
    assert secrets.profile["build"] == "142.0.7444.60"

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
    assert paid.json()["billing"]["country"] == "JP"
    request = pipeline.deps.payment.calls[0]
    assert request.country == "JP"
    assert request.billing["country"] == "JP"
    assert request.billing["line1"] and request.billing["line1"] != "1 Chome"
    vault.drop(body["id"])
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
    assert created.json()["country_aligned_to_session"] is False
    store.clear()
