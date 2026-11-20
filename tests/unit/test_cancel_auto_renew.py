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
        return {"will_renew": True} if calls["n"] == 1 else {"will_renew": False}

    monkeypatch.setattr(client, "subscription_payload", subscription_payload)
    result = client.cancel_auto_renew(plan_type="pro_lite")
    assert result["canceled"] is True
    assert result["will_renew"] is False
    assert http.calls and http.calls[0][1].endswith("/backend-api/subscriptions//cancel")
    assert http.calls[0][2] == {
        "plan_type": "prolite",
        "subscription_id": "",
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
