"""Create-and-charge is an at-most-once operation; exercise it with a lost payment reply."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.routers import orders
from gss_api.store import store
from gss_core.vault import vault


@pytest.fixture
def attempt(monkeypatch, tmp_path):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    monkeypatch.setenv("GSS_DATA_DIR", str(tmp_path))
    store.clear()
    pipeline = orders.build_pipeline()
    payment = AsyncMock(side_effect=TimeoutError("lost test response"))
    monkeypatch.setattr(pipeline, "pay", payment)
    monkeypatch.setattr(orders, "build_pipeline", lambda: pipeline)
    with TestClient(create_app()) as api:
        yield api, payment, {"plan": "plus", "billing_country": "US",
                             "session": "private-session-fixture",
                             "payment_method_token": "pm_private_fixture"}
    for oid in list(store.orders):
        vault.drop(oid)
    store.clear()


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": "  "}])
def test_create_and_charge_without_stable_key_never_dispatches(attempt, headers):
    api, payment, body = attempt
    responses = [api.post("/api/orders", json=body, headers=headers) for _ in range(2)]
    assert payment.await_count == 0
    assert not store.orders
    assert all(r.status_code == 400 for r in responses)


def test_same_key_reads_original_unknown_and_rejects_different_purchase(attempt):
    api, payment, body = attempt
    headers = {"Idempotency-Key": "fixed-purchase-7"}
    first = api.post("/api/orders", json=body, headers=headers)
    second = api.post("/api/orders", json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["status"] == second.json()["status"] == "unknown"
    assert first.json()["id"] == second.json()["id"]
    changed = api.post("/api/orders", json={**body, "session": "unrelated-session-fixture"}, headers=headers)
    assert changed.status_code == 409
    assert first.json()["id"] not in changed.text
    payment.assert_awaited_once()


def test_lost_in_memory_order_never_reopens_durable_charge_key(attempt, tmp_path):
    api, payment, body = attempt
    headers = {"Idempotency-Key": "fixed-purchase-private-header"}
    first = api.post("/api/orders", json=body, headers=headers)
    assert first.status_code == 201
    vault.drop(first.json()["id"])
    store.clear()  # restarted worker: no session, request signature or receipt in RAM
    replay = api.post("/api/orders", json=body, headers=headers)
    payment.assert_awaited_once()
    assert replay.status_code == 409
    blob = b"".join(f.read_bytes() for f in tmp_path.rglob("*") if f.is_file())
    assert blob
    # An operator can reconcile the order by UUID; the HTTP conflict doesn't
    # disclose a financial result to a caller we can no longer authenticate.
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(tmp_path / "purchase-claims.sqlite3")) as ledger:
        assert ledger.execute("SELECT order_id, attempt_state FROM purchase_claim").fetchall() == [
            (first.json()["id"], "unknown"),
        ]
    for secret in (body["session"], body["payment_method_token"], headers["Idempotency-Key"]):
        assert secret.encode() not in blob


def test_separate_connections_cannot_both_claim_the_same_purchase(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from gss_api import purchase_claims

    start = Barrier(2)

    def reserve(worker):
        start.wait(timeout=5)
        return purchase_claims.reserve(tmp_path, "one-stable-business-key", f"order-{worker}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, (1, 2))) == [False, True]


def test_unavailable_ledger_prevents_payment(attempt, tmp_path, monkeypatch):
    api, payment, body = attempt
    target = tmp_path / "not-a-directory"
    target.touch()
    monkeypatch.setenv("GSS_DATA_DIR", str(target))
    r = api.post("/api/orders", json=body, headers={"Idempotency-Key": "unique-operation"})
    assert r.status_code == 503
    payment.assert_not_awaited()
    assert not store.orders

def test_persisted_dispatch_is_not_a_retryable_lease(tmp_path):
    from gss_api import purchase_claims

    assert purchase_claims.reserve(tmp_path, "one-purchase", "order-1", includes_payment=True)
    assert purchase_claims.begin_payment(tmp_path, "order-1")
    # Crash at the provider boundary: no response and no outcome update.
    assert not purchase_claims.reserve(tmp_path, "one-purchase", "order-2", includes_payment=True)
    assert not purchase_claims.begin_payment(tmp_path, "order-1")

def test_overlapping_create_replay_cannot_dispatch_twice(attempt):
    import asyncio
    from httpx import ASGITransport, AsyncClient

    sync_api, payment, body = attempt

    async def exercise():
        invoked, release = asyncio.Event(), asyncio.Event()

        async def no_reply(**kwargs):
            invoked.set()
            await release.wait()
            raise TimeoutError()

        payment.side_effect = no_reply
        headers = {"Idempotency-Key": "same-inflight-purchase"}
        async with AsyncClient(transport=ASGITransport(app=sync_api.app), base_url="http://test") as api:
            async def post():
                return await api.post("/api/orders", json=body, headers=headers)
            first = asyncio.create_task(post())
            try:
                await asyncio.wait_for(invoked.wait(), timeout=3)
                assert (await post()).status_code == 409
            finally:
                release.set()
            result = await first
            assert result.json()["status"] == "unknown"
            assert (await post()).json()["id"] == result.json()["id"]
            payment.assert_awaited_once()

    asyncio.run(exercise())
