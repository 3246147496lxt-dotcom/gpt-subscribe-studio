"""代理出口国家以订单目标国家为准，Session 签发地只兜底。"""

from __future__ import annotations

import json

from gss_core.proxies import (
    checkout_proxy_for,
    exit_country_for,
    proxy_exit_region,
    session_proxy_for,
)


def _write_config(tmp_path, monkeypatch):
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
    return config


def test_exit_country_prefers_target_billing_country() -> None:
    assert exit_country_for("US", "PH") == "PH"
    assert exit_country_for("us", "ph") == "PH"
    assert exit_country_for("US", "") == "US"
    assert exit_country_for("", "") == ""
    # 非法目标值才回退到 Session 签发地
    assert exit_country_for("PH", "USA") == "PH"
    assert exit_country_for("PH", "J") == "PH"


def test_checkout_proxy_uses_target_country(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path, monkeypatch)

    proxy = checkout_proxy_for("checkout", session_country="JP", billing_country="US")
    assert proxy is not None
    assert proxy_exit_region(proxy) == "US"
    assert "-sid-PINNED01" in proxy

    # 目标国家不同时，出口跟目标国家走，不跟 Session 签发地。
    assert proxy_exit_region(
        checkout_proxy_for("checkout", session_country="JP", billing_country="PH")
    ) == "PH"


def test_checkout_proxy_falls_back_to_billing_country(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path, monkeypatch)
    proxy = checkout_proxy_for("checkout", session_country="", billing_country="PH")
    assert proxy is not None
    assert proxy_exit_region(proxy) == "PH"


def test_checkout_proxy_keeps_base_region_without_any_country(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path, monkeypatch)
    proxy = checkout_proxy_for("checkout")
    assert proxy is not None
    assert proxy_exit_region(proxy) == "US"


def test_session_proxy_for_matches_checkout_helper(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path, monkeypatch)
    assert proxy_exit_region(
        session_proxy_for("checkout", session_country="JP", billing_country="US")
    ) == "US"
