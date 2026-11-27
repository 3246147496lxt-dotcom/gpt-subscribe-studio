"""固定美国免税账单 + 自由出口。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.store import store
from gss_core.billing import address_for
from gss_core.billing_profiles import load_billing_profiles
from gss_core.fixed_billing import load_fixed_billing_file, validate_address
from gss_core.verified_payer import checkout_address, load_profile

ADDRESS = {
    "name": "Fixed Payer",
    "email": "fixed@example.invalid",
    "line1": "1425 SW Morrison Street",
    "city": "Portland",
    "state": "OR",
    "postal_code": "97205",
    "country": "US",
}


def write_fixed(tmp_path: Path, **overrides) -> Path:
    path = tmp_path / "fixed-billing.json"
    path.write_text(json.dumps({**ADDRESS, **overrides}))
    path.chmod(0o600)
    return path


def test_us_address_is_one_fixed_tax_free_address() -> None:
    first = address_for("US")
    assert first == address_for("us") == address_for("") == address_for("ZZ")
    assert first["country"] == "US"
    assert first["state"] in {"AK", "DE", "MT", "NH", "OR"}
    assert first["line1"] and first["postal_code"]


def test_fixed_file_must_be_private_and_tax_free(tmp_path, monkeypatch) -> None:
    path = write_fixed(tmp_path)
    monkeypatch.setenv("GSS_FIXED_BILLING_FILE", str(path))
    assert load_fixed_billing_file() == ADDRESS

    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_fixed_billing_file()

    path.chmod(0o600)
    with pytest.raises(ValueError):
        validate_address({**ADDRESS, "state": "NY"})
    with pytest.raises(ValueError):
        validate_address({**ADDRESS, "country": "PH"})
    with pytest.raises(ValueError):
        load_fixed_billing_file("relative.json")


def test_split_exit_is_allowed_without_an_opt_in_flag(tmp_path, monkeypatch) -> None:
    path = write_fixed(tmp_path)
    monkeypatch.setenv("GSS_FIXED_BILLING_FILE", str(path))
    profile = load_billing_profiles().resolve_for_country("US", "PHP")
    assert profile["country"] == "US"
    assert checkout_address("PH", "US", profile=profile)["country"] == "US"
    assert checkout_address("JP", "", profile=profile)["country"] == "US"

    # 支付阶段只拿 claim、不传 profile 时，也必须读到同一份固定资料与同一个核验 id。
    loaded = load_profile()
    assert loaded == profile
    assert loaded.verification_id == profile.verification_id
    assert checkout_address("PH", "US", loaded)["country"] == "US"

    monkeypatch.setenv("GSS_REQUIRE_EXIT_BILLING_MATCH", "1")
    from gss_contracts.errors import DomainError

    with pytest.raises(DomainError):
        checkout_address("PH", "US", profile=profile)


def test_create_order_keeps_us_billing_and_uses_the_selected_exit(tmp_path, monkeypatch) -> None:
    path = write_fixed(tmp_path)
    monkeypatch.setenv("GSS_FIXED_BILLING_FILE", str(path))
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    store.clear()
    try:
        response = TestClient(create_app()).post("/api/orders", json={
            "plan": "plus", "billing_country": "PH", "exit_country": "JP",
            "session": json.dumps({"accessToken": "tok_fixture"}),
        })
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["billing_country"] == "US"
        assert body["exit_country"] == "JP"
        assert body["billing"] == {**ADDRESS}
        # 币种随出口（JPY），账单地址与账单国固定 US。
        assert body["currency"] == "JPY"
    finally:
        store.clear()


def test_preflight_passes_with_split_exit(tmp_path, monkeypatch) -> None:
    from gss_api.services.preflight import _check_verified_billing

    monkeypatch.setenv("GSS_FIXED_BILLING_FILE", str(write_fixed(tmp_path)))
    monkeypatch.delenv("GSS_USE_FAKE", raising=False)
    check = _check_verified_billing("plus", "PHP", exit_country="PH", billing_country="PH")
    assert check.status == "pass"
    assert "US" in check.message


def test_admin_settings_api_manages_fixed_us_billing_without_restart(tmp_path, monkeypatch) -> None:
    import stat as stat_mod
    from gss_api.services.preflight import _check_verified_billing
    from gss_core.verified_payer import MissingProfile

    cfg_dir = tmp_path / "admin-config"
    monkeypatch.setenv("GSS_CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("GSS_FIXED_BILLING_FILE", raising=False)
    monkeypatch.delenv("GSS_VERIFIED_BILLING_FILE", raising=False)
    monkeypatch.delenv("GSS_BILLING_PROFILES_FILE", raising=False)
    monkeypatch.delenv("GSS_USE_FAKE", raising=False)

    client = TestClient(create_app())

    # 1) 初始未配置
    res0 = client.get("/api/settings/fixed-billing")
    assert res0.status_code == 200, res0.text
    snap0 = res0.json()
    assert snap0["configured"] is False
    assert snap0["valid"] is False
    assert snap0["source"] == "none"
    assert snap0["address"] is None
    assert len(snap0["tax_free_states"]) == 5

    # 2) 非免税州 / 非 US / 非法邮箱均被 400 拦截
    for bad in (
        {**ADDRESS, "state": "CA"},
        {**ADDRESS, "country": "JP"},
        {**ADDRESS, "email": "not-an-email"},
    ):
        bad_res = client.put("/api/settings/fixed-billing", json=bad)
        assert bad_res.status_code == 400, bad_res.text

    # 3) 保存合法美国免税州地址 -> 自动 0600 落盘且立即对 load_profile / preflight 生效
    save_res = client.put("/api/settings/fixed-billing", json={**ADDRESS, "state": "de", "country": "us"})
    assert save_res.status_code == 200, save_res.text
    snap1 = save_res.json()
    assert snap1["configured"] is True
    assert snap1["valid"] is True
    assert snap1["source"] == "managed"
    assert snap1["can_delete"] is True
    assert snap1["address"]["state"] == "DE"
    assert snap1["address"]["country"] == "US"
    assert snap1["verification_id"]

    managed_file = cfg_dir / "fixed-billing.json"
    assert managed_file.is_file()
    assert stat_mod.S_IMODE(managed_file.stat().st_mode) == 0o600
    assert load_profile()["state"] == "DE"
    assert load_billing_profiles().resolve_for_country("US", "JPY")["state"] == "DE"
    assert _check_verified_billing("plus", "JPY", exit_country="JP").status == "pass"

    # 4) 清空后立即回到 fail-closed（MissingProfile）
    del_res = client.delete("/api/settings/fixed-billing")
    assert del_res.status_code == 200, del_res.text
    snap2 = del_res.json()
    assert snap2["configured"] is False
    assert snap2["valid"] is False
    assert not managed_file.exists()
    with pytest.raises(MissingProfile):
        load_profile()


def test_admin_settings_api_respects_env_fixed_billing_path(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / "env-fixed.json"
    monkeypatch.setenv("GSS_FIXED_BILLING_FILE", str(env_file))
    client = TestClient(create_app())

    save_res = client.put("/api/settings/fixed-billing", json=ADDRESS)
    assert save_res.status_code == 200, save_res.text
    snap = save_res.json()
    assert snap["source"] == "env_fixed"
    assert snap["storage_label"] == "GSS_FIXED_BILLING_FILE"
    assert env_file.is_file()
    assert load_fixed_billing_file() == ADDRESS


def test_fixed_billing_settings_require_admin_auth(tmp_path) -> None:
    from gss_api.test_access import TestAccessMiddleware

    creds = tmp_path / "auth.json"
    creds.write_text(json.dumps({"password": "admin-secret-fixture"}))
    login_html = tmp_path / "login.html"
    login_html.write_text("<html>login</html>")

    app = create_app()
    app.add_middleware(TestAccessMiddleware, credentials_file=str(creds), login_file=str(login_html))
    client = TestClient(app)
    assert client.get("/api/settings/fixed-billing").status_code == 401
    assert client.put("/api/settings/fixed-billing", json=ADDRESS).status_code == 401
    assert client.delete("/api/settings/fixed-billing").status_code == 401
