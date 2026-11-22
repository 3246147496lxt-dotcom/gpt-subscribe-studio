"""SQLite persistence for orders, quick-pay jobs, idempotency and payment locks.

Only non-secret metadata is stored.  This module has no knowledge of cards,
cookies, session tokens or proxy passwords, and its callers must never add them.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from gss_contracts import Mode, Order, OrderStatus, Plan
from gss_core.checkout_diagnostics import safe_checkout_diagnostics

# Imported lazily in save_order to avoid a store <-> persistence import cycle.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .store import OrderView

SCHEMA_VERSION = 2
_INTENT_DIGEST = re.compile(r"v1:[0-9a-f]{64}")


class QuickPayAdmissionConflict(Exception):
    """Cannot prove this key refers to the same account, intent and job."""


class QuickPayAdmissionBlocked(Exception):
    """A new job is blocked; replaying an admitted job never redispatches."""

    def __init__(self, lock: dict[str, Any]) -> None:
        super().__init__("quick_pay_admission_blocked")
        self.lock = lock


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


def load_checkout_diagnostics(value: Any) -> dict:
    """Database content (including historical rows) is not a trusted diagnostic."""
    if not isinstance(value, str) or len(value) > 8192:
        return {}
    try:
        return safe_checkout_diagnostics(json.loads(value))
    except (ValueError, RecursionError):
        return {}


def _existing_diagnostics_json(value: Any) -> Optional[str]:
    saved = load_checkout_diagnostics(value)
    return json.dumps(saved) if saved else None


class Persistence:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or resolve_db_path()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self._conn.row_factory = sqlite3.Row
        # Choose the first *valid* recorded snapshot within the atomic UPSERT,
        # including when another worker saved its snapshot before this writer.
        self._conn.create_function("gss_existing_diag", 1, _existing_diagnostics_json)
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
                auto_cancel_renew INTEGER NOT NULL DEFAULT 0,
                auto_renew_status TEXT NOT NULL DEFAULT 'disabled',
                auto_renew_attempts INTEGER NOT NULL DEFAULT 0,
                auto_renew_canceled_at TEXT,
                auto_renew_error TEXT,
                auto_renew_http_status INTEGER,
                proxy_rotations INTEGER NOT NULL DEFAULT 0,
                proxy_last_sid TEXT,
                proxy_last_exit_ip TEXT,
                unknown_since TEXT,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_orders_payment ON orders(payment_status);
            CREATE INDEX IF NOT EXISTS idx_orders_account ON orders(account_fingerprint);

            CREATE TABLE IF NOT EXISTS quick_pay_jobs (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                intent_digest TEXT,
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
                message TEXT,
                code TEXT,
                elapsed_ms INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_order ON order_events(order_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_job ON order_events(job_id, created_at);
            """
        )
        for statement in (
            "ALTER TABLE quick_pay_jobs ADD COLUMN lock_id TEXT",
            "ALTER TABLE order_events ADD COLUMN message TEXT",
            "ALTER TABLE orders ADD COLUMN auto_cancel_renew INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE orders ADD COLUMN auto_renew_status TEXT NOT NULL DEFAULT 'disabled'",
            "ALTER TABLE orders ADD COLUMN auto_renew_attempts INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE orders ADD COLUMN auto_renew_canceled_at TEXT",
            "ALTER TABLE orders ADD COLUMN auto_renew_error TEXT",
            "ALTER TABLE orders ADD COLUMN auto_renew_http_status INTEGER",
            "ALTER TABLE orders ADD COLUMN proxy_rotations INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE orders ADD COLUMN proxy_last_sid TEXT",
            "ALTER TABLE orders ADD COLUMN proxy_last_exit_ip TEXT",
        ):
            try:
                self._conn.execute(statement)
            except sqlite3.OperationalError:
                pass
        # CREATE TABLE IF NOT EXISTS does not upgrade deployed schemas. Old
        # jobs have no comparable intent or recorded diagnostics; do not infer
        # history. Ignore only a column already present, never I/O errors.
        for column, definition in (
            ("intent_digest", "TEXT"),
            ("diagnostics_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE quick_pay_jobs ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                if str(exc).lower() != f"duplicate column name: {column}":
                    raise
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
                "receipt", "auto_cancel_renew", "auto_renew_status", "auto_renew_attempts",
                "auto_renew_canceled_at", "auto_renew_error", "auto_renew_http_status",
                "proxy_rotations", "proxy_last_sid", "proxy_last_exit_ip",
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
            int(view.auto_cancel_renew), view.auto_renew_status, int(view.auto_renew_attempts),
            view.auto_renew_canceled_at, view.auto_renew_error, view.auto_renew_http_status,
            int(view.proxy_rotations), view.proxy_last_sid, view.proxy_last_exit_ip,
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
                    auto_cancel_renew, auto_renew_status, auto_renew_attempts,
                    auto_renew_canceled_at, auto_renew_error, auto_renew_http_status,
                    proxy_rotations, proxy_last_sid, proxy_last_exit_ip,
                    view_json, created_at, updated_at, unknown_since, finished_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    auto_cancel_renew=excluded.auto_cancel_renew,
                    auto_renew_status=excluded.auto_renew_status,
                    auto_renew_attempts=excluded.auto_renew_attempts,
                    auto_renew_canceled_at=excluded.auto_renew_canceled_at,
                    auto_renew_error=excluded.auto_renew_error,
                    auto_renew_http_status=excluded.auto_renew_http_status,
                    proxy_rotations=excluded.proxy_rotations,
                    proxy_last_sid=excluded.proxy_last_sid,
                    proxy_last_exit_ip=excluded.proxy_last_exit_ip,
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
    def get_quick_pay_by_key(self, key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM quick_pay_jobs WHERE idempotency_key=?", (key,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_quick_pay_by_id(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM quick_pay_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def reserve_quick_pay(
        self, job: Any, intent_digest: str,
    ) -> tuple[dict[str, Any], bool]:
        """Commit a new queued job and key, or return the existing row read-only.

        Only the insert winner can dispatch. BEGIN IMMEDIATE serializes the entire
        admission decision, including the funds-lock check, across SQLite connections.
        """
        if not _INTENT_DIGEST.fullmatch(intent_digest):
            raise ValueError("invalid quick-pay intent digest")
        key, owner = job.idempotency_key, job.account_fingerprint
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            with self._conn:  # any SQL or commit failure rolls back both inserts
                row = self.get_quick_pay_by_key(key)
                idx = self._conn.execute(
                    "SELECT scope, job_id, order_id FROM idempotency_keys WHERE key=?", (key,)
                ).fetchone()
                order = self._conn.execute(
                    "SELECT id, account_fingerprint FROM orders WHERE idempotency_key=?", (key,)
                ).fetchone()
                if row is not None:
                    if (not owner or owner != row["account_fingerprint"]
                            or intent_digest != row["intent_digest"] or idx is None
                            or idx["job_id"] != row["id"] or idx["scope"] != "quick_pay"):
                        raise QuickPayAdmissionConflict()
                    # The worker may publish its order link in either record first.
                    # While an order exists without a verified link, use GET by job ID.
                    links = {v for v in (row["order_id"], idx["order_id"]) if v}
                    if (links != ({order["id"]} if order is not None else set())
                            or (order is not None and order["account_fingerprint"]
                                and order["account_fingerprint"] != owner)):
                        raise QuickPayAdmissionConflict()
                    return row, False

                if idx is not None or order is not None:
                    raise QuickPayAdmissionConflict()
                held = self.find_active_lock(f"account:{owner}") if owner else None
                if held is not None:
                    raise QuickPayAdmissionBlocked(held)
                now = _utcnow()
                self._conn.execute(
                    """INSERT INTO quick_pay_jobs
                       (id,idempotency_key,intent_digest,account_fingerprint,status,stage,created_at,updated_at)
                       VALUES(?,?,?,?,'queued','queued',?,?)""",
                    (job.id, key, intent_digest, owner or None, now, now),
                )
                self._conn.execute(
                    """INSERT INTO idempotency_keys (key,scope,job_id,state,created_at,last_seen_at)
                       VALUES(?,'quick_pay',?,'queued',?,?)""", (key, job.id, now, now),
                )
                created = self.get_quick_pay_by_id(job.id)
                if created is None:
                    raise sqlite3.DatabaseError("quick-pay reservation unreadable")
                return created, True

    def save_job(self, job: Any) -> None:
        diagnostics = safe_checkout_diagnostics(getattr(job, "diagnostics", None))
        if job.status == "failed" and not diagnostics:
            # All writers share this boundary. No wire evidence is not the
            # same as no failure; record only an allowlisted local stage.
            diagnostics = safe_checkout_diagnostics({"stage": job.stage}) or {"stage": "failed"}
        payload = (
            job.id, job.idempotency_key, getattr(job, "account_fingerprint", None),
            job.order_id, job.status, job.stage, job.failure_code, job.message,
            job.amount, job.currency, job.purchase_attempt_id, getattr(job, "lock_id", None),
            json.dumps(getattr(job, "timings", {}) or {}, ensure_ascii=False),
            json.dumps(diagnostics),
            getattr(job, "locked_by", None), getattr(job, "lease_until", None),
            datetime.fromtimestamp(job.started_at, timezone.utc).isoformat()
            if getattr(job, "started_at", None) else _utcnow(),
            _utcnow(),
            datetime.fromtimestamp(job.finished_at, timezone.utc).isoformat()
            if getattr(job, "finished_at", None) else None,
        )
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO quick_pay_jobs(
                    id, idempotency_key, account_fingerprint, order_id, status, stage,
                    failure_code, message, amount, currency, purchase_attempt_id, lock_id,
                    timings_json, diagnostics_json, locked_by, lease_until,
                    created_at, updated_at, finished_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status, stage=excluded.stage,
                    order_id=COALESCE(NULLIF(quick_pay_jobs.order_id,''), excluded.order_id),
                    failure_code=excluded.failure_code, message=excluded.message,
                    amount=excluded.amount, currency=excluded.currency,
                    purchase_attempt_id=excluded.purchase_attempt_id,
                    lock_id=excluded.lock_id,
                    timings_json=excluded.timings_json,
                    diagnostics_json=COALESCE(gss_existing_diag(quick_pay_jobs.diagnostics_json), excluded.diagnostics_json),
                    updated_at=excluded.updated_at, finished_at=excluded.finished_at""",
                payload,
            )
            self._conn.commit()

    def rejected_before_payment(self, job_id: str, order_id: str) -> bool:
        """A conservative read-only UI hint, never payment authority.

        The job links its order *before* returning from creation only on our
        verified rejection path. Require the matching persisted order, explicit
        HTTP rejection, pre-payment event history and no unresolved funds.
        Missing or inconsistent evidence means no handoff.
        """
        with self._lock:
            try:
                row = self._conn.execute(
                    """SELECT j.diagnostics_json FROM quick_pay_jobs j
                       JOIN orders o ON o.id=j.order_id AND o.idempotency_key=j.idempotency_key
                                    AND o.account_fingerprint=j.account_fingerprint
                       WHERE j.id=? AND o.id=? AND j.status='failed' AND j.stage='failed'
                         AND COALESCE(j.account_fingerprint,'')!=''
                         AND COALESCE(j.lock_id,'')='' AND COALESCE(j.purchase_attempt_id,'')=''
                         AND j.failure_code IN ('checkout_upstream_error','invalid_session')
                         AND o.status='rejected' AND o.payment_status='unpaid' AND o.delivery_status='pending'
                         AND o.failure_code IN ('unusual_activity','invalid_session')
                         AND COALESCE(o.checkout_session_id,'')='' AND COALESCE(o.payment_intent_id,'')=''
                         AND CASE WHEN json_valid(o.view_json) THEN
                             json_extract(o.view_json,'$.checkout_url') IS NULL AND
                             json_extract(o.view_json,'$.observed_payment_id') IS NULL AND
                             json_extract(o.view_json,'$.paid_amount') IS NULL AND
                             json_extract(o.view_json,'$.receipt') IS NULL ELSE 0 END
                         AND EXISTS (SELECT 1 FROM order_events WHERE job_id=j.id AND stage='creating_checkout')
                         AND (SELECT stage FROM order_events WHERE job_id=j.id ORDER BY id DESC LIMIT 1)='failed'
                         AND NOT EXISTS (SELECT 1 FROM order_events WHERE job_id=j.id AND stage NOT IN
                             ('creating_order','running','reissuing','reissued','session_refresh',
                              'session_refreshed','browser_decode','browser_decoded','browser_decode_skipped',
                              'creating_checkout','failed'))
                         AND NOT EXISTS (SELECT 1 FROM account_locks l WHERE l.state='held' AND
                             (l.lock_key='account:' || j.account_fingerprint OR l.order_id=o.id OR l.job_id=j.id))
                         AND NOT EXISTS (SELECT 1 FROM quick_pay_jobs other
                             WHERE other.account_fingerprint=j.account_fingerprint AND other.id!=j.id
                               AND (other.created_at>=j.created_at OR other.status IN ('running','queued','unknown')))
                         AND NOT EXISTS (SELECT 1 FROM orders other WHERE other.id!=o.id
                             AND other.account_fingerprint=j.account_fingerprint
                             AND (other.created_at>=o.created_at OR other.status NOT IN ('failed','rejected')
                                  OR other.payment_status NOT IN ('unpaid','failed')))""",
                    (job_id, order_id),
                ).fetchone()
            except sqlite3.Error:
                return False
        return row is not None and load_checkout_diagnostics(row[0]).get("upstream_status") in (400, 401, 403)

    def load_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM quick_pay_jobs ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    # -- idempotency -------------------------------------------------------- #
    def save_idempotency(self, key: str, scope: str, *, order_id: str = "", job_id: str = "", state: str) -> None:
        now = _utcnow()
        # Job and order writers share the key and may learn different links at
        # different times. Do not erase a link with NULL or rebind a populated one.
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO idempotency_keys(key, scope, order_id, job_id, state, created_at, last_seen_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     order_id=COALESCE(NULLIF(idempotency_keys.order_id,''), excluded.order_id),
                     job_id=COALESCE(NULLIF(idempotency_keys.job_id,''), excluded.job_id),
                     state=excluded.state, last_seen_at=excluded.last_seen_at
                   WHERE (excluded.order_id IS NULL OR COALESCE(idempotency_keys.order_id,'')=''
                          OR excluded.order_id=idempotency_keys.order_id)
                     AND (excluded.job_id IS NULL OR COALESCE(idempotency_keys.job_id,'')=''
                          OR excluded.job_id=idempotency_keys.job_id)
                     AND (idempotency_keys.scope=excluded.scope
                          OR (idempotency_keys.scope IN ('quick_pay','order_create')
                              AND excluded.scope IN ('quick_pay','order_create')))""",
                (key, scope, order_id or None, job_id or None, state, now, now),
            )
            if cur.rowcount != 1:
                raise QuickPayAdmissionConflict()

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
                  message: str = "", code: str = "", elapsed_ms: Optional[int] = None,
                  metadata: Optional[dict[str, Any]] = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO order_events(order_id, job_id, stage, event, message, code,
                   elapsed_ms, metadata_json, created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (order_id or None, job_id or None, stage, event, message or None, code or None,
                 elapsed_ms, json.dumps(metadata or {}, ensure_ascii=False, default=str), _utcnow()),
            )
            self._conn.commit()

    def list_events(self, *, job_id: str = "", order_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            if job_id:
                rows = self._conn.execute(
                    "SELECT * FROM order_events WHERE job_id=? ORDER BY id ASC", (job_id,)
                ).fetchall()
            elif order_id:
                rows = self._conn.execute(
                    "SELECT * FROM order_events WHERE order_id=? ORDER BY id ASC", (order_id,)
                ).fetchall()
            else:
                return []
        return [
            {"state": row["event"], "stage": row["stage"], "message": row["message"] or "",
             "created_at": row["created_at"]}
            for row in rows
        ]

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


__all__ = [
    "Persistence", "QuickPayAdmissionBlocked", "QuickPayAdmissionConflict",
    "init_db", "get_db_optional", "resolve_db_path",
]
