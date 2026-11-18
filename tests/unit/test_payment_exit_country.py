"""支付 adapter 使用订单目标国家，不跟随 Session 签发地。"""

from __future__ import annotations

import asyncio
import json

from gss_contracts import PaymentRequest
from gss_core.proxies import proxy_exit_region
from gss_payment.adapter import UpstreamPaymentAdapter


def test_payment_proxy_follows_session_country(tmp_path, monkeypatch):
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

    captured = {}

    class FakeClient:
        def __init__(self, proxy):
            self.proxy = proxy
            self.timeout = 0
            self.language = ""
            self.timezone = ""

        def close(self):
            pass

    def fake_from_raw_session(raw, proxy=None):
        captured["proxy"] = proxy
        return FakeClient(proxy)

    monkeypatch.setattr(
        "gss_payment.adapter.ChatGPTClient.from_raw_session", fake_from_raw_session
    )
    adapter = UpstreamPaymentAdapter()
    monkeypatch.setattr(adapter, "_pay_team", lambda client, req: "ok")

    req = PaymentRequest(
        checkout_url="https://chatgpt.com/checkout/openai_llc/cs_test_fixture",
        country="PH",
        payment_method_token="pm_fixture",
        checkout_session_id="cs_test_fixture",
        publishable_key="pk_test_fixture",
        billing={"country": "PH"},
        plan="team",
        session=json.dumps(
            {
                "accessToken": "tok_fixture",
                "statsigContext": {"deviceId": "did_fixture", "country": "JP"},
            }
        ),
    )
    assert asyncio.run(adapter.pay(req)) == "ok"
    assert proxy_exit_region(captured["proxy"]) == "PH"
