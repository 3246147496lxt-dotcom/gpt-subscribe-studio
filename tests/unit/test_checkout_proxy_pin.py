"""Team selects a live proxy before checkout and preserves it privately."""
import asyncio
from types import SimpleNamespace

import pytest

from gss_checkout.adapter import UpstreamCheckoutAdapter, _probe_stripe_proxy
from gss_contracts import CheckoutRequest, BillingMode, Plan
from gss_contracts.errors import DomainError, ErrorCode


def request(plan=Plan.TEAM):
    return CheckoutRequest(session="fixture", plan=plan, billing_country="EG", billing_mode=BillingMode.STANDARD,
                           proxy="http://fixture.invalid:3010", seat_quantity=2)


def created():
    return SimpleNamespace(checkout_url="https://example.invalid/fixture", currency="EGP", amount=None,
                           checkout_session_id="cs_test_fixture", publishable_key="pk_test_fixture",
                           customer_session_client_secret=None, checkout_kind="hosted", confirm_return_url="")


def test_healthy_team_proxy_is_preserved_without_rotation(monkeypatch):
    checked, attempted = [], []
    monkeypatch.setattr("gss_checkout.adapter._probe_stripe_proxy",
                        lambda proxy, **kw: checked.append(proxy) or True)
    adapter = UpstreamCheckoutAdapter()
    monkeypatch.setattr(adapter, "_create", lambda req, proxy: attempted.append(proxy) or created())
    result = asyncio.run(adapter.create_checkout(request()))
    assert checked == attempted == [request().proxy]
    assert result.effective_proxy == request().proxy
    assert "effective_proxy" not in repr(result)


@pytest.mark.parametrize("plan", [Plan.PLUS, Plan.TEAM])
def test_unusual_activity_is_terminal(monkeypatch, plan):
    monkeypatch.setattr("gss_checkout.adapter._probe_stripe_proxy", lambda *a, **kw: True)
    attempted = []

    def refused(req, proxy):
        attempted.append(proxy)
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "异常流量")

    adapter = UpstreamCheckoutAdapter()
    monkeypatch.setattr(adapter, "_create", refused)
    with pytest.raises(DomainError, match="异常流量"):
        asyncio.run(adapter.create_checkout(request(plan)))
    assert attempted == [request().proxy]


def test_failed_proxy_aborts_without_checkout_or_fallback(monkeypatch):
    seen = []
    monkeypatch.setattr("gss_checkout.adapter._probe_stripe_proxy", lambda proxy, **kw: seen.append(proxy) or False)
    adapter = UpstreamCheckoutAdapter()
    monkeypatch.setattr(adapter, "_create", lambda *a: pytest.fail("No checkout on a failed probe"))
    with pytest.raises(DomainError, match="未创建结账/未付款"):
        asyncio.run(adapter.create_checkout(request()))
    assert seen == [request().proxy]


def test_plus_path_does_not_add_team_preflight(monkeypatch):
    monkeypatch.setattr("gss_checkout.adapter._probe_stripe_proxy", lambda *a, **kw: pytest.fail("Plus preflight changed"))
    adapter = UpstreamCheckoutAdapter(); monkeypatch.setattr(adapter, "_create", lambda *a: created())
    result = asyncio.run(adapter.create_checkout(request(Plan.PLUS)))
    assert result.effective_proxy == "http://fixture.invalid:3010"


@pytest.mark.parametrize("http_status,expected", [(401,True),(404,True),(407,False),(503,False)])
def test_preflight_uses_proxy_and_accepts_unauthenticated_stripe_response(monkeypatch,http_status,expected):
    calls=[]
    class HTTP:
        def get(self,url,**kw): calls.append((url,kw)); return SimpleNamespace(status_code=http_status)
        def close(self): calls.append("closed")
    def session(**kw):
        assert kw["proxy"] == "http://fixture.invalid:3010"
        assert kw["timeout"] == 15
        return HTTP()
    monkeypatch.setattr("gss_core.chatgpt._new_session",session)
    assert _probe_stripe_proxy("http://fixture.invalid:3010") is expected
    assert calls[-1] == "closed"


def test_checkout_proxy_follows_target_billing_country(tmp_path, monkeypatch):
    """建单链路以订单 billing_country 为准，不被旧 statsig 国家改走。"""
    import json

    from gss_core.proxies import proxy_exit_region

    config = tmp_path / "proxy-gateways.json"
    gateway = {
        "host": "gw.example.com",
        "port": 3010,
        "scheme": "http",
        "username": "u-region-US-sid-BASE",
        "password": "pw",
        "region": "US",
        "sid": "BASE",
        "enabled": True,
    }
    config.write_text(json.dumps({"version": 1, "checkout": gateway, "approve": gateway}))
    monkeypatch.setenv("GSS_PROXY_CONFIG", str(config))
    monkeypatch.setenv("GSS_PROXY_SID", "PINNED01")

    session = json.dumps(
        {
            "accessToken": "tok_fixture",
            "statsigContext": {"deviceId": "did_fixture", "country": "JP"},
        }
    )
    req = CheckoutRequest(
        session=session,
        plan=Plan.PLUS,
        billing_country="PH",
        billing_mode=BillingMode.STANDARD,
        proxy=None,
    )
    adapter = UpstreamCheckoutAdapter()
    seen = []
    monkeypatch.setattr(
        adapter, "_create", lambda request, proxy: seen.append(proxy) or created()
    )
    asyncio.run(adapter.create_checkout(req))
    assert seen and proxy_exit_region(seen[0]) == "PH"
