"""The order vault is memory-only and must release secrets even while idle."""

from datetime import datetime, timedelta, timezone
from importlib import import_module
from threading import Event
from weakref import finalize

from gss_core.vault import OrderSecrets, SecretVault


def test_replacement_cannot_extend_order_deadline(monkeypatch):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(import_module("gss_core.vault"), "_utcnow", lambda: now)
    vault = SecretVault()
    first = OrderSecrets(session="fixture", expires_at=now + timedelta(days=1))
    vault.put("order", first)
    deadline = now + timedelta(minutes=30)
    try:
        assert first.expires_at == deadline
        now += timedelta(minutes=20)
        second = OrderSecrets(session="replacement")
        vault.put("order", second)
        assert vault.get("order") is second
        assert second.expires_at == deadline

        # Out-of-lock edits to the mutable object cannot reset its private bound.
        second.expires_at += timedelta(days=1)
        now = deadline
        assert vault.get("order") is None
        vault.put("order", second)
        assert vault.get("order") is None
    finally:
        vault.drop("order")


def test_expired_write_does_not_overwrite_a_live_entry():
    vault = SecretVault()
    expired = OrderSecrets(session="stale", expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
    vault.put("order", expired)
    assert vault.get("order") is None
    live = OrderSecrets(session="live")
    vault.put("order", live)
    try:
        vault.put("order", expired)
        assert vault.get("order") is live
    finally:
        vault.drop("order")


def test_idle_vault_releases_credentials_and_stops_its_worker():
    vault = SecretVault()
    vault.put("later", OrderSecrets(session="unrelated"))
    worker = vault._worker
    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=250)
    earlier = OrderSecrets(session="fixture", expires_at=deadline)
    released = Event()
    finalize(earlier, released.set)
    try:
        vault.put("sooner", earlier)
        assert vault._worker is worker
        del earlier
        assert released.wait(3)  # no intervening get/put to trigger a lazy purge
    finally:
        vault.drop("sooner")
        vault.drop("later")
    worker.join(timeout=3)
    assert not worker.is_alive()
