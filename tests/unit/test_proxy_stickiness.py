"""重签发后建单重试不得更换出口 SID。"""

from __future__ import annotations

import asyncio

import pytest

from gss_api.routers.orders import _create_checkout_once
from gss_contracts.errors import DomainError


class _DeadPipeline:
    def __init__(self):
        self.proxies = []

    async def create_checkout(self, *, proxy, **kwargs):
        self.proxies.append(proxy)
        raise RuntimeError("SSL: UNEXPECTED_EOF_WHILE_READING")


def test_ambiguous_checkout_is_never_repeated_or_rerouted():
    pipeline = _DeadPipeline()
    proxy = "http://proxy.example.invalid:3128"
    with pytest.raises(DomainError, match="不自动重试"):
        asyncio.run(_create_checkout_once(pipeline, proxy=proxy, session="fixture"))
    assert pipeline.proxies == [proxy]
