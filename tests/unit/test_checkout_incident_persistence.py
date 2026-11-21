"""Offline, synthetic checkout failure diagnostics; no payment request or credentials."""
import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from gss_api.persistence import Persistence
from gss_api.routers import quick_pay
from gss_api.schemas import ModeIn, QuickPayRequest
from gss_api.services import order_flow as flow
from gss_contracts.errors import DomainError, ErrorCode


SNAPSHOT = {
    "upstream_status": 403, "upstream_code": "unusual_activity",
    "cf_ray": "a1bcde98a7cdef12-SJC", "request_id": "req_" + "ab1dc8ef" * 4,
}
MARKER = "never-export-me@example.invalid"


@pytest.fixture
def db(monkeypatch):
    db = Persistence(":memory:")
    monkeypatch.setattr(flow, "get_db_optional", lambda: db)
    monkeypatch.setattr(quick_pay, "get_db_optional", lambda: db)
    monkeypatch.setattr(flow, "_jobs", {})
    yield db
    db._conn.close()


@pytest.mark.parametrize("shape", ["domain", "http_nested", "http_flat"])
def test_failure_before_payment_has_same_safe_projection_after_cache_loss(db, monkeypatch, shape):
    job, _ = flow._reserve_job(QuickPayRequest(plan="plus", session="offline", debug_checkout_only=True),
                               "offline-key", "test-owner")
    raw = {**SNAPSHOT, "cookie": MARKER}
    oid = str(uuid.uuid4())
    monkeypatch.setattr(flow.orders.store, "get_order", lambda i: (
        SimpleNamespace(idempotency_key=job.idempotency_key) if i == oid else None))

    async def reject(*_a, progress, **_kw):
        progress("creating_checkout")
        if shape == "domain":
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "已拒绝", raw)
        detail = {"code": "checkout_upstream_error", **({"detail": raw} if shape == "http_nested" else raw)}
        error = HTTPException(502, detail=detail)
        error._checkout_order_id = oid
        raise error

    def forbidden(*_a, **_kw):
        pytest.fail("must not bind, lock or confirm on a checkout error")

    monkeypatch.setattr(flow.orders, "_create_order", reject)
    monkeypatch.setattr(flow.protocol_cards, "bind_and_pay_protocol", forbidden)
    monkeypatch.setattr(flow.locks, "hold_order_unknown", forbidden)
    payload = SimpleNamespace(plan="plus", billing_country="US", exit_country="US", session="offline",
        mode=ModeIn(), promo_code="", promo_campaign_id="",
        workspace_name="", seat_quantity=None, price_interval="month", debug_checkout_only=True)
    result = asyncio.run(flow.run_quick_pay(job.id, payload, job.idempotency_key))
    assert result.status == "failed" and result.diagnostics == SNAPSHOT
    assert MARKER not in repr(db.load_jobs()) and MARKER not in repr(db.list_events(job_id=job.id))
    flow._jobs.clear()
    out = asyncio.run(quick_pay.quick_pay_status(job.id))
    assert out.diagnostics == SNAPSHOT and out.status == "failed"
    assert out.order_id == (None if shape == "domain" else oid)
    assert flow.get_job_by_key(job.idempotency_key).diagnostics == SNAPSHOT
    assert MARKER not in repr(out)


@pytest.mark.parametrize("again", [{}, {"upstream_status": 401, "upstream_code": "token_revoked"}])
def test_first_snapshot_survives_stale_job_overwrite(db, again):
    job, _ = flow._reserve_job(QuickPayRequest(plan="plus", session="offline", debug_checkout_only=True),
                               "offline-key", "test-owner")
    original = DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "已拒绝", SNAPSHOT)
    flow._capture_checkout_failure(job, original)
    flow._capture_checkout_failure(job, HTTPException(401, {"detail": again}))
    assert job.diagnostics == SNAPSHOT
    db.save_job(job)
    # Detached job imitates a stale writer; changing MyJob.status remains effective.
    stale = flow.QuickPayJob(id=job.id, idempotency_key=job.idempotency_key,
                             diagnostics=again, status="failed")
    db.save_job(stale)
    row = db.load_jobs()[0]
    assert row["status"] == "failed" and json.loads(row["diagnostics_json"]) == SNAPSHOT


@pytest.mark.parametrize("stored", [None, "", "{bad json", "[]", '{"cf_ray":{"secret":"x"}}'])
def test_legacy_row_never_exposes_garbage_and_can_acquire_first_valid_snapshot(db, stored):
    job, _ = flow._reserve_job(QuickPayRequest(plan="plus", session="offline", debug_checkout_only=True),
                               "offline-key", "test-owner")
    row = db.load_jobs()[0]
    assert flow._from_db_job({**row, "diagnostics_json": stored}).diagnostics == {}
    if stored is not None:  # schema is NOT NULL, but older projections may lack the field
        db._conn.execute("UPDATE quick_pay_jobs SET diagnostics_json=?", (stored,))
        db._conn.commit()
    job.diagnostics = {**SNAPSHOT, "skipped": MARKER}
    db.save_job(job)
    assert json.loads(db.load_jobs()[0]["diagnostics_json"]) == SNAPSHOT


def test_never_use_untrusted_or_cross_job_order_id(monkeypatch):
    oid = str(uuid.uuid4())
    job = flow.QuickPayJob(id="offline", idempotency_key="my-key")
    monkeypatch.setattr(flow.orders.store, "get_order", lambda _: SimpleNamespace(idempotency_key="other-key"))
    error = HTTPException(502, {"_checkout_order_id": oid, "order_id": oid})
    flow._capture_checkout_failure(job, error)
    assert job.order_id is None
    error._checkout_order_id = oid
    flow._capture_checkout_failure(job, error)
    assert job.order_id is None


@pytest.mark.parametrize("operation", ["save_job", "save_idempotency", "add_event"])
def test_storage_fault_is_visible_without_exception_payload(db, monkeypatch, caplog, operation):
    def broken(*_a, **_kw):
        raise RuntimeError(MARKER)
    monkeypatch.setattr(db, operation, broken)
    job = flow.QuickPayJob(id="offline", idempotency_key="offline-key")
    if operation == "add_event":
        flow._event(job, "failed")
    else:
        flow._persist_job(job)
    assert "quick_pay_persistence_failed operation=" + operation in caplog.text
    assert MARKER not in caplog.text
