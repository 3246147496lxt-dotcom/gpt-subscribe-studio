"""建单出口必须跟随订单目标充值国家。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.store import store
from gss_core.proxies import proxy_exit_region
from gss_core.vault import vault

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.7444.60 Safari/537.36"
)


def _configure_proxy(tmp_path, monkeypatch):
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
    monkeypatch.setenv("GSS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GSS_USE_FAKE", "1")


def test_create_order_pins_exit_to_target_billing_country(tmp_path, monkeypatch):
    _configure_proxy(tmp_path, monkeypatch)
    store.clear()
    api = TestClient(create_app())
    session = json.dumps({"accessToken": "tok_fixture"})
    response = api.post(
        "/api/orders",
        json={"plan": "plus", "billing_country": "PH", "session": session},
    )
    assert response.status_code == 201, response.text
    order_id = response.json()["id"]
    try:
        secrets = vault.get(order_id)
        assert secrets is not None
        assert proxy_exit_region(secrets.proxy) == "PH"
    finally:
        vault.drop(order_id)
        store.clear()
