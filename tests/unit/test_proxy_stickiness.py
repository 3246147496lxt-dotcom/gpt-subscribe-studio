"""重签发后建单重试不得更换出口 SID。"""

from __future__ import annotations

import asyncio

import pytest

from gss_api.routers.orders import _create_checkout_with_retry


class _DeadPipeline:
    def __init__(self):
        self.proxies = []

    async def create_checkout(self, *, proxy, **kwargs):
        self.proxies.append(proxy)
        raise RuntimeError("SSL: UNEXPECTED_EOF_WHILE_READING")


def test_pinned_proxy_never_rotates_sid(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("gss_api.routers.orders.asyncio.sleep", no_sleep)
    pipeline = _DeadPipeline()
    proxy = "http://u-region-PH-sid-PINNED01:pw@gw.example.com:3010"
    with pytest.raises(RuntimeError):
        asyncio.run(
            _create_checkout_with_retry(
                pipeline, proxy=proxy, pin_proxy=True, session="x"
            )
        )
    assert pipeline.proxies == [proxy, proxy, proxy]


def test_unpinned_proxy_rotates_sid(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setenv("GSS_PROXY_SID", "ROTATED1")
    monkeypatch.setattr("gss_api.routers.orders.asyncio.sleep", no_sleep)
    pipeline = _DeadPipeline()
    proxy = "http://u-region-PH-sid-PINNED01:pw@gw.example.com:3010"
    with pytest.raises(RuntimeError):
        asyncio.run(
            _create_checkout_with_retry(
                pipeline, proxy=proxy, pin_proxy=False, session="x"
            )
        )
    assert len(set(pipeline.proxies)) > 1
