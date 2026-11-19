import json
import os
import stat

import pytest

from gss_core.billing_profiles import load_billing_profiles
from gss_core.verified_payer import MissingProfile

ADDR = {"name": "Payer", "email": "p@example.com", "line1": "1 Main", "city": "Portland",
        "state": "OR", "postal_code": "97201", "country": "US"}


def _write(path, record):
    path.write_text(json.dumps(record))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def test_new_profiles_select_by_currency(monkeypatch, tmp_path):
    path = tmp_path / "profiles.json"
    _write(path, {"version": 2, "payer_id": "p", "verification_ref": "r",
                  "profiles": {"PHP": ADDR, "EGP": ADDR, "USD": ADDR}})
    monkeypatch.setenv("GSS_BILLING_PROFILES_FILE", str(path))
    profiles = load_billing_profiles()
    assert profiles.resolve("PHP")["country"] == "US"
    assert profiles.resolve("EGP")["country"] == "US"


def test_legacy_flat_file_is_fallback(monkeypatch, tmp_path):
    path = tmp_path / "legacy.json"
    _write(path, {"payer_id": "p", "verification_ref": "r", "billing": ADDR})
    monkeypatch.delenv("GSS_BILLING_PROFILES_FILE", raising=False)
    monkeypatch.setenv("GSS_VERIFIED_BILLING_FILE", str(path))
    assert load_billing_profiles().resolve("PHP")["city"] == "Portland"


def test_missing_profiles_fail_closed(monkeypatch):
    monkeypatch.delenv("GSS_BILLING_PROFILES_FILE", raising=False)
    monkeypatch.delenv("GSS_VERIFIED_BILLING_FILE", raising=False)
    with pytest.raises(MissingProfile):
        load_billing_profiles()
