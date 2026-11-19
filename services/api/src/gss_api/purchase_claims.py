"""Durable at-most-once reservation for keyed purchases.

Disk contains only a digest of the caller's opaque operation key, the order
UUID, and the last observed state of the create operation. A reservation alone
is not proof of charge. Request equality (including private inputs) can only be
checked in the original process. A restarted worker cannot expose the receipt
or dispatch another payment based on a matching operation key.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path


_request_key = secrets.token_bytes(32)


def request_tag(key: str, canonical_body: str) -> bytes:
    # In RAM only, scoped per key: different accounts or payment sources cannot
    # retrieve each other's old checkout through an accidentally re-used key.
    return hmac.digest(_request_key, (key + "\x00" + canonical_body).encode(), "sha256")


class ClaimUnavailable(Exception):
    """Cannot safely determine whether a prior purchase exists."""


def reserve(data_dir: Path, key: str, order_id: str, *, includes_payment: bool = False) -> bool:
    try:
        return _reserve(data_dir, key, order_id, includes_payment)
    except (OSError, sqlite3.Error) as exc:
        raise ClaimUnavailable() from exc


def _reserve(data_dir: Path, key: str, order_id: str, includes_payment: bool) -> bool:
    """Return True only to the first writer, committing before upstream side effects."""
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = data_dir / "purchase-claims.sqlite3"
    os.close(os.open(db, os.O_CREAT | os.O_WRONLY, 0o600))
    digest = hashlib.sha256(key.encode()).digest()
    with closing(sqlite3.connect(db, timeout=2)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS purchase_claim "
                     "(key_digest BLOB PRIMARY KEY NOT NULL, order_id TEXT NOT NULL UNIQUE, "
                     "attempt_state TEXT NOT NULL)")
        row = conn.execute("INSERT INTO purchase_claim VALUES (?, ?, ?) "
                           "ON CONFLICT(key_digest) DO NOTHING",
                           (digest, order_id, "reserved" if includes_payment else "checkout_only"))
        return row.rowcount == 1


# For a keyed create-and-charge, a crash can leave "dispatching". It is an
# uncertain outcome, never a lease that expires or permits another confirmation.
def _transition(data_dir: Path, order_id: str, previous: str, observed: str) -> bool:
    db = data_dir / "purchase-claims.sqlite3"
    try:
        # mode=rw: an absent/unavailable ledger is never silently re-created here.
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=rw", uri=True, timeout=2)) as conn, conn:
            row = conn.execute("UPDATE purchase_claim SET attempt_state = ? "
                               "WHERE order_id = ? AND attempt_state = ?", (observed, order_id, previous))
            return row.rowcount == 1
    except (OSError, sqlite3.Error) as exc:
        raise ClaimUnavailable() from exc


def begin_payment(data_dir: Path, order_id: str) -> bool:
    return _transition(data_dir, order_id, "reserved", "dispatching")


def record_observation(data_dir: Path, order_id: str, observed: str) -> None:
    # No free-form text, provider payloads, or receipts cross this boundary.
    if observed not in {"unknown", "processing", "succeeded", "failed"}:
        raise ValueError("invalid financial observation")
    if not _transition(data_dir, order_id, "dispatching", observed):
        raise ClaimUnavailable()


def release_checkout(data_dir: Path, order_id: str) -> None:
    """Remove a checkout-only reservation after a pre-checkout failure.

    Only ``checkout_only`` reservations are removable; a payment dispatch or
    outcome must remain at-most-once.
    """
    db = data_dir / "purchase-claims.sqlite3"
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=rw", uri=True, timeout=2)) as conn, conn:
            conn.execute(
                "DELETE FROM purchase_claim WHERE order_id = ? AND attempt_state = 'checkout_only'",
                (order_id,),
            )
    except (OSError, sqlite3.Error):
        return
