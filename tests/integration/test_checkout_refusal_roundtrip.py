"""One synthetic checkout refusal through real HTTP error parsing, DB and public polling."""
import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gss_api import persistence
from gss_api.routers import orders, quick_pay
from gss_api.schemas import CardIn, QuickPayRequest
from gss_api.services import order_flow
from gss_api.store import InMemoryStore
from gss_core import chatgpt


def test_checkout_refusal_survives_cache_loss_and_sqlite_reopen(tmp_path, monkeypatch, verified_us_payer, caplog):
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "offline.db"
    monkeypatch.setenv("GSS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GSS_USE_FAKE", "0")  # exercise the actual checkout adapter
    monkeypatch.setenv("GSS_ROUTE_PROFILE", "A")
    monkeypatch.setenv("GSS_CHECKOUT_ROTATE", "1")
    monkeypatch.delenv("GSS_BILLING_PROFILES_FILE", raising=False)
    monkeypatch.setattr(order_flow, "_jobs", {})
    monkeypatch.setattr(order_flow, "_by_key", {})
    store = InMemoryStore()
    monkeypatch.setattr(orders, "store", store)
    monkeypatch.setattr(orders, "vault", Mock())
    monkeypatch.setattr(orders, "checkout_proxy_for", lambda *a, **kw: "http://unused.invalid")
    rotate, pay, lock = Mock(), AsyncMock(), Mock()
    monkeypatch.setattr(orders, "rotate_proxy_sid", rotate)
    monkeypatch.setattr(order_flow.protocol_cards, "bind_and_pay_protocol", pay)
    monkeypatch.setattr(order_flow.locks, "hold_order_unknown", lock)

    # No browser, challenge or login call is part of this regression. No real
    # HTTP client can be constructed: a local, single-response object is used.
    def no_network(*a, **kw):
        raise AssertionError("the diagnostic test attempted to open a network session")
    monkeypatch.setattr(chatgpt, "_new_session", no_network)
    monkeypatch.setattr(chatgpt.ChatGPTClient, "prepare_risk", lambda *a, **kw: None)
    monkeypatch.setattr(chatgpt.ChatGPTClient, "hydrate", lambda *a, **kw: None)
    ray = "a1b2c3d4e5f60718-ORD"
    rid = "req_0123456789abcdef0123456789abcdef"
    canary = "private-body-marker-never-emit"
    session_canary = "offline-private-session-marker"
    cookie_canary = "private-cookie-marker"
    response = httpx.Response(403, json={"error": {"code": "unusual_activity", "message": canary}},
                              headers={"CF-Ray": ray, "X-Request-Id": rid, "set-cookie": "name=" + cookie_canary})
    post = Mock(side_effect=[response])  # another POST fails, even before call-count assertions
    monkeypatch.setattr(chatgpt.ChatGPTClient, "http", property(lambda _: SimpleNamespace(post=post, headers={})))
    pan, cvc = "4242" * 4, "CVCX"  # inert synthetic markers, not card credentials
    payload = QuickPayRequest(plan="plus", billing_country="US", exit_country="US", debug_checkout_only=True,
                              card=CardIn(number=pan, cvc=cvc, exp_month="11", exp_year="2030"),
                              session=json.dumps({"accessToken": session_canary}))
    expected = {"upstream_status": 403, "upstream_code": "unusual_activity", "cf_ray": ray, "request_id": rid}
    key = "offline-first-refusal"
    db = persistence.Persistence(str(path))
    monkeypatch.setattr(persistence, "_db", db)
    store.attach_db(db)
    try:
        job = order_flow._new_job(key)
        job = asyncio.run(order_flow.run_quick_pay(job.id, payload, key))
        assert job.status == "failed" and job.diagnostics == expected and job.order_id
        assert post.call_count == 1 and post.call_args.args[0].endswith("/backend-api/payments/checkout")
        rotate.assert_not_called()
        pay.assert_not_called()
        lock.assert_not_called()
        assert db._conn.execute("select count(*) from account_locks").fetchone()[0] == 0
    finally:
        db.close()
    order_flow._jobs.clear()
    order_flow._by_key.clear()
    fresh = persistence.Persistence(str(path))
    monkeypatch.setattr(persistence, "_db", fresh)
    try:
        app = FastAPI()
        app.include_router(quick_pay.router)
        with TestClient(app) as client:
            result = client.get("/api/payments/quick-pay/" + job.id)
        assert result.status_code == 200
        body = result.json()
        assert body["status"] == "failed" and body["diagnostics"] == expected and body["order_id"] == job.order_id
        assert not body["lock_id"] and body["proxy_rotations"] == 0
        assert body["events"][-1]["state"] == "failed"
        assert fresh.load_orders()[0][1].failure_code == "unusual_activity"
        exported = result.text + caplog.text + "\n".join(fresh._conn.iterdump())
        assert all(x not in exported for x in (canary, cookie_canary, session_canary, pan, cvc))
    finally:
        fresh.close()
