import asyncio
import pytest

from gss_core.browser_context import BrowserContext
from gss_core.browser_decode import BrowserDecodeUnavailable, FakeBrowserDecoder, NullBrowserDecoder


def test_fake_decoder_returns_context():
    expected = BrowserContext(user_agent="UA", cookies={"__cf_bm": "cf"})
    got = asyncio.run(FakeBrowserDecoder(expected).decode(target_url="https://chatgpt.com/"))
    assert got is expected


def test_null_decoder_fails_closed():
    with pytest.raises(BrowserDecodeUnavailable):
        asyncio.run(NullBrowserDecoder().decode(target_url="https://chatgpt.com/"))
