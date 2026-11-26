from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gss_api import persistence
from gss_api.main import create_app
from gss_api.persistence import Persistence
from gss_api.store import store
from gss_api.test_access import TestAccessMiddleware


def _fake_jwt(email: str = "buyer@example.test", account_id: str = "acct-portal-1") -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({
            "exp": int(time.time()) + 3600,
            "https://api.openai.com/profile": {"email": email},
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        }).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _session_payload(email: str = "buyer@example.test") -> str:
    return json.dumps({
        "accessToken": _fake_jwt(email),
        "user": {"email": email},
    })


@pytest.fixture
def portal_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    monkeypatch.setenv("GSS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GSS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GSS_PORTAL_AUTH_SECRET", "unit-test-portal-secret-key")
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
    test_db_path = tmp_path / "portal-test.db"
    monkeypatch.setenv("GSS_DB_PATH", str(test_db_path))

    db = Persistence(str(test_db_path))
    monkeypatch.setattr(persistence, "_db", db)
    store.orders.clear()
    store.jobs.clear()
    store.views.clear()
    store._idempotency.clear()
    store.db = None
    store._db_loaded = False
    store.attach_db(db)

    app = create_app()
    with TestClient(app) as client:
        yield client
    db.close()


def test_public_site_config_and_admin_update(portal_client: TestClient) -> None:
    resp = portal_client.get("/api/public/site-config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["connected"] is True
    assert data["guest_ttl_days"] == 7
    assert any(p["id"] == "plus" and p["available"] is True for p in data["plans"])
    assert any(p["id"] == "codex" and p.get("coming_soon") is True for p in data["plans"])

    upd = portal_client.put(
        "/api/settings/portal-site",
        json={
            "brand_name": "我的自建充值站",
            "card_shop_url": "https://cards.example.test",
            "support_url": "https://t.me/example_support",
            "announcements": [{"id": "a1", "text": "测试公告"}],
        },
    )
    assert upd.status_code == 200
    after = portal_client.get("/api/public/site-config").json()
    assert after["brand_name"] == "我的自建充值站"
    assert after["card_shop_url"] == "https://cards.example.test"
    assert after["support_url"] == "https://t.me/example_support"
    assert after["announcements"][0]["text"] == "测试公告"


def test_public_quick_pay_rejects_codex_coming_soon(portal_client: TestClient) -> None:
    resp = portal_client.post(
        "/api/public/quick-pay",
        headers={"Idempotency-Key": "portal-codex-1"},
        json={
            "plan": "codex",
            "exit_country": "PH",
            "session": _session_payload(),
            "card": {"number": "4242424242424242", "exp_month": "12", "exp_year": "2030", "cvc": "123"},  # release-check: ignore
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "plan_coming_soon"


def test_guest_task_isolation_and_user_login_claims_tasks(portal_client: TestClient) -> None:
    submit = portal_client.post(
        "/api/public/quick-pay",
        headers={"Idempotency-Key": "portal-guest-job-1"},
        json={
            "plan": "plus",
            "exit_country": "PH",
            "session": _session_payload("alice@example.test"),
            "card": {"number": "4242424242424242", "exp_month": "12", "exp_year": "2030", "cvc": "123"},  # release-check: ignore
        },
    )
    assert submit.status_code == 202
    created = submit.json()
    job_id = created["job_id"]
    guest_token = created.get("guest_token")
    assert guest_token
    assert created["masked_email"] == "al***@example.test"
    # Public response must not expose internal proxy metadata.
    assert "proxy_last_sid" not in created
    assert "proxy_last_exit_ip" not in created

    # Same guest sees the task.
    own_list = portal_client.get("/api/public/tasks", headers={"X-Portal-Guest-Token": guest_token})
    assert own_list.status_code == 200
    assert own_list.json()["total"] == 1
    assert own_list.json()["items"][0]["job_id"] == job_id

    # Another guest cannot view or fetch this task.
    portal_client.cookies.clear()
    other_list = portal_client.get("/api/public/tasks", headers={"X-Portal-Guest-Token": "gst_unrelated_other_token_123456"})
    assert other_list.status_code == 200
    assert other_list.json()["total"] == 0

    other_get = portal_client.get(
        f"/api/public/tasks/{job_id}",
        headers={"X-Portal-Guest-Token": "gst_unrelated_other_token_123456"},
    )
    assert other_get.status_code == 404

    # Register with original guest token -> claims guest task into user account.
    reg = portal_client.post(
        "/api/public/auth/register",
        headers={"X-Portal-Guest-Token": guest_token},
        json={"username": "alice_user", "password": "secretpassword"},
    )
    assert reg.status_code == 200
    assert reg.json()["authenticated"] is True
    assert reg.json()["claimed_tasks"] == 1

    # Simulate a second device (new cookies, no guest token) logging into alice_user.
    portal_client.cookies.clear()
    login = portal_client.post(
        "/api/public/auth/login",
        json={"username": "alice_user", "password": "secretpassword"},
    )
    assert login.status_code == 200
    assert login.json()["username"] == "alice_user"

    synced = portal_client.get("/api/public/tasks")
    assert synced.status_code == 200
    assert synced.json()["total"] == 1
    assert synced.json()["items"][0]["job_id"] == job_id

    detail = portal_client.get(f"/api/public/tasks/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["job_id"] == job_id


def test_public_cancel_auto_renew_fake_mode(portal_client: TestClient) -> None:
    resp = portal_client.post(
        "/api/public/account/cancel-auto-renew",
        json={"session": _session_payload(), "plan": "plus"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["canceled"] is True
    assert body["manual_required"] is False


def test_public_two_phase_subscription_and_billing_history(
    portal_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gss_api.routers import public_portal

    async def fake_basic(*_args, **_kwargs):
        return {
            "accounts": {"accounts": {"default": {"account": {"account_id": "acct-portal-1", "plan_type": "plus"}}}},
            "subscriptions": {
                "plan_type": "chatgptplusplan",
                "is_active": True,
                "is_processor_stripe": True,
                "billing_currency": "PHP",
            },
            "_portal_meta": {"attempted": False, "stage": "deferred", "has_portal": True},
        }

    async def fake_history(*_args, **_kwargs):
        return {
            "invoices": [
                {
                    "plan": "ChatGPT Plus",
                    "paidAt": "2026/10/25 08:00",
                    "status": "已支付",
                    "amount": "1100.00 PHP",
                    "invoiceUrl": "https://pay.openai.com/i/acct_1/inv_1",
                }
            ],
            "billingSource": "stripe_portal",
            "billingNote": None,
            "currency": "PHP",
            "retryable": False,
            "verified": True,
        }

    monkeypatch.setattr(public_portal, "fetch_basic_snapshot", fake_basic)
    monkeypatch.setattr(public_portal, "fetch_billing_history", fake_history)

    # Phase 1: fast basic info returns immediately with billingSource == "loading"
    p1 = portal_client.post(
        "/api/public/account/subscription-detail",
        json={"session": _session_payload("buyer@example.test"), "includeInvoices": False},
    )
    assert p1.status_code == 200
    d1 = p1.json()
    assert d1["planName"] == "ChatGPT Plus"
    assert d1["billingSource"] == "loading"
    assert d1["invoices"] == []

    # Phase 2: background billing history returns verified invoices & updates cache
    p2 = portal_client.post(
        "/api/public/account/billing-history",
        json={"session": _session_payload("buyer@example.test"), "planName": "ChatGPT Plus", "currency": "PHP"},
    )
    assert p2.status_code == 200
    d2 = p2.json()
    assert d2["billingSource"] == "stripe_portal"
    assert d2["retryable"] is False
    assert len(d2["invoices"]) == 1
    assert d2["invoices"][0]["amount"] == "1100.00 PHP"

    # Subsequent Phase 1 read hits cache and immediately returns cached invoices!
    p3 = portal_client.post(
        "/api/public/account/subscription-detail",
        json={"session": _session_payload("buyer@example.test"), "includeInvoices": False},
    )
    assert p3.status_code == 200
    d3 = p3.json()
    assert d3["cached"] is True
    assert len(d3["invoices"]) == 1


def test_public_and_admin_access_isolation(tmp_path: Path) -> None:
    creds = tmp_path / "auth.json"
    creds.write_text(json.dumps({"password": "admin-secret-pw"}))
    login = tmp_path / "login.html"
    login.write_text("<html>{{ERROR_HIDDEN}}{{INVALID}}</html>")

    app = FastAPI()

    @app.get("/")
    def root():
        return {"page": "portal"}

    @app.get("/admin")
    def admin_page():
        return {"page": "admin"}

    @app.get("/api/public/site-config")
    def pub_cfg():
        return {"connected": True}

    @app.get("/api/settings/proxies")
    def admin_api():
        return {"secret": True}

    app.add_middleware(
        TestAccessMiddleware,
        credentials_file=str(creds),
        login_file=str(login),
        origin="",
    )
    client = TestClient(app, base_url="https://pay.example.org", follow_redirects=False)

    # Public routes work without admin cookie.
    assert client.get("/").status_code == 200
    assert client.get("/api/public/site-config").status_code == 200

    # Admin page redirects to /login; admin API returns 401.
    assert client.get("/admin").status_code == 303
    assert client.get("/admin").headers["location"] == "/login"
    assert client.get("/api/settings/proxies").status_code == 401

    # Admin login redirects to /admin and unlocks admin API.
    res = client.post(
        "/login",
        data={"password": "admin-secret-pw"},
        headers={"Origin": "https://pay.example.org"},
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200
    assert client.get("/api/settings/proxies").status_code == 200
