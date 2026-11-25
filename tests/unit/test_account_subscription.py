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
        "id": "in_fixture_0000000001",
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
    assert detail["invoices"][0]["invoiceId"] == "in_fixture_0000000001"


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

    def post(self, url, json=None, headers=None, timeout=None):
        path = url.split("chatgpt.com", 1)[-1]
        self.paths.append("POST " + path)
        queue = self.plan.get(path)
        item = queue.pop(0) if queue else FakeResponse(404, {})
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
        "/backend-api/payments/transactions",
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


def test_stripe_portal_returns_a_stripe_url(monkeypatch):
    http = FakeHttp({
        "/backend-api/payments/customer_portal": [
            FakeResponse(200, {"url": "https://billing.stripe.com/p/session/abc"}),
        ],
    })
    _install_client(monkeypatch, http)

    result = account_detail.fetch_stripe_portal("s", "t", account_id="a", country="")
    assert result == {"url": "https://billing.stripe.com/p/session/abc",
                      "source": "/backend-api/payments/customer_portal"}


def test_stripe_portal_rejects_a_foreign_redirect(monkeypatch):
    http = FakeHttp({
        "/backend-api/payments/customer_portal": [
            FakeResponse(200, {"url": "https://evil.example/steal"}),
        ],
    })
    _install_client(monkeypatch, http)

    with pytest.raises(DomainError) as caught:
        account_detail.fetch_stripe_portal("s", "t", account_id="a", country="")
    assert caught.value.detail["code"] == "stripe_portal_unavailable"


def test_stripe_portal_reports_a_401(monkeypatch):
    http = FakeHttp({
        "/backend-api/payments/customer_portal": [
            FakeResponse(401, {"error": {"code": "token_expired"}}),
        ],
    })
    _install_client(monkeypatch, http, session_token="")

    with pytest.raises(DomainError) as caught:
        account_detail.fetch_stripe_portal("s", "t", account_id="a", country="")
    assert caught.value.code is ErrorCode.INVALID_SESSION
    assert caught.value.detail["upstream_code"] == "token_expired"


def test_stripe_portal_refreshes_session_on_401(monkeypatch):
    http = FakeHttp({
        "/backend-api/payments/customer_portal": [
            FakeResponse(401, {"error": {"code": "token_expired"}}),
            FakeResponse(200, {"url": "https://billing.stripe.com/p/session/refreshed"}),
        ],
    })
    fake = _install_client(monkeypatch, http)

    result = account_detail.fetch_stripe_portal("s", "t", account_id="a", country="")
    assert fake.refresh_calls == 1
    assert result["url"] == "https://billing.stripe.com/p/session/refreshed"


def test_stripe_portal_finds_nested_links_after_a_405(monkeypatch):
    http = FakeHttp({
        "/backend-api/payments/customer_portal": [
            FakeResponse(200, {"data": {"customer_portal_url": "https://billing.stripe.com/p/session/nested"}}),
        ],
    })
    _install_client(monkeypatch, http)

    result = account_detail.fetch_stripe_portal("s", "t", account_id="a", country="")
    assert result["url"] == "https://billing.stripe.com/p/session/nested"


def test_stripe_portal_get_is_tried_before_post(monkeypatch):
    http = FakeHttp({
        "/backend-api/payments/customer_portal": [
            FakeResponse(200, {"url": "https://billing.stripe.com/p/session/get-first"}),
        ],
    })
    _install_client(monkeypatch, http)

    account_detail.fetch_stripe_portal("s", "t", account_id="a", country="")
    assert http.paths[0] == "/backend-api/payments/customer_portal"
    assert not http.paths[0].startswith("POST ")


def test_extract_invoices_from_json_handles_nested_stripe_payload():
    from gss_api.stripe_portal_invoices import extract_invoices_from_json

    inv = {
        "id": "in_test_001",
        "object": "invoice",
        "amount_paid": 2000,
        "currency": "usd",
        "status": "paid",
        "created": 1760000000,
        "status_transitions": {"paid_at": 1760000120},
        "hosted_invoice_url": "https://invoice.stripe.com/i/acct_1/test_001",
        "lines": {"data": [{"description": "1 × ChatGPT Plus (at $20.00 / month)"}]},
    }
    payload = {
        "invoices": {"object": "list", "data": [inv]},
        "subscriptions": {"data": [{"latest_invoice": inv}]},
        "upcoming_invoice": {"object": "invoice", "status": "draft", "amount_due": 2000, "currency": "usd"},
    }
    rows = extract_invoices_from_json(payload)
    # Upcoming draft without id is skipped; duplicate in_test_001 appears twice before dedupe
    assert any(r["id"] == "in_test_001" for r in rows)
    first = rows[0]
    assert first["paid_at"] == 1760000120
    assert first["amount_paid"] == 2000
    assert first["amount_unit"] == "minor"
    assert first["currency"] == "USD"
    assert first["invoice_url"] == "https://invoice.stripe.com/i/acct_1/test_001"
    assert "ChatGPT Plus" in first["description"]
    assert all(r["status"] != "draft" for r in rows)


def test_extract_invoices_from_html_parses_embedded_bootstrap_json():
    from gss_api.stripe_portal_invoices import extract_invoices_from_html

    blob = json.dumps({
        "invoice_history": [
            {
                "id": "in_html_1",
                "object": "invoice",
                "amount_paid": 3000,
                "currency": "jpy",
                "status": "paid",
                "created": 1761000000,
                "hosted_invoice_url": "https://invoice.stripe.com/i/acct_1/html_1",
                "lines": {"data": [{"description": "ChatGPT Plus"}]},
            }
        ]
    })
    html_doc = f'<html><body><script type="application/json" id="boot">{blob}</script></body></html>'
    rows = extract_invoices_from_html(html_doc)
    assert len(rows) == 1
    assert rows[0]["id"] == "in_html_1"
    assert rows[0]["currency"] == "JPY"
    assert rows[0]["amount_paid"] == 3000


def test_parse_dom_invoice_rows_handles_currencies_and_dates():
    from gss_api.stripe_portal_invoices import parse_dom_invoice_rows

    rows = parse_dom_invoice_rows([
        {
            "text": "Oct 22, 2026  $20.00  Paid  ChatGPT Plus",
            "href": "https://invoice.stripe.com/i/acct_1/dom_1",
        },
        {
            "text": "2026年09月22日  ₱1,100  Refunded",
            "href": "https://evil.example/phish",
        },
        {
            "text": "22 Aug 2026  ¥3,000  Paid  ChatGPT Pro",
            "href": "https://pay.stripe.com/invoice/acct_1/dom_3",
        },
    ])
    assert len(rows) == 3
    assert rows[0]["amount_paid"] == "20.00"
    assert rows[0]["amount_unit"] == "major"
    assert rows[0]["currency"] == "USD"
    assert rows[0]["description"] == "plus"
    assert rows[0]["invoice_url"] == "https://invoice.stripe.com/i/acct_1/dom_1"

    assert rows[1]["amount_paid"] == "1100"
    assert rows[1]["currency"] == "PHP"
    assert rows[1]["status"] == "refunded"
    assert "invoice_url" not in rows[1]  # non-stripe host rejected

    assert rows[2]["amount_paid"] == "3000"
    assert rows[2]["currency"] == "JPY"
    assert rows[2]["description"] == "pro"


def test_fetch_snapshot_falls_back_to_stripe_portal_invoices_when_billing_apis_404(monkeypatch):
    import asyncio

    t_old = int(datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc).timestamp())
    t_new = int(datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc).timestamp())
    http = FakeHttp({
        "/backend-api/accounts/check/v4-2023-04-27": [
            FakeResponse(200, {"accounts": {"default": {"account": {"account_id": "acc-1", "plan_type": "plus"}}}})
        ],
        "/backend-api/subscriptions": [
            FakeResponse(200, {
                "plan_type": "chatgptplusplan",
                "is_active": True,
                "is_processor_stripe": True,
                "billing_currency": "USD",
                "current_billing_period": None,
                "last_active_subscription": {
                    "purchase_origin_platform": "chatgpt_web",
                    "subscription_id": "sub_test_1",
                    "will_renew": True,
                },
            })
        ],
        "/backend-api/payments/invoices": [FakeResponse(404, {})],
        "/backend-api/payments/billing_history": [FakeResponse(404, {})],
        "/backend-api/payments/receipts": [FakeResponse(404, {})],
        "/backend-api/accounts/acc-1/billing_history": [FakeResponse(404, {})],
        "/backend-api/accounts/acc-1/invoices": [FakeResponse(404, {})],
        "/backend-api/payments/transactions": [FakeResponse(404, {})],
        "/backend-api/payments/customer_portal": [
            FakeResponse(200, {"url": "https://billing.stripe.com/p/session/live_test_portal"})
        ],
    })
    _install_client(monkeypatch, http)

    captured = {}

    def fake_portal_invoices(url, *, proxy=None, timeout=20.0):
        captured["url"] = url
        return (
            [
                {
                    "id": "in_old",
                    "paid_at": t_old,
                    "amount_paid": 2000,
                    "amount_unit": "minor",
                    "currency": "USD",
                    "status": "paid",
                    "description": "1 × ChatGPT Plus (at $20.00 / month)",
                    "invoice_url": "https://invoice.stripe.com/i/acct_1/old",
                },
                {
                    "id": "in_new",
                    "paid_at": t_new,
                    "amount_paid": 2000,
                    "amount_unit": "minor",
                    "currency": "USD",
                    "status": "paid",
                    "description": "",
                    "invoice_url": "https://invoice.stripe.com/i/acct_1/new",
                },
            ],
            "browser_xhr",
        )

    monkeypatch.setattr(account_detail, "fetch_portal_invoices", fake_portal_invoices)

    merged = asyncio.run(account_detail.fetch_snapshot("s", "t", account_id="acc-1", country=""))
    assert captured["url"] == "https://billing.stripe.com/p/session/live_test_portal"

    detail = normalize(merged, email="plus@example.test", account_id="acc-1")
    assert detail["paymentChannel"] == "网页支付"
    assert detail["billingSource"] == "stripe_portal"
    assert detail["billingNote"] is None
    assert len(detail["invoices"]) == 2
    assert detail["invoices"][0]["paidAt"] == "2026/10/22 08:00"
    assert detail["invoices"][0]["plan"] == "ChatGPT Plus"  # fallback from account plan when description empty
    assert detail["invoices"][0]["amount"] == "20.00 USD"
    assert detail["invoices"][0]["invoiceUrl"] == "https://invoice.stripe.com/i/acct_1/new"
    assert detail["invoices"][1]["paidAt"] == "2026/09/22 08:00"
    assert detail["invoices"][1]["plan"] == "ChatGPT Plus"


def test_zero_decimal_currency_and_major_unit_amounts_are_not_divided():
    when = int(datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp())
    detail = normalize({
        "subscriptions": {"plan_type": "chatgptplusplan", "is_active": True},
        "portal_invoices": [
            {
                "paid_at": when,
                "amount_paid": 3000,
                "amount_unit": "minor",
                "currency": "JPY",
                "status": "paid",
                "description": "ChatGPT Plus",
            },
            {
                "paid_at": when - 86400,
                "amount_paid": "1100",
                "amount_unit": "major",
                "currency": "PHP",
                "status": "paid",
                "description": "ChatGPT Plus",
            },
        ],
    }, email="jp@example.test", account_id="acc-jp")
    assert detail["invoices"][0]["amount"] == "3000 JPY"
    assert detail["invoices"][1]["amount"] == "1100.00 PHP"


def test_purchase_origin_platform_maps_mobile_channel_and_skips_stripe_portal(monkeypatch):
    import asyncio

    http = FakeHttp({
        "/backend-api/subscriptions": [
            FakeResponse(200, {
                "plan_type": "chatgptplusplan",
                "is_active": True,
                "is_processor_stripe": False,
                "last_active_subscription": {
                    "purchase_origin_platform": "chatgpt_ios",
                    "subscription_id": "apple_sub_1",
                    "will_renew": True,
                },
            })
        ],
        "/backend-api/payments/invoices": [FakeResponse(404, {})],
        "/backend-api/payments/billing_history": [FakeResponse(404, {})],
        "/backend-api/payments/receipts": [FakeResponse(404, {})],
        "/backend-api/payments/transactions": [FakeResponse(404, {})],
    })
    _install_client(monkeypatch, http)

    merged = asyncio.run(account_detail.fetch_snapshot("s", "t", account_id="", country=""))
    assert "/backend-api/payments/customer_portal" not in http.paths

    detail = normalize(merged, email="ios@example.test", account_id="acc-ios")
    assert detail["paymentChannel"] == "App Store"
    assert detail["billingSource"] == "mobile_store"
    assert "App Store" in (detail["billingNote"] or "")


def test_stale_cache_version_is_ignored_after_bump(api):
    client, calls, db = api
    db.save_account_snapshot(
        "acc-fixture",
        email="fixture@example.test",
        account_id="acc-fixture",
        detail={
            "email": "fixture@example.test",
            "accountId": "acc-fixture",
            "status": "使用中",
            "planName": "ChatGPT Plus",
            "autoRenew": "已开启",
            "paymentChannel": "网页支付",
            "currency": "USD",
            "invoices": [],
            "queriedAt": "2026/11/01 08:00",
            "cacheVersion": 3,
        },
        queried_at="2026/11/01 08:00",
    )
    assert account_detail.CACHE_VERSION >= 4
    resp = post(client, session=session_json())
    assert resp.status_code == 200
    assert resp.json()["cached"] is False
    assert calls["count"] == 1


def test_stripe_portal_accepts_openai_custom_domain_and_invoice_links(monkeypatch):
    from gss_api.stripe_portal_invoices import _safe_portal_entry_url, safe_invoice_url

    portal_url = "https://pay.openai.com/p/session/live_YWNjdF90ZXN0"
    http = FakeHttp({
        "/backend-api/payments/customer_portal": [
            FakeResponse(200, {"url": portal_url}),
        ],
    })
    _install_client(monkeypatch, http)

    result = account_detail.fetch_stripe_portal("s", "t", account_id="a", country="")
    assert result == {"url": portal_url, "source": "/backend-api/payments/customer_portal"}
    assert _safe_portal_entry_url(portal_url) == portal_url
    assert safe_invoice_url("https://pay.openai.com/i/acct_1/inv_1") == "https://pay.openai.com/i/acct_1/inv_1"
    assert account_detail._safe_portal_url("https://chatgpt.com/") == ""
    assert safe_invoice_url("https://evil.example/i/acct_1/inv_1") is None


def test_safe_url_hint_redacts_portal_tokens():
    hint = account_detail._safe_url_hint({"url": "https://pay.openai.com/p/session/live_secret_token_12345?foo=bar"})
    assert hint == {
        "key": "url",
        "type": "str",
        "scheme": "https",
        "host": "pay.openai.com",
        "path_prefix": "/p/session",
    }
    assert "secret" not in str(hint)
    assert account_detail._safe_url_hint({"url": None}) == {"key": "url", "type": "NoneType"}
