"""Route C wiring: decode direct, checkout/confirm stay on the order proxy."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.routers import orders
from gss_api.store import store
from gss_core.browser_context import BrowserContext
from gss_core.browser_decode import BrowserDecodeUnavailable
from gss_core.vault import vault


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/142.0.0.0 Safari/537.36"


def _proxy(tmp_path, monkeypatch):
    config = tmp_path / "proxy-gateways.json"
    gateway = {"host": "gw.example.com", "port": 3010, "scheme": "http",
               "username": "u-region-PH-sid-BASE", "password": "pw",
               "region": "PH", "sid": "BASE", "enabled": True}
    config.write_text(json.dumps({"version": 1, "checkout": gateway, "approve": gateway}))
    monkeypatch.setenv("GSS_PROXY_CONFIG", str(config))
    monkeypatch.setenv("GSS_PROXY_SID", "PINNED01")
    monkeypatch.setenv("GSS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GSS_USE_FAKE", "1")


class FakeDecoder:
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def decode(self, *, target_url: str):
        self.calls.append(target_url)
        return BrowserContext(
            user_agent=UA, sec_ch_ua='"Chromium";v="142"', sec_ch_ua_platform="Windows",
            cookies={"__cf_bm": "cf", "oai-sc": "sc"}, sentinel_token="sentinel",
        )


def test_route_c_decodes_and_keeps_checkout_proxy(tmp_path, monkeypatch, verified_payer):
    verified_payer(country="PH")
    _proxy(tmp_path, monkeypatch)
    monkeypatch.setenv("GSS_ROUTE_PROFILE", "C")
    monkeypatch.setenv("GSS_BROWSER_DECODE", "1")
    monkeypatch.setattr(orders, "PlaywrightBrowserDecoder", FakeDecoder)
    store.clear()
    api = TestClient(create_app())
    response = api.post("/api/orders", json={
        "plan": "plus", "billing_country": "PH", "exit_country": "PH",
        "session": json.dumps({"accessToken": "tok_fixture"}),
    })
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["route_profile"] == "C"
    assert body["purchase_attempt_id"].startswith("pa_")
    secrets = vault.get(body["id"])
    assert secrets is not None
    assert secrets.browser_context["user_agent"] == UA
    assert secrets.proxy.startswith("http://")
    vault.drop(body["id"])
    store.clear()


def test_route_c_fails_closed_then_explicit_fallback_a(tmp_path, monkeypatch, verified_payer):
    verified_payer(country="PH")
    _proxy(tmp_path, monkeypatch)
    monkeypatch.setenv("GSS_ROUTE_PROFILE", "C")
    monkeypatch.setenv("GSS_BROWSER_DECODE", "1")
    monkeypatch.delenv("GSS_BROWSER_DECODE_FALLBACK", raising=False)
    class Broken:
        def __init__(self, **kwargs): pass
        async def decode(self, *, target_url: str):
            raise BrowserDecodeUnavailable("x")
    monkeypatch.setattr(orders, "PlaywrightBrowserDecoder", Broken)
    store.clear()
    api = TestClient(create_app())
    response = api.post("/api/orders", json={
        "plan": "plus", "billing_country": "PH", "exit_country": "PH",
        "session": json.dumps({"accessToken": "tok_fixture"}),
    })
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "browser_decode_unavailable"

    monkeypatch.setenv("GSS_BROWSER_DECODE_FALLBACK", "A")
    response = api.post("/api/orders", json={
        "plan": "plus", "billing_country": "PH", "exit_country": "PH",
        "session": json.dumps({"accessToken": "tok_fixture"}),
    })
    assert response.status_code == 201, response.text
    assert response.json()["route_profile"] == "A"
    store.clear()


def test_php_billing_profile_is_selected_by_currency(tmp_path, monkeypatch, verified_payer):
    verified_payer(country="US")
    _proxy(tmp_path, monkeypatch)
    profile = tmp_path / "profiles.json"
    address = {"name": "Payer", "email": "p@example.com", "line1": "1 Main",
               "city": "Portland", "state": "OR", "postal_code": "97201", "country": "US"}
    profile.write_text(json.dumps({"version": 2, "payer_id": "p", "verification_ref": "r",
                                   "profiles": {"PHP": address}}))
    import os, stat
    os.chmod(profile, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("GSS_BILLING_PROFILES_FILE", str(profile))
    monkeypatch.setenv("GSS_ALLOW_SPLIT_EXIT_BILLING", "1")
    store.clear()
    response = TestClient(create_app()).post("/api/orders", json={
        "plan": "plus", "billing_country": "US", "exit_country": "PH",
        "session": json.dumps({"accessToken": "tok_fixture"}),
    })
    assert response.status_code == 201, response.text
    assert response.json()["currency"] == "PHP"
    store.clear()
