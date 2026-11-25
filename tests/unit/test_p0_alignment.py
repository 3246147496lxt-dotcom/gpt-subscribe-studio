"""P0 alignment tests: TLS/UA/sec-ch-ua, PH exit + US billing split, SID-rotation reissue, StripeProtocol."""
from __future__ import annotations

import asyncio
import base64
import gzip
import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.routers import orders as orders_router
from gss_api.runtime import build_pipeline
from gss_api.store import store
from gss_checkout.adapter import UpstreamCheckoutAdapter
from gss_contracts import BillingMode, CheckoutRequest, Plan
from gss_contracts.errors import DomainError, ErrorCode
from gss_core import chatgpt as cg
from gss_core.chatgpt import ChatGPTClient, impersonate_for_major, impersonate_for_ua
from gss_core.har_device import DeviceProfile, apply_user_agent, device_for_account
from gss_core.har_telemetry import StripeTelemetry, TelemetryContext
from gss_core.plus_har_protocol import PlusHarProtocol
from gss_core.stripe_protocol import CheckoutContext, StripeProtocol
from gss_core.vault import vault

WIN_146_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.7680.180 Safari/537.36"
)


def _make_jwt(account_id: str = "acct-p0") -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({
        "exp": 4102444800,
        "sub": account_id,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def test_device_exit_country_overrides_host_and_realigns_on_switch(tmp_path, monkeypatch):
    # Host fingerprint has a US timezone, but exit country is PH.
    fp_file = tmp_path / "host-fp.json"
    fp_file.write_text(json.dumps({"timezone": "America/New_York", "platform": "MacIntel"}))
    monkeypatch.setenv("GSS_DEVICE_FINGERPRINT", str(fp_file))
    monkeypatch.setattr("gss_core.har_device._HOST_CACHE", None)
    monkeypatch.setenv("GSS_DEVICE_STORE", str(tmp_path / "devices.json"))

    ph = DeviceProfile.random(country="PH", seed="ph-seed")
    assert ph.timezone == "Asia/Manila"
    assert ph.timezone_offset_min == 480
    assert ph.locale == "en-PH"
    assert ph.languages == "en-PH,en"

    # Returning buyer first on US, then switches to PH exit: guid/muid/sid persist,
    # while browser timezone & locale realign to PH (never leaking US timezone onto PH IP).
    first = device_for_account("acct-switch", "US")
    assert first.timezone == "America/New_York"
    assert first.locale == "en-US"

    switched = device_for_account("acct-switch", "PH")
    assert (switched.guid, switched.muid, switched.sid) == (first.guid, first.muid, first.sid)
    assert switched.stripe_js_id != first.stripe_js_id
    assert switched.timezone == "Asia/Manila"
    assert switched.timezone_offset_min == 480
    assert switched.locale == "en-PH"


def test_ph_exit_with_us_billing_keeps_billing_us_and_browser_ph():
    captured_requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured_requests.append(req)
        return httpx.Response(200, json={})

    device = DeviceProfile.random(country="PH", seed="split-ph-us")
    ctx = TelemetryContext(
        device=device,
        key="pk_live_fixture",
        currency="PHP",
        amount=110000,
        email="payer@example.invalid",
        country="US",        # fixed US tax-free billing address
        exit_country="PH",   # Philippines exit IP
    )
    tel = StripeTelemetry(httpx.Client(transport=httpx.MockTransport(handler)), ctx)
    tel.ces_register()

    assert len(captured_requests) == 1
    body = json.loads(gzip.decompress(captured_requests[0].content).decode("utf-8"))
    user = body["events"][0]["user"]
    assert user["country"] == "PH"
    assert user["locale"] == "en-PH"

    # Stripe Address Element token still resolves {{country}} to the US billing country.
    tokens = tel._tokens()
    assert tokens["{{country}}"] == "US"
    assert tokens["{{timezone}}"] == "Asia/Manila"
    assert tokens["{{currency}}"] == "php"


def test_sec_ch_ua_and_impersonate_match_between_chatgpt_and_har_device():
    client = ChatGPTClient(access_token="tok", user_agent=WIN_146_UA, device_id="did-146")
    device = apply_user_agent(DeviceProfile.random(country="PH", seed="match-146"), WIN_146_UA)

    assert client.chrome_major == "146"
    assert client.impersonate == "chrome146"
    assert device.impersonate == "chrome146"
    assert client.sec_ch_ua == device.sec_ch_ua
    assert impersonate_for_major("150") == "chrome150"
    assert impersonate_for_ua(WIN_146_UA) == "chrome146"


def test_mint_sentinel_passes_matching_impersonate_and_client_hints(monkeypatch):
    seen = {}

    def fake_mint(**kwargs):
        seen.update(kwargs)
        return {"p": "gAAAAABok~S", "c": "tok-c"}

    monkeypatch.setattr(cg, "mint", fake_mint)
    client = ChatGPTClient(
        access_token="tok",
        user_agent=WIN_146_UA,
        device_id="did-146",
        language="en-PH",
        timezone="Asia/Manila",
    )
    try:
        client.mint_sentinel(flow="chatgpt_checkout")
    finally:
        client.close()

    assert seen["impersonate"] == "chrome146"
    assert seen["sec_ch_ua"] == client.sec_ch_ua
    assert seen["sec_ch_ua_platform"] == '"Windows"'
    assert seen["language"] == "en-PH"
    assert seen["timezone"] == "Asia/Manila"


def test_sentinel_curl_fetch_uses_requested_impersonate_and_falls_back(monkeypatch):
    import importlib.util

    path = (
        Path(__file__).resolve().parents[2]
        / "packages/core/src/gss_core/sentinel_assets/sentinel_curl_fetch.py"
    )
    spec = importlib.util.spec_from_file_location("sentinel_curl_fetch_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    seen: list[str] = []

    def fake_request(**kwargs):
        seen.append(kwargs["impersonate"])
        if kwargs["impersonate"] == "chrome999":
            raise ValueError("unsupported target")
        return SimpleNamespace(status_code=200, text='{"ok":true}')

    monkeypatch.setattr(mod.requests, "request", fake_request)
    res = mod.handle({"url": "https://chatgpt.com/backend-api/sentinel/req", "impersonate": "chrome146"})
    assert res == {"status": 200, "body": '{"ok":true}'}
    assert seen == ["chrome146"]

    seen.clear()
    res2 = mod.handle({"url": "https://chatgpt.com/backend-api/sentinel/req", "impersonate": "chrome999"})
    assert res2["status"] == 200
    assert seen == ["chrome999", "chrome150"]


def test_stripe_protocol_accepts_publishable_key_currency_and_ph_exit_with_us_billing(monkeypatch):
    captured: list[tuple[str, str, dict | str]] = []

    class FakeResp:
        def __init__(self, payload: dict):
            self.status_code = 200
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    def fake_post(self, url, data, headers):
        captured.append(("POST", url, data))
        if url.endswith("/6"):
            return FakeResp({"guid": "g1", "muid": "m1", "sid": "s1"})
        if url.endswith("/init"):
            return FakeResp({
                "total_summary": {"due": 110000},
                "currency": "php",
                "config_id": "cfg_ph",
                "link_settings": {},
            })
        return FakeResp({"id": "pm_test", "card": {"display_brand": "visa", "last4": "4242"}})

    def fake_get(self, url, params, headers):
        captured.append(("GET", url, params))
        return FakeResp({"session_id": "es_ph", "config_id": "cfg_ph"})

    monkeypatch.setattr(StripeProtocol, "_post", fake_post)
    monkeypatch.setattr(StripeProtocol, "_get", fake_get)

    sp = StripeProtocol(
        proxy=None,
        billing={"country": "US", "line1": "1425 SW Morrison St", "city": "Portland", "state": "OR", "postal_code": "97205"},
        user_agent=WIN_146_UA,
        publishable_key="pk_live_order_specific",
        currency="PHP",
        exit_country="PH",
    )
    try:
        assert sp.publishable_key == "pk_live_order_specific"
        fp = sp.register_fingerprint()
        assert (fp.guid, fp.muid, fp.sid) == ("g1", "m1", "s1")

        # m.stripe.com/6 v2=2 payload must use PH locale + Manila offset (+480), NOT US -300..-480.
        m6_posts = [data for method, url, data in captured if url.endswith("/6")]
        decoded_v2 = json.loads(urllib.parse.unquote(base64.b64decode(m6_posts[1]).decode()))
        assert decoded_v2["a"]["c"]["v"] == "en-PH,en"
        assert decoded_v2["a"]["g"]["v"] == "480"

        _, ctx = sp.init_checkout("cs_live_ph")
        assert ctx.currency == "php"
        assert sp.locale == "en-PH"
        assert sp.timezone == "Asia/Manila"

        sp.elements_session("cs_live_ph", ctx, locale=sp.locale)
        el_params = [p for m, u, p in captured if m == "GET" and u.endswith("/v1/elements/sessions")][0]
        assert el_params["key"] == "pk_live_order_specific"
        assert el_params["currency"] == "php"
        assert el_params["deferred_intent[currency]"] == "php"
        assert el_params["browser_timezone"] == "Asia/Manila"

        # Billing address / tax_region remains US despite PH exit.
        sp.update_address("cs_live_ph", ctx)
        tax_data = [d for m, u, d in captured if m == "POST" and u.endswith("/v1/payment_pages/cs_live_ph")][0]
        parsed_tax = urllib.parse.parse_qs(tax_data)
        assert parsed_tax["key"] == ["pk_live_order_specific"]
        assert parsed_tax["tax_region[country]"] == ["US"]
    finally:
        sp.close()


def test_sentinel_sid_rotation_reissues_at_on_new_ph_exit(monkeypatch, verified_payer):
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    monkeypatch.delenv("GSS_REISSUE_SESSION_ON_EXIT", raising=False)

    fixed_us = verified_payer("US")
    initial_proxy = "http://u-region-PH-sid-11111111:pw@gw.example:3010"
    initial_session = json.dumps({
        "accessToken": _make_jwt("acct-rot"),
        "sessionToken": "st-rot-1",
        "statsigContext": {"country": "PH"},
    })
    req = CheckoutRequest(
        session=initial_session,
        plan=Plan.PLUS,
        billing_country="US",
        exit_country="PH",
        billing_mode=BillingMode.STANDARD,
        proxy=initial_proxy,
        verified_billing=fixed_us,
    )

    reissued_at = _make_jwt("acct-rot-fresh")
    reauth_calls = []

    def fake_reauth(cls, raw, *, proxy, country, **kwargs):
        reauth_calls.append({"proxy": proxy, "country": country})
        return SimpleNamespace(
            access_token=reissued_at,
            session_token="st-rot-2",
            refresh_token="",
            device_id="did-rotated",
            user_agent=WIN_146_UA,
            oai_session_id="sid-rotated",
            extra_cookies={"__Secure-next-auth.session-token": "st-rot-2", "oai-did": "did-rotated"},
            close=lambda: None,
        )

    monkeypatch.setattr(ChatGPTClient, "reauthorize_for_country", classmethod(fake_reauth))

    attempts = []

    def flaky_create(request, used_proxy):
        attempts.append((used_proxy, request.session))
        if len(attempts) == 1:
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                "无法签发 ChatGPT 风控令牌（Sentinel）：proxy fetch: curl: (7) CONNECT tunnel failed",
            )
        return SimpleNamespace(
            checkout_url="https://chatgpt.com/checkout/openai_llc/oaics_ph",
            currency="PHP",
            amount=None,
            checkout_session_id="oaics_ph",
            publishable_key="pk_live_fixture",
            customer_session_client_secret=None,
            checkout_kind="custom",
            confirm_return_url="",
        )

    adapter = UpstreamCheckoutAdapter()
    monkeypatch.setattr(adapter, "_create", flaky_create)

    result = asyncio.run(adapter.create_checkout(req))
    assert len(attempts) == 2
    assert attempts[0][0] == initial_proxy
    assert attempts[1][0] != initial_proxy
    assert reauth_calls == [{"proxy": attempts[1][0], "country": "PH"}]
    assert reissued_at in attempts[1][1]
    assert result.effective_proxy == attempts[1][0]
    assert result.effective_session == attempts[1][1]
    assert result.effective_user_agent == WIN_146_UA
    assert result.effective_device_id == "did-rotated"
    assert result.billing == fixed_us
    assert reissued_at not in repr(result)


@pytest.mark.usefixtures("verified_us_payer")
def test_orders_pure_st_uses_clean_reauthorize_on_ph_exit_with_us_billing(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    fake_pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: fake_pipeline)
    monkeypatch.setattr(orders_router, "_guard_refresh_egress", AsyncMock())

    fresh_at = _make_jwt("acct-pure-st")
    reauth_calls = []

    def fake_reauth(cls, raw, *, proxy, country, **kwargs):
        reauth_calls.append(country)
        return SimpleNamespace(
            access_token=fresh_at,
            session_token="st-fresh",
            refresh_token="",
            device_id="did-pure-st",
            user_agent=WIN_146_UA,
            oai_session_id="sid-pure-st",
            extra_cookies={"__Secure-next-auth.session-token": "st-fresh", "oai-did": "did-pure-st"},
            close=lambda: None,
        )

    monkeypatch.setattr(ChatGPTClient, "reauthorize_for_country", classmethod(fake_reauth))
    store.clear()
    try:
        res = TestClient(create_app()).post(
            "/api/orders",
            json={
                "plan": "plus",
                "billing_country": "US",
                "exit_country": "PH",
                "session": json.dumps({"sessionToken": "st-only-input"}),
            },
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["billing_country"] == "US"
        assert body["exit_country"] == "PH"
        assert body["currency"] == "PHP"
        assert reauth_calls == ["PH"]
        secrets = vault.get(body["id"])
        assert secrets is not None
        assert secrets.user_agent == WIN_146_UA
        assert secrets.device_id == "did-pure-st"
        assert fresh_at in secrets.session
    finally:
        store.clear()


@pytest.mark.usefixtures("verified_us_payer")
def test_orders_checkout_rotate_reissues_at_on_new_ph_sid(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    monkeypatch.setenv("GSS_CHECKOUT_ROTATE", "1")
    monkeypatch.delenv("GSS_REISSUE_SESSION_ON_EXIT", raising=False)
    monkeypatch.setattr(orders_router, "_guard_refresh_egress", AsyncMock())
    monkeypatch.setattr(
        orders_router,
        "checkout_proxy_for",
        lambda *a, **kw: "http://u-region-PH-sid-11111111:pw@gw.example:3010",
    )

    at_first = _make_jwt("acct-rot-1")
    at_second = _make_jwt("acct-rot-2")
    reauth_calls: list[dict] = []

    def fake_reauth(cls, raw, *, proxy, country, **kwargs):
        idx = len(reauth_calls)
        reauth_calls.append({"proxy": proxy, "country": country})
        token = at_first if idx == 0 else at_second
        did = f"did-rot-{idx}"
        return SimpleNamespace(
            access_token=token,
            session_token=f"st-rot-{idx}",
            refresh_token="",
            device_id=did,
            user_agent=WIN_146_UA,
            oai_session_id=f"sid-rot-{idx}",
            extra_cookies={"__Secure-next-auth.session-token": f"st-rot-{idx}", "oai-did": did},
            close=lambda: None,
        )

    monkeypatch.setattr(ChatGPTClient, "reauthorize_for_country", classmethod(fake_reauth))

    checkout_calls: list[tuple[str, str]] = []

    async def flaky_checkout(pipeline, *, proxy: str, **kwargs):
        checkout_calls.append((proxy, kwargs["session"]))
        if len(checkout_calls) == 1:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "proxy tunnel failed: connect tunnel failed")
        return SimpleNamespace(
            checkout_url="https://chatgpt.com/checkout/openai_llc/oaics_rot",
            currency="PHP",
            amount=1100,
            checkout_session_id="oaics_rot",
            publishable_key="pk_live_fixture",
            customer_session_client_secret="cuss_rot",
            billing=dict(kwargs["verified_billing"]),
            tax_delta=0,
            checkout_kind="custom",
            confirm_return_url="https://chatgpt.com/checkout/verify?stripe_session_id=oaics_rot",
            effective_proxy=proxy,
        )

    monkeypatch.setattr(orders_router, "_create_checkout_once", flaky_checkout)
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: SimpleNamespace())

    store.clear()
    try:
        res = TestClient(create_app()).post(
            "/api/orders",
            json={
                "plan": "plus",
                "billing_country": "US",
                "exit_country": "PH",
                "session": json.dumps({
                    "accessToken": _make_jwt("acct-old"),
                    "sessionToken": "st-orig",
                    "statsigContext": {"country": "US"},
                }),
            },
        )
        assert res.status_code == 201, res.text
        assert len(checkout_calls) == 2
        assert checkout_calls[0][0] != checkout_calls[1][0]
        assert len(reauth_calls) == 2
        assert reauth_calls[0]["country"] == "PH"
        assert reauth_calls[1]["country"] == "PH"
        assert reauth_calls[1]["proxy"] == checkout_calls[1][0]
        assert at_second in checkout_calls[1][1]
        body = res.json()
        assert body["billing_country"] == "US"
        assert body["exit_country"] == "PH"
        secrets = vault.get(body["id"])
        assert secrets is not None
        assert at_second in secrets.session
        assert secrets.proxy == checkout_calls[1][0]
    finally:
        store.clear()
