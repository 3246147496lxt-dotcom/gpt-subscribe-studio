"""单一固定账单地址：美国免税州。

出口代理可以按订单自由变化，账单地址永远只取这一份，禁止按国家/币种随机生成。
优先读取管理员配置的 ``GSS_FIXED_BILLING_FILE``（私有 JSON，0600/0400、绝对路径、
非符号链接）；没有配置时使用仓库内置的固定样例地址。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Dict

BILLING_COUNTRY = "US"

#: 没有州级销售税的五个州（AK / DE / MT / NH / OR）。
TAX_FREE_STATES = frozenset({"AK", "DE", "MT", "NH", "OR"})

FIELDS = ("name", "email", "line1", "city", "state", "postal_code", "country")
_LIMITS = {"name": 100, "email": 254, "line1": 200, "city": 100, "state": 100,
           "postal_code": 20, "country": 2}
_LIMIT = 8192

#: Built-in fixture. It is deterministic on purpose: the same address for every
#: order, so a checkout can never drift to another street/ZIP between attempts.
DEFAULT_ADDRESS: Dict[str, str] = {
    "name": "Daniel Mercer",
    "email": "billing@example.com",
    "line1": "1425 SW Morrison Street",
    "city": "Portland",
    "state": "OR",
    "postal_code": "97205",
    "country": BILLING_COUNTRY,
}


def is_tax_free_state(state: str) -> bool:
    return str(state or "").strip().upper() in TAX_FREE_STATES


def _clean(raw, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(raw, str):
        raise ValueError
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError
    value = raw.strip()
    if len(value) > limit or (not value and not allow_empty):
        raise ValueError
    return value


def validate_address(raw) -> Dict[str, str]:
    """只接受完整的七个字段；美国账单必须落在免税州。"""
    if not isinstance(raw, dict) or set(raw) != set(FIELDS):
        raise ValueError
    address = {key: _clean(raw[key], _LIMITS[key], allow_empty=key == "state") for key in FIELDS}
    address["country"] = address["country"].upper()
    address["state"] = address["state"].upper()
    if address["country"] != BILLING_COUNTRY:
        raise ValueError
    if not is_tax_free_state(address["state"]):
        raise ValueError
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address["email"]):
        raise ValueError
    return address


def load_fixed_billing_file(path: Path | str | None = None) -> Dict[str, str] | None:
    """Read the operator's fixed address. ``None`` when no file is configured.

    Invalid content raises ``ValueError``: a broken billing config must stop new
    checkouts instead of silently falling back to a different address.
    """
    configured = path if path is not None else os.environ.get("GSS_FIXED_BILLING_FILE", "")
    text = str(configured or "").strip()
    if not text:
        return None
    target = Path(text)
    if not target.is_absolute():
        raise ValueError
    fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) not in {0o600, 0o400}
                or info.st_uid not in {0, os.geteuid()}
                or info.st_size > _LIMIT):
            raise ValueError
        with os.fdopen(fd, "rb", closefd=False) as source:
            blob = source.read(_LIMIT + 1)
    finally:
        os.close(fd)
    if len(blob) > _LIMIT:
        raise ValueError
    record = json.loads(blob.decode("utf-8"))
    if isinstance(record, dict) and "billing" in record:
        record = record["billing"]
    return validate_address(record)


def fixed_billing_address() -> Dict[str, str]:
    """The one billing address used by every non-Team order and every fallback."""
    try:
        configured = load_fixed_billing_file()
    except (OSError, ValueError, AttributeError, RecursionError):
        configured = None
    return dict(configured or DEFAULT_ADDRESS)


def fixed_verification_id(address: Dict[str, str]) -> str:
    """Stable id for the fixed address. Same value for create and later payment."""
    material = json.dumps(address, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(material).hexdigest()


def billing_country() -> str:
    return BILLING_COUNTRY


__all__ = [
    "BILLING_COUNTRY",
    "DEFAULT_ADDRESS",
    "TAX_FREE_STATES",
    "billing_country",
    "fixed_billing_address",
    "fixed_verification_id",
    "is_tax_free_state",
    "load_fixed_billing_file",
    "validate_address",
]
