"""Trusted single-payer billing profile. No fixture, random, or geography-based fallback."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from gss_contracts.errors import DomainError, ErrorCode

from .countries import is_supported

FIELDS = {"name": 100, "email": 254, "line1": 200, "city": 100, "state": 100, "postal_code": 20, "country": 2}
_LIMIT = 8192


class MissingProfile(Exception):
    """Operator profile unavailable. Never include its path or contents in the error."""


class BillingMismatch(Exception):
    """A write conflicts with verified facts."""


class VerifiedBilling(dict):
    """Only billing fields serialize. The order's private reference is not a client assertion."""

    def __init__(self, billing: dict, verification_id: str):
        super().__init__(billing)
        self.verification_id = verification_id

    def __repr__(self):
        return "<VerifiedBilling redacted>"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _clean(raw, limit, *, empty=False):
    if not isinstance(raw, str) or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError
    value = raw.strip()
    if len(value) > limit or (not value and not empty):
        raise ValueError
    return value


def _address(raw) -> dict:
    if not isinstance(raw, dict) or set(raw) != set(FIELDS):
        raise ValueError
    addr = {k: _clean(raw[k], limit, empty=k == "state") for k, limit in FIELDS.items()}
    addr["country"] = addr["country"].upper()
    if not is_supported(addr["country"]) or (not addr["state"] and addr["country"] in {"US", "CA"}):
        raise ValueError
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", addr["email"]):
        raise ValueError
    return addr


def _fixed_profile() -> VerifiedBilling:
    """The single US address from ``GSS_FIXED_BILLING_FILE``, when configured."""
    from .fixed_billing import fixed_verification_id, load_fixed_billing_file

    try:
        address = load_fixed_billing_file()
    except (OSError, ValueError, RecursionError, AttributeError):
        raise MissingProfile from None
    if address is None:
        raise MissingProfile
    try:
        clean = _address(address)
    except ValueError:
        raise MissingProfile from None
    return VerifiedBilling(clean, fixed_verification_id(clean))


def load_profile() -> VerifiedBilling:
    from .fixed_billing import has_fixed_billing_source

    if has_fixed_billing_source():
        return _fixed_profile()
    configured = os.environ.get("GSS_VERIFIED_BILLING_FILE", "").strip()
    if not configured:
        return _fixed_profile()
    try:
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError
        # Inspect the opened object, not a path check followed by a second open.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) not in {0o600, 0o400}
                    or st.st_uid not in {0, os.geteuid()} or st.st_size > _LIMIT):
                raise ValueError
            with os.fdopen(fd, "rb", closefd=False) as source:
                blob = source.read(_LIMIT + 1)
        finally:
            os.close(fd)
        if len(blob) > _LIMIT:
            raise ValueError
        record = json.loads(blob.decode("utf-8"), object_pairs_hook=_unique)
        if not isinstance(record, dict) or set(record) != {"payer_id", "verification_ref", "billing"}:
            raise ValueError
        payer = _clean(record["payer_id"], 160)
        ref = _clean(record["verification_ref"], 160)
        address = _address(record["billing"])
        material = json.dumps([payer, ref, address], sort_keys=True, separators=(",", ":")).encode()
        return VerifiedBilling(address, hashlib.sha256(material).hexdigest())
    except (OSError, ValueError, RecursionError, AttributeError):
        raise MissingProfile from None


def _authoritative_profile(claim=None) -> VerifiedBilling:
    """Load whichever single source the deployment configured.

    Fixed file and legacy verified payer are tried first; multi-currency
    profiles are resolved by the claimed country when only that file exists.
    """
    try:
        return load_profile()
    except MissingProfile:
        pass
    from .billing_profiles import load_billing_profiles

    country = str(claim.get("country") or "") if isinstance(claim, dict) else ""
    return load_billing_profiles().resolve_for_country(country or "US", "")


def verify_claims(*claims, order_billing=None, verification_id=None, profile=None) -> VerifiedBilling:
    profile = profile or _authoritative_profile(claims[0] if claims else None)
    if verification_id is not None and verification_id != profile.verification_id:
        raise BillingMismatch
    for claim in (*claims, order_billing):
        if claim is None:  # absent claim: use only the authoritative server source
            continue
        try:
            if _address(claim) != profile or getattr(claim, "verification_id", profile.verification_id) != profile.verification_id:
                raise BillingMismatch
        except ValueError:
            raise BillingMismatch from None
    return profile


def exit_billing_must_match() -> bool:
    """Opt-in strict mode. Off by default: the exit may differ from the fixed billing."""
    from . import runtime_flags

    return runtime_flags.get_bool("require_exit_billing_match")


def split_exit_billing_allowed() -> bool:
    """Default: billing is fixed (US tax-free) and the exit proxy is free to change."""
    if exit_billing_must_match():
        return False
    legacy = os.environ.get("GSS_ALLOW_SPLIT_EXIT_BILLING", "").strip().lower()
    if legacy:
        return legacy in {"1", "true", "yes", "on"}
    return True


def checkout_address(country: str, declared_country: str, claim=None, profile=None) -> VerifiedBilling:
    """Return the fixed profile. The exit never rewrites the billing address.

    ``declared_country`` is the billing country the caller intends to write; if it
    is present it must still be the profile country. ``country`` is only the
    network exit and may be any supported region unless strict mode is enabled.
    """
    try:
        profile = verify_claims(claim, profile=profile)
        declared = str(declared_country or "").strip().upper()
        if declared and declared != profile["country"]:
            raise BillingMismatch
        if exit_billing_must_match() and str(country or "").strip().upper() != profile["country"]:
            raise BillingMismatch
        return profile
    except MissingProfile:
        raise DomainError(ErrorCode.COMPLIANCE_BLOCKED, "固定账单资料不可用，未授权新的结账。") from None
    except BillingMismatch:
        raise DomainError(ErrorCode.BILLING_COUNTRY_MISMATCH, "账单与已核验资料不匹配，已拒绝。") from None


class ObservedBillingConflict(DomainError):
    """An explicit mismatch observed before any payment confirmation was attempted."""

    def __init__(self):
        super().__init__(ErrorCode.BILLING_COUNTRY_MISMATCH, "上游账单与本单核验地址不符，未提交付款。")


def check_observed_billing(payload, expected: dict, *, alternate_countries=()) -> None:
    """Veto a conflicting provider echo; silence, nulls and metadata attest nothing.

    Only inspect the current checkout/session and its billing blocks, not arbitrary
    nested histories, shipping, receipts or unrelated payment instruments.
    """
    if not isinstance(payload, dict):
        return
    allowed = {str(code or "").strip().upper() for code in alternate_countries if str(code or "").strip()}

    def compare(block, mapping):
        if not isinstance(block, dict):
            return
        for wire, field in mapping.items():
            value = block.get(wire)
            if value is None or value == "":
                continue
            wanted = expected.get(field, "")
            if field == "country" and split_exit_billing_allowed():
                # The fixed billing country is authoritative. An upstream echo may
                # repeat the *selected exit* region; any other country is a conflict.
                if isinstance(value, str) and value.strip().upper() in allowed:
                    continue
            if isinstance(value, str):
                value = value.strip()
                if field == "country":
                    value = value.upper()
            if not isinstance(value, str) or value != wanted:
                raise ObservedBillingConflict()

    fields = {field: field for field in (*FIELDS, "line2")}
    for session in (payload, payload.get("checkout_session")):
        if not isinstance(session, dict):
            continue
        for block in (session, session.get("checkout_state")):
            if not isinstance(block, dict):
                continue
            compare(block, {"billing_country": "country", "billing_name": "name", "billing_email": "email"})
            details = block.get("billing_details")
            compare(details, fields)
            if isinstance(details, dict):
                compare(details.get("address"), fields)
                compare(details.get("billing_address"), fields)
            compare(block.get("billing_address"), fields)
