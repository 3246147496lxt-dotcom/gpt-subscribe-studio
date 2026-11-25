"""All identities, references and addresses are synthetic. Never print actual billing."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi import HTTPException
from gss_api.verified_billing import require_verified_billing
from gss_core.verified_payer import checkout_address
from gss_contracts.errors import DomainError


def test_no_default_or_client_verification(monkeypatch):
    monkeypatch.delenv("GSS_VERIFIED_BILLING_FILE", raising=False)
    with pytest.raises(HTTPException) as error:
        require_verified_billing({"billing_verified": True, "name": "client-only"})
    assert error.value.status_code == 503 and "client-only" not in str(error.value.detail)


@pytest.mark.parametrize("field", ["name", "email", "line1", "city", "state", "postal_code", "country"])
def test_one_conflicting_field_is_never_replaced(verified_us_payer, field):
    address = verified_us_payer
    with pytest.raises(HTTPException) as error:
        require_verified_billing({**address, field: "mismatch-marker"})
    assert error.value.status_code == 409
    assert not any(s in str(error.value.detail) for s in ("mismatch-marker", address["line1"], address["email"]))


def test_canonical_match_only_and_private_record_rotation(verified_payer):
    a = verified_payer("US")
    pinned = require_verified_billing({**a, "country": "us", "city": " " + a["city"]})
    assert pinned == a
    assert "payer_id" not in pinned and a["line1"] not in repr(pinned)
    verified_payer(billing=a, payer_id="another-synthetic-payer")
    with pytest.raises(HTTPException) as e:
        require_verified_billing(order_billing=pinned, verification_id=pinned.verification_id)
    assert e.value.status_code == 409


@pytest.mark.parametrize("problem", ["unknown", "missing", "empty", "duplicate", "public", "executable", "link", "directory", "too_large"])
def test_invalid_source_is_unavailable_without_pii(verified_us_payer, problem, tmp_path, monkeypatch):
    path = Path(os.environ["GSS_VERIFIED_BILLING_FILE"])
    record = json.loads(path.read_text())
    if problem == "unknown":
        record["billing"]["line2"] = "sensitive-marker"
    elif problem == "missing":
        del record["verification_ref"]
    elif problem == "empty":
        record["billing"]["name"] = ""
    if problem in {"unknown", "missing", "empty"}:
        path.write_text(json.dumps(record))
    elif problem == "duplicate":
        path.write_text('{"payer_id":"synthetic-marker",' + json.dumps(record)[1:])
    elif problem in {"public", "executable"}:
        path.chmod(0o644 if problem == "public" else 0o700)
    elif problem == "directory":
        monkeypatch.setenv("GSS_VERIFIED_BILLING_FILE", str(tmp_path))
    elif problem == "link":
        link = tmp_path / "alias"
        link.symlink_to(path)
        monkeypatch.setenv("GSS_VERIFIED_BILLING_FILE", str(link))
    elif problem == "too_large":
        path.write_bytes(b" " * 8200)
    with pytest.raises(HTTPException) as error:
        require_verified_billing()
    assert error.value.status_code == 503 and str(path) not in str(error.value.detail)


def test_existing_channel_billing_declarations_must_agree(verified_us_payer):
    assert checkout_address("US", "US", verified_us_payer) == verified_us_payer
    for route, country, billing in [
        ("US", "JP", verified_us_payer),
        ("US", "US", {**verified_us_payer, "line1": "Different"}),
    ]:
        with pytest.raises(DomainError):
            checkout_address(route, country, billing)
    # 出口自由：账单国 US 不变，出口 JP/PH 都允许。
    assert checkout_address("JP", "US", verified_us_payer)["country"] == "US"
    assert checkout_address("PH", "US", verified_us_payer)["country"] == "US"


@pytest.mark.parametrize("name", ["plus_run", "plus_har_run", "plus_probe"])
def test_standalone_writes_stop_before_loading_credentials_or_payment_sdk(name, tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / (name + ".py")
    # No site packages or credential files: stop even before importing any provider.
    result = subprocess.run([sys.executable, "-I", "-S", str(script)], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0 and not result.stdout
    assert "独立写入入口已关闭" in result.stderr and "Traceback" not in result.stderr
