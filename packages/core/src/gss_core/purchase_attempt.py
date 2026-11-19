"""Stable purchase-attempt correlation id.

One order gets exactly one id.  Reissue, route changes and retryable recovery
reuse it; a new order gets a new one.  It is an internal correlation id, never
an invented upstream field.
"""
from __future__ import annotations

import uuid

_PREFIX = "pa_"


def new_purchase_attempt_id() -> str:
    return _PREFIX + uuid.uuid4().hex


def stable_purchase_attempt_id(existing: str | None = None) -> str:
    value = str(existing or "").strip()
    return value if value.startswith(_PREFIX) and len(value) > 3 else new_purchase_attempt_id()


__all__ = ["new_purchase_attempt_id", "stable_purchase_attempt_id"]
