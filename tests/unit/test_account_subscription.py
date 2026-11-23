"""Account subscription detail: credential parsing, mapping and cache behavior."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gss_api import account_detail, persistence
from gss_api.account_detail import CredentialError, normalize, parse_credentials
from gss_api.routers import account
from gss_contracts.errors import DomainError, ErrorCode


def jwt(*, email="fixture@example.test", account_id="acc-fixture", expires_in=3600) -> str:
    payload = {
        "exp": int(datetime.now(timezone.utc).timestamp()) + expires_in,
        "https://api.openai.com/profile": {"email": email},
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return "header." + body + ".signature"


def session_json(token: str | None = None) -> str:
    return json.dumps({"accessToken": token or jwt(), "sessionToken": "session-fixture"})


def test_credentials_extract_email_and_account():
    credentials = parse_credentials(session_json())
    assert credentials["email"] == "fixture@example.test"
    assert credentials["account_id"] == "acc-fixture"
    assert credentials["access_token"].count(".") == 2


def test_expired_token_is_rejected_before_any_upstream_call():
    with pytest.raises(CredentialError) as caught:
        parse_credentials(session_json(jwt(expires_in=-60)))
    assert caught.value.detail["code"] == "session_expired"
    assert caught.value.code is ErrorCode.INVALID_SESSION


@pytest.mark.parametrize("raw", ["", "{not-json", json.dumps({"sessionToken": "x"})])
def test_invalid_session_shapes_are_rejected(raw):
    with pytest.raises(CredentialError):
        parse_credentials(raw)


MERGED = {
    "accounts": {"accounts": {"default": {"account": {
        "account_id": "acc-fixture", "plan_type": "plus", "email": "fixture@example.test",
    }}}},
    "subscriptions": {
        "plan_type": "chatgptplusplan",
        "is_active": True,
        "current_period_start": "2026-11-01T00:00:00Z",
        "current_period_end": "2026-12-01T00:00:00Z",
        "cancel_at_period_end": True,
        "currency": "PHP",
        "payment_provider": "stripe",
    },
    "invoices": {"data": [{
        "plan_name": "chatgptplusplan",
        "created": int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()),
        "status": "paid",
        "amount": 98214,
        "currency": "PHP",
    }]},
}


def test_normalize_maps_upstream_fields_to_display_values():
    now = datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc)
    detail = normalize(MERGED, email="fixture@example.test", account_id="acc-fixture", now=now)

    assert detail["status"] == "使用中"
    assert detail["planName"] == "ChatGPT Plus"
    assert detail["startTime"] == "2026/11/01 08:00"       # UTC+8
    assert detail["endTime"] == "2026/12/01 08:00"
    assert detail["remainingDays"] == 9
    assert detail["autoRenew"] == "已关闭"
    assert detail["paymentChannel"] == "网页支付"
    assert detail["currency"] == "PHP"
    assert detail["invoices"][0]["plan"] == "ChatGPT Plus"
    assert detail["invoices"][0]["status"] == "已支付"
    assert detail["invoices"][0]["amount"] == "982.14 PHP"
    assert detail["invoices"][0]["paidAt"] == "2026/11/01 08:00"


def test_normalize_empty_account_is_not_subscribed():
    detail = normalize({}, email="free@example.test", account_id="acc-free")
    assert detail["status"] == "未订阅"
    assert detail["planName"] == "未知套餐"
    assert detail["invoices"] == []
    assert detail["remainingDays"] is None


def test_normalize_expired_plan_is_marked_expired():
    merged = {
        "accounts": {"accounts": {"default": {"account": {"plan_type": "plus"}}}},
        "subscriptions": {"plan_type": "chatgptplusplan", "is_active": False,
                          "current_period_end": "2026-11-01T00:00:00Z"},
    }
    detail = normalize(merged, email="e@example.test", account_id="a")
    assert detail["status"] == "已过期"


@pytest.fixture
def api(monkeypatch):
    db = persistence.Persistence(":memory:")
    monkeypatch.setattr(persistence, "_db", db)
    calls = {"count": 0}

    async def fake_fetch(session, access_token, *, account_id, country, timeout=None):
        calls["count"] += 1
        return MERGED

    monkeypatch.setattr(account, "fetch_snapshot", fake_fetch)
    app = FastAPI()
    app.include_router(account.router)
    try:
        yield TestClient(app), calls, db
    finally:
        db.close()


def post(client, **body):
    return client.post("/api/account/subscription-detail", json=body)


def test_first_query_hits_upstream_and_second_serves_cache(api):
    client, calls, db = api
    first = post(client, session=session_json())
    assert first.status_code == 200
    assert first.json()["cached"] is False
    assert calls["count"] == 1
    assert db.get_account_snapshot("acc-fixture") is not None

    second = post(client, session=session_json())
    assert second.status_code == 200
    assert second.json()["cached"] is True
    assert calls["count"] == 1  # cache hit must not touch the upstream


def test_force_refresh_bypasses_the_cache(api):
    client, calls, _db = api
    post(client, session=session_json())
    refreshed = post(client, session=session_json(), forceRefresh=True)
    assert refreshed.status_code == 200
    assert refreshed.json()["cached"] is False
    assert calls["count"] == 2


def test_upstream_failure_serves_stale_cache_with_warning(api, monkeypatch):
    client, _calls, _db = api
    post(client, session=session_json())

    async def failing(session, access_token, *, account_id, country, timeout=None):
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "上游不可达",
                          detail={"code": "upstream_unavailable"})

    monkeypatch.setattr(account, "fetch_snapshot", failing)
    response = post(client, session=session_json(), forceRefresh=True)
    assert response.status_code == 200
    assert response.json()["stale"] is True
    assert response.json()["warning"]


def test_revoked_token_is_not_masked_by_a_cached_snapshot(api, monkeypatch):
    client, _calls, _db = api
    post(client, session=session_json())

    async def revoked(session, access_token, *, account_id, country, timeout=None):
        raise DomainError(ErrorCode.INVALID_SESSION, "Session 已被上游撤销",
                          detail={"code": "token_revoked"})

    monkeypatch.setattr(account, "fetch_snapshot", revoked)
    response = post(client, session=session_json(), forceRefresh=True)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "token_revoked"


def test_missing_session_returns_a_typed_error(api):
    client, _calls, _db = api
    response = post(client, session="")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "session_required"


def test_accounts_check_is_the_fallback_when_subscriptions_is_empty():
    merged = {"accounts": {"accounts": {"default": {"account": {
        "account_id": "acc-fixture", "plan_type": "plus", "email": "fixture@example.test",
    }}}}}
    detail = normalize(merged, email="", account_id="")
    assert detail["email"] == "fixture@example.test"
    assert detail["accountId"] == "acc-fixture"
    assert detail["planName"] == "ChatGPT Plus"
    assert detail["status"] == "使用中"


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None,
                 headers: dict | None = None, text: str | None = None):
        self.status_code = status
        self.payload = payload or {}
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self.payload)

    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self, plan):
        self.plan = plan          # path -> list[FakeResponse]
        self.paths: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.split("chatgpt.com", 1)[-1]
        self.paths.append(path)
        queue = self.plan.get(path)
        item = queue.pop(0) if queue else FakeResponse(200, {})
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClient:
    def __init__(self, http, *, session_token="session-fixture", token="token-fixture"):
        self.http = http
        self.session_token = session_token
        self.access_token = token
        self.timeout = 5.0
        self.refresh_calls = 0

    def _route_headers(self, path):
        return {}

    def _identity_headers(self):
        return {}

    def refresh_session(self, **kwargs):
        self.refresh_calls += 1
        self.access_token = jwt()
        return True

    def close(self):
        pass


def _install_client(monkeypatch, http, **kwargs):
    from gss_core.chatgpt import ChatGPTClient

    fake = FakeClient(http, **kwargs)
    monkeypatch.setattr(ChatGPTClient, "from_raw_session",
                        staticmethod(lambda *a, **k: fake))
    monkeypatch.setattr(account_detail, "configured_proxy", lambda role: None)
    return fake


def test_endpoints_are_queried_serially_on_one_client(monkeypatch):
    import asyncio

    http = FakeHttp({})
    _install_client(monkeypatch, http)

    asyncio.run(account_detail.fetch_snapshot("s", "t", account_id="", country=""))

    assert http.paths == [
        "/backend-api/accounts/check/v4-2023-04-27",
        "/backend-api/subscriptions",
        "/backend-api/payments/invoices",
        "/backend-api/payments/billing_history",
        "/backend-api/payments/receipts",
    ]


def test_401_exchanges_the_session_cookie_and_retries_once(monkeypatch):
    import asyncio

    http = FakeHttp({
        "/backend-api/subscriptions": [
            FakeResponse(401, {"error": {"code": "token_expired"}}),
            FakeResponse(200, {"plan_type": "chatgptplusplan"}),
        ],
    })
    fake = _install_client(monkeypatch, http)

    merged = asyncio.run(account_detail.fetch_snapshot("s", "t", account_id="", country=""))

    assert fake.refresh_calls == 1
    assert "subscriptions" in merged
    assert http.paths.count("/backend-api/subscriptions") == 2


def test_401_without_session_cookie_reports_the_upstream_code(monkeypatch):
    import asyncio

    http = FakeHttp({
        "/backend-api/accounts/check/v4-2023-04-27": [
            FakeResponse(401, {"error": {"code": "token_expired"}},
                         headers={"cf-ray": "ray-fixture"}),
        ],
    })
    _install_client(monkeypatch, http, session_token="", token=jwt(expires_in=120))

    with pytest.raises(DomainError) as caught:
        asyncio.run(account_detail.fetch_snapshot("s", jwt(expires_in=120),
                                                  account_id="", country=""))

    detail = caught.value.detail
    assert detail["upstream_code"] == "token_expired"
    assert detail["code"] != "token_revoked"          # not hardcoded
    assert detail["cf_ray"] == "ray-fixture"
    assert detail["jwt_expires_at"] is not None


def test_403_is_reported_as_a_forbidden_upstream(monkeypatch):
    import asyncio

    http = FakeHttp({
        "/backend-api/accounts/check/v4-2023-04-27": [
            FakeResponse(403, text="<html>Just a moment...</html>"),
        ],
    })
    _install_client(monkeypatch, http)

    with pytest.raises(DomainError) as caught:
        asyncio.run(account_detail.fetch_snapshot("s", "t", account_id="", country=""))

    assert caught.value.detail["code"] == "cloudflare_challenge"


def test_active_start_maps_to_start_time():
    detail = normalize({
        "subscriptions": {
            "plan_type": "chatgptplusplan", "is_active": True,
            "active_start": "2026-10-22T00:00:00Z",
            "active_until": "2026-11-22T00:00:00Z",
        },
    }, email="e@example.test", account_id="a")
    assert detail["startTime"] == "2026/10/22 08:00"
    assert detail["endTime"] == "2026/11/22 08:00"


def test_one_transport_failure_does_not_abort_the_other_endpoints(monkeypatch):
    import asyncio

    http = FakeHttp({
        "/backend-api/accounts/check/v4-2023-04-27": [FakeResponse(200, {"accounts": {"a": 1}})],
        "/backend-api/subscriptions": [FakeResponse(200, {"plan_type": "chatgptplusplan"})],
        "/backend-api/payments/invoices": [OSError("connection reset")],
    })
    _install_client(monkeypatch, http)

    merged = asyncio.run(account_detail.fetch_snapshot("s", "t", account_id="", country=""))

    assert "accounts" in merged and "subscriptions" in merged
    assert "/backend-api/payments/receipts" in http.paths   # kept going after the failure


def test_current_billing_period_is_used_as_a_bill_fallback():
    when = int(datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp())
    detail = normalize({
        "subscriptions": {
            "plan_type": "chatgptplusplan", "is_active": True,
            "billing_currency": "PHP",
            "current_billing_period": {
                "amount": 98214, "currency": "PHP", "status": "paid", "created": when,
            },
        },
    }, email="e@example.test", account_id="a")
    assert detail["currency"] == "PHP"
    assert detail["invoices"][0]["amount"] == "982.14 PHP"
