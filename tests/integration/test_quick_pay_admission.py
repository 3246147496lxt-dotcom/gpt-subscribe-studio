"""Exercise the real admission boundary, but never execute an order or payment."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
import sqlite3
import subprocess
import sys
from threading import Barrier
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gss_api import locks, persistence
from gss_api.routers import quick_pay
from gss_api.services import order_flow as flow


PATH = "/api/payments/quick-pay"
SESSION = "private-offline-session-canary"
SECOND_SESSION = "refreshed-offline-session-canary"
OTHER = "different-offline-owner"
OWNER = "a" * 64
CARD = {"number": "4242" * 4, "cvc": "CVCX", "exp_month": "12", "exp_year": "2030"}
DEBUG = {"plan": "plus", "session": SESSION, "debug_checkout_only": True}
PAY = {**DEBUG, "debug_checkout_only": False, "card": CARD}


@pytest.fixture
def app(tmp_path, monkeypatch):
    d = persistence.Persistence(str(tmp_path / "fixture.sqlite"))
    worker = AsyncMock(return_value=None)  # stop at dispatch, retain the real durable queued row
    monkeypatch.setattr(persistence, "_db", d)
    monkeypatch.setattr(locks, "account_fingerprint",
                        lambda s: {SESSION: OWNER, SECOND_SESSION: OWNER, OTHER: "b" * 64}.get(s, ""))
    monkeypatch.setattr(flow, "run_quick_pay", worker)
    monkeypatch.setattr(flow, "_jobs", {})
    api = FastAPI()
    api.include_router(quick_pay.router)
    try:
        yield api, d, worker
    finally:
        d.close()


def post(client, body=DEBUG, key="once"):
    return client.post(PATH, json=body, headers={"Idempotency-Key": key})


@pytest.mark.parametrize("fault", ["unavailable", "readonly", "second_write", "commit"])
def test_failed_reservation_returns_503_without_dispatch_or_partial_state(app, monkeypatch, fault, caplog):
    api, d, worker = app
    if fault == "unavailable":
        monkeypatch.setattr(persistence, "_db", None)
    elif fault == "readonly":
        d._conn.execute("PRAGMA query_only=ON")
    elif fault == "second_write":
        d._conn.executescript("""CREATE TRIGGER bad_index BEFORE INSERT ON idempotency_keys
            BEGIN SELECT RAISE(ABORT, 'private-sql-canary'); END;""")
    else:
        # Unlike a failed INSERT, both writes now succeed; only COMMIT fails.
        d._conn.executescript("""CREATE TABLE test_parent(id INTEGER PRIMARY KEY);
            CREATE TABLE test_child(p INTEGER REFERENCES test_parent(id) DEFERRABLE INITIALLY DEFERRED);
            CREATE TRIGGER bad_commit AFTER INSERT ON idempotency_keys
            BEGIN INSERT INTO test_child VALUES(1); END;""")
    with TestClient(api) as http:
        response = post(http, PAY)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "quick_pay_unavailable"
    worker.assert_not_called()
    assert not flow._jobs and not d._conn.in_transaction
    assert not d.load_jobs() and not d.load_idempotency()
    assert all(s not in response.text + caplog.text for s in (SESSION, CARD["number"], CARD["cvc"], "private-sql-canary"))


def test_dispatch_observes_both_records_already_committed(app, monkeypatch):
    api, db, worker = app
    observed = []

    def make_coroutine(job_id, payload, key):
        # This hook runs when scheduling, not later when the coroutine is awaited.
        with closing(sqlite3.connect(db.path)) as fresh:
            observed.append(fresh.execute("""SELECT count(*) FROM quick_pay_jobs j
                JOIN idempotency_keys k ON k.key=j.idempotency_key AND k.job_id=j.id
                WHERE j.id=? AND k.key=?""", (job_id, key)).fetchone()[0])
        return worker(job_id, payload, key)

    monkeypatch.setattr(flow, "run_quick_pay", make_coroutine)
    with TestClient(api) as http:
        assert post(http).status_code == 202
    assert observed == [1]
    worker.assert_called_once()


@pytest.mark.parametrize("second, expected", [
    ({**DEBUG, "session": SECOND_SESSION}, 202),
    ({**DEBUG, "session": OTHER}, 409),
    ({**DEBUG, "session": "unidentified"}, 409),
    ({**DEBUG, "plan": "team"}, 409),
    (PAY, 409),
])
def test_repeat_never_dispatches_again(app, second, expected):
    api, d, worker = app
    with TestClient(api) as http:
        first = post(http)
        assert first.status_code == 202
        again = post(http, second)
    assert again.status_code == expected
    if expected == 202:
        assert again.json()["job_id"] == first.json()["job_id"]
    else:
        assert again.json()["detail"]["code"] == "idempotency_conflict"
        assert first.json()["job_id"] not in again.text
    assert len(d.load_jobs()) == len(d.load_idempotency()) == 1
    worker.assert_called_once()


def test_replay_checks_business_not_card_or_session_secrets(app, caplog):
    api, d, worker = app
    with TestClient(api) as http:
        first = post(http, PAY)
        again = post(http, {**PAY, "card": {**CARD, "number": "4000" * 4, "cvc": "SYNX"}, "session": SECOND_SESSION})
    assert first.status_code == again.status_code == 202
    assert again.json()["job_id"] == first.json()["job_id"]
    worker.assert_called_once()
    stored = "\n".join(d._conn.iterdump()) + first.text + again.text + caplog.text
    assert all(v not in stored for v in (SESSION, SECOND_SESSION, CARD["number"], CARD["cvc"], "4000" * 4, "SYNX"))


def test_existing_job_can_be_looked_up_under_lock_but_fresh_job_cannot_start(app):
    api, d, worker = app
    with TestClient(api) as http:
        first = post(http)
        assert first.status_code == 202
        d.acquire_lock(lock_id="fixture-lock", lock_key="account:" + OWNER, lock_type="account", reason="unknown")
        replay = post(http)
        denied = post(http, key="second-key")
    assert replay.status_code == 202 and replay.json()["job_id"] == first.json()["job_id"]
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "payment_unknown_lock"
    assert len(d.load_jobs()) == len(d.load_idempotency()) == 1
    worker.assert_called_once()


@pytest.mark.parametrize("second, expected", [(DEBUG, [202, 202]), (PAY, [202, 409])])
def test_concurrent_handlers_reserve_on_independent_sqlite_connections(app, monkeypatch, second, expected):
    api, db1, worker = app
    db2 = persistence.Persistence(db1.path)
    enter = Barrier(2, timeout=4)
    connections = iter((db1.reserve_quick_pay, db2.reserve_quick_pay))

    def race(*args, **kwargs):
        reserve = next(connections)
        enter.wait()  # synchronize immediately before each connection's first DB statement
        return reserve(*args, **kwargs)

    monkeypatch.setattr(db1, "reserve_quick_pay", race)
    def submit(payload):
        with TestClient(api) as http:
            return post(http, payload)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, (DEBUG, second)))
        assert sorted(r.status_code for r in results) == expected
        assert len({r.json()["job_id"] for r in results if r.status_code == 202}) == 1
        assert len(db2.load_jobs()) == len(db2.load_idempotency()) == 1
        worker.assert_called_once()
    finally:
        db2.close()


def test_restart_can_read_committed_queue_without_replaying(app, monkeypatch):
    api, db, worker = app
    # A separate process exits without cleanup immediately after commit. No
    # scheduler, application startup, live session, or card is involved.
    child = """
import os, sys
from gss_api import persistence
from gss_api.schemas import QuickPayRequest
from gss_api.services.order_flow import _reserve_job
persistence._db = persistence.Persistence(sys.argv[1])
job, created = _reserve_job(QuickPayRequest(plan="plus", session="unused", debug_checkout_only=True),
                            "once", sys.argv[2])
os._exit(0 if created else 1)
"""
    subprocess.run([sys.executable, "-B", "-c", child, db.path, OWNER], check=True, timeout=15,
                   env={"PYTHONPATH": os.pathsep.join(sys.path), "PYTHONDONTWRITEBYTECODE": "1"})
    job = db.get_quick_pay_by_key("once")["id"]
    db.close()
    fresh = persistence.Persistence(db.path)
    monkeypatch.setattr(persistence, "_db", fresh)
    flow._jobs.clear()
    worker.reset_mock()
    try:
        locks.recover_incomplete_jobs()  # a reserved row is not permission to submit again
        with TestClient(api) as http:
            lookup = http.get(f"{PATH}/{job}")
            replay = post(http)
        assert lookup.status_code == 200 and lookup.json()["job_id"] == job
        assert replay.status_code == 202 and replay.json()["job_id"] == job
        worker.assert_not_called()
    finally:
        fresh.close()


@pytest.mark.parametrize("kind", ["legacy", "orphan_job_link", "order_link", "order_without_index"])
def test_unverifiable_preexisting_key_does_not_create_or_backfill(app, kind):
    api, d, worker = app
    if kind == "legacy":
        d.save_job(flow.QuickPayJob(id="legacy-job", idempotency_key="once", account_fingerprint=OWNER))
    elif kind == "order_without_index":
        d._conn.execute("""INSERT INTO orders(id,idempotency_key,account_fingerprint,plan,billing_country,
            account_source,card_source,billing_mode,status,created_at,updated_at)
            VALUES('old','once',?,'plus','US','customer_session','customer','standard','created','t','t')""", (OWNER,))
        d._conn.commit()
    else:
        links = {"job_id": "dangling"} if kind == "orphan_job_link" else {"order_id": "existing-order"}
        d.save_idempotency("once", "quick_pay" if kind == "orphan_job_link" else "order_create", state="queued", **links)
    before = list(d._conn.iterdump())
    with TestClient(api) as http:
        refused = post(http)
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "idempotency_conflict"
        if kind == "legacy":
            assert http.get(PATH + "/legacy-job").status_code == 200
    assert list(d._conn.iterdump()) == before
    worker.assert_not_called()


def test_shared_index_preserves_links_during_alternating_writes(app):
    _, d, _ = app
    d.save_idempotency("once", "quick_pay", job_id="j1", state="queued")
    d.save_idempotency("once", "order_create", order_id="o1", state="created")
    d.save_idempotency("once", "quick_pay", job_id="j1", state="running")
    [row] = d.load_idempotency()
    assert (row["job_id"], row["order_id"]) == ("j1", "o1")


def test_open_pre_upgrade_database_preserves_history_and_refuses_legacy_replay(tmp_path, app, monkeypatch):
    api, _, worker = app
    path = str(tmp_path / "pre-upgrade.sqlite")
    # A real v1 quick_pay_jobs schema without the new digest; no Git binary or
    # SQLite DROP COLUMN support is needed to reconstruct the previous layout.
    with closing(sqlite3.connect(path)) as old:
        old.executescript("""
            CREATE TABLE quick_pay_jobs (
                id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, account_fingerprint TEXT,
                order_id TEXT, status TEXT NOT NULL, stage TEXT NOT NULL, failure_code TEXT,
                message TEXT, amount TEXT, currency TEXT, purchase_attempt_id TEXT, lock_id TEXT,
                timings_json TEXT NOT NULL DEFAULT '{}', diagnostics_json TEXT NOT NULL DEFAULT '{}',
                locked_by TEXT, lease_until TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, finished_at TEXT
            );
            INSERT INTO quick_pay_jobs(id,idempotency_key,status,stage,diagnostics_json,created_at,updated_at)
            VALUES('old-job','once','failed','failed','{"upstream_status":400}','t','t');
        """)
    fresh = persistence.Persistence(path)
    try:
        monkeypatch.setattr(persistence, "_db", fresh)
        with TestClient(api) as http:
            assert post(http).status_code == 409
            read = http.get(PATH + "/old-job")
        assert read.status_code == 200 and read.json()["diagnostics"] == {"upstream_status": 400}
        [row] = fresh.load_jobs()
        assert row["intent_digest"] is None and row["status"] == "failed"
        assert not fresh.load_idempotency()
        worker.assert_not_called()
    finally:
        fresh.close()


def test_optional_projection_failure_does_not_revoke_committed_receipt(app, monkeypatch, caplog):
    api, d, worker = app
    def fail_read(**kwargs):
        raise sqlite3.OperationalError("private-read-canary")
    monkeypatch.setattr(d, "list_events", fail_read)
    with TestClient(api) as http:
        response = post(http)
        duplicate = post(http)
    assert response.status_code == duplicate.status_code == 202
    assert response.json()["job_id"] == duplicate.json()["job_id"] == d.get_quick_pay_by_key("once")["id"]
    assert not response.json()["events"] and response.json()["checkout_outcome"] is None
    assert "private-read-canary" not in response.text + caplog.text
    worker.assert_called_once()

@pytest.mark.parametrize("mismatched", [False, True])
def test_replay_of_job_with_an_order_requires_consistent_links(app, mismatched):
    api, db, worker = app
    with TestClient(api) as http:
        job_id = post(http).json()["job_id"]
        db._conn.execute("""INSERT INTO orders(id,idempotency_key,account_fingerprint,plan,billing_country,
            account_source,card_source,billing_mode,status,created_at,updated_at)
            VALUES('linked','once',?,'plus','US','customer_session','customer','standard','created','t','t')""", (OWNER,))
        db._conn.commit()
        db.save_idempotency("once", "order_create", order_id="linked", state="created")
        job = flow._jobs.pop(job_id)
        job.order_id = "different-order" if mismatched else "linked"
        job.status = job.stage = "failed"
        db.save_job(job)
        replay = post(http)
    if mismatched:
        assert replay.status_code == 409 and job_id not in replay.text
    else:
        assert replay.status_code == 202 and replay.json()["job_id"] == job_id
        assert replay.json()["status"] == "failed" and replay.json()["order_id"] == "linked"
    worker.assert_called_once()


@pytest.mark.parametrize("fault", ["no_db", "read_error"])
def test_cold_status_lookup_distinguishes_unavailable_from_not_found(app, monkeypatch, fault):
    api, db, worker = app
    with TestClient(api) as http:
        path = PATH + "/test-job"
        assert http.get(path).status_code == 404
        if fault == "no_db":
            monkeypatch.setattr(persistence, "_db", None)
        else:
            def fail_read(_job_id):
                raise sqlite3.OperationalError("private-db-canary")
            monkeypatch.setattr(db, "get_quick_pay_by_id", fail_read)
        resp = http.get(path)
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "quick_pay_status_unavailable"
    assert "private-db-canary" not in resp.text
    worker.assert_not_called()
