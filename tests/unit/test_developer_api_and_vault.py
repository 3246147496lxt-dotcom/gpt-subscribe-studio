"""Tests for AES-256-GCM 30-day order vault and Developer Open API (/api/v1/*)."""
from __future__ import annotations

import base64
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient

from gss_contracts import OrderStatus
from gss_api import order_vault, persistence
from gss_api.main import create_app
from gss_api.persistence import Persistence
from gss_api.routers import open_api, orders as orders_router, protocol_cards
from gss_api.store import store
from gss_api.test_access import TestAccessMiddleware

_TEST_PAN = "".join(["4242", "4242", "4242", "4242"])
_TEST_CVC = "789"


def _fake_jwt(email: str = "dev-user@example.test", account_id: str = "acct-dev-1") -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({
            "exp": int(time.time()) + 3600,
            "https://api.openai.com/profile": {"email": email},
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        }).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _session_json(email: str = "dev-user@example.test", account_id: str = "acct-dev-1") -> str:
    return json.dumps({
        "accessToken": _fake_jwt(email, account_id),
        "sessionToken": f"st-secret-canary-{account_id}",
        "user": {"email": email},
    })


@pytest.fixture
def env_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    monkeypatch.setenv("GSS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GSS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GSS_VAULT_MASTER_KEY", "test-master-key-for-aes256gcm-unit-suite")
    monkeypatch.setenv("GSS_VAULT_RETENTION_DAYS", "30")
    billing = tmp_path / "fixed-billing.json"
    billing.write_text(json.dumps({
        "name": "Daniel Mercer",
        "email": "billing@example.test",
        "line1": "1425 SW Morrison Street",
        "city": "Portland",
        "state": "OR",
        "postal_code": "97205",
        "country": "US",
    }), encoding="utf-8")
    billing.chmod(0o600)
    monkeypatch.setenv("GSS_FIXED_BILLING_FILE", str(billing))
    monkeypatch.delenv("GSS_VERIFIED_BILLING_FILE", raising=False)
    db_path = tmp_path / "dev-vault-test.db"
    monkeypatch.setenv("GSS_DB_PATH", str(db_path))

    db = Persistence(str(db_path))
    monkeypatch.setattr(persistence, "_db", db)
    order_vault._KEY_CACHE.clear()
    with open_api._RATE_LOCK:
        open_api._RATE_BUCKETS.clear()

    store.orders.clear()
    store.jobs.clear()
    store.views.clear()
    store._idempotency.clear()
    store.db = None
    store._db_loaded = False
    store.attach_db(db)

    async def _fake_pay_protocol(order_id: str, payload):
        view = store.view(order_id)
        view.payment_status = "succeeded"
        view.delivery_status = "delivered"
        store.get_order(order_id).status = OrderStatus.DELIVERED  # type: ignore[union-attr]
        store.persist(order_id)
        return {
            "order_id": order_id,
            "state": "paid",
            "payment_status": "succeeded",
            "delivery_status": "delivered",
            "message": "ok",
        }

    monkeypatch.setattr(orders_router, "pay_order_protocol", _fake_pay_protocol)
    monkeypatch.setattr(protocol_cards, "bind_and_pay_protocol", _fake_pay_protocol)

    app = create_app()
    with TestClient(app) as client:
        yield client, db, db_path, tmp_path
    db.close()


def _wait_for_job_order(client: TestClient, job_id: str, headers: dict[str, str], timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/recharges/{job_id}", headers=headers)
        assert resp.status_code == 200
        last = resp.json()
        if last.get("status") not in ("queued", "running"):
            return last
        time.sleep(0.05)
    return last


def test_order_vault_aes256gcm_aad_encryption_and_30_day_ttl(env_app) -> None:
    client, db, db_path, _tmp = env_app

    # AAD binds ciphertext to (order_id, field_name): transplanting fails authentication.
    blob = order_vault.encrypt_field("ord-aad-1", "session", "secret-session-value")
    assert order_vault.decrypt_field("ord-aad-1", "session", blob) == "secret-session-value"
    with pytest.raises(InvalidTag):
        order_vault.decrypt_field("ord-aad-2", "session", blob)
    with pytest.raises(InvalidTag):
        order_vault.decrypt_field("ord-aad-1", "card", blob)

    # Create a developer API key and submit an order via /api/v1/recharges.
    key_resp = client.post(
        "/api/developer/keys",
        json={"name": "VaultPartner", "rate_limit_rpm": 30, "allowed_plans": ["plus", "pro100"]},
    )
    assert key_resp.status_code == 201
    api_key = key_resp.json()["api_key"]
    assert api_key.startswith("gss_live_")

    session_str = _session_json("vault-buyer@example.test", "acct-vault-1")
    submit = client.post(
        "/api/v1/recharges",
        headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": "vault-idem-1"},
        json={
            "plan": "pro100",
            "exit_country": "CL",
            "external_order_id": "MCH-VAULT-01",
            "auto_cancel_renew": False,
            "session": session_str,
            "card": {"number": _TEST_PAN, "exp_month": "11", "exp_year": "2029", "cvc": _TEST_CVC},
        },
    )
    assert submit.status_code == 202
    job_id = submit.json()["job_id"]
    settled = _wait_for_job_order(client, job_id, {"Authorization": f"Bearer {api_key}"})
    order_id = settled["order_id"]
    assert order_id

    # 1. Raw SQLite bytes must NOT contain plaintext PAN, CVC, or sessionToken!
    raw_db_bytes = db_path.read_bytes()
    assert _TEST_PAN.encode() not in raw_db_bytes
    assert b"st-secret-canary-acct-vault-1" not in raw_db_bytes

    # 2. Regular ledger & CSV export must include source="api" metadata and ZERO plaintext secrets.
    ledger = client.get("/api/orders?source=api").json()
    assert ledger["total"] == 1
    row = ledger["items"][0]
    assert row["id"] == order_id
    assert row["source"] == "api"
    assert row["api_key_name"] == "VaultPartner"
    assert row["external_order_id"] == "MCH-VAULT-01"
    assert row["vault_has_session"] is True
    assert row["vault_has_card"] is True
    assert row["vault_card_last4"] == "4242"
    assert _TEST_PAN not in json.dumps(ledger)
    assert "st-secret-canary-acct-vault-1" not in json.dumps(ledger)

    csv_resp = client.get("/api/orders/export.csv?source=api")
    assert csv_resp.status_code == 200
    assert "VaultPartner" in csv_resp.text
    assert "MCH-VAULT-01" in csv_resp.text
    assert _TEST_PAN not in csv_resp.text
    assert "st-secret-canary-acct-vault-1" not in csv_resp.text

    # 3. GET /api/orders/{id}/vault returns metadata ONLY (no plaintext).
    meta_resp = client.get(f"/api/orders/{order_id}/vault")
    assert meta_resp.status_code == 200
    meta = meta_resp.json()
    assert meta["available"] is True
    assert meta["has_session"] is True
    assert meta["has_card"] is True
    assert meta["card_bin"] == "424242"
    assert meta["card_last4"] == "4242"
    assert meta["card_exp"] == "11/2029"
    assert meta["card_fingerprint"]
    assert meta["retention_days"] == 30
    assert 29 <= meta["remaining_days"] <= 30
    assert "session" not in meta
    assert "card" not in meta

    # 4. POST /api/orders/{id}/vault/reveal decrypts full session + full card and writes audit event.
    reveal_resp = client.post(
        f"/api/orders/{order_id}/vault/reveal",
        json={"operator": "ops-admin", "reason": "客户咨询售后核对"},
    )
    assert reveal_resp.status_code == 200
    assert reveal_resp.headers.get("cache-control") == "no-store"
    revealed = reveal_resp.json()
    assert revealed["session"]
    assert "st-secret-canary-acct-vault-1" in revealed["session"]
    assert revealed["card"]["number"] == _TEST_PAN
    assert revealed["card"]["exp_month"] == "11"
    assert revealed["card"]["exp_year"] == "2029"
    assert revealed["card"]["cvc"] == _TEST_CVC

    events = client.get(f"/api/orders/{order_id}/events").json()
    assert any(ev["stage"] == "vault" and ev["state"] == "vault_revealed" for ev in events)

    # 5. Simulate 31-day TTL expiration -> auto-purged on read & reveal returns None!
    past_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with db._lock, db._conn:
        db._conn.execute(
            "UPDATE order_sensitive_vault SET expires_at = ? WHERE order_id = ?",
            (past_iso, order_id),
        )
    expired_reveal = client.post(f"/api/orders/{order_id}/vault/reveal")
    assert expired_reveal.status_code == 200
    exp_body = expired_reveal.json()
    assert exp_body["meta"]["available"] is False
    assert exp_body["meta"]["expired"] is True
    assert exp_body["meta"]["purge_reason"] == "ttl_expired"
    assert exp_body["meta"]["card_last4"] == "4242"  # safe analytics preserved
    assert exp_body["session"] is None
    assert exp_body["card"] is None


def test_order_vault_manual_purge_shreds_immediately(env_app) -> None:
    client, db, _db_path, _tmp = env_app
    order_id = "ord-manual-purge-1"
    # Seed minimal order row so 404 check passes.
    now = datetime.now(timezone.utc).isoformat()
    with db._lock, db._conn:
        db._conn.execute(
            """INSERT INTO orders(
                id, plan, billing_country, account_source, card_source, billing_mode,
                status, payment_status, delivery_status, created_at, updated_at
            ) VALUES(?, 'plus', 'US', 'customer_session', 'customer', 'standard', 'delivered', 'succeeded', 'delivered', ?, ?)""",
            (order_id, now, now),
        )
    order_vault.capture_order_credentials(
        order_id,
        session=_session_json("purge@example.test", "acct-purge"),
        card={"number": _TEST_PAN, "exp_month": "05", "exp_year": "2030", "cvc": _TEST_CVC},
    )
    purge_resp = client.delete(f"/api/orders/{order_id}/vault")
    assert purge_resp.status_code == 200
    body = purge_resp.json()
    assert body["available"] is False
    assert body["purge_reason"] == "manual_purge"
    assert body["has_session"] is False
    assert body["has_card"] is False
    assert body["card_last4"] == "4242"

    # Subsequent reveal cannot return credentials.
    reveal = client.post(f"/api/orders/{order_id}/vault/reveal").json()
    assert reveal["session"] is None
    assert reveal["card"] is None


def test_developer_open_api_auth_isolation_ratelimit_rotation_and_middleware(env_app) -> None:
    client, db, _db_path, tmp_path = env_app

    # Create two keys: Key A (all plans, rpm=10) and Key B (plus only, rpm=2).
    k_a = client.post(
        "/api/developer/keys",
        json={"name": "Studio-A", "rate_limit_rpm": 20, "allowed_plans": ["go", "plus", "pro100", "pro200", "pro500"]},
    ).json()
    k_b = client.post(
        "/api/developer/keys",
        json={"name": "Studio-B", "rate_limit_rpm": 2, "allowed_plans": ["plus"]},
    ).json()
    secret_a = k_a["api_key"]
    secret_b = k_b["api_key"]
    key_a_id = k_a["key"]["id"]
    key_b_id = k_b["key"]["id"]

    # Stored key in SQLite is SHA-256 hash only (never plaintext).
    with sqlite3.connect(db.path) as con:
        hashes = [r[0] for r in con.execute("SELECT key_hash FROM api_keys").fetchall()]
    assert secret_a not in hashes
    assert secret_b not in hashes

    # Missing / invalid key -> 401.
    assert client.get("/api/v1/ping").status_code == 401
    assert client.get("/api/v1/ping", headers={"Authorization": "Bearer gss_live_invalid_key_1234567890"}).status_code == 401

    # Valid ping & plans work with both Authorization Bearer and X-API-Key.
    ping = client.get("/api/v1/ping", headers={"Authorization": f"Bearer {secret_a}"})
    assert ping.status_code == 200
    assert ping.json()["key_name"] == "Studio-A"

    plans = client.get("/api/v1/plans", headers={"X-API-Key": secret_a})
    assert plans.status_code == 200
    plan_ids = {p["id"]: p["display_name"] for p in plans.json()["plans"]}
    assert plan_ids["pro_lite"] == "Pro100"
    assert plan_ids["pro"] == "Pro200"
    assert plan_ids["pro_max"] == "Pro500"

    # Key B cannot request pro_max (plan_not_allowed -> 403).
    blocked_plan = client.post(
        "/api/v1/recharges",
        headers={"Authorization": f"Bearer {secret_b}", "Idempotency-Key": "b-pro-denied"},
        json={
            "plan": "pro500",
            "session": _session_json("b@example.test", "acct-b"),
            "card": {"number": _TEST_PAN, "exp_month": "12", "exp_year": "2030", "cvc": _TEST_CVC},
        },
    )
    assert blocked_plan.status_code == 403
    assert blocked_plan.json()["detail"]["code"] == "plan_not_allowed"

    # Key B rpm=2: 2nd request succeeds (ping), 3rd hits 429 with Retry-After!
    assert client.get("/api/v1/ping", headers={"Authorization": f"Bearer {secret_b}"}).status_code == 200
    rl = client.get("/api/v1/ping", headers={"Authorization": f"Bearer {secret_b}"})
    assert rl.status_code == 429
    assert rl.json()["detail"]["code"] == "rate_limit_exceeded"
    assert int(rl.headers.get("retry-after", "0")) >= 1

    # Tenant isolation: Key A submits with shared Idempotency-Key; Key B cannot see it & can reuse same Idempotency-Key.
    with open_api._RATE_LOCK:
        open_api._RATE_BUCKETS.clear()
    sub_a = client.post(
        "/api/v1/recharges",
        headers={"Authorization": f"Bearer {secret_a}", "Idempotency-Key": "shared-idem-001"},
        json={
            "plan": "plus",
            "exit_country": "PH",
            "external_order_id": "A-EXT-1",
            "auto_cancel_renew": False,
            "session": _session_json("a1@example.test", "acct-iso-a"),
            "card": {"number": _TEST_PAN, "exp_month": "12", "exp_year": "2030", "cvc": _TEST_CVC},
        },
    )
    assert sub_a.status_code == 202
    job_a = sub_a.json()["job_id"]

    # Key B gets 404 on Key A's job and 0 in list.
    assert client.get(f"/api/v1/recharges/{job_a}", headers={"Authorization": f"Bearer {secret_b}"}).status_code == 404
    assert client.get("/api/v1/recharges", headers={"Authorization": f"Bearer {secret_b}"}).json()["total"] == 0

    # Raise Key B RPM via PATCH and verify Key B can submit with the EXACT SAME Idempotency-Key without conflict!
    assert client.patch(f"/api/developer/keys/{key_b_id}", json={"rate_limit_rpm": 20}).status_code == 200
    sub_b = client.post(
        "/api/v1/recharges",
        headers={"Authorization": f"Bearer {secret_b}", "Idempotency-Key": "shared-idem-001"},
        json={
            "plan": "plus",
            "exit_country": "PH",
            "external_order_id": "B-EXT-1",
            "auto_cancel_renew": False,
            "session": _session_json("b1@example.test", "acct-iso-b"),
            "card": {"number": _TEST_PAN, "exp_month": "12", "exp_year": "2030", "cvc": _TEST_CVC},
        },
    )
    assert sub_b.status_code == 202
    assert sub_b.json()["job_id"] != job_a

    # Wait for Key A job to finish and verify admin key list reflects total_orders >= 1.
    _wait_for_job_order(client, job_a, {"Authorization": f"Bearer {secret_a}"})
    admin_keys = client.get("/api/developer/keys").json()["items"]
    by_id = {item["id"]: item for item in admin_keys}
    assert by_id[key_a_id]["total_orders"] >= 1
    assert by_id[key_a_id]["total_requests"] >= 3

    # Disable Key A -> 403; rotate Key A -> old key 401, new key 200; revoke Key B -> 401.
    assert client.patch(f"/api/developer/keys/{key_a_id}", json={"status": "disabled"}).status_code == 200
    assert client.get("/api/v1/ping", headers={"Authorization": f"Bearer {secret_a}"}).status_code == 403

    rot = client.post(f"/api/developer/keys/{key_a_id}/rotate")
    assert rot.status_code == 200
    new_secret_a = rot.json()["api_key"]
    assert new_secret_a != secret_a
    assert client.get("/api/v1/ping", headers={"Authorization": f"Bearer {secret_a}"}).status_code == 401
    assert client.get("/api/v1/ping", headers={"Authorization": f"Bearer {new_secret_a}"}).status_code == 200

    assert client.delete(f"/api/developer/keys/{key_b_id}").status_code == 200
    assert client.get("/api/v1/ping", headers={"Authorization": f"Bearer {secret_b}"}).status_code == 401

    # Malformed /api/v1/recharges must not echo session or card.
    bad = client.post(
        "/api/v1/recharges",
        headers={"Authorization": f"Bearer {new_secret_a}", "Idempotency-Key": "bad-input-1"},
        json={"plan": "plus", "session": {"raw": "secret-do-not-echo"}, "card": {"number": _TEST_PAN}},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "INVALID_ORDER_INPUT"
    assert "secret-do-not-echo" not in bad.text
    assert _TEST_PAN not in bad.text


def test_test_access_middleware_allows_v1_bearer_and_blocks_admin_and_vault(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    monkeypatch.setenv("GSS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GSS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GSS_VAULT_MASTER_KEY", "test-master-key-mw")
    db_path = tmp_path / "mw-test.db"
    monkeypatch.setenv("GSS_DB_PATH", str(db_path))
    db = Persistence(str(db_path))
    monkeypatch.setattr(persistence, "_db", db)
    store.attach_db(db)

    raw_key, prefix, key_hash = open_api._mint_api_key()
    db.create_api_key(
        key_id="key_mw_test",
        name="MW-Partner",
        key_prefix=prefix,
        key_hash=key_hash,
        rate_limit_rpm=30,
        allowed_plans=["plus"],
    )

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"password": "admin-secret-password"}), encoding="utf-8")
    login_html = tmp_path / "login.html"
    login_html.write_text("<html>login {{ERROR_HIDDEN}} {{INVALID}}</html>", encoding="utf-8")

    protected_app = create_app()
    protected_app.add_middleware(
        TestAccessMiddleware,
        credentials_file=str(auth_file),
        login_file=str(login_html),
        origin="https://api.example.test",
    )
    try:
        with TestClient(protected_app) as pclient:
            # Unauthenticated admin APIs and vault reveal remain strictly protected (401).
            assert pclient.get("/api/developer/keys").status_code == 401
            assert pclient.post("/api/orders/any-id/vault/reveal").status_code == 401
            # External /api/v1/* with Bearer key bypasses admin cookie and succeeds!
            ok_ping = pclient.get("/api/v1/ping", headers={"Authorization": f"Bearer {raw_key}"})
            assert ok_ping.status_code == 200
            assert ok_ping.json()["key_name"] == "MW-Partner"
            # External /api/v1/* without Bearer key gets clean 401 JSON (never 303 redirect to /login).
            no_key = pclient.get("/api/v1/ping", follow_redirects=False)
            assert no_key.status_code == 401
            assert no_key.json()["detail"]["code"] == "invalid_api_key"
    finally:
        db.close()
