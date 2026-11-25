"""Explicit offline single-payer attestation and isolated runtime dirs during tests."""
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TEST_RUNTIME_ROOT: Path | None = None


def _ensure_isolated_dirs() -> Path:
    global _TEST_RUNTIME_ROOT
    if _TEST_RUNTIME_ROOT is None:
        _TEST_RUNTIME_ROOT = Path(tempfile.mkdtemp(prefix="gss-pytest-"))
    os.environ.setdefault("GSS_DATA_DIR", str(_TEST_RUNTIME_ROOT / "data"))
    os.environ.setdefault("GSS_CONFIG_DIR", str(_TEST_RUNTIME_ROOT / "config"))
    return _TEST_RUNTIME_ROOT


# Apply before test modules import gss_api.main (which initializes the default DB).
_ensure_isolated_dirs()


def pytest_unconfigure(config):  # noqa: ARG001
    global _TEST_RUNTIME_ROOT
    if _TEST_RUNTIME_ROOT is not None:
        shutil.rmtree(_TEST_RUNTIME_ROOT, ignore_errors=True)
        _TEST_RUNTIME_ROOT = None


@pytest.fixture
def verified_payer(monkeypatch, tmp_path):
    def install(country="US", billing=None, *, payer_id="synthetic-payer", verification_ref="offline-attestation"):
        address = (dict(billing) if billing is not None else {
            "name": "Test Payer", "email": "payer@example.invalid", "line1": "Fixture Only",
            "city": "Test City", "state": "NY", "postal_code": "10001", "country": country,
        })
        path = tmp_path / "offline-verified-payer.json"
        path.write_text(json.dumps({"payer_id": payer_id, "verification_ref": verification_ref, "billing": address}))
        path.chmod(0o600)
        monkeypatch.setenv("GSS_VERIFIED_BILLING_FILE", str(path))
        return address
    return install


@pytest.fixture(autouse=True)
def _no_real_billing_profile(monkeypatch):
    monkeypatch.delenv("GSS_VERIFIED_BILLING_FILE", raising=False)
    monkeypatch.delenv("GSS_BILLING_PROFILES_FILE", raising=False)
    monkeypatch.delenv("GSS_FIXED_BILLING_FILE", raising=False)


@pytest.fixture
def verified_us_payer(verified_payer):
    return verified_payer("US")
