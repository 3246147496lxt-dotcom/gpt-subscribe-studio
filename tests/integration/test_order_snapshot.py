"""Only local fixtures: a failed checkout does not roll back a rotated RAM snapshot."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fastapi import HTTPException
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.routers import orders
from gss_api.schemas import OrderCreateRequest
from gss_api.store import store
from gss_core.vault import vault


pytestmark = pytest.mark.usefixtures("verified_us_payer")

@pytest.mark.parametrize("origin", ["US", "JP"])
def test_rotation_is_checkpointed_before_client_close_and_failed_checkout(monkeypatch, origin, tmp_path):
    monkeypatch.setenv("GSS_DATA_DIR", str(tmp_path))
    store.clear()
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    guard = AsyncMock()
    monkeypatch.setattr(orders, "_guard_refresh_egress", guard)
    key = "offline-snapshot-test"
    events = []
    cookie_name = "__Secure-next-auth.session-token"

    def snapshot():
        order = store.find_by_idempotency(key)
        assert order is not None
        item = vault.get(order.id)
        assert item is not None
        snap = json.loads(item.session)
        assert snap["accessToken"] == "new-fixture-access"
        assert snap["cookies"][cookie_name] == snap["sessionToken"] == "new-fixture-cookie"
        return item

    def closed():
        snapshot()
        events.append("close")

    client = SimpleNamespace(
        access_token="new-fixture-access", session_token="old-fixture-cookie",
        device_id="", user_agent="", oai_session_id="",
        extra_cookies={cookie_name: "new-fixture-cookie"}, close=closed, refresh_session=lambda **kwargs: True,
    )
    monkeypatch.setattr(orders.ChatGPTClient, "from_raw_session", lambda *a, **kw: client)
    monkeypatch.setattr(orders.ChatGPTClient, "reauthorize_for_country", lambda *a, **kw: client)

    async def uncertain(**kwargs):
        snapshot()
        events.append("checkout")
        raise TimeoutError("fixture: no upstream reply")

    monkeypatch.setattr(orders, "build_pipeline", lambda: SimpleNamespace(create_checkout=uncertain))
    api = TestClient(create_app())
    submitted = "eyJ-fixture..iv.cipher.tag" if origin == "US" else json.dumps({
        "accessToken": "old-fixture-access", "sessionToken": "old-fixture-cookie",
        "statsigContext": {"country": origin},
    })
    payload = {"plan": "plus", "billing_country": "US", "session": submitted}
    try:
        response = api.post("/api/orders", json=payload, headers={"Idempotency-Key": key})
        assert response.status_code == 400
        saved = snapshot()
        deadline = saved.expires_at
        assert events == ["close", "checkout"]
        repeat = api.post("/api/orders", json=payload, headers={"Idempotency-Key": key})
        assert repeat.json()["status"] == "rejected"
        assert events == ["close", "checkout"]
        assert snapshot().expires_at == deadline
        assert guard.await_count == 1
        for result in (response, repeat):
            assert "new-fixture" not in result.text and "old-fixture" not in result.text
    finally:
        order = store.find_by_idempotency(key)
        if order:
            vault.drop(order.id)
        store.clear()


def test_invalid_plan_details_do_not_consume_session(monkeypatch):
    guard = AsyncMock()
    monkeypatch.setattr(orders, "_guard_refresh_egress", guard)
    api = TestClient(create_app())
    response = api.post("/api/orders", json={"plan": "team", "billing_country": "US",
        "session": json.dumps({"accessToken": "old", "sessionToken": "fixture-only", "statsigContext": {"country": "JP"}})})
    assert response.status_code == 400
    guard.assert_not_called()


def test_inflight_duplicate_does_not_start_another_order(monkeypatch):
    async def exercise():
        entered, finish = asyncio.Event(), asyncio.Event()

        async def pending(*_):
            entered.set()
            await finish.wait()
            return None

        stub = AsyncMock(side_effect=pending)
        monkeypatch.setattr(orders, "_create_order", stub)
        payload = OrderCreateRequest(plan="plus", billing_country="US", session="fixture")
        task = asyncio.create_task(orders.create_order(payload, "in-flight-fixture"))
        await entered.wait()
        try:
            with pytest.raises(HTTPException) as exc:
                await orders.create_order(payload, "in-flight-fixture")
            assert exc.value.status_code == 409
            assert stub.await_count == 1
        finally:
            finish.set()
            await task
        assert "in-flight-fixture" not in orders._creating_keys

    asyncio.run(exercise())
