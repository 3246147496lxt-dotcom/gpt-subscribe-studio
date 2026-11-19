"""Explicit offline single-payer attestation; never consult an operator's profile during tests."""
import json

import pytest


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


@pytest.fixture
def verified_us_payer(verified_payer):
    return verified_payer("US")
