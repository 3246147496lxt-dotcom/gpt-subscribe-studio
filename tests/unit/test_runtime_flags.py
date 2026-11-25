"""Tests for the runtime & protocol feature switches management module."""
from __future__ import annotations

import os
import stat

import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.routers import orders
from gss_api.services import order_flow, reconciler
from gss_core import plus_har_protocol, runtime_flags, verified_payer


@pytest.fixture(autouse=True)
def _isolated_flags_file(tmp_path, monkeypatch):
    flags_path = tmp_path / "runtime-flags.json"
    monkeypatch.setenv("GSS_RUNTIME_FLAGS_FILE", str(flags_path))
    monkeypatch.delenv("GSS_REISSUE_SESSION_ON_EXIT", raising=False)
    monkeypatch.delenv("GSS_REISSUE_KEEP_UA", raising=False)
    monkeypatch.delenv("GSS_PLUS_ENGINE", raising=False)
    monkeypatch.delenv("GSS_HAR_TELEMETRY", raising=False)
    monkeypatch.delenv("GSS_REQUIRE_EXIT_BILLING_MATCH", raising=False)
    monkeypatch.delenv("GSS_QUICK_PAY_CONCURRENCY", raising=False)
    monkeypatch.delenv("GSS_RECONCILER_ENABLED", raising=False)
    runtime_flags.invalidate_cache()
    yield flags_path
    runtime_flags.invalidate_cache()


def test_flag_precedence_override_beats_env_beats_default(monkeypatch, _isolated_flags_file):
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    # 1) Default
    val, src = runtime_flags.resolve_flag("reissue_session_on_exit")
    assert val is True and src == "default"

    # 2) Env overrides default
    monkeypatch.setenv("GSS_REISSUE_SESSION_ON_EXIT", "0")
    val, src = runtime_flags.resolve_flag("reissue_session_on_exit")
    assert val is False and src == "env"

    # 3) Persisted panel override beats env
    snap = runtime_flags.update_flags({"reissue_session_on_exit": True, "quick_pay_concurrency": 6})
    assert snap["override_count"] == 2
    val, src = runtime_flags.resolve_flag("reissue_session_on_exit")
    assert val is True and src == "override"
    assert runtime_flags.get_int("quick_pay_concurrency") == 6

    # File persisted with 0600 permissions
    mode = stat.S_IMODE(_isolated_flags_file.stat().st_mode)
    assert mode == 0o600

    # 4) Resetting single key falls back to env
    snap = runtime_flags.reset_flags(["reissue_session_on_exit"])
    assert snap["override_count"] == 1
    val, src = runtime_flags.resolve_flag("reissue_session_on_exit")
    assert val is False and src == "env"

    # 5) Resetting all falls back to defaults
    snap = runtime_flags.reset_flags()
    assert snap["override_count"] == 0
    assert runtime_flags.get_int("quick_pay_concurrency") == 3


def test_validation_rejects_unknown_keys_bad_enums_and_out_of_range_ints():
    with pytest.raises(ValueError, match="未知"):
        runtime_flags.update_flags({"nonexistent_flag": True})

    with pytest.raises(ValueError, match="仅支持"):
        runtime_flags.update_flags({"plus_engine": "turbo"})

    with pytest.raises(ValueError, match="不能大于"):
        runtime_flags.update_flags({"quick_pay_concurrency": 99})

    with pytest.raises(ValueError, match="不能小于"):
        runtime_flags.update_flags({"quick_pay_concurrency": 0})

    with pytest.raises(ValueError, match="布尔"):
        runtime_flags.update_flags({"reissue_keep_ua": "maybe"})

    with pytest.raises(ValueError, match="整数"):
        runtime_flags.update_flags({"quick_pay_concurrency": True})


def test_flags_wire_into_orders_har_billing_queue_and_reconciler(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    # Default fake mode
    assert orders._reissue_on_exit_enabled() is False
    assert orders._reissue_keep_ua() is False
    assert plus_har_protocol.har_engine_enabled() is True
    assert verified_payer.exit_billing_must_match() is False
    assert order_flow.max_concurrency() == 3
    assert reconciler.reconciler_enabled() is True

    runtime_flags.update_flags({
        "reissue_session_on_exit": True,
        "reissue_keep_ua": True,
        "plus_engine": "legacy",
        "har_telemetry": "off",
        "require_exit_billing_match": True,
        "quick_pay_concurrency": 8,
        "reconciler_enabled": False,
    })

    assert orders._reissue_on_exit_enabled() is True
    assert orders._reissue_keep_ua() is True
    assert plus_har_protocol.har_engine_enabled() is False
    assert verified_payer.exit_billing_must_match() is True
    assert order_flow.max_concurrency() == 8
    assert reconciler.reconciler_enabled() is False
    har_client = plus_har_protocol.PlusHarProtocol(session="")
    assert har_client.mode == "off"


def test_settings_flags_api_get_put_reset_and_validation(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    with TestClient(create_app()) as client:
        r_get = client.get("/api/settings/flags")
        assert r_get.status_code == 200
        body = r_get.json()
        assert body["override_count"] == 0
        by_key = {item["key"]: item for item in body["items"]}
        assert by_key["reissue_session_on_exit"]["value"] is True
        assert by_key["reissue_session_on_exit"]["source"] == "default"
        assert by_key["reissue_keep_ua"]["value"] is False

        r_put = client.put(
            "/api/settings/flags",
            json={"updates": {"reissue_keep_ua": True, "har_telemetry": "off", "reconciler_enabled": False}},
        )
        assert r_put.status_code == 200
        put_body = r_put.json()
        assert put_body["override_count"] == 3
        put_by_key = {item["key"]: item for item in put_body["items"]}
        assert put_by_key["reissue_keep_ua"]["value"] is True
        assert put_by_key["reissue_keep_ua"]["source"] == "override"
        assert put_by_key["har_telemetry"]["value"] == "off"
        assert reconciler.status()["running"] is False

        # Invalid flag rejected with 400
        r_bad = client.put("/api/settings/flags", json={"updates": {"unknown_key": 1}})
        assert r_bad.status_code == 400
        assert r_bad.json()["detail"]["code"] == "invalid_runtime_flag"

        # Single-key reset
        r_reset_one = client.post("/api/settings/flags/reset", json={"keys": ["har_telemetry"]})
        assert r_reset_one.status_code == 200
        assert r_reset_one.json()["override_count"] == 2

        # Reset all restores reconciler too
        r_reset_all = client.post("/api/settings/flags/reset", json={})
        assert r_reset_all.status_code == 200
        assert r_reset_all.json()["override_count"] == 0
        assert reconciler.reconciler_enabled() is True
