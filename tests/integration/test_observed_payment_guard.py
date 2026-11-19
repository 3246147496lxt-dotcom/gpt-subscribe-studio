"""同单付款观察与 Vault 生命周期；所有提供方调用均为本地桩。"""
import asyncio
import importlib
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from gss_api.routers import orders
from gss_api.schemas import OrderPayRequest
from gss_api.store import store
from gss_contracts import PaymentResult, PaymentStatus
from gss_core.vault import vault
from test_team_hosted_api import BILLING, _open_for_first_payment, setup_team


@pytest.mark.parametrize("observation", [
    {"payment_intent": {"id": "pi_in_flight", "object": "payment_intent", "status": "processing"}},
    {"status": "expired"},
])
def test_hosted_observation_blocks_queued_payment(setup_team, monkeypatch, observation):
    client, oid, _ = setup_team
    url = f"/api/orders/{oid}"
    assert client.post(url + "/team/prepare", json={"billing": BILLING}).status_code == 200
    entered, release = threading.Event(), threading.Event()

    def poll(self, **kw):
        entered.set()
        assert release.wait(3)
        return observation

    monkeypatch.setattr(orders.TeamHostedClient, "poll", poll)
    pay = AsyncMock(return_value=PaymentResult(status=PaymentStatus.PROCESSING))
    monkeypatch.setattr(orders, "build_pipeline", lambda: SimpleNamespace(pay=pay))

    async def check():
        first = asyncio.create_task(orders.poll_team(oid))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            queued = asyncio.create_task(orders.pay_order(oid, OrderPayRequest(
                billing=BILLING, team_context_id="fixture-context", payment_method_token="pm_fixture")))
            await asyncio.sleep(0)
        finally:
            release.set()
        result, duplicate = await asyncio.gather(first, queued, return_exceptions=True)
        pay.assert_not_awaited()
        assert result.status in {"unknown", "paying"}
        assert result.payment_status == "processing"
        assert isinstance(duplicate, HTTPException) and duplicate.status_code == 409
    try:
        asyncio.run(check())
    finally:
        vault.drop(oid)


def test_receipt_not_credentials_survives_inflight_expiry(setup_team, monkeypatch):
    client, oid, _ = setup_team
    url = f"/api/orders/{oid}"
    assert client.post(url + "/team/prepare", json={"billing": BILLING}).status_code == 200
    clock = importlib.import_module("gss_core.vault")
    deadline = vault.get(oid).expires_at
    verify = AsyncMock(side_effect=AssertionError("expired credentials reused"))
    def pay_reply(**kwargs):
        monkeypatch.setattr(clock, "_utcnow", lambda: deadline + timedelta(seconds=1))
        assert vault.get(oid) is None
        # The channel has a settled receipt even though the lookup credential just expired.
        return orders.hosted_payment_result({
            "session_id": store.view(oid).checkout_session_id,
            "payment_status": "paid", "amount_total": 115000, "currency": "egp",
            "payment_intent": {"id": "pi_settled", "object": "payment_intent",
                               "status": "succeeded", "amount_received": 115000, "currency": "egp"},
        }, store.view(oid).checkout_session_id, "")
    monkeypatch.setattr(orders, "build_pipeline", lambda: SimpleNamespace(pay=AsyncMock(side_effect=pay_reply), verify=verify))
    _open_for_first_payment(monkeypatch)
    paid = client.post(url + "/pay", json={"billing": BILLING, "team_context_id": "fixture-context",
                                          "payment_method_token": "pm_fixture"})
    verify.assert_not_awaited()
    assert paid.status_code == 200, paid.text
    r = client.post(url + "/verify")
    assert r.status_code == 200, r.text
    assert r.json()["receipt"]["payment_status"] == "succeeded"
    assert r.json()["status_summary"] == "已扣款，待核验"
    verify.assert_not_awaited()


def test_first_local_team_payment_checks_for_an_external_attempt(setup_team, monkeypatch):
    client, oid, _ = setup_team
    url = f"/api/orders/{oid}"
    assert client.post(url + "/team/prepare", json={"billing": BILLING}).status_code == 200
    # There has been no local /poll. The hosted page has independently submitted payment.
    monkeypatch.setattr(orders.TeamHostedClient, "poll", lambda self, **kw: {
        "session_id": kw["session_id"], "payment_intent": {
            "id": "pi_external", "object": "payment_intent", "status": "processing"}})
    pay = AsyncMock(return_value=PaymentResult(status=PaymentStatus.PROCESSING))
    monkeypatch.setattr(orders, "build_pipeline", lambda: SimpleNamespace(pay=pay))
    res = client.post(url + "/pay", json={"billing": BILLING, "team_context_id": "fixture-context",
                                         "payment_method_token": "pm_fixture"})
    pay.assert_not_awaited()
    assert res.json()["status"] == "paying"
    assert res.json()["payment_status"] == "processing"
