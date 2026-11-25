"""Per-currency verified billing profiles with legacy single-profile fallback."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from .fixed_billing import fixed_verification_id, load_fixed_billing_file
from .verified_payer import MissingProfile, VerifiedBilling, _address, _unique

_LIMIT = 16384


@dataclass(frozen=True)
class BillingProfiles:
    by_currency: Dict[str, VerifiedBilling]
    fallback: VerifiedBilling | None = None

    def resolve(self, currency: str) -> VerifiedBilling:
        key = str(currency or "").strip().upper()
        profile = self.by_currency.get(key) or self.fallback
        if profile is None:
            raise MissingProfile()
        return profile

    def resolve_for_country(self, country: str, currency: str = "") -> VerifiedBilling:
        """固定账单以国家为准，币种只用于在同国多条记录中挑一条。

        美国是固定账单国；出口币种（PHP/JPY/...）不应把账单换到别的国家。
        """
        code = str(country or "").strip().upper()
        key = str(currency or "").strip().upper()
        preferred = self.by_currency.get(key)
        if preferred is not None and str(preferred.get("country") or "").upper() == code:
            return preferred
        for profile in self.by_currency.values():
            if str(profile.get("country") or "").upper() == code:
                return profile
        if self.fallback is not None and str(self.fallback.get("country") or "").upper() == code:
            return self.fallback
        return self.resolve(currency)


def _read_record(path: Path) -> dict:
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
    if not isinstance(record, dict):
        raise ValueError
    return record


def _fixed_verified(address: dict) -> VerifiedBilling:
    return VerifiedBilling(_address(address), fixed_verification_id(_address(address)))


def _verified(payer: str, ref: str, address: dict) -> VerifiedBilling:
    material = json.dumps([payer, ref, address], sort_keys=True, separators=(",", ":")).encode()
    return VerifiedBilling(_address(address), hashlib.sha256(material).hexdigest())


def _load_from_path(path: Path) -> BillingProfiles:
    record = _read_record(path)
    by_currency: Dict[str, VerifiedBilling] = {}
    fallback = None
    if "profiles" in record:
        if record.get("version") != 2:
            raise ValueError
        payer = str(record.get("payer_id") or "")
        ref = str(record.get("verification_ref") or "")
        if not payer or not ref:
            raise ValueError
        raw_profiles = record.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ValueError
        for key, value in raw_profiles.items():
            if not isinstance(value, dict):
                raise ValueError
            address = value.get("billing") if isinstance(value.get("billing"), dict) else value
            by_currency[str(key).upper()] = _verified(payer, ref, address)
        return BillingProfiles(by_currency)
    # legacy verified-payer file: {"payer_id","verification_ref","billing"}
    if set(record) != {"payer_id", "verification_ref", "billing"}:
        raise ValueError
    payer = str(record["payer_id"])
    ref = str(record["verification_ref"])
    fallback = _verified(payer, ref, record["billing"])
    return BillingProfiles({}, fallback)


def load_billing_profiles() -> BillingProfiles:
    # A single fixed US address is the primary deployment mode; when set it wins
    # over the older per-currency / single-payer files.
    fixed_value = os.environ.get("GSS_FIXED_BILLING_FILE", "").strip()
    if fixed_value:
        try:
            address = load_fixed_billing_file(Path(fixed_value))
            if address is None:
                raise ValueError
            return BillingProfiles({}, _fixed_verified(address))
        except (OSError, ValueError, RecursionError, AttributeError):
            raise MissingProfile from None
    path_value = os.environ.get("GSS_BILLING_PROFILES_FILE", "").strip()
    if path_value:
        path = Path(path_value)
    else:
        path = Path(os.environ.get("GSS_VERIFIED_BILLING_FILE", ""))
    try:
        if not path.is_absolute():
            raise ValueError
        return _load_from_path(path)
    except (OSError, ValueError, RecursionError, AttributeError):
        raise MissingProfile from None


def resolve_billing_profile(currency: str, country: str = "US") -> VerifiedBilling:
    return load_billing_profiles().resolve_for_country(country, currency)


__all__ = ["BillingProfiles", "load_billing_profiles", "resolve_billing_profile"]
