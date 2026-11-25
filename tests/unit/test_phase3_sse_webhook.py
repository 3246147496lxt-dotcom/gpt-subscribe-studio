"""Phase 3 tests: QuickPay SSE stream, requires_action handoff, and Webhook alerts."""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import os
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from gss_api import locks, main, persistence
from gss_api.routers import events as events_router, orders as orders_router
from gss_api.schemas import BillingIn, CardIn, ModeIn, QuickPayRequest
from gss_api.services import order_flow, reconciler, webhooks
from gss_api.store import InMemoryStore, OrderView
from gss_contracts import Mode, Order, OrderStatus, Plan
from gss_core.vault import OrderSecrets, vault


@pytest.fixture(autouse=True)
def _isolated_phase3(tmp_path, monkeypatch):
    db = persistence.Persistence(":memory:")
    store = InMemoryStore()
    store.attach_db(db)
    monkeypatch.setattr(persistence, "_db", db)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(orders_router, "store", store)
    monkeypatch.setattr(locks, "store", store)
    monkeypatch.setattr(order_flow, "_jobs", {})
    monkeypatch.setattr(order_flow, "_tasks", set())
    monkeypatch.setenv("GSS_RECONCILER_ENABLED", "0")
    monkeypatch.setenv("GSS_CONFIG_DIR", str(tmp_path / "cfg"))
    mgr = webhooks.WebhookManager(tmp_path / "cfg" / "webhooks.json")
    monkeypatch.setattr(webhooks, "manager", mgr)
    from gss_api.routers import webhooks as webhooks_router
    monkeypatch.setattr(webhooks_router, "manager", mgr)
    order_flow.reset_shutdown()
    yield db, store, mgr
    order_flow.reset_shutdown()
    db.close()


def _pay_payload():
    return QuickPayRequest(
        plan="plus",
        billing_country="US",
        exit_country="US",
        session="session-fixture",
        card=CardIn(number="4242424242424242", cvc="123", exp_month="11", exp_year="2028"),  # release-check: ignore
        billing=BillingIn(name="Payer", email="p@example.com", line1="1 Main",
                          city="Portland", state="OR", postal_code="97201", country="US"),
        mode=ModeIn(),
        debug_checkout_only=False,
    )


def test_quick_pay_sse_emits_init_and_live_updates_until_terminal(_isolated_phase3):
    db, _store, _mgr = _isolated_phase3
    job = order_flow.QuickPayJob(
        id="job-sse-1",
        idempotency_key="idem-sse-1",
        status="running",
        stage="creating_checkout",
    )
    db.save_job(job)
    order_flow._jobs[job.id] = job

    async def collect():
        gen = events_router._quick_pay_stream(job.id, {"job_id": job.id, "status": "running", "stage": "creating_checkout"})
        first = await anext(gen)
        events_router.publish_quick_pay(job.id, {"job_id": job.id, "status": "running", "stage": "confirming_payment"})
        second = await anext(gen)
        events_router.publish_quick_pay(job.id, {"job_id": job.id, "status": "succeeded", "stage": "succeeded"})
        third = await anext(gen)
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
        return first, second, third

    c1, c2, c3 = asyncio.run(collect())
    p1 = json.loads(c1.removeprefix("data: ").strip())
    p2 = json.loads(c2.removeprefix("data: ").strip())
    p3 = json.loads(c3.removeprefix("data: ").strip())
    assert p1["type"] == "init" and p1["job"]["stage"] == "creating_checkout"
    assert p2["type"] == "update" and p2["job"]["stage"] == "confirming_payment"
    assert p3["type"] == "update" and p3["job"]["status"] == "succeeded"

    # Terminal job closes right after init frame via HTTP SSE endpoint.
    job.status = "succeeded"
    job.stage = "succeeded"
    db.save_job(job)
    with TestClient(main.create_app()) as client:
        resp = client.get(f"/api/events/quick-pay/{job.id}/stream")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        frame = json.loads(resp.text.strip().removeprefix("data: "))
        assert frame["type"] == "init" and frame["job"]["status"] == "succeeded"


def test_requires_action_handoff_sanitizes_url_notifies_and_reconciles(monkeypatch, _isolated_phase3):
    db, store, _mgr = _isolated_phase3
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(webhooks, "dispatch_event", lambda ev, data: emitted.append((ev, data)) or True)

    async def create_order(payload, key, progress=None):
        order = Order(
            id="ord-3ds",
            idempotency_key=key,
            purchase_attempt_id="pa-3ds",
            plan=Plan.PLUS,
            billing_country="US",
            mode=Mode(),
            status=OrderStatus.REQUIRES_ACTION,
            amount=Decimal("20.00"),
        )
        view = OrderView(
            purchase_attempt_id="pa-3ds",
            account_fingerprint="fp-3ds",
            exit_country="US",
            currency="USD",
            amount="20.00",
            payment_status="processing",
        )
        store.orders[order.id] = order
        store.views[order.id] = view
        store.persist(order.id)
        return SimpleNamespace(id="ord-3ds", purchase_attempt_id="pa-3ds", amount="20.00", currency="USD")

    async def bind_requires_action(order_id, payload):
        return {
            "state": "requires_action",
            "payment_status": "requires_action",
            "next_action": {
                "type": "redirect_to_url",
                "redirect_url": "https://hooks.stripe.com/3ds/verify/sample",
                "message": "请完成 3DS 验证",
            },
            "message": "需要完成发卡行 3DS 验证",
        }

    monkeypatch.setattr(order_flow.orders, "_create_order", create_order)
    monkeypatch.setattr(order_flow.protocol_cards, "bind_and_pay_protocol", bind_requires_action)

    # Confirm non-https URLs are stripped by sanitizer.
    assert order_flow._safe_next_action({"type": "x", "redirect_url": "javascript:alert(1)"}) == {"type": "x"}

    job, _ = order_flow._reserve_job(_pay_payload(), "key-3ds", "fp-3ds")
    res_job = asyncio.run(order_flow.run_quick_pay(job.id, _pay_payload(), "key-3ds"))
    assert res_job.status == "requires_action"
    assert res_job.stage == "requires_action"
    assert res_job.next_action["redirect_url"] == "https://hooks.stripe.com/3ds/verify/sample"
    assert emitted and emitted[-1][0] == "requires_action"
    assert emitted[-1][1]["next_action_url"] == "https://hooks.stripe.com/3ds/verify/sample"

    # Simulate user finishing 3DS in browser -> read-only reconciler resolves job to succeeded.
    vault.put("ord-3ds", OrderSecrets(session="synthetic-session"))

    async def fake_probe(oid):
        ord_obj = store.get_order(oid)
        ord_view = store.view(oid)
        ord_obj.status = OrderStatus.DELIVERED
        ord_view.payment_status = "succeeded"
        ord_view.delivery_status = "delivered"
        store.persist(oid)
        return {"state": "paid"}

    monkeypatch.setattr(reconciler.protocol_cards, "get_bind_protocol_status", fake_probe)
    try:
        rec = asyncio.run(reconciler.reconcile_job(job.id, force=True))
    finally:
        vault.drop("ord-3ds")

    assert rec["outcome"] == "resolved_delivered"
    assert rec["job_status"] == "succeeded"
    assert order_flow.get_job(job.id).next_action is None
    assert emitted[-1][0] == "reconcile_resolved"


def test_webhook_config_ssrf_signing_redaction_and_test_delivery(monkeypatch, _isolated_phase3):
    _db, _store, mgr = _isolated_phase3
    captured: list[tuple[str, bytes, dict]] = []

    class DummyClient:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, content, headers):
            captured.append((url, content, dict(headers)))
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(httpx, "AsyncClient", DummyClient)

    # Payload redacts accidental sensitive patterns and strips unknown keys.
    payload = mgr.build_payload("unknown", {
        "job_id": "j-1",
        "order_id": "o-1",
        "status": "unknown",
        "message": "authorization: Bearer supersecret123456",
        "session": "should-never-appear",
        "card": "4242424242424242",  # release-check: ignore
    })
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "supersecret123456" not in dumped
    assert "should-never-appear" not in dumped
    assert "4242424242424242" not in dumped  # release-check: ignore

    with TestClient(main.create_app()) as client:
        # SSRF & scheme rejections
        for bad_url in (
            "http://example.com/hook",
            "file:///etc/passwd",
            "https://169.254.169.254/latest/meta-data",
        ):
            r_bad = client.put("/api/settings/webhooks", json={"enabled": True, "url": bad_url})
            assert r_bad.status_code == 400

        # Valid HTTPS + secret saves as 0600 and never echoes secret
        r_save = client.put("/api/settings/webhooks", json={
            "enabled": True,
            "url": "https://hooks.example.com/services/abc123456789?token=secretquery",
            "secret": "my-hmac-signing-key",
            "events": ["succeeded", "unknown", "requires_action"],
        })
        assert r_save.status_code == 200
        cfg = r_save.json()
        assert cfg["enabled"] is True
        assert cfg["has_secret"] is True
        assert "my-hmac-signing-key" not in r_save.text
        assert "secretquery" not in r_save.text
        assert stat.S_IMODE(os.stat(mgr._path()).st_mode) == 0o600

        # Test delivery signs body with HMAC-SHA256 and logs delivery history
        r_test = client.post("/api/settings/webhooks/test")
        assert r_test.status_code == 200
        assert r_test.json()["ok"] is True
        assert r_test.json()["status_code"] == 200
        assert len(captured) == 1
        sent_url, sent_body, sent_headers = captured[0]
        assert sent_url.startswith("https://hooks.example.com/")
        expected_sig = mgr.sign_body("my-hmac-signing-key", sent_body)
        assert sent_headers["X-GSS-Signature-256"] == expected_sig
        assert sent_headers["X-GSS-Event"] == "webhook_test"

        r_get = client.get("/api/settings/webhooks")
        assert r_get.status_code == 200
        assert len(r_get.json()["deliveries"]) == 1
        assert r_get.json()["deliveries"][0]["ok"] is True

        r_health = client.get("/api/health")
        assert r_health.status_code == 200
        assert r_health.json()["webhooks"]["enabled"] is True
