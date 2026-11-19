"""订单凭证金库：session / Stripe 上下文只在进程内存，任务结束后可丢弃。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Condition, Thread
from typing import Any, Dict, Optional


_TTL = timedelta(minutes=30)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class OrderSecrets:
    session: str
    account_id: str = ""
    checkout_session_id: str = ""
    publishable_key: str = ""
    setup_intent_id: str = ""
    setup_client_secret: str = ""
    attestation: str = ""
    client_build_number: str = ""
    client_version: str = ""
    oai_session_id: str = ""
    proxy: str = ""
    # Session 签发时的真实身份，支付/求解阶段必须原样复用，不能再随机生成。
    user_agent: str = ""
    device_id: str = ""
    session_country: str = ""
    exit_country: str = ""
    session_reissued: bool = False
    # 建单时选定的指纹 profile，支付与求解器必须复用同一份
    profile: Optional[Dict[str, Any]] = None
    billing: Dict[str, str] = field(default_factory=dict)
    team_context: Optional[Dict[str, Any]] = None
    saved_card_context: Optional[Dict[str, Any]] = None
    expires_at: datetime = field(default_factory=lambda: _utcnow() + _TTL)
    # get() returns a mutable object. Keep the retention limit independent of its public
    # expires_at, including after a stale holder tries to put that reference back.
    _retain_until: datetime = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._retain_until = min(self.expires_at, _utcnow() + _TTL)


class SecretVault:
    """按订单号暂存凭证。不写磁盘、不进日志。"""

    def __init__(self) -> None:
        self._changed = Condition()
        self._items: Dict[str, OrderSecrets] = {}
        self._worker: Optional[Thread] = None

    def put(self, order_id: str, secrets: OrderSecrets) -> None:
        with self._changed:
            now = _utcnow()
            self._purge(now)
            current = self._items.get(order_id)
            deadline = min(secrets.expires_at, secrets._retain_until)
            if current is not None:
                deadline = min(deadline, current._retain_until)
            if deadline <= now:
                return  # Never retain an expired object, or overwrite a live one with it.
            secrets.expires_at = secrets._retain_until = deadline
            self._items[order_id] = secrets
            if self._worker is None:
                self._worker = Thread(target=self._expire_idle, name="vault-expiry", daemon=True)
                self._worker.start()
            self._changed.notify()

    def get(self, order_id: str) -> Optional[OrderSecrets]:
        with self._changed:
            self._purge(_utcnow())
            return self._items.get(order_id)

    def drop(self, order_id: str) -> None:
        with self._changed:
            self._items.pop(order_id, None)
            self._changed.notify()

    def _purge(self, now: datetime) -> None:
        dead = []
        for key, item in self._items.items():
            item._retain_until = min(item._retain_until, item.expires_at)
            if item._retain_until <= now:
                dead.append(key)
        for key in dead:
            del self._items[key]

    def _expire_idle(self) -> None:
        # One sleeper per nonempty vault, not one timer per order. No credentials
        # are kept in the worker's stack while it waits.
        with self._changed:
            while self._items:
                now = _utcnow()
                self._purge(now)
                if self._items:
                    deadline = min(item._retain_until for item in self._items.values())
                    self._changed.wait((deadline - now).total_seconds())
            self._worker = None


vault = SecretVault()

__all__ = ["OrderSecrets", "SecretVault", "vault"]
