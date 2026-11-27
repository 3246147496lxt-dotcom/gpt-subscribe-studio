from types import SimpleNamespace
import json

import pytest

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ChatGPTClient


class _Response:
    status_code = 200
    url = "https://chatgpt.com/backend-api/subscriptions/cancel"
    text = "{}"

    def json(self):
        return {}


class _Http:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None, **kwargs):
        self.calls.append(("POST", url, json, headers))
        return _Response()

    def get(self, url, **kwargs):
        return _Response()


def test_cancel_auto_renew_uses_template_and_verifies(monkeypatch):
    monkeypatch.setenv("GSS_CANCEL_AUTORENEW_PATH", "/backend-api/subscriptions/{subscription_id}/cancel")
    monkeypatch.setenv("GSS_CANCEL_AUTORENEW_METHOD", "POST")
    monkeypatch.setenv("GSS_CANCEL_AUTORENEW_BODY",
                       '{"plan_type":"{plan_type}","subscription_id":"{subscription_id}","account_id":"{account_id}"}')
    client = ChatGPTClient(access_token="token", account_id="acct-1")
    http = _Http()
    client._http = http
    calls = {"n": 0}

    def subscription_payload():
        calls["n"] += 1
        base = {"will_renew": True} if calls["n"] == 1 else {"will_renew": False}
        base["entitlement"] = {"subscription_id": "sub_nested_1"}
        return base

    monkeypatch.setattr(client, "subscription_payload", subscription_payload)
    result = client.cancel_auto_renew(plan_type="pro_lite")
    assert result["canceled"] is True
    assert result["will_renew"] is False
    assert http.calls and http.calls[0][1].endswith("/backend-api/subscriptions/sub_nested_1/cancel")
    assert http.calls[0][2] == {
        "plan_type": "prolite",
        "subscription_id": "sub_nested_1",
        "account_id": "acct-1",
    }


def test_cancel_auto_renew_requires_verified_endpoint(monkeypatch):
    monkeypatch.delenv("GSS_CANCEL_AUTORENEW_PATH", raising=False)
    client = ChatGPTClient(access_token="token")
    with pytest.raises(DomainError) as exc:
        client.cancel_auto_renew(plan_type="pro_lite")
    assert "未配置" in exc.value.message
    assert exc.value.code is ErrorCode.SUBSCRIPTION_NOT_ACTIVE


def test_order_flow_cancel_marks_canceled(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from gss_api.services import order_flow
    from gss_api.store import OrderView
    from gss_contracts import Order, OrderStatus, Plan

    store = order_flow.orders.store
    store.orders.clear()
    store.views.clear()
    order = Order(id="cancel-order", plan=Plan.PRO_LITE, billing_country="US", status=OrderStatus.ACTIVE)
    store.orders[order.id] = order
    view = OrderView(payment_status="succeeded", auto_cancel_renew=True,
                     auto_renew_status="pending")
    store.views[order.id] = view
    job = order_flow.QuickPayJob(id="cancel-job", idempotency_key="cancel-key")
    monkeypatch.setattr(order_flow.orders.vault, "get", lambda oid: SimpleNamespace(session="s", proxy="http://p"))
    class FakeClient:
        def cancel_auto_renew(self, **kwargs):
            return {"canceled": True}
        def close(self):
            pass
    monkeypatch.setattr(order_flow.ChatGPTClient, "from_raw_session", classmethod(lambda cls, *a, **kw: FakeClient()))
    asyncio.run(order_flow._cancel_auto_renew(job, order_id=order.id, plan="pro_lite"))
    assert view.auto_renew_status == "canceled"
    assert job.auto_renew_status == "canceled"


class _RedirectHttp(_Http):
    def post(self, url, **kwargs):
        return type("R", (), {"status_code": 302, "url": url, "text": "",
                               "headers": {"location": "https://billing.stripe.com/p/session/x"},
                               "json": lambda self: {}})()


def test_cancel_redirects_to_stripe_portal(monkeypatch):
    monkeypatch.setenv("GSS_CANCEL_AUTORENEW_PATH", "/backend-api/subscriptions/cancel")
    client = ChatGPTClient(access_token="token", account_id="acct-1")
    client._http = _RedirectHttp()
    monkeypatch.setattr(client, "subscription_payload", lambda: {"entitlement": {"subscription_id": "sub_1"}})
    with pytest.raises(DomainError) as exc:
        client.cancel_auto_renew(plan_type="pro_lite")
    assert "Stripe" in exc.value.message


def test_will_renew_handles_cancel_at_period_end_and_cancels_at():
    from gss_core.chatgpt_parsers import _will_renew

    assert _will_renew({"last_active_subscription": {"will_renew": False}}) is False
    assert _will_renew({"cancel_at_period_end": True}) is False
    assert _will_renew({"entitlement": {"cancels_at": "2026-11-26T07:35:00Z", "renews_at": None}}) is False
    assert _will_renew({"entitlement": {"renews_at": "2026-11-26T07:35:00Z"}}) is None


def test_extract_portal_cancel_signals_reads_feature_flags_and_subscriptions():
    from gss_api.stripe_portal_invoices import _extract_portal_cancel_signals

    sig = _extract_portal_cancel_signals({
        "configuration": {
            "features": {
                "subscription_cancel": {
                    "enabled": True,
                    "mode": "at_period_end",
                    "cancellation_reason": {"enabled": True},
                }
            }
        },
        "subscriptions": {
            "data": [{
                "object": "subscription",
                "id": "sub_1234567890",
                "status": "active",
                "cancel_at_period_end": True,
            }]
        },
    })
    assert sig["cancel_enabled"] is True
    assert sig["cancel_mode"] == "at_period_end"
    assert sig["reason_enabled"] is True
    assert sig["canceled_confirmed"] is True
    assert len(sig["subscriptions"]) == 1


def test_cancel_account_auto_renew_via_stripe_portal_updates_cache(monkeypatch, tmp_path):
    import base64
    from datetime import datetime, timezone
    from gss_api import auto_renew, persistence

    monkeypatch.delenv("GSS_CANCEL_AUTORENEW_PATH", raising=False)
    monkeypatch.setattr(auto_renew, "use_fake_adapters", lambda: False)
    monkeypatch.setattr(auto_renew, "configured_proxy", lambda role: None)

    db = persistence.Persistence(str(tmp_path / "cancel-test.db"))
    monkeypatch.setattr(persistence, "_db", db)
    db.save_account_snapshot(
        "acc-portal",
        email="u@example.test",
        account_id="acc-portal",
        detail={
            "email": "u@example.test",
            "accountId": "acc-portal",
            "status": "使用中",
            "planName": "ChatGPT Plus",
            "autoRenew": "已开启",
            "invoices": [{"plan": "ChatGPT Plus", "paidAt": "2026/10/26 15:36", "status": "已支付", "amount": "982.14 PHP"}],
            "cacheVersion": auto_renew.CACHE_VERSION,
        },
        queried_at="2026/11/25 10:00",
    )

    payload = {
        "exp": int(datetime.now(timezone.utc).timestamp()) + 3600,
        "https://api.openai.com/profile": {"email": "u@example.test"},
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-portal"},
    }
    tok = "h." + base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode() + ".s"
    sess = json.dumps({"accessToken": tok, "sessionToken": "st"})

    calls = {"sub": 0}

    class FakeClient:
        access_token = tok
        account_id = "acc-portal"
        session_token = "st"
        timeout = 10.0

        def subscription_payload(self):
            calls["sub"] += 1
            return {
                "is_active": True,
                "plan_type": "chatgptplusplan",
                "active_until": "2026-11-26T07:35:00Z",
                "is_processor_stripe": True,
                "last_active_subscription": {"will_renew": calls["sub"] == 1},
            }

        def close(self):
            pass

    monkeypatch.setattr(auto_renew.ChatGPTClient, "from_raw_session", classmethod(lambda cls, *a, **k: FakeClient()))
    monkeypatch.setattr(
        auto_renew,
        "_request_portal_url",
        lambda client, account_id, timeout, allow_refresh=True: (
            "https://pay.openai.com/p/session/live_test123",
            "/backend-api/payments/customer_portal",
            ["/backend-api/payments/customer_portal:200"],
        ),
    )
    monkeypatch.setattr(
        auto_renew,
        "cancel_portal_subscription",
        lambda url, **kw: {"canceled": True, "stage": "portal_canceled", "xhr_confirmed": True},
    )

    try:
        res = auto_renew.cancel_account_auto_renew(sess, plan="plus")
        assert res["canceled"] is True
        assert res["will_renew"] is False
        assert res["end_time"] == "2026/11/26 15:35"
        assert "2026/11/26 15:35" in res["message"]
        cached = db.get_account_snapshot("acc-portal")
        assert cached["autoRenew"] == "已关闭"
        assert len(cached["invoices"]) == 1  # invoices preserved
    finally:
        db.close()


def test_cancel_account_auto_renew_is_idempotent_when_already_off(monkeypatch):
    import base64
    from datetime import datetime, timezone
    from gss_api import auto_renew

    monkeypatch.delenv("GSS_CANCEL_AUTORENEW_PATH", raising=False)
    monkeypatch.setattr(auto_renew, "use_fake_adapters", lambda: False)
    monkeypatch.setattr(auto_renew, "configured_proxy", lambda role: None)

    payload = {
        "exp": int(datetime.now(timezone.utc).timestamp()) + 3600,
        "https://api.openai.com/profile": {"email": "u@example.test"},
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-already"},
    }
    tok = "h." + base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode() + ".s"
    sess = json.dumps({"accessToken": tok})

    class FakeClient:
        access_token = tok
        account_id = "acc-already"
        session_token = ""
        timeout = 10.0

        def subscription_payload(self):
            return {
                "is_active": True,
                "plan_type": "chatgptplusplan",
                "active_until": "2026-11-26T07:35:00Z",
                "last_active_subscription": {"will_renew": False},
            }

        def close(self):
            pass

    monkeypatch.setattr(auto_renew.ChatGPTClient, "from_raw_session", classmethod(lambda cls, *a, **k: FakeClient()))
    res = auto_renew.cancel_account_auto_renew(sess)
    assert res["canceled"] is True
    assert res["stage"] == "already_canceled"


def test_cancel_account_auto_renew_rotates_proxy_on_ssl_error(monkeypatch):
    import base64
    from datetime import datetime, timezone
    from gss_api import auto_renew

    class SSLError(Exception):
        pass

    monkeypatch.delenv("GSS_CANCEL_AUTORENEW_PATH", raising=False)
    monkeypatch.setattr(auto_renew, "use_fake_adapters", lambda: False)
    monkeypatch.setattr(
        auto_renew,
        "configured_proxy",
        lambda role: "http://user-region-US-sid-init001:pw@proxy.test:1111",
    )
    monkeypatch.setattr(auto_renew, "cooling_sids", lambda: set())

    payload = {
        "exp": int(datetime.now(timezone.utc).timestamp()) + 3600,
        "https://api.openai.com/profile": {"email": "u@example.test"},
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-ssl"},
    }
    tok = "h." + base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode() + ".s"
    sess = json.dumps({"accessToken": tok})
    seen_proxies = []

    class FakeClient:
        def __init__(self, proxy):
            self.proxy = proxy
            self.access_token = tok
            self.account_id = "acc-ssl"
            self.session_token = ""
            self.timeout = 10.0

        def subscription_payload(self):
            if len(seen_proxies) == 1:
                raise SSLError("curl: (35) BoringSSL SSL_connect: Connection closed abruptly (SSL_ERROR_SYSCALL)")
            return {
                "is_active": True,
                "plan_type": "prolite",
                "active_until": "2099-11-26T07:35:00Z",
                "last_active_subscription": {"will_renew": False},
            }

        def close(self):
            pass

    def make_client(cls, raw, proxy=None, **kw):
        seen_proxies.append(proxy)
        return FakeClient(proxy)

    monkeypatch.setattr(auto_renew.ChatGPTClient, "from_raw_session", classmethod(make_client))
    res = auto_renew.cancel_account_auto_renew(sess, plan="pro_lite")
    assert res["canceled"] is True
    assert res["stage"] == "already_canceled"
    assert len(seen_proxies) == 2
    assert seen_proxies[0] != seen_proxies[1]


def test_cancel_account_auto_renew_exhausted_proxy_raises_domain_error(monkeypatch):
    import base64
    from datetime import datetime, timezone
    from gss_api import auto_renew

    class SSLError(Exception):
        pass

    monkeypatch.delenv("GSS_CANCEL_AUTORENEW_PATH", raising=False)
    monkeypatch.setattr(auto_renew, "use_fake_adapters", lambda: False)
    monkeypatch.setattr(
        auto_renew,
        "configured_proxy",
        lambda role: "http://user-region-US-sid-init001:pw@proxy.test:1111",
    )
    monkeypatch.setattr(auto_renew, "cooling_sids", lambda: set())

    payload = {
        "exp": int(datetime.now(timezone.utc).timestamp()) + 3600,
        "https://api.openai.com/profile": {"email": "u@example.test"},
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-ssl-fail"},
    }
    tok = "h." + base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode() + ".s"
    sess = json.dumps({"accessToken": tok})
    attempts = []

    class AlwaysFailClient:
        access_token = tok
        account_id = "acc-ssl-fail"
        session_token = ""
        timeout = 10.0

        def subscription_payload(self):
            raise SSLError("curl: (35) SSL_ERROR_SYSCALL")

        def close(self):
            pass

    def make_client(cls, raw, proxy=None, **kw):
        attempts.append(proxy)
        return AlwaysFailClient()

    monkeypatch.setattr(auto_renew.ChatGPTClient, "from_raw_session", classmethod(make_client))
    with pytest.raises(DomainError) as exc:
        auto_renew.cancel_account_auto_renew(sess)
    assert exc.value.code is ErrorCode.CHECKOUT_UPSTREAM_ERROR
    assert (exc.value.detail or {}).get("code") == "proxy_tunnel_failed"
    assert len(attempts) == 3


def test_cancel_subscription_direct_posts_account_id_and_refreshes_on_401(monkeypatch):
    calls = []
    refresh_calls = []

    class _SeqHttp:
        def __init__(self):
            self.headers = {}

        def post(self, url, json=None, headers=None, timeout=None, allow_redirects=False):
            calls.append((url, json, dict(headers or {})))
            status = 401 if len(calls) == 1 else 200
            return type(
                "Resp",
                (),
                {"status_code": status, "headers": {}, "text": "{}", "json": lambda self: {}},
            )()

    client = ChatGPTClient(access_token="old-at", session_token="st-live", account_id="acc-live-1")
    client._http = _SeqHttp()
    monkeypatch.setattr(
        client,
        "refresh_session",
        lambda **kw: (refresh_calls.append(kw), setattr(client, "access_token", "new-at"), True)[2],
    )
    res = client.cancel_subscription_direct()
    assert res["canceled"] is True
    assert res["http_status"] == 200
    assert len(calls) == 2
    assert calls[0][0] == "https://chatgpt.com/backend-api/subscriptions/cancel"
    assert calls[0][1] == {"account_id": "acc-live-1"}
    assert calls[0][2]["chatgpt-account-id"] == "acc-live-1"
    assert refresh_calls == [{"trust_statsig": False, "reject_unauthorized": False, "force_refresh": True}]


def test_cancel_account_auto_renew_uses_first_party_chatgpt_api(monkeypatch):
    import base64
    from datetime import datetime, timezone
    from gss_api import auto_renew

    monkeypatch.delenv("GSS_CANCEL_AUTORENEW_PATH", raising=False)
    monkeypatch.setattr(auto_renew, "use_fake_adapters", lambda: False)
    monkeypatch.setattr(auto_renew, "configured_proxy", lambda role: None)

    payload = {
        "exp": int(datetime.now(timezone.utc).timestamp()) + 3600,
        "https://api.openai.com/profile": {"email": "u@example.test"},
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-direct"},
    }
    tok = "h." + base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode() + ".s"
    sess = json.dumps({"accessToken": tok, "sessionToken": "st"})

    sub_calls = {"n": 0}
    direct_calls = []

    class DirectClient:
        access_token = tok
        account_id = "acc-direct"
        session_token = "st"
        timeout = 10.0

        def subscription_payload(self):
            sub_calls["n"] += 1
            return {
                "is_active": True,
                "plan_type": "plus",
                "active_until": "2026-12-27T03:01:00Z",
                "is_processor_stripe": True,
                "last_active_subscription": {"will_renew": sub_calls["n"] == 1},
            }

        def cancel_subscription_direct(self, *, account_id=""):
            direct_calls.append(account_id)
            return {"canceled": True, "http_status": 200}

        def close(self):
            pass

    monkeypatch.setattr(auto_renew.ChatGPTClient, "from_raw_session", classmethod(lambda cls, *a, **k: DirectClient()))
    monkeypatch.setattr(
        auto_renew,
        "_request_portal_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not fall back to portal when direct API succeeds")),
    )

    res = auto_renew.cancel_account_auto_renew(sess, plan="plus")
    assert res["canceled"] is True
    assert res["will_renew"] is False
    assert res["stage"] == "chatgpt_api_canceled"
    assert direct_calls == ["acc-direct"]
    assert sub_calls["n"] == 2
