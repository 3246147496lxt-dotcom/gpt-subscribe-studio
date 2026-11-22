import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gss_checkout.adapter import UpstreamCheckoutAdapter
from gss_contracts import BillingMode, CheckoutRequest, Plan
from gss_contracts.errors import DomainError, ErrorCode
from gss_core import chatgpt
from gss_core.browser_context import BROWSER_COOKIE_DENYLIST, BrowserContext


def test_context_headers_and_cookie_merge():
    ctx = BrowserContext(
        user_agent="UA", sec_ch_ua='"Chromium";v="157"', sec_ch_ua_platform="macOS",
        locale="en-US", cookies={"__cf_bm": "cf", "oai-did": "DID"},
    )
    assert ctx.headers()["User-Agent"] == "UA"
    assert ctx.headers()["sec-ch-ua-platform"] == '"macOS"'
    merged = ctx.merged_cookies({"a": "1"})
    # Direct-browser cookies must not cross into the order proxy session.
    assert merged == {"a": "1"}


def test_repr_does_not_expose_secrets():
    ctx = BrowserContext(cookies={"secret": "cookie"}, sentinel_token="token")
    assert "cookie" not in repr(ctx)
    assert "token" not in repr(ctx)
    assert "cookies" not in ctx.to_dict()
    assert "sentinel_token" not in ctx.to_dict()


def test_context_headers_reject_cookie_auth_and_pseudo_headers():
    ctx = BrowserContext(user_agent="UA", cookies={"oai-did": "BROWSER"})
    headers = ctx.headers()
    lower = {k.lower() for k in headers}
    assert "cookie" not in lower
    assert "authorization" not in lower
    assert not any(k.startswith(":") for k in lower)


@pytest.mark.parametrize("session_token", ["", "session-bound-fixture"])
def test_anonymous_browser_attestation_does_not_replace_session_token(session_token):
    client = chatgpt.ChatGPTClient(access_token="tok", sentinel_token=session_token)
    client.apply_browser_context(BrowserContext(sentinel_token="anonymous-fixture"))
    assert client.sentinel_token == session_token


def test_browser_cookies_do_not_override_live_oai_did():
    from gss_core.chatgpt import ChatGPTClient
    client = ChatGPTClient(access_token="tok", device_id="LIVE_DID",
                           extra_cookies={"oai-did": "LIVE_DID"})
    client.apply_browser_context(BrowserContext(cookies={"oai-did": "BROWSER", "__cf_bm": "cf"}))
    cookie = client._cookie_header()
    assert "oai-did=LIVE_DID" in cookie
    assert "oai-did=BROWSER" not in cookie
    assert client._identity_headers()["Authorization"] == "Bearer tok"


@pytest.mark.parametrize(("plan", "wire_plan"), [
    (Plan.PLUS, "chatgptplusplan"), (Plan.PRO_LITE, "chatgptprolite"),
    (Plan.PRO, "chatgptpro"), (Plan.PRO_MAX, "chatgptpromax"),
])
def test_adapter_checkout_uses_selected_plan_and_isolates_browser_cookies(
    monkeypatch, verified_us_payer, plan, wire_plan,
):
    marker = "direct-private-fixture"
    direct = dict.fromkeys(BROWSER_COOKIE_DENYLIST, marker)
    direct.update({
        "__Secure-next-auth.session-token.0": marker, "authjs.session-token.1": marker,
        "bad;name": marker, "bad-value": "a; session-token=" + marker, "pref": "local",
    })
    ctx = BrowserContext(user_agent="offline-test", cookies=direct)
    assert ctx.vault_dict()["cookies"] == {"pref": "local"}
    response = SimpleNamespace(status_code=401, headers={}, text="", json=lambda: {})
    http = SimpleNamespace(headers={}, cookies=Mock(), post=Mock(return_value=response))
    monkeypatch.setattr(chatgpt, "_new_session", lambda **_: http)
    def offline_prepare(client, **_):
        assert client.browser_context is ctx
    monkeypatch.setattr(chatgpt.ChatGPTClient, "prepare_risk", offline_prepare)
    monkeypatch.setattr(chatgpt.ChatGPTClient, "hydrate", lambda _: None)
    req = CheckoutRequest(
        session=json.dumps({"accessToken": "offline-token", "sessionToken": "from-order"}),
        plan=plan, billing_country="US", billing_mode=BillingMode.STANDARD,
        proxy="http://offline.invalid", browser_context=ctx,
    )
    with pytest.raises(DomainError) as failure:
        asyncio.run(UpstreamCheckoutAdapter().create_checkout(req))
    assert failure.value.code == ErrorCode.INVALID_SESSION
    http.post.assert_called_once()
    assert http.post.call_args.kwargs["json"]["plan_name"] == wire_plan
    assert marker not in http.headers["Cookie"]
    assert "pref=local" in http.headers["Cookie"]
    assert "__Secure-next-auth.session-token=from-order" in http.headers["Cookie"]
    assert all(marker not in call.args[1] for call in http.cookies.set.call_args_list)
