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
