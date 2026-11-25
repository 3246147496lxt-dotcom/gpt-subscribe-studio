"""Phase 4 unit tests: bounded store cache, SQLite maintenance, diagnostics, batch preflight, CSV export & parser split."""
from __future__ import annotations

import base64
import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from gss_contracts import AccountSource, BillingMode, CardSource, Mode, Order, OrderStatus, Plan
from gss_api import main, persistence, store as store_mod
from gss_api.persistence import Persistence
from gss_api.store import InMemoryStore, OrderView


def _make_order(
    order_id: str,
    *,
    plan: Plan = Plan.PLUS,
    status: OrderStatus = OrderStatus.DELIVERED,
    idem_key: str | None = None,
) -> Order:
    return Order(
        id=order_id,
        idempotency_key=idem_key or f"idem-{order_id}",
        plan=plan,
        billing_country="US",
        mode=Mode(
            account_source=AccountSource.CUSTOMER_SESSION,
            card_source=CardSource.CUSTOMER,
            billing_mode=BillingMode.STANDARD,
            risk_acknowledged=True,
        ),
        status=status,
    )


def _fake_jwt(email: str = "tester@example.com", exp_offset_s: int = 3600) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "exp": int((datetime.now(timezone.utc) + timedelta(seconds=exp_offset_s)).timestamp()),
                "https://api.openai.com/profile": {"email": email},
                "https://api.openai.com/auth": {"chatgpt_account_id": "acct-test-001"},
            }
        ).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _isolate_db(tmp_path, monkeypatch) -> Persistence:
    db = Persistence(str(tmp_path / "phase4.db"))
    monkeypatch.setattr(persistence, "_db", db)
    monkeypatch.setattr(store_mod.store, "db", None)
    monkeypatch.setattr(store_mod.store, "_db_loaded", False)
    store_mod.store.orders.clear()
    store_mod.store.views.clear()
    store_mod.store._idempotency.clear()
    store_mod.store.attach_db(db)
    return db


def test_store_bounded_rehydrate_eviction_and_ondemand_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("GSS_STORE_MAX_REHYDRATE", "10")
    monkeypatch.setenv("GSS_STORE_MAX_CACHED", "50")

    db = Persistence(str(tmp_path / "bounded.db"))
    mem = InMemoryStore()
    mem.attach_db(db)

    active = _make_order("ord-active-0", status=OrderStatus.PAYING, idem_key="idem-active-0")
    mem.create_order(active, idempotency_key=active.idempotency_key)

    for i in range(60):
        ord_item = _make_order(f"ord-settled-{i:03d}", status=OrderStatus.DELIVERED, idem_key=f"idem-s-{i:03d}")
        mem.create_order(ord_item, idempotency_key=ord_item.idempotency_key)
        view = mem.view(ord_item.id)
        view.amount = "20.00"
        view.currency = "USD"
        mem.persist(ord_item.id)

    # Eviction must keep total cached orders <= 50 and never evict the active order.
    assert len(mem.orders) <= 50
    assert "ord-active-0" in mem.orders
    assert "ord-settled-000" not in mem.orders

    # On-demand lookup restores evicted order + view + idempotency from SQLite.
    restored = mem.get_order("ord-settled-000")
    assert restored is not None
    assert restored.id == "ord-settled-000"
    assert mem.view("ord-settled-000").amount == "20.00"

    by_idem = mem.find_by_idempotency("idem-s-001")
    assert by_idem is not None
    assert by_idem.id == "ord-settled-001"

    # Fresh store rehydrates active orders + bounded recent orders only.
    cold_mem = InMemoryStore()
    cold_mem.attach_db(db)
    assert "ord-active-0" in cold_mem.orders
    assert len(cold_mem.orders) <= 12
    assert cold_mem.get_order("ord-settled-002") is not None


def test_persistence_maintenance_prunes_only_old_settled_events(tmp_path):
    db = Persistence(str(tmp_path / "maint.db"))

    settled = _make_order("ord-settled", status=OrderStatus.DELIVERED)
    active = _make_order("ord-active", status=OrderStatus.PAYING)
    locked = _make_order("ord-locked", status=OrderStatus.FAILED)

    for item in (settled, active, locked):
        db.save_order(item, OrderView())

    # Hold a payment lock on the failed order.
    db.acquire_lock(
        lock_id="lock-keep-1",
        lock_key="account:fp-locked",
        lock_type="payment_ambiguous",
        reason="awaiting manual check",
        order_id=locked.id,
    )

    old_ts = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

    db.add_event(order_id=settled.id, stage="done", event="delivered", message="old settled")
    db.add_event(order_id=settled.id, stage="done", event="delivered", message="recent settled")
    db.add_event(order_id=active.id, stage="confirm", event="paying", message="old active")
    db.add_event(order_id=locked.id, stage="locked", event="failed", message="old locked")

    with db._lock, db._conn:
        db._conn.execute("UPDATE order_events SET created_at=? WHERE message LIKE 'old %'", (old_ts,))
        db._conn.execute("UPDATE order_events SET created_at=? WHERE message = 'recent settled'", (recent_ts,))

    res = db.run_maintenance(retention_days=90)
    assert res["pruned_events"] == 1
    assert res["wal_checkpoint"] is not None
    assert res["table_counts"]["orders"] == 3
    assert res["table_counts"]["order_events"] == 3

    # Verify remaining events belong to recent settled, active, and locked orders.
    assert len(db.list_events(order_id=settled.id)) == 1
    assert len(db.list_events(order_id=active.id)) == 1
    assert len(db.list_events(order_id=locked.id)) == 1


def test_system_diagnostics_and_maintenance_api_redacted(tmp_path, monkeypatch):
    billing_file = tmp_path / "secret-billing.json"
    billing_file.write_text(
        json.dumps(
            {
                "payer_id": "payer-local-test",
                "verification_ref": "verify-ref-001",
                "billing": {
                    "name": "PRIVATE_NAME_MARKER",
                    "email": "private-billing@example.com",
                    "line1": "999 Secret Ave",
                    "city": "Portland",
                    "state": "OR",
                    "postal_code": "97201",
                    "country": "US",
                },
            }
        ),
        encoding="utf-8",
    )
    os.chmod(billing_file, 0o600)
    monkeypatch.setenv("GSS_USE_FAKE", "false")
    monkeypatch.setenv("GSS_VERIFIED_BILLING_FILE", str(billing_file))
    _isolate_db(tmp_path, monkeypatch)

    with TestClient(main.create_app()) as client:
        diag = client.get("/api/system/diagnostics")
        assert diag.status_code == 200
        body = diag.json()
        assert body["ok"] is True
        assert body["python_version"]
        assert "orders" in body["table_counts"]
        raw_text = diag.text
        assert "PRIVATE_NAME_MARKER" not in raw_text
        assert "private-billing@example.com" not in raw_text
        assert "999 Secret Ave" not in raw_text
        assert str(billing_file) not in raw_text

        maint = client.post("/api/system/maintenance?retention_days=90")
        assert maint.status_code == 200
        m_body = maint.json()
        assert m_body["retention_days"] == 90
        assert m_body["pruned_events"] == 0
        assert "orders" in m_body["table_counts"]


def test_batch_preflight_api_mixed_and_no_session_echo(tmp_path, monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "true")
    _isolate_db(tmp_path, monkeypatch)

    canary_token = _fake_jwt("batch-user@example.com")
    expired_token = _fake_jwt("expired-user@example.com", exp_offset_s=-3600)
    valid_session = json.dumps({"accessToken": canary_token, "user": {"email": "batch-user@example.com"}})
    expired_session = json.dumps({"accessToken": expired_token, "user": {"email": "expired-user@example.com"}})

    with TestClient(main.create_app()) as client:
        resp = client.post(
            "/api/payments/preflight/batch",
            json={
                "items": [
                    {"label": "ok-account", "plan": "plus", "exit_country": "US", "session": valid_session},
                    {"label": "expired-account", "plan": "plus", "exit_country": "US", "session": expired_session},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["passed"] == 1
        assert data["failed"] == 1
        assert data["items"][0]["ok"] is True
        assert data["items"][0]["label"] == "ok-account"
        assert data["items"][1]["ok"] is False
        assert data["items"][1]["label"] == "expired-account"
        assert data["items"][1]["blocking_code"] == "session_expired"
        assert canary_token not in resp.text
        assert expired_token not in resp.text


def test_orders_date_filter_and_csv_export_formula_protection(tmp_path, monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "true")
    db = _isolate_db(tmp_path, monkeypatch)

    with TestClient(main.create_app()) as client:
        o1 = _make_order("ord-csv-1", plan=Plan.PLUS, status=OrderStatus.FAILED)
        v1 = OrderView(amount="20.00", currency="USD", failure_code="=CMD|' /C calc'!A0")
        db.save_order(o1, v1)

        o2 = _make_order("ord-csv-2", plan=Plan.PRO, status=OrderStatus.DELIVERED)
        v2 = OrderView(amount="200.00", currency="USD")
        db.save_order(o2, v2)

        with db._lock, db._conn:
            db._conn.execute("UPDATE orders SET created_at=? WHERE id=?", ("2026-01-15T10:00:00+00:00", o1.id))
            db._conn.execute("UPDATE orders SET created_at=? WHERE id=?", ("2026-05-20T10:00:00+00:00", o2.id))

        # Date + plan filtering via JSON API
        ledger = client.get("/api/orders", params={"plan": "plus", "date_from": "2026-01-01", "date_to": "2026-02-01"})
        assert ledger.status_code == 200
        l_body = ledger.json()
        assert l_body["total"] == 1
        assert l_body["items"][0]["id"] == "ord-csv-1"

        # CSV export with BOM + formula injection escape + no sensitive columns
        csv_resp = client.get(
            "/api/orders/export.csv",
            params={"plan": "plus", "date_from": "2026-01-01", "date_to": "2026-02-01"},
        )
        assert csv_resp.status_code == 200
        assert "text/csv" in csv_resp.headers["content-type"]
        raw = csv_resp.content
        assert raw.startswith(b"\xef\xbb\xbf")

        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        assert len(rows) == 1
        assert rows[0]["id"] == "ord-csv-1"
        assert rows[0]["failure_code"].startswith("'=")
        for forbidden_col in ("session", "card", "cvc", "view_json", "account_fingerprint"):
            assert forbidden_col not in rows[0]


def test_chatgpt_parsers_reexport_backwards_compatible():
    from gss_core import chatgpt, chatgpt_parsers

    for name in (
        "_json",
        "_log_upstream_failure",
        "_raise_stripe",
        "checkout_confirm_blocked",
        "checkout_confirm_blocked_advice",
        "expected_amount_minor",
        "intent_id_from_secret",
        "payment_intent_from_confirm",
        "payment_page_settled",
        "setup_intent_from_payload",
    ):
        assert getattr(chatgpt, name) is getattr(chatgpt_parsers, name)
