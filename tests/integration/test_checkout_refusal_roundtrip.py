"""One synthetic checkout refusal through real HTTP error parsing, DB and public polling."""
import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gss_api import persistence
from gss_api.routers import orders, quick_pay
from gss_api.schemas import QuickPayRequest
from gss_api.services import order_flow
from gss_api.store import InMemoryStore
from gss_core import chatgpt


_RAY = "a1b2c3d4e5f60718-ORD"
_RID = "req_0123456789abcdef0123456789abcdef"
_BLOCKED_CODE = "private-code-marker-never-emit"
_BLOCKED_RID = "private-header-marker-never-emit"


@pytest.mark.parametrize(
    ("status", "wire_code", "wire_request_id", "source", "code_state", "rid_state", "local_code"),
    [
        pytest.param(403, "unusual_activity", _RID, "structured_code", "accepted", "accepted",
                     "unusual_activity", id="code-and-body-structured-wins"),
        pytest.param(400, None, None, "body_match", "not_found", "not_found",
                     "unusual_activity", id="missing-code-and-header"),
        pytest.param(400, _BLOCKED_CODE, _BLOCKED_RID, "body_match", "filtered", "filtered",
                     "unusual_activity", id="discard-values-but-preserve-rejection-states"),
        pytest.param(401, "token_revoked", None, None, "accepted", "not_found",
                     "invalid_session", id="session-refusal-has-no-unusual-activity-origin"),
        pytest.param(401, "token_revoked", _RID, None, "accepted", "accepted",
                     "invalid_session", id="session-refusal-all-four-fields"),
    ],
)
def test_checkout_refusal_survives_cache_loss_and_sqlite_reopen(
    tmp_path, monkeypatch, verified_us_payer, caplog, status, wire_code, wire_request_id,
    source, code_state, rid_state, local_code,
):
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "offline.db"
    monkeypatch.setenv("GSS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GSS_USE_FAKE", "0")  # exercise the actual checkout adapter
    monkeypatch.setenv("GSS_ROUTE_PROFILE", "A")
    monkeypatch.setenv("GSS_CHECKOUT_ROTATE", "1")
    monkeypatch.delenv("GSS_BILLING_PROFILES_FILE", raising=False)
    monkeypatch.setattr(order_flow, "_jobs", {})
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
    canary = "private-body-marker-never-emit"
    session_canary = "offline-private-session-marker"
    cookie_canary = "private-cookie-marker"
    pan, cvc = "4242" * 4, "CVCX"  # synthetic response-body leak canaries
    error = {"message": "Unusual activity: " + canary, "private_extra": [pan, cvc]}
    headers = {"CF-Ray": _RAY, "set-cookie": "name=" + cookie_canary}
    if wire_code is not None:
        error["code"] = wire_code
    if wire_request_id is not None:
        headers["X-Request-Id"] = wire_request_id
    response = httpx.Response(status, json={"error": error}, headers=headers,
                              request=httpx.Request("POST", "https://checkout.invalid/test"))
    post = Mock(side_effect=[response])  # another POST fails, even before call-count assertions
    monkeypatch.setattr(chatgpt.ChatGPTClient, "http", property(lambda _: SimpleNamespace(post=post, headers={})))
    payload = QuickPayRequest(plan="plus", billing_country="US", exit_country="US", debug_checkout_only=True,
                              session=json.dumps({"accessToken": session_canary}))
    expected = {"stage": "creating_checkout", "upstream_status": status, "cf_ray": _RAY, "upstream_code_state": code_state,
                "request_id_state": rid_state}
    if source is not None:
        expected["unusual_activity_source"] = source
    if code_state == "accepted":
        expected["upstream_code"] = wire_code
    if rid_state == "accepted":
        expected["request_id"] = wire_request_id
    fingerprint = "synthetic-owner"
    monkeypatch.setattr(order_flow.locks, "account_fingerprint", lambda _session: fingerprint)
    key = "offline-first-refusal"
    db = persistence.Persistence(str(path))
    monkeypatch.setattr(persistence, "_db", db)
    store.attach_db(db)
    try:
        job, created = order_flow._reserve_job(payload, key, fingerprint)
        assert created
        job = asyncio.run(order_flow.run_quick_pay(job.id, payload, key))
        assert job.status == "failed" and job.diagnostics == expected and job.order_id
        assert post.call_count == 1 and post.call_args.args[0].endswith("/backend-api/payments/checkout")
        rotate.assert_not_called()
        pay.assert_not_called()
        lock.assert_not_called()
        assert db._conn.execute("select count(*) from account_locks").fetchone()[0] == 0
        first = json.loads(db.load_jobs()[0]["diagnostics_json"])
        assert first == expected
        # Simulate a stale persistence writer, not another request. It must neither
        # replace this snapshot nor backfill an originally absent/filtered value.
        db.save_job(replace(job, diagnostics={
            "upstream_status": 429, "upstream_code": "rate_limit_error",
            "upstream_code_state": "accepted", "request_id": _RID, "request_id_state": "accepted",
        }))
        assert json.loads(db.load_jobs()[0]["diagnostics_json"]) == first
    finally:
        db.close()
    order_flow._jobs.clear()
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
        assert body["checkout_outcome"] == "rejected_before_payment"
        assert not body["lock_id"] and body["proxy_rotations"] == 0
        assert body["events"][-1]["state"] == "failed"
        assert fresh.load_orders()[0][1].failure_code == local_code
        exported = result.text + caplog.text + "\n".join(fresh._conn.iterdump())
        assert all(x not in exported for x in
                   (canary, cookie_canary, session_canary, _BLOCKED_CODE, _BLOCKED_RID, pan, cvc))
    finally:
        fresh.close()


@pytest.mark.parametrize("legacy", [
    {},
    {"upstream_status": 400, "cf_ray": _RAY},
    {"upstream_status": 403, "upstream_code": "unusual_activity", "request_id": _RID},
], ids=["empty", "partial", "values-only"])
def test_poll_does_not_infer_field_states_for_pre_upgrade_rows(tmp_path, monkeypatch, legacy):
    path = tmp_path / "historical.db"
    job = order_flow.QuickPayJob(id="historical", idempotency_key="test-only", status="failed",
                                 stage="failed", failure_code="checkout_upstream_error")
    raw = json.dumps(legacy)
    old = persistence.Persistence(str(path))
    try:
        old.save_job(job)
        # Inject the serialized pre-upgrade representation instead of exercising
        # the new writer, including the cases of an empty or partial diagnostic.
        with old._conn:
            old._conn.execute("UPDATE quick_pay_jobs SET diagnostics_json=? WHERE id=?", (raw, job.id))
    finally:
        old.close()
    monkeypatch.setattr(order_flow, "_jobs", {})
    fresh = persistence.Persistence(str(path))
    monkeypatch.setattr(persistence, "_db", fresh)
    try:
        app = FastAPI()
        app.include_router(quick_pay.router)
        with TestClient(app) as client:
            result = client.get("/api/payments/quick-pay/" + job.id)
        assert result.status_code == 200
        assert result.json()["diagnostics"] == legacy
        assert fresh.load_jobs()[0]["diagnostics_json"] == raw
    finally:
        fresh.close()
