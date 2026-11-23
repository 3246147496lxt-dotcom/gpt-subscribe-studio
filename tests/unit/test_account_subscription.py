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


def test_parallel_failure_falls_back_to_one_connection(monkeypatch):
    import asyncio

    from gss_api import account_detail

    calls: list[str] = []

    def flaky(session, token, path, params, *, proxy, timeout):
        calls.append(path)
        if len(calls) <= 5:          # all parallel tunnels fail
            raise OSError("proxy busy")
        return {"path": path}        # the sequential retry succeeds

    monkeypatch.setattr(account_detail, "_fetch_one", flaky)
    monkeypatch.setattr(account_detail, "configured_proxy", lambda role: None)

    merged = asyncio.run(account_detail.fetch_snapshot("s", "t", account_id="", country=""))
    assert set(merged) == {"accounts", "subscriptions", "invoices",
                           "billing_history", "receipts"}
    assert len(calls) == 10


def test_billing_history_drives_currency_start_and_amount():
    merged = {
        "subscriptions": {"plan_type": "chatgptplusplan", "is_active": True,
                          "current_period_end": "2026-11-22T00:00:00Z"},
        "billing_history": {"items": [
            {"plan_name": "chatgptplusplan",
             "created": int(datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp()),
             "status": "paid", "amount": 98214, "currency": "PHP"},
            {"plan_name": "chatgptplusplan",
             "created": int(datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp()),
             "status": "paid", "amount": 98214, "currency": "PHP"},
        ]},
    }
    detail = normalize(merged, email="e@example.test", account_id="a",
                       now=datetime(2026, 11, 22, tzinfo=timezone.utc))

    assert len(detail["invoices"]) == 2
    assert detail["currency"] == "PHP"
    assert detail["startTime"] == "2026/10/22 08:00"   # latest settled invoice
    assert detail["invoices"][0]["amount"] == "982.14 PHP"
    assert detail["invoices"][1]["paidAt"] == "2026/09/22 08:00"


def test_receipts_and_invoices_are_merged_without_duplicates():
    when = int(datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp())
    row = {"plan_name": "chatgptplusplan", "created": when, "status": "paid",
           "amount": 98214, "currency": "PHP"}
    detail = normalize(
        {"receipts": {"receipts": [row]}, "invoices": {"data": [dict(row)]}},
        email="e@example.test", account_id="a",
    )
    assert len(detail["invoices"]) == 1


def test_currency_prefers_billing_currency_over_hardcoded_default():
    detail = normalize(
        {"subscriptions": {"plan_type": "chatgptplusplan", "is_active": True,
                           "billing_currency": "PHP"}},
        email="e@example.test", account_id="a",
    )
    assert detail["currency"] == "PHP"


def test_missing_currency_is_dash_not_usd():
    detail = normalize(
        {"subscriptions": {"plan_type": "chatgptplusplan", "is_active": True}},
        email="e@example.test", account_id="a",
    )
    assert detail["currency"] == "-"


def test_start_time_is_estimated_from_expiry_as_a_last_resort():
    detail = normalize(
        {"subscriptions": {"plan_type": "chatgptplusplan", "is_active": True,
                           "current_period_end": "2026-12-01T00:00:00Z"}},
        email="e@example.test", account_id="a",
        now=datetime(2026, 11, 22, tzinfo=timezone.utc),
    )
    assert detail["startTime"] == "2026/11/01 08:00（约）"
