import pytest

from gss_core.browser_context import BrowserContext


def test_context_headers_and_cookie_merge():
    ctx = BrowserContext(
        user_agent="UA", sec_ch_ua='"Chromium";v="157"', sec_ch_ua_platform="macOS",
        locale="en-US", cookies={"__cf_bm": "cf", "oai-did": "DID"},
    )
    assert ctx.headers()["User-Agent"] == "UA"
    assert ctx.headers()["sec-ch-ua-platform"] == '"macOS"'
    merged = ctx.merged_cookies({"a": "1"})
    assert merged == {"a": "1", "__cf_bm": "cf", "oai-did": "DID"}


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


def test_browser_cookies_do_not_override_live_oai_did():
    from gss_core.chatgpt import ChatGPTClient
    client = ChatGPTClient(access_token="tok", device_id="LIVE_DID",
                           extra_cookies={"oai-did": "LIVE_DID"})
    client.apply_browser_context(BrowserContext(cookies={"oai-did": "BROWSER", "__cf_bm": "cf"}))
    cookie = client._cookie_header()
    assert "oai-did=LIVE_DID" in cookie
    assert "oai-did=BROWSER" not in cookie
    assert client._identity_headers()["Authorization"] == "Bearer tok"
