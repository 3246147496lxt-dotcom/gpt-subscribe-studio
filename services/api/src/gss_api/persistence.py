"""SQLite persistence for orders, quick-pay jobs, idempotency and payment locks.

Only non-secret metadata is stored.  This module has no knowledge of cards,
cookies, session tokens or proxy passwords, and its callers must never add them.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from gss_contracts import Mode, Order, OrderStatus, Plan

# Imported lazily in save_order to avoid a store <-> persistence import cycle.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .store import OrderView

SCHEMA_VERSION = 1


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_db_path() -> str:
    explicit = os.environ.get("GSS_DB_PATH", "").strip()
    if explicit:
        if explicit == ":memory:":
            return explicit
        path = Path(explicit)
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)
    data_dir = os.environ.get("GSS_DATA_DIR", "").strip()
    candidates = []
    if data_dir:
        candidates.append(Path(data_dir))
    prod = Path("/data")
    if prod.exists() and os.access(prod, os.W_OK):
        candidates.append(prod)
    candidates.append(Path.cwd() / "data")
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if os.access(directory, os.W_OK):
                return str(directory / "gss.db")
        except OSError:
            continue
    return str(Path.cwd() / "gss.db")


class Persistence:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or resolve_db_path()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._create_schema()
            self._conn.commit()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                account_fingerprint TEXT,
                purchase_attempt_id TEXT UNIQUE,
                plan TEXT NOT NULL,
                billing_country TEXT NOT NULL,
                exit_country TEXT,
                account_source TEXT NOT NULL,
                card_source TEXT NOT NULL,
                billing_mode TEXT NOT NULL,
                risk_acknowledged INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                payment_status TEXT NOT NULL DEFAULT 'unpaid',
                delivery_status TEXT NOT NULL DEFAULT 'pending',
                amount TEXT,
                cost TEXT,
                currency TEXT,
                payment_intent_id TEXT,
                checkout_session_id TEXT,
                checkout_kind TEXT,
                failure_code TEXT,
                last_error TEXT,
                tax_delta TEXT,
                route_profile TEXT,
                billing_verified INTEGER NOT NULL DEFAULT 0,
                session_reissued INTEGER NOT NULL DEFAULT 0,
                view_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                unknown_since TEXT,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_orders_payment ON orders(payment_status);
            CREATE INDEX IF NOT EXISTS idx_orders_account ON orders(account_fingerprint);

            CREATE TABLE IF NOT EXISTS quick_pay_jobs (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                account_fingerprint TEXT,
                order_id TEXT,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                failure_code TEXT,
                message TEXT,
                amount TEXT,
                currency TEXT,
                purchase_attempt_id TEXT,
                lock_id TEXT,
                timings_json TEXT NOT NULL DEFAULT '{}',
                diagnostics_json TEXT NOT NULL DEFAULT '{}',
                locked_by TEXT,
                lease_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON quick_pay_jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_account ON quick_pay_jobs(account_fingerprint);

            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                order_id TEXT,
                job_id TEXT,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS account_locks (
                id TEXT PRIMARY KEY,
                lock_key TEXT NOT NULL,
                lock_type TEXT NOT NULL,
                order_id TEXT,
                job_id TEXT,
                idempotency_key TEXT,
                payment_intent_id TEXT,
                state TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                released_at TEXT,
                released_by TEXT,
                release_note TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_account_lock_active
                ON account_locks(lock_key) WHERE state = 'held';
            CREATE INDEX IF NOT EXISTS idx_locks_order ON account_locks(order_id);

            CREATE TABLE IF NOT EXISTS order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT,
                job_id TEXT,
                stage TEXT NOT NULL,
                event TEXT NOT NULL,
                code TEXT,
                elapsed_ms INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_order ON order_events(order_id, created_at);
            """
        )
        try:
            self._conn.execute("ALTER TABLE quick_pay_jobs ADD COLUMN lock_id TEXT")
        except sqlite3.OperationalError:
            pass
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
            (str(SCHEMA_VERSION),),
        )

    # -- orders ------------------------------------------------------------- #
    @staticmethod
    def _view_to_json(view: OrderView) -> str:
        payload = {
            name: getattr(view, name)
            for name in (
                "purchase_attempt_id", "route_profile", "failure_code", "checkout_url",
                "checkout_session_id", "publishable_key", "billing", "billing_verified",
                "amount", "currency", "last_error", "quote_error", "next_action",
                "checkout_kind", "seat_quantity", "workspace_name", "promo_code",
                "promo_campaign_id", "price_interval", "confirm_return_url",
                "session_reissued_for_country", "exit_country", "payment_status",
                "delivery_status", "payment_intent_id", "observed_payment_id",
                "paid_amount", "paid_currency", "tax_delta", "plan_type", "status_summary",
                "receipt",
            )
            if not isinstance(getattr(view, name, None), (bytes, bytearray))
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    def save_order(self, order: Order, view: "OrderView", *, account_fingerprint: Optional[str] = None) -> None:
        payload = (
            order.id, order.idempotency_key or None, account_fingerprint,
            order.purchase_attempt_id or None, str(order.plan), order.billing_country,
            view.exit_country or None, str(order.mode.account_source),
            str(order.mode.card_source), str(order.mode.billing_mode),
            int(order.mode.risk_acknowledged), str(order.status), view.payment_status,
            view.delivery_status, str(order.amount) if order.amount is not None else None,
            str(order.cost) if order.cost is not None else None, view.currency,
            view.payment_intent_id, view.checkout_session_id, view.checkout_kind,
            view.failure_code, view.last_error or view.quote_error, view.tax_delta,
            view.route_profile, int(view.billing_verified), int(view.session_reissued_for_country),
            self._view_to_json(view), order.created_at.isoformat(), _utcnow(),
            view.unknown_since if hasattr(view, "unknown_since") else None,
            view.finished_at if hasattr(view, "finished_at") else None,
        )
        with self._lock:
            self._conn.execute(
                """INSERT INTO orders(
                    id, idempotency_key, account_fingerprint, purchase_attempt_id, plan,
                    billing_country, exit_country, account_source, card_source, billing_mode,
                    risk_acknowledged, status, payment_status, delivery_status, amount, cost,
                    currency, payment_intent_id, checkout_session_id, checkout_kind, failure_code,
                    last_error, tax_delta, route_profile, billing_verified, session_reissued,
                    view_json, created_at, updated_at, unknown_since, finished_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    idempotency_key=excluded.idempotency_key,
                    account_fingerprint=excluded.account_fingerprint,
                    purchase_attempt_id=excluded.purchase_attempt_id,
                    status=excluded.status,
                    payment_status=excluded.payment_status,
                    delivery_status=excluded.delivery_status,
                    amount=excluded.amount,
                    currency=excluded.currency,
                    payment_intent_id=excluded.payment_intent_id,
                    checkout_session_id=excluded.checkout_session_id,
                    checkout_kind=excluded.checkout_kind,
                    failure_code=excluded.failure_code,
                    last_error=excluded.last_error,
                    tax_delta=excluded.tax_delta,
                    route_profile=excluded.route_profile,
                    billing_verified=excluded.billing_verified,
                    session_reissued=excluded.session_reissued,
                    view_json=excluded.view_json,
                    updated_at=excluded.updated_at,
                    unknown_since=excluded.unknown_since,
                    finished_at=excluded.finished_at""",
                payload,
            )
            self._conn.commit()

    def load_orders(self) -> list[tuple[Order, "OrderView", Optional[str]]]:
        from .store import OrderView
        with self._lock:
            rows = self._conn.execute("SELECT * FROM orders ORDER BY created_at").fetchall()
        result = []
        for row in rows:
            mode = Mode(
                account_source=row["account_source"],
                card_source=row["card_source"],
                billing_mode=row["billing_mode"],
                risk_acknowledged=bool(row["risk_acknowledged"]),
            )
            order = Order(
                id=row["id"], idempotency_key=row["idempotency_key"] or "",
                purchase_attempt_id=row["purchase_attempt_id"] or "",
                plan=Plan(row["plan"]), billing_country=row["billing_country"],
                mode=mode, status=OrderStatus(row["status"]),
                amount=Decimal(row["amount"]) if row["amount"] else None,
                cost=Decimal(row["cost"]) if row["cost"] else None,
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            view = OrderView()
            try:
                data = json.loads(row["view_json"] or "{}")
            except (TypeError, ValueError):
                data = {}
            for key, value in data.items():
                if hasattr(view, key) and not isinstance(value, (bytes, bytearray)):
                    setattr(view, key, value)
            result.append((order, view, row["account_fingerprint"]))
        return result

    # -- jobs --------------------------------------------------------------- #
    def save_job(self, job: Any) -> None:
        payload = (
            job.id, job.idempotency_key, getattr(job, "account_fingerprint", None),
            job.order_id, job.status, job.stage, job.failure_code, job.message,
            job.amount, job.currency, job.purchase_attempt_id, getattr(job, "lock_id", None),
            json.dumps(getattr(job, "timings", {}) or {}, ensure_ascii=False),
            json.dumps(getattr(job, "diagnostics", {}) or {}, ensure_ascii=False),
            getattr(job, "locked_by", None), getattr(job, "lease_until", None),
            datetime.fromtimestamp(job.started_at, timezone.utc).isoformat()
            if getattr(job, "started_at", None) else _utcnow(),
            _utcnow(),
            datetime.fromtimestamp(job.finished_at, timezone.utc).isoformat()
            if getattr(job, "finished_at", None) else None,
        )
        with self._lock:
            self._conn.execute(
                """INSERT INTO quick_pay_jobs(
                    id, idempotency_key, account_fingerprint, order_id, status, stage,
                    failure_code, message, amount, currency, purchase_attempt_id, lock_id,
                    timings_json, diagnostics_json, locked_by, lease_until,
                    created_at, updated_at, finished_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status, stage=excluded.stage,
                    failure_code=excluded.failure_code, message=excluded.message,
                    amount=excluded.amount, currency=excluded.currency,
                    purchase_attempt_id=excluded.purchase_attempt_id,
                    lock_id=excluded.lock_id,
                    timings_json=excluded.timings_json,
                    diagnostics_json=excluded.diagnostics_json,
                    updated_at=excluded.updated_at, finished_at=excluded.finished_at""",
                payload,
            )
            self._conn.commit()

    def load_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM quick_pay_jobs ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    # -- idempotency -------------------------------------------------------- #
    def save_idempotency(self, key: str, scope: str, *, order_id: str = "", job_id: str = "", state: str) -> None:
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """INSERT INTO idempotency_keys(key, scope, order_id, job_id, state, created_at, last_seen_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     order_id=excluded.order_id, job_id=excluded.job_id,
                     state=excluded.state, last_seen_at=excluded.last_seen_at""",
                (key, scope, order_id or None, job_id or None, state, now, now),
            )
            self._conn.commit()

    def load_idempotency(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM idempotency_keys").fetchall()
        return [dict(row) for row in rows]

    # -- locks -------------------------------------------------------------- #
    def acquire_lock(self, *, lock_id: str, lock_key: str, lock_type: str, reason: str,
                     order_id: str = "", job_id: str = "", idempotency_key: str = "",
                     payment_intent_id: str = "") -> Optional[dict[str, Any]]:
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO account_locks(
                        id, lock_key, lock_type, order_id, job_id, idempotency_key,
                        payment_intent_id, state, reason, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (lock_id, lock_key, lock_type, order_id or None, job_id or None,
                     idempotency_key or None, payment_intent_id or None, "held", reason, _utcnow()),
                )
                self._conn.commit()
                return self.get_lock(lock_id)
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return self.find_active_lock(lock_key)

    def find_active_lock(self, lock_key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM account_locks WHERE lock_key=? AND state='held'", (lock_key,)
            ).fetchone()
        return dict(row) if row else None

    def get_lock(self, lock_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM account_locks WHERE id=?", (lock_id,)).fetchone()
        return dict(row) if row else None

    def release_lock(self, lock_id: str, *, released_by: str, note: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                """UPDATE account_locks SET state='released', released_at=?,
                   released_by=?, release_note=? WHERE id=? AND state='held'""",
                (_utcnow(), released_by, note, lock_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    # -- events ------------------------------------------------------------- #
    def add_event(self, *, stage: str, event: str, order_id: str = "", job_id: str = "",
                  code: str = "", elapsed_ms: Optional[int] = None,
                  metadata: Optional[dict[str, Any]] = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO order_events(order_id, job_id, stage, event, code,
                   elapsed_ms, metadata_json, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (order_id or None, job_id or None, stage, event, code or None,
                 elapsed_ms, json.dumps(metadata or {}, ensure_ascii=False, default=str), _utcnow()),
            )
            self._conn.commit()

    def clear_all(self) -> None:
        """Test/reset helper; never called by a live request path."""
        with self._lock:
            for table in ("order_events", "account_locks", "idempotency_keys",
                          "quick_pay_jobs", "orders"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_db: Optional[Persistence] = None
_db_lock = threading.Lock()


def init_db(path: Optional[str] = None) -> Persistence:
    global _db
    with _db_lock:
        if _db is None:
            _db = Persistence(path)
        return _db


def get_db_optional() -> Optional[Persistence]:
    return _db


__all__ = ["Persistence", "init_db", "get_db_optional", "resolve_db_path"]
