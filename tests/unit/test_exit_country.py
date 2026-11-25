"""出口自由：显式出口 / Session 签发地优先，账单国只兜底。"""

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


def test_exit_country_prefers_explicit_exit_not_billing() -> None:
    # 固定账单国不决定出口：显式出口优先，账单国只在出口缺失时兜底。
    assert exit_country_for("PH", "US") == "PH"
    assert exit_country_for("us", "ph") == "US"
    assert exit_country_for("JP", "") == "JP"
    assert exit_country_for("", "US") == "US"
    assert exit_country_for("", "") == ""
    # 非法值跳过，继续找下一个候选。
    assert exit_country_for("USA", "PH") == "PH"
    assert exit_country_for("J", "") == ""


def test_checkout_proxy_uses_target_country(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path, monkeypatch)

    proxy = checkout_proxy_for("checkout", session_country="JP", billing_country="US")
    assert proxy is not None
    assert proxy_exit_region(proxy) == "JP"
    assert "-sid-PINNED01" in proxy

    # 账单国固定也不改写出口。
    assert proxy_exit_region(
        checkout_proxy_for("checkout", session_country="PH", billing_country="US")
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
    ) == "JP"
