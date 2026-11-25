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
