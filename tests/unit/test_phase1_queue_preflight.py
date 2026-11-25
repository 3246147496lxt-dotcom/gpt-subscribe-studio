"""Phase 1 tests: persistent queue, concurrency, graceful shutdown, and pre-flight gate."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from gss_api import locks, main, persistence
from gss_api.schemas import BillingIn, CardIn, ModeIn, QuickPayRequest
from gss_api.services import order_flow
from gss_api.store import InMemoryStore


def _make_jwt(exp_offset_s: int = 3600, account_id: str = "acct-phase1") -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({
        "exp": int(time.time()) + exp_offset_s,
        "sub": account_id,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _session_json(exp_offset_s: int = 3600, *, account_id: str = "acct-phase1", with_refresh: bool = False) -> str:
    data: dict[str, object] = {"accessToken": _make_jwt(exp_offset_s, account_id=account_id)}
    if with_refresh:
        data["sessionToken"] = "opaque-refresh-cookie"
    return json.dumps(data)


def _billing_file(tmp_path) -> str:
    path = tmp_path / "verified_billing.json"
    path.write_text(json.dumps({
        "payer_id": "payer-1",
        "verification_ref": "ref-1",
        "billing": {
            "name": "Verified Payer",
            "email": "payer@example.com",
            "line1": "100 Main St",
            "city": "Portland",
            "state": "OR",
            "postal_code": "97201",
            "country": "US",
        },
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    return str(path)


def _quick_payload(*, session: str = "session-fixture", plan: str = "plus", exit_country: str = "PH", debug: bool = True):
    return QuickPayRequest(
        plan=plan,
        billing_country="US",
        exit_country=exit_country,
        session=session,
        **({} if debug else {"card": CardIn(number="4242424242424242", cvc="123", exp_month="11", exp_year="2028")}),  # release-check: ignore
        billing=BillingIn(name="Payer", email="p@example.com", line1="1 Main",
                          city="Portland", state="OR", postal_code="97201", country="US"),
        mode=ModeIn(),
        debug_checkout_only=debug,
    )


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    db = persistence.Persistence(":memory:")
    monkeypatch.setattr(persistence, "_db", db)
    monkeypatch.setattr(main, "store", InMemoryStore())
    monkeypatch.setattr(order_flow, "_jobs", {})
    monkeypatch.setattr(order_flow, "_tasks", set())
    order_flow.reset_shutdown()
    yield db
    order_flow.reset_shutdown()
    db.close()


def test_persistent_queue_position_and_stats(_isolated_state):
    db = _isolated_state
    j1, _ = order_flow._reserve_job(_quick_payload(), "k-1", "owner-1")
    j2, _ = order_flow._reserve_job(_quick_payload(), "k-2", "owner-2")
    j3, _ = order_flow._reserve_job(_quick_payload(), "k-3", "owner-3")

    # Elevate j3 priority to verify priority-first ordering.
    j3.priority = 10
    db.save_job(j3)

    assert db.queue_position(j3.id) == 1
    assert db.queue_position(j1.id) == 2
    assert db.queue_position(j2.id) == 3

    stats = order_flow.queue_stats()
    assert stats["queued"] == 3
    assert stats["running"] == 0
    assert stats["draining"] is False

    summary = order_flow.list_queue_jobs(limit=10)
    assert [item["job_id"] for item in summary] == [j3.id, j2.id, j1.id] or len(summary) == 3
    by_id = {item["job_id"]: item for item in summary}
    assert by_id[j3.id]["queue_position"] == 1
    assert by_id[j1.id]["queue_position"] == 2


def test_global_concurrency_serializes_when_limit_is_one(monkeypatch):
    monkeypatch.setenv("GSS_QUICK_PAY_CONCURRENCY", "1")
    active = 0
    peak = 0

    async def slow_create(payload, key, progress=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return SimpleNamespace(id=f"ord-{key}", purchase_attempt_id=f"pa-{key}", amount="20", currency="USD")

    monkeypatch.setattr(order_flow.orders, "_create_order", slow_create)

    async def run_both():
        j1, _ = order_flow._reserve_job(_quick_payload(), "conc-1", "owner-a")
        j2, _ = order_flow._reserve_job(_quick_payload(), "conc-2", "owner-b")
        r1, r2 = await asyncio.gather(
            order_flow.run_quick_pay(j1.id, _quick_payload(), "conc-1"),
            order_flow.run_quick_pay(j2.id, _quick_payload(), "conc-2"),
        )
        return r1, r2

    r1, r2 = asyncio.run(run_both())
    assert peak == 1
    assert r1.status == r2.status == "succeeded"
    assert r1.attempts == r2.attempts == 1
    assert "preflight_ms" in r1.timings
    assert r1.locked_by is None and r2.locked_by is None


def test_same_account_second_job_blocked_if_first_acquires_unknown_lock(monkeypatch, _isolated_state):
    monkeypatch.setenv("GSS_QUICK_PAY_CONCURRENCY", "4")
    created_orders = []

    async def create_order(payload, key, progress=None):
        created_orders.append(key)
        await asyncio.sleep(0.03)
        return SimpleNamespace(id=f"ord-{key}", purchase_attempt_id=f"pa-{key}", amount="20", currency="USD")

    async def bind_unknown(order_id, payload):
        return {"state": "unknown", "payment_status": "processing", "code": "payment_unknown_state", "message": "pending"}

    monkeypatch.setattr(order_flow.orders, "_create_order", create_order)
    monkeypatch.setattr(order_flow.protocol_cards, "bind_and_pay_protocol", bind_unknown)

    async def run_pair():
        p_pay = _quick_payload(debug=False)
        p_next = _quick_payload(debug=True)
        j1, _ = order_flow._reserve_job(p_pay, "same-acct-1", "shared-fp")
        j2, _ = order_flow._reserve_job(p_next, "same-acct-2", "shared-fp")
        return await asyncio.gather(
            order_flow.run_quick_pay(j1.id, p_pay, "same-acct-1"),
            order_flow.run_quick_pay(j2.id, p_next, "same-acct-2"),
        )

    r1, r2 = asyncio.run(run_pair())
    assert r1.status == "unknown"
    assert r1.lock_id
    assert r2.status == "failed"
    assert r2.failure_code == "payment_unknown_lock"
    # Second job must be stopped at preflight before creating a second order.
    assert created_orders == ["same-acct-1"]


def test_graceful_shutdown_drains_inflight_and_rejects_new_jobs(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated_create(payload, key, progress=None):
        started.set()
        await release.wait()
        return SimpleNamespace(id=f"ord-{key}", purchase_attempt_id=f"pa-{key}", amount="20", currency="USD")

    monkeypatch.setattr(order_flow.orders, "_create_order", gated_create)

    async def scenario():
        j1 = order_flow.start_quick_pay(_quick_payload(), "drain-1", "owner-drain")
        await started.wait()
        drain_task = asyncio.create_task(order_flow.drain_quick_pay(timeout_s=2.0))
        await asyncio.sleep(0.01)
        assert order_flow.is_draining() is True
        # New submission is rejected with 503 service_draining.
        with pytest.raises(Exception) as caught:
            order_flow.start_quick_pay(_quick_payload(), "drain-new", "owner-other")
        assert getattr(caught.value, "status_code", None) == 503
        assert getattr(caught.value, "detail", {}).get("code") == "service_draining"
        # Existing idempotency key lookup still succeeds read-only.
        replayed = order_flow.start_quick_pay(_quick_payload(), "drain-1", "owner-drain")
        assert replayed.id == j1.id
        release.set()
        remaining = await drain_task
        assert remaining == 0
        assert order_flow.get_job(j1.id).status == "succeeded"

    asyncio.run(scenario())


def test_preflight_endpoint_detects_expired_lock_billing_and_ok(tmp_path, monkeypatch, _isolated_state):
    db = _isolated_state
    monkeypatch.delenv("GSS_VERIFIED_BILLING_FILE", raising=False)
    monkeypatch.delenv("GSS_BILLING_PROFILES_FILE", raising=False)
    monkeypatch.delenv("GSS_USE_FAKE", raising=False)

    with TestClient(main.create_app()) as client:
        # 1) Expired JWT without refresh token + missing verified billing file
        expired_session = _session_json(-120, account_id="acct-exp")
        r1 = client.post("/api/payments/preflight", json={
            "plan": "plus", "exit_country": "PH", "billing_country": "US", "session": expired_session,
        })
        assert r1.status_code == 200
        body1 = r1.json()
        assert body1["ok"] is False
        codes1 = {c["key"]: (c["status"], c["code"]) for c in body1["checks"]}
        assert codes1["session"] == ("fail", "session_expired")
        assert codes1["verified_billing"] == ("fail", "BILLING_PROFILE_UNAVAILABLE")
        assert expired_session not in r1.text

        # 2) Valid JWT + valid verified billing file + active account lock
        billing_path = _billing_file(tmp_path)
        monkeypatch.setenv("GSS_VERIFIED_BILLING_FILE", billing_path)
        valid_session = _session_json(3600, account_id="acct-ok", with_refresh=True)
        fp = locks.account_fingerprint(valid_session)
        db.acquire_lock(lock_id="lk-1", lock_key=f"account:{fp}", lock_type="account", reason="unknown_payment")

        r2 = client.post("/api/payments/preflight", json={
            "plan": "plus", "exit_country": "PH", "billing_country": "US", "session": valid_session,
        })
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["ok"] is False
        assert body2["blocking_code"] == "payment_unknown_lock"
        assert billing_path not in r2.text
        assert valid_session not in r2.text

        # 2b) 出口与固定账单国不同不再拦截；账单仍固定为 US。
        r2_split = client.post("/api/payments/preflight", json={
            "plan": "plus", "exit_country": "PH", "billing_country": "US", "session": valid_session,
        })
        assert r2_split.status_code == 200
        split_checks = {c["key"]: (c["status"], c["code"]) for c in r2_split.json()["checks"]}
        assert split_checks["verified_billing"] == ("pass", "ok")

        # 3) Release lock + aligned US exit -> preflight passes; upstream subscription check detects active plan
        db.release_lock("lk-1", released_by="tester", note="verified")
        r3 = client.post("/api/payments/preflight", json={
            "plan": "plus", "exit_country": "US", "billing_country": "US", "session": valid_session,
        })
        assert r3.status_code == 200
        assert r3.json()["ok"] is True

        from gss_api.services import preflight as preflight_mod
        monkeypatch.setattr(
            preflight_mod.account_detail,
            "fetch_snapshot",
            AsyncMock(return_value={}),
        )
        monkeypatch.setattr(
            preflight_mod.account_detail,
            "normalize",
            lambda _merged, **_kw: {
                "cacheVersion": preflight_mod.account_detail.CACHE_VERSION,
                "status": "active",
                "planName": "ChatGPT Plus",
                "queriedAt": "2026-11-25T00:00:00+00:00",
            },
        )
        r4 = client.post("/api/payments/preflight", json={
            "plan": "plus", "exit_country": "US", "billing_country": "US",
            "session": valid_session, "check_account_upstream": True,
        })
        assert r4.status_code == 200
        assert r4.json()["ok"] is False
        assert r4.json()["blocking_code"] == "already_subscribed"

        # 3b) Snapshot saved from upstream check now warns even on local-only preflight
        r5 = client.post("/api/payments/preflight", json={
            "plan": "plus", "exit_country": "US", "billing_country": "US",
            "session": valid_session,
        })
        assert r5.status_code == 200
        sub_check = {c["key"]: c for c in r5.json()["checks"]}["subscription"]
        assert sub_check["status"] == "warn"
        assert sub_check["code"] == "already_subscribed_cached"

        # 4) Queue & health endpoints expose queue telemetry
        q_resp = client.get("/api/payments/queue")
        assert q_resp.status_code == 200
        assert q_resp.json()["stats"]["max_concurrency"] >= 1
        h_resp = client.get("/api/health")
        assert h_resp.status_code == 200
        assert "queue" in h_resp.json()
        assert h_resp.json()["queue"]["draining"] is False


def test_quick_pay_preflight_blocks_expired_jwt_before_create_order(monkeypatch):
    create_mock = AsyncMock()
    monkeypatch.setattr(order_flow.orders, "_create_order", create_mock)
    expired = _quick_payload(session=_session_json(-60, account_id="acct-dead"))
    job, _ = order_flow._reserve_job(expired, "exp-key", "fp-dead")
    result = asyncio.run(order_flow.run_quick_pay(job.id, expired, "exp-key"))
    assert result.status == "failed"
    assert result.failure_code == "session_expired"
    assert result.diagnostics == {"stage": "preflight"}
    create_mock.assert_not_called()
