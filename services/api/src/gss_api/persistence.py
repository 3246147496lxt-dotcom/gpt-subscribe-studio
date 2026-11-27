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
                account_email TEXT,
                card_brand TEXT,
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
                finished_at TEXT,
                failure_trace_json TEXT
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
                next_action_json TEXT NOT NULL DEFAULT '{}',
                priority INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                reconcile_attempts INTEGER NOT NULL DEFAULT 0,
                last_reconciled_at TEXT,
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

            CREATE TABLE IF NOT EXISTS account_subscription_snapshots (
                account_key TEXT PRIMARY KEY,
                email TEXT,
                account_id TEXT,
                detail_json TEXT NOT NULL,
                queried_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_account_snapshots_email
                ON account_subscription_snapshots(email);

            CREATE TABLE IF NOT EXISTS portal_users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS portal_tasks (
                job_id TEXT PRIMARY KEY,
                owner_user_id TEXT,
                guest_token_hash TEXT,
                guest_expires_at TEXT,
                masked_email TEXT,
                plan TEXT NOT NULL,
                exit_country TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_portal_tasks_user
                ON portal_tasks(owner_user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_portal_tasks_guest
                ON portal_tasks(guest_token_hash, created_at DESC);

            CREATE TABLE IF NOT EXISTS order_sensitive_vault (
                order_id TEXT PRIMARY KEY,
                session_ciphertext BLOB,
                card_ciphertext BLOB,
                has_session INTEGER NOT NULL DEFAULT 0,
                has_card INTEGER NOT NULL DEFAULT 0,
                card_brand TEXT,
                card_bin TEXT,
                card_last4 TEXT,
                card_exp TEXT,
                card_fingerprint TEXT,
                key_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                purged_at TEXT,
                purge_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_vault_expires
                ON order_sensitive_vault(expires_at) WHERE purged_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_vault_card_fp
                ON order_sensitive_vault(card_fingerprint);

            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                key_prefix TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'active',
                rate_limit_rpm INTEGER NOT NULL DEFAULT 30,
                allowed_plans_json TEXT NOT NULL DEFAULT '["go","plus","pro_lite","pro","pro_max"]',
                note TEXT,
                total_requests INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_api_keys_status ON api_keys(status, created_at DESC);

            CREATE TABLE IF NOT EXISTS api_tasks (
                job_id TEXT PRIMARY KEY,
                api_key_id TEXT NOT NULL,
                external_order_id TEXT,
                masked_email TEXT,
                plan TEXT NOT NULL,
                exit_country TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_api_tasks_key
                ON api_tasks(api_key_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_api_tasks_ext
                ON api_tasks(api_key_id, external_order_id);
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
            "ALTER TABLE orders ADD COLUMN account_email TEXT",
            "ALTER TABLE orders ADD COLUMN card_brand TEXT",
            "ALTER TABLE orders ADD COLUMN failure_trace_json TEXT",
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
            ("next_action_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("priority", "INTEGER NOT NULL DEFAULT 0"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("reconcile_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("last_reconciled_at", "TEXT"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE quick_pay_jobs ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                if str(exc).lower() != f"duplicate column name: {column}":
                    raise
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_queue ON quick_pay_jobs(status, priority DESC, created_at ASC)"
        )
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
                "purchase_attempt_id", "account_email", "card_brand", "route_profile", "failure_code", "checkout_url",
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

    # -- account subscription snapshots -------------------------------------- #

    def save_account_snapshot(self, account_key: str, *, email: str, account_id: str,
                              detail: dict, queried_at: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO account_subscription_snapshots(
                    account_key, email, account_id, detail_json, queried_at, updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(account_key) DO UPDATE SET
                    email=excluded.email,
                    account_id=excluded.account_id,
                    detail_json=excluded.detail_json,
                    queried_at=excluded.queried_at,
                    updated_at=excluded.updated_at""",
                (account_key, email or None, account_id or None,
                 json.dumps(detail, ensure_ascii=False), queried_at, _utcnow()),
            )

    def get_account_snapshot(self, account_key: str) -> Optional[dict]:
        if not account_key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM account_subscription_snapshots WHERE account_key = ?",
                (account_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            detail = json.loads(row["detail_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(detail, dict):
            return None
        return {
            **detail,
            "email": detail.get("email") or row["email"] or "",
            "accountId": detail.get("accountId") or row["account_id"] or "",
            "queriedAt": row["queried_at"] or detail.get("queriedAt"),
        }

    def save_order(self, order: Order, view: "OrderView", *, account_fingerprint: Optional[str] = None) -> None:
        payload = (
            order.id, order.idempotency_key or None, account_fingerprint,
            view.account_email or None, view.card_brand or None,
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
            json.dumps(view.failure_trace, ensure_ascii=False)
            if isinstance(getattr(view, "failure_trace", None), dict)
            else None,
        )
        with self._lock:
            self._conn.execute(
                """INSERT INTO orders(
                    id, idempotency_key, account_fingerprint, account_email, card_brand,
                    purchase_attempt_id, plan,
                    billing_country, exit_country, account_source, card_source, billing_mode,
                    risk_acknowledged, status, payment_status, delivery_status, amount, cost,
                    currency, payment_intent_id, checkout_session_id, checkout_kind, failure_code,
                    last_error, tax_delta, route_profile, billing_verified, session_reissued,
                    auto_cancel_renew, auto_renew_status, auto_renew_attempts,
                    auto_renew_canceled_at, auto_renew_error, auto_renew_http_status,
                    proxy_rotations, proxy_last_sid, proxy_last_exit_ip,
                    view_json, created_at, updated_at, unknown_since, finished_at, failure_trace_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    idempotency_key=excluded.idempotency_key,
                    account_fingerprint=excluded.account_fingerprint,
                    account_email=COALESCE(NULLIF(excluded.account_email,''), orders.account_email),
                    card_brand=COALESCE(NULLIF(excluded.card_brand,''), orders.card_brand),
                    purchase_attempt_id=excluded.purchase_attempt_id,
                    exit_country=excluded.exit_country,
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
                    finished_at=excluded.finished_at,
                    failure_trace_json=COALESCE(NULLIF(excluded.failure_trace_json,''), orders.failure_trace_json)""",
                payload,
            )
            self._conn.commit()

    @staticmethod
    def _row_to_order(row: sqlite3.Row) -> tuple[Order, "OrderView", Optional[str]]:
        from .store import OrderView
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
        if not view.exit_country and row["exit_country"]:
            view.exit_country = row["exit_country"]
        if not view.account_email and row["account_email"]:
            view.account_email = row["account_email"]
        if not view.card_brand and row["card_brand"]:
            view.card_brand = row["card_brand"]
        return order, view, row["account_fingerprint"]

    def load_orders(self, *, limit: Optional[int] = None) -> list[tuple[Order, "OrderView", Optional[str]]]:
        with self._lock:
            if limit is None:
                rows = self._conn.execute("SELECT * FROM orders ORDER BY created_at").fetchall()
            else:
                capped = max(1, int(limit))
                # Always load all non-terminal/unknown orders plus the newest N settled orders.
                rows = self._conn.execute(
                    """SELECT * FROM (
                           SELECT * FROM orders WHERE status NOT IN ('delivered','failed','rejected','refunded')
                           UNION
                           SELECT * FROM (
                               SELECT * FROM orders ORDER BY created_at DESC LIMIT ?
                           )
                       ) ORDER BY created_at ASC""",
                    (capped,),
                ).fetchall()
        return [self._row_to_order(row) for row in rows]

    def get_order_by_id(self, order_id: str) -> Optional[tuple[Order, "OrderView", Optional[str]]]:
        if not order_id:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return self._row_to_order(row) if row is not None else None

    def find_order_id_by_idempotency(self, key: str) -> Optional[str]:
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT order_id FROM idempotency_keys WHERE key=? AND COALESCE(order_id,'')!=''",
                (key,),
            ).fetchone()
            if row is not None:
                return str(row["order_id"])
            ord_row = self._conn.execute(
                "SELECT id FROM orders WHERE idempotency_key=?", (key,)
            ).fetchone()
        return str(ord_row["id"]) if ord_row is not None else None

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
                priority = int(getattr(job, "priority", 0) or 0)
                self._conn.execute(
                    """INSERT INTO quick_pay_jobs
                       (id,idempotency_key,intent_digest,account_fingerprint,status,stage,priority,attempts,created_at,updated_at)
                       VALUES(?,?,?,?,'queued','queued',?,0,?,?)""",
                    (job.id, key, intent_digest, owner or None, priority, now, now),
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
        lease_raw = getattr(job, "lease_until", None)
        lease_text = (
            datetime.fromtimestamp(lease_raw, timezone.utc).isoformat()
            if isinstance(lease_raw, (int, float))
            else (str(lease_raw) if lease_raw else None)
        )
        next_action_raw = getattr(job, "next_action", None)
        next_action_json = json.dumps(next_action_raw if isinstance(next_action_raw, dict) else {}, ensure_ascii=False)
        payload = (
            job.id, job.idempotency_key, getattr(job, "account_fingerprint", None),
            job.order_id, job.status, job.stage, job.failure_code, job.message,
            job.amount, job.currency, job.purchase_attempt_id, getattr(job, "lock_id", None),
            json.dumps(getattr(job, "timings", {}) or {}, ensure_ascii=False),
            json.dumps(diagnostics),
            next_action_json,
            int(getattr(job, "priority", 0) or 0),
            int(getattr(job, "attempts", 0) or 0),
            int(getattr(job, "reconcile_attempts", 0) or 0),
            getattr(job, "last_reconciled_at", None),
            getattr(job, "locked_by", None), lease_text,
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
                    timings_json, diagnostics_json, next_action_json, priority, attempts,
                    reconcile_attempts, last_reconciled_at, locked_by, lease_until,
                    created_at, updated_at, finished_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status, stage=excluded.stage,
                    order_id=COALESCE(NULLIF(quick_pay_jobs.order_id,''), excluded.order_id),
                    failure_code=excluded.failure_code, message=excluded.message,
                    amount=excluded.amount, currency=excluded.currency,
                    purchase_attempt_id=excluded.purchase_attempt_id,
                    lock_id=excluded.lock_id,
                    timings_json=excluded.timings_json,
                    diagnostics_json=COALESCE(gss_existing_diag(quick_pay_jobs.diagnostics_json), excluded.diagnostics_json),
                    next_action_json=excluded.next_action_json,
                    priority=excluded.priority,
                    attempts=excluded.attempts,
                    reconcile_attempts=excluded.reconcile_attempts,
                    last_reconciled_at=COALESCE(excluded.last_reconciled_at, quick_pay_jobs.last_reconciled_at),
                    locked_by=excluded.locked_by,
                    lease_until=excluded.lease_until,
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
                             ('preflight','preflight_passed','creating_order','running','reissuing','reissued',
                              'session_refresh','session_refreshed','browser_decode','browser_decoded',
                              'browser_decode_skipped','creating_checkout','failed'))
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

    def queue_position(self, job_id: str) -> Optional[int]:
        """Return 1-based position among currently queued jobs, or None if not queued."""
        with self._lock:
            target = self._conn.execute(
                "SELECT id, status, priority, created_at FROM quick_pay_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if target is None or target["status"] != "queued":
                return None
            ahead = self._conn.execute(
                """SELECT COUNT(*) FROM quick_pay_jobs
                   WHERE status='queued'
                     AND (
                         priority > ?
                         OR (priority = ? AND created_at < ?)
                         OR (priority = ? AND created_at = ? AND id < ?)
                     )""",
                (
                    target["priority"],
                    target["priority"], target["created_at"],
                    target["priority"], target["created_at"], target["id"],
                ),
            ).fetchone()[0]
        return int(ahead) + 1

    def queue_stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM quick_pay_jobs WHERE status IN ('queued','running') GROUP BY status"
            ).fetchall()
        counts = {row["status"]: int(row["cnt"]) for row in rows}
        return {"queued": counts.get("queued", 0), "running": counts.get("running", 0)}

    def list_queue_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        capped = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, order_id, status, stage, failure_code, message, amount, currency,
                          purchase_attempt_id, lock_id, timings_json, priority, attempts,
                          reconcile_attempts, last_reconciled_at,
                          created_at, updated_at, finished_at
                   FROM quick_pay_jobs
                   ORDER BY
                       CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END ASC,
                       priority DESC,
                       created_at DESC
                   LIMIT ?""",
                (capped,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_reconcilable_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        capped = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM quick_pay_jobs
                   WHERE status IN ('paid_pending_delivery', 'unknown', 'requires_action')
                     AND COALESCE(order_id, '') != ''
                   ORDER BY updated_at ASC
                   LIMIT ?""",
                (capped,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job_by_order_id(self, order_id: str) -> Optional[dict[str, Any]]:
        if not order_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM quick_pay_jobs WHERE order_id=? ORDER BY created_at DESC LIMIT 1",
                (order_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_order_failure_row(self, order_id: str) -> Optional[dict[str, Any]]:
        """Fetch redacted order failure metadata and stored HTTP trace for admin diagnostics."""
        if not order_id:
            return None
        with self._lock:
            row = self._conn.execute(
                """SELECT o.id, o.plan, o.billing_country, o.exit_country, o.billing_mode,
                          o.status, o.payment_status, o.delivery_status, o.amount, o.currency,
                          o.checkout_kind, o.route_profile, o.billing_verified, o.session_reissued,
                          o.failure_code, o.last_error, o.auto_renew_status, o.auto_renew_error,
                          o.auto_renew_http_status, o.failure_trace_json,
                          o.created_at, o.updated_at,
                          j.id AS job_id, j.stage AS job_stage, j.status AS job_status,
                          j.failure_code AS job_failure_code, j.message AS job_message,
                          j.diagnostics_json AS diagnostics_json
                   FROM orders o
                   LEFT JOIN quick_pay_jobs j
                     ON j.id = (SELECT j2.id FROM quick_pay_jobs j2
                                WHERE j2.order_id = o.id
                                ORDER BY j2.created_at DESC, j2.rowid DESC LIMIT 1)
                   WHERE o.id = ?
                   LIMIT 1""",
                (order_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_orders_page(
        self,
        *,
        status: str = "",
        plan: str = "",
        source: str = "",
        search: str = "",
        date_from: str = "",
        date_to: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Paginated read-only order ledger. Never returns secrets or raw view_json."""
        capped = max(1, min(int(limit), 100))
        skip = max(0, int(offset))
        where_sql, params = self._orders_filter_sql(
            status=status, plan=plan, source=source, search=search,
            date_from=date_from, date_to=date_to,
        )
        with self._lock:
            total = int(
                self._conn.execute(f"SELECT COUNT(*) FROM orders o {where_sql}", params).fetchone()[0]
            )
            rows = self._conn.execute(
                f"""WITH page_orders AS (
                        SELECT o.id, o.account_fingerprint,
                               COALESCE(
                                   NULLIF(o.account_email, ''),
                                   CASE WHEN o.billing_verified = 0 THEN json_extract(o.view_json, '$.billing.email') END
                               ) AS account_email,
                               COALESCE(NULLIF(o.card_brand, ''), json_extract(o.view_json, '$.receipt.card.brand')) AS card_brand,
                               o.plan, o.billing_country, o.exit_country,
                               o.account_source, o.card_source, o.billing_mode, o.status,
                               o.payment_status, o.delivery_status, o.amount, o.cost, o.currency,
                               o.failure_code, o.last_error, o.purchase_attempt_id,
                               o.payment_intent_id, o.checkout_session_id, o.checkout_kind,
                               o.tax_delta, o.route_profile, o.billing_verified, o.session_reissued,
                               o.auto_cancel_renew, o.auto_renew_status, o.auto_renew_attempts,
                               o.auto_renew_canceled_at, o.auto_renew_error, o.auto_renew_http_status,
                               o.proxy_rotations, o.proxy_last_sid, o.proxy_last_exit_ip,
                               o.created_at, o.updated_at, o.unknown_since, o.finished_at,
                               (SELECT j.id FROM quick_pay_jobs j
                                WHERE j.order_id = o.id
                                ORDER BY j.created_at DESC, j.rowid DESC LIMIT 1) AS latest_job_id,
                               (SELECT l.id FROM account_locks l
                                WHERE l.order_id = o.id AND l.state = 'held' LIMIT 1) AS active_lock_id
                        FROM orders o
                        {where_sql}
                        ORDER BY o.created_at DESC
                        LIMIT ? OFFSET ?
                    )
                    SELECT po.*,
                           j.id AS job_id,
                           j.status AS job_status,
                           j.stage AS job_stage,
                           j.attempts AS job_attempts,
                           j.reconcile_attempts AS reconcile_attempts,
                           j.last_reconciled_at AS last_reconciled_at,
                           j.timings_json AS timings_json,
                           j.diagnostics_json AS diagnostics_json,
                           j.finished_at AS job_finished_at,
                           pt.job_id AS portal_task_job_id,
                           pt.masked_email AS masked_email,
                           pu.username AS portal_username,
                           at.job_id AS api_task_job_id,
                           at.api_key_id AS api_key_id,
                           at.external_order_id AS external_order_id,
                           COALESCE(pt.masked_email, at.masked_email) AS resolved_masked_email,
                           ak.name AS api_key_name,
                           ov.has_session AS vault_has_session,
                           ov.has_card AS vault_has_card,
                           ov.card_last4 AS vault_card_last4,
                           ov.expires_at AS vault_expires_at,
                           ov.purged_at AS vault_purged_at
                    FROM page_orders po
                    LEFT JOIN quick_pay_jobs j ON j.id = po.latest_job_id
                    LEFT JOIN portal_tasks pt ON pt.job_id = po.latest_job_id
                    LEFT JOIN portal_users pu ON pu.id = pt.owner_user_id
                    LEFT JOIN api_tasks at ON at.job_id = po.latest_job_id
                    LEFT JOIN api_keys ak ON ak.id = at.api_key_id
                    LEFT JOIN order_sensitive_vault ov ON ov.order_id = po.id
                    ORDER BY po.created_at DESC""",
                [*params, capped, skip],
            ).fetchall()
        return [dict(row) for row in rows], total

    @staticmethod
    def _orders_filter_sql(
        *,
        status: str = "",
        plan: str = "",
        source: str = "",
        search: str = "",
        date_from: str = "",
        date_to: str = "",
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        status_norm = (status or "").strip().lower()
        if status_norm and status_norm != "all":
            if status_norm == "succeeded":
                clauses.append("o.status IN ('active', 'delivered')")
            elif status_norm == "failed":
                clauses.append("o.status IN ('failed', 'rejected')")
            elif status_norm == "paid_pending_delivery":
                clauses.append("o.payment_status = 'succeeded' AND o.delivery_status != 'delivered'")
            elif status_norm == "pending":
                clauses.append("o.status IN ('created', 'pending_checkout', 'awaiting_payment', 'paying', 'requires_action', 'verifying')")
            else:
                clauses.append("o.status = ?")
                params.append(status_norm)
        plan_norm = (plan or "").strip().lower()
        if plan_norm and plan_norm != "all":
            clauses.append("o.plan = ?")
            params.append(plan_norm)
        source_norm = (source or "").strip().lower()
        if source_norm and source_norm != "all":
            if source_norm == "portal":
                clauses.append(
                    "EXISTS (SELECT 1 FROM quick_pay_jobs j "
                    "JOIN portal_tasks pt ON pt.job_id = j.id WHERE j.order_id = o.id)"
                )
            elif source_norm == "api":
                clauses.append(
                    "EXISTS (SELECT 1 FROM quick_pay_jobs j "
                    "JOIN api_tasks at ON at.job_id = j.id WHERE j.order_id = o.id)"
                )
            elif source_norm == "quick_pay":
                clauses.append(
                    "EXISTS (SELECT 1 FROM quick_pay_jobs j WHERE j.order_id = o.id) "
                    "AND NOT EXISTS (SELECT 1 FROM quick_pay_jobs j "
                    "JOIN portal_tasks pt ON pt.job_id = j.id WHERE j.order_id = o.id) "
                    "AND NOT EXISTS (SELECT 1 FROM quick_pay_jobs j "
                    "JOIN api_tasks at ON at.job_id = j.id WHERE j.order_id = o.id)"
                )
            elif source_norm == "step":
                clauses.append("NOT EXISTS (SELECT 1 FROM quick_pay_jobs j WHERE j.order_id = o.id)")
        search_norm = (search or "").strip()
        if search_norm:
            like = f"%{search_norm}%"
            clauses.append(
                "("
                "o.id LIKE ?"
                " OR COALESCE(o.account_email,'') LIKE ?"
                " OR (o.billing_verified = 0 AND COALESCE(json_extract(o.view_json, '$.billing.email'),'') LIKE ?)"
                " OR COALESCE(o.card_brand,'') LIKE ?"
                " OR COALESCE(o.purchase_attempt_id,'') LIKE ?"
                " OR COALESCE(o.payment_intent_id,'') LIKE ?"
                " OR COALESCE(o.checkout_session_id,'') LIKE ?"
                " OR COALESCE(o.failure_code,'') LIKE ?"
                " OR EXISTS (SELECT 1 FROM quick_pay_jobs j WHERE j.order_id = o.id AND j.id LIKE ?)"
                " OR EXISTS ("
                "     SELECT 1 FROM quick_pay_jobs j"
                "     JOIN portal_tasks pt ON pt.job_id = j.id"
                "     LEFT JOIN portal_users pu ON pu.id = pt.owner_user_id"
                "     WHERE j.order_id = o.id"
                "       AND (COALESCE(pt.masked_email,'') LIKE ? OR COALESCE(pu.username,'') LIKE ?)"
                " )"
                " OR EXISTS ("
                "     SELECT 1 FROM quick_pay_jobs j"
                "     JOIN api_tasks at ON at.job_id = j.id"
                "     LEFT JOIN api_keys ak ON ak.id = at.api_key_id"
                "     WHERE j.order_id = o.id"
                "       AND (COALESCE(at.external_order_id,'') LIKE ? OR COALESCE(ak.name,'') LIKE ?)"
                " )"
                " OR EXISTS ("
                "     SELECT 1 FROM order_sensitive_vault ov"
                "     WHERE ov.order_id = o.id"
                "       AND (COALESCE(ov.card_last4,'') LIKE ? OR COALESCE(ov.card_bin,'') LIKE ?)"
                " )"
                ")"
            )
            params.extend([like] * 15)
        df = (date_from or "").strip()
        if df:
            clauses.append("o.created_at >= ?")
            params.append(df if "T" in df else f"{df}T00:00:00")
        dt = (date_to or "").strip()
        if dt:
            clauses.append("o.created_at <= ?")
            params.append(dt if "T" in dt else f"{dt}T23:59:59.999999+99:99")
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where_sql, params

    def export_orders_rows(
        self,
        *,
        status: str = "",
        plan: str = "",
        source: str = "",
        search: str = "",
        date_from: str = "",
        date_to: str = "",
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        capped = max(1, min(int(limit), 5000))
        where_sql, params = self._orders_filter_sql(
            status=status, plan=plan, source=source, search=search,
            date_from=date_from, date_to=date_to,
        )
        with self._lock:
            rows = self._conn.execute(
                f"""WITH export_orders AS (
                        SELECT o.id, o.account_fingerprint,
                               COALESCE(
                                   NULLIF(o.account_email, ''),
                                   CASE WHEN o.billing_verified = 0 THEN json_extract(o.view_json, '$.billing.email') END
                               ) AS account_email,
                               COALESCE(NULLIF(o.card_brand, ''), json_extract(o.view_json, '$.receipt.card.brand')) AS card_brand,
                               o.plan, o.billing_country, o.exit_country,
                               o.billing_mode, o.status, o.payment_status, o.delivery_status,
                               o.amount, o.currency, o.tax_delta, o.failure_code,
                               o.purchase_attempt_id, o.payment_intent_id, o.checkout_session_id,
                               o.checkout_kind, o.route_profile, o.billing_verified, o.session_reissued,
                               o.auto_renew_status, o.auto_renew_attempts, o.auto_renew_canceled_at,
                               o.proxy_rotations, o.proxy_last_sid, o.proxy_last_exit_ip,
                               o.created_at, o.updated_at, o.finished_at,
                               (SELECT j.id FROM quick_pay_jobs j
                                WHERE j.order_id = o.id
                                ORDER BY j.created_at DESC, j.rowid DESC LIMIT 1) AS latest_job_id,
                               (SELECT l.id FROM account_locks l
                                WHERE l.order_id = o.id AND l.state = 'held' LIMIT 1) AS active_lock_id
                        FROM orders o
                        {where_sql}
                        ORDER BY o.created_at DESC
                        LIMIT ?
                    )
                    SELECT eo.*,
                           j.id AS job_id,
                           j.reconcile_attempts AS reconcile_attempts,
                           j.timings_json AS timings_json,
                           j.diagnostics_json AS diagnostics_json,
                           j.finished_at AS job_finished_at,
                           pt.job_id AS portal_task_job_id,
                           pt.masked_email AS masked_email,
                           pu.username AS portal_username,
                           at.job_id AS api_task_job_id,
                           at.api_key_id AS api_key_id,
                           at.external_order_id AS external_order_id,
                           ak.name AS api_key_name
                    FROM export_orders eo
                    LEFT JOIN quick_pay_jobs j ON j.id = eo.latest_job_id
                    LEFT JOIN portal_tasks pt ON pt.job_id = eo.latest_job_id
                    LEFT JOIN portal_users pu ON pu.id = pt.owner_user_id
                    LEFT JOIN api_tasks at ON at.job_id = eo.latest_job_id
                    LEFT JOIN api_keys ak ON ak.id = at.api_key_id
                    ORDER BY eo.created_at DESC""",
                [*params, capped],
            ).fetchall()
        return [dict(row) for row in rows]

    def maintenance_stats(self) -> dict[str, Any]:
        with self._lock:
            counts = {}
            for table in (
                "orders",
                "quick_pay_jobs",
                "order_events",
                "account_locks",
                "account_subscription_snapshots",
                "order_sensitive_vault",
                "api_keys",
                "api_tasks",
            ):
                counts[table] = int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        db_bytes = 0
        wal_bytes = 0
        if self.path != ":memory:":
            p = Path(self.path)
            if p.exists():
                db_bytes = p.stat().st_size
            wal = Path(f"{self.path}-wal")
            if wal.exists():
                wal_bytes = wal.stat().st_size
        return {
            "in_memory": self.path == ":memory:",
            "db_bytes": db_bytes,
            "wal_bytes": wal_bytes,
            "table_counts": counts,
        }

    def run_maintenance(self, *, retention_days: int = 90) -> dict[str, Any]:
        """Prune old settled events and truncate WAL safely without touching held locks or active orders."""
        from datetime import timedelta
        days = max(7, min(int(retention_days), 3650))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """DELETE FROM order_events
                   WHERE created_at < ?
                     AND (order_id IS NULL OR order_id NOT IN (
                         SELECT id FROM orders WHERE status NOT IN ('delivered','failed','rejected','refunded')
                     ))
                     AND (order_id IS NULL OR order_id NOT IN (
                         SELECT order_id FROM account_locks WHERE state='held' AND order_id IS NOT NULL
                     ))""",
                (cutoff,),
            )
            pruned_events = int(cur.rowcount or 0)
            self._conn.commit()
            checkpoint = None
            if self.path != ":memory:":
                row = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if row is not None:
                    checkpoint = {"busy": int(row[0]), "log": int(row[1]), "checkpointed": int(row[2])}
        self.purge_expired_order_vaults()
        stats = self.maintenance_stats()
        return {
            "retention_days": days,
            "pruned_events": pruned_events,
            "wal_checkpoint": checkpoint,
            **stats,
        }

    def order_summary_stats(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                """SELECT
                       COUNT(*) AS total,
                       SUM(CASE WHEN status IN ('active', 'delivered') THEN 1 ELSE 0 END) AS delivered,
                       SUM(CASE WHEN payment_status = 'succeeded' AND delivery_status != 'delivered' THEN 1 ELSE 0 END) AS paid_pending_delivery,
                       SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END) AS unknown,
                       SUM(CASE WHEN status IN ('failed', 'rejected') THEN 1 ELSE 0 END) AS failed
                   FROM orders"""
            ).fetchone()
            active_locks = int(
                self._conn.execute("SELECT COUNT(*) FROM account_locks WHERE state = 'held'").fetchone()[0]
            )
        return {
            "total": int(row["total"] or 0),
            "delivered": int(row["delivered"] or 0),
            "paid_pending_delivery": int(row["paid_pending_delivery"] or 0),
            "unknown": int(row["unknown"] or 0),
            "failed": int(row["failed"] or 0),
            "active_locks": active_locks,
        }

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

    def resolve_lock_paid(self, lock_id: str, *, resolved_by: str, note: str) -> bool:
        """Mark a held lock as resolved after authoritative confirmation of paid delivery."""
        with self._lock:
            cur = self._conn.execute(
                """UPDATE account_locks SET state='resolved', released_at=?,
                   released_by=?, release_note=? WHERE id=? AND state='held'""",
                (_utcnow(), resolved_by, note, lock_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def list_locks(
        self,
        *,
        state: str = "held",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        capped = max(1, min(int(limit), 100))
        skip = max(0, int(offset))
        state_norm = (state or "held").strip().lower()
        where_sql = ""
        params: list[Any] = []
        if state_norm and state_norm != "all":
            where_sql = "WHERE l.state = ?"
            params.append(state_norm)
        with self._lock:
            counts_rows = self._conn.execute(
                "SELECT state, COUNT(*) AS cnt FROM account_locks GROUP BY state"
            ).fetchall()
            counts = {"held": 0, "released": 0, "resolved": 0}
            for crow in counts_rows:
                counts[str(crow["state"])] = int(crow["cnt"] or 0)
            total = int(
                self._conn.execute(f"SELECT COUNT(*) FROM account_locks l {where_sql}", params).fetchone()[0]
            )
            rows = self._conn.execute(
                f"""SELECT l.*,
                           o.plan AS order_plan,
                           o.status AS order_status,
                           o.payment_status AS order_payment_status,
                           o.delivery_status AS order_delivery_status,
                           o.amount AS order_amount,
                           o.currency AS order_currency,
                           o.billing_country AS order_billing_country
                    FROM account_locks l
                    LEFT JOIN orders o ON o.id = l.order_id
                    {where_sql}
                    ORDER BY CASE l.state WHEN 'held' THEN 0 ELSE 1 END ASC, l.created_at DESC
                    LIMIT ? OFFSET ?""",
                [*params, capped, skip],
            ).fetchall()
        return [dict(row) for row in rows], total, counts

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

    # -- portal users & scoped customer tasks ------------------------------- #
    def create_portal_user(self, *, user_id: str, username: str, password_hash: str) -> Optional[dict[str, Any]]:
        now = _utcnow()
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO portal_users(id, username, password_hash, created_at, last_login_at)
                       VALUES(?,?,?,?,?)""",
                    (user_id, username, password_hash, now, now),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return None
        return {"id": user_id, "username": username, "created_at": now, "last_login_at": now}

    def get_portal_user_by_username(self, username: str) -> Optional[dict[str, Any]]:
        if not username:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM portal_users WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_portal_user_by_id(self, user_id: str) -> Optional[dict[str, Any]]:
        if not user_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, created_at, last_login_at FROM portal_users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def touch_portal_user_login(self, user_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE portal_users SET last_login_at = ? WHERE id = ?",
                (_utcnow(), user_id),
            )

    def bind_portal_task(
        self,
        *,
        job_id: str,
        owner_user_id: Optional[str],
        guest_token_hash: Optional[str],
        guest_expires_at: Optional[str],
        masked_email: str,
        plan: str,
        exit_country: str,
    ) -> None:
        now = _utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO portal_tasks(
                    job_id, owner_user_id, guest_token_hash, guest_expires_at,
                    masked_email, plan, exit_country, created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    owner_user_id = COALESCE(portal_tasks.owner_user_id, excluded.owner_user_id),
                    guest_token_hash = COALESCE(portal_tasks.guest_token_hash, excluded.guest_token_hash),
                    guest_expires_at = COALESCE(portal_tasks.guest_expires_at, excluded.guest_expires_at),
                    masked_email = CASE WHEN COALESCE(portal_tasks.masked_email,'')='' THEN excluded.masked_email ELSE portal_tasks.masked_email END""",
                (
                    job_id,
                    owner_user_id or None,
                    guest_token_hash or None,
                    guest_expires_at or None,
                    masked_email or None,
                    plan,
                    exit_country or None,
                    now,
                ),
            )

    def claim_guest_tasks(self, *, guest_token_hash: str, user_id: str) -> int:
        if not guest_token_hash or not user_id:
            return 0
        now = _utcnow()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE portal_tasks
                   SET owner_user_id = ?
                   WHERE guest_token_hash = ?
                     AND owner_user_id IS NULL
                     AND (guest_expires_at IS NULL OR guest_expires_at >= ?)""",
                (user_id, guest_token_hash, now),
            )
            return int(cur.rowcount or 0)

    def get_portal_task_ownership(
        self,
        job_id: str,
        *,
        owner_user_id: Optional[str],
        guest_token_hash: Optional[str],
    ) -> Optional[dict[str, Any]]:
        if not job_id or (not owner_user_id and not guest_token_hash):
            return None
        now = _utcnow()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM portal_tasks WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        rec = dict(row)
        if owner_user_id and rec.get("owner_user_id") == owner_user_id:
            return rec
        if (
            guest_token_hash
            and rec.get("guest_token_hash") == guest_token_hash
            and (not rec.get("guest_expires_at") or str(rec["guest_expires_at"]) >= now)
        ):
            return rec
        return None

    def list_portal_tasks(
        self,
        *,
        owner_user_id: Optional[str],
        guest_token_hash: Optional[str],
        search: str = "",
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        if not owner_user_id and not guest_token_hash:
            return [], 0
        capped = max(1, min(int(limit), 50))
        skip = max(0, int(offset))
        now = _utcnow()
        owner_clauses: list[str] = []
        params: list[Any] = []
        if owner_user_id:
            owner_clauses.append("t.owner_user_id = ?")
            params.append(owner_user_id)
        if guest_token_hash:
            owner_clauses.append("(t.guest_token_hash = ? AND (t.guest_expires_at IS NULL OR t.guest_expires_at >= ?))")
            params.extend([guest_token_hash, now])
        clauses = ["(" + " OR ".join(owner_clauses) + ")"]
        q = (search or "").strip()
        if q:
            like = f"%{q}%"
            clauses.append(
                "(COALESCE(t.masked_email,'') LIKE ? OR t.plan LIKE ? OR t.job_id LIKE ? OR COALESCE(j.order_id,'') LIKE ?)"
            )
            params.extend([like, like, like, like])
        where_sql = "WHERE " + " AND ".join(clauses)
        with self._lock:
            total = int(
                self._conn.execute(
                    f"""SELECT COUNT(*)
                        FROM portal_tasks t
                        LEFT JOIN quick_pay_jobs j ON j.id = t.job_id
                        {where_sql}""",
                    params,
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                f"""SELECT t.job_id, t.masked_email, t.plan, t.exit_country, t.created_at,
                           j.status, j.stage, j.order_id, j.amount, j.currency,
                           j.failure_code, j.message, j.finished_at, j.updated_at,
                           o.auto_renew_status, o.auto_renew_canceled_at, o.auto_renew_error,
                           o.payment_status, o.delivery_status
                    FROM portal_tasks t
                    LEFT JOIN quick_pay_jobs j ON j.id = t.job_id
                    LEFT JOIN orders o ON o.id = j.order_id
                    {where_sql}
                    ORDER BY t.created_at DESC
                    LIMIT ? OFFSET ?""",
                [*params, capped, skip],
            ).fetchall()
        return [dict(row) for row in rows], total

    # -- order sensitive vault (30-day encrypted after-sales retention) ------ #
    def upsert_order_vault(
        self,
        *,
        order_id: str,
        session_ciphertext: Optional[bytes] = None,
        card_ciphertext: Optional[bytes] = None,
        card_brand: Optional[str] = None,
        card_bin: Optional[str] = None,
        card_last4: Optional[str] = None,
        card_exp: Optional[str] = None,
        card_fingerprint: Optional[str] = None,
        expires_at: str,
        key_version: int = 1,
    ) -> None:
        now = _utcnow()
        has_session = 1 if session_ciphertext is not None else 0
        has_card = 1 if card_ciphertext is not None else 0
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO order_sensitive_vault(
                    order_id, session_ciphertext, card_ciphertext,
                    has_session, has_card,
                    card_brand, card_bin, card_last4, card_exp, card_fingerprint,
                    key_version, created_at, updated_at, expires_at, purged_at, purge_reason
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)
                ON CONFLICT(order_id) DO UPDATE SET
                    session_ciphertext = COALESCE(excluded.session_ciphertext, order_sensitive_vault.session_ciphertext),
                    card_ciphertext = COALESCE(excluded.card_ciphertext, order_sensitive_vault.card_ciphertext),
                    has_session = CASE WHEN excluded.session_ciphertext IS NOT NULL THEN 1 ELSE order_sensitive_vault.has_session END,
                    has_card = CASE WHEN excluded.card_ciphertext IS NOT NULL THEN 1 ELSE order_sensitive_vault.has_card END,
                    card_brand = COALESCE(excluded.card_brand, order_sensitive_vault.card_brand),
                    card_bin = COALESCE(excluded.card_bin, order_sensitive_vault.card_bin),
                    card_last4 = COALESCE(excluded.card_last4, order_sensitive_vault.card_last4),
                    card_exp = COALESCE(excluded.card_exp, order_sensitive_vault.card_exp),
                    card_fingerprint = COALESCE(excluded.card_fingerprint, order_sensitive_vault.card_fingerprint),
                    key_version = excluded.key_version,
                    updated_at = excluded.updated_at,
                    expires_at = COALESCE(order_sensitive_vault.expires_at, excluded.expires_at),
                    purged_at = NULL,
                    purge_reason = NULL""",
                (
                    order_id,
                    session_ciphertext,
                    card_ciphertext,
                    has_session,
                    has_card,
                    card_brand,
                    card_bin,
                    card_last4,
                    card_exp,
                    card_fingerprint,
                    key_version,
                    now,
                    now,
                    expires_at,
                ),
            )

    def get_order_vault_row(self, order_id: str) -> Optional[dict[str, Any]]:
        if not order_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM order_sensitive_vault WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def purge_order_vault(self, order_id: str, *, reason: str = "manual_purge") -> bool:
        if not order_id:
            return False
        now = _utcnow()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE order_sensitive_vault
                   SET session_ciphertext = NULL,
                       card_ciphertext = NULL,
                       has_session = 0,
                       has_card = 0,
                       updated_at = ?,
                       purged_at = COALESCE(purged_at, ?),
                       purge_reason = COALESCE(purge_reason, ?)
                   WHERE order_id = ? AND (session_ciphertext IS NOT NULL OR card_ciphertext IS NOT NULL OR purged_at IS NULL)""",
                (now, now, reason, order_id),
            )
            return int(cur.rowcount or 0) > 0

    def purge_expired_order_vaults(self, *, now_iso: Optional[str] = None) -> int:
        cutoff = now_iso or _utcnow()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE order_sensitive_vault
                   SET session_ciphertext = NULL,
                       card_ciphertext = NULL,
                       has_session = 0,
                       has_card = 0,
                       updated_at = ?,
                       purged_at = ?,
                       purge_reason = 'ttl_expired'
                   WHERE purged_at IS NULL AND expires_at <= ?""",
                (cutoff, cutoff, cutoff),
            )
            return int(cur.rowcount or 0)

    # -- developer API keys & scoped API tasks ------------------------------ #
    def create_api_key(
        self,
        *,
        key_id: str,
        name: str,
        key_prefix: str,
        key_hash: str,
        rate_limit_rpm: int = 30,
        allowed_plans: Optional[list[str]] = None,
        note: str = "",
    ) -> dict[str, Any]:
        now = _utcnow()
        plans_json = json.dumps(allowed_plans or ["go", "plus", "pro_lite", "pro", "pro_max"])
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO api_keys(
                    id, name, key_prefix, key_hash, status, rate_limit_rpm,
                    allowed_plans_json, note, total_requests, last_used_at,
                    created_at, updated_at, revoked_at
                ) VALUES(?,?,?,?,'active',?,?,?,0,NULL,?,?,NULL)""",
                (key_id, name, key_prefix, key_hash, int(rate_limit_rpm), plans_json, note or None, now, now),
            )
        return self.get_api_key_by_id(key_id) or {}

    def get_api_key_by_hash(self, key_hash: str) -> Optional[dict[str, Any]]:
        if not key_hash:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ?",
                (key_hash,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_api_key_by_id(self, key_id: str) -> Optional[dict[str, Any]]:
        if not key_id:
            return None
        with self._lock:
            row = self._conn.execute(
                """SELECT k.*,
                          COUNT(at.job_id) AS total_orders,
                          SUM(CASE WHEN COALESCE(o.status, j.status) IN ('active', 'delivered', 'succeeded') THEN 1 ELSE 0 END) AS succeeded_orders,
                          SUM(CASE WHEN COALESCE(o.status, j.status) IN ('failed', 'rejected') THEN 1 ELSE 0 END) AS failed_orders,
                          SUM(CASE WHEN COALESCE(o.status, j.status) NOT IN ('active', 'delivered', 'succeeded', 'failed', 'rejected', 'refunded') THEN 1 ELSE 0 END) AS pending_orders
                   FROM api_keys k
                   LEFT JOIN api_tasks at ON at.api_key_id = k.id
                   LEFT JOIN quick_pay_jobs j ON j.id = at.job_id
                   LEFT JOIN orders o ON o.id = j.order_id
                   WHERE k.id = ?
                   GROUP BY k.id""",
                (key_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_api_keys(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT k.*,
                          COUNT(at.job_id) AS total_orders,
                          SUM(CASE WHEN COALESCE(o.status, j.status) IN ('active', 'delivered', 'succeeded') THEN 1 ELSE 0 END) AS succeeded_orders,
                          SUM(CASE WHEN COALESCE(o.status, j.status) IN ('failed', 'rejected') THEN 1 ELSE 0 END) AS failed_orders,
                          SUM(CASE WHEN COALESCE(o.status, j.status) NOT IN ('active', 'delivered', 'succeeded', 'failed', 'rejected', 'refunded') THEN 1 ELSE 0 END) AS pending_orders
                   FROM api_keys k
                   LEFT JOIN api_tasks at ON at.api_key_id = k.id
                   LEFT JOIN quick_pay_jobs j ON j.id = at.job_id
                   LEFT JOIN orders o ON o.id = j.order_id
                   GROUP BY k.id
                   ORDER BY CASE k.status WHEN 'active' THEN 0 WHEN 'disabled' THEN 1 ELSE 2 END ASC,
                            k.created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def update_api_key(
        self,
        key_id: str,
        *,
        name: Optional[str] = None,
        status: Optional[str] = None,
        rate_limit_rpm: Optional[int] = None,
        allowed_plans: Optional[list[str]] = None,
        note: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        existing = self.get_api_key_by_id(key_id)
        if existing is None:
            return None
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [_utcnow()]
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
            if status == "revoked":
                sets.append("revoked_at = COALESCE(revoked_at, ?)")
                params.append(_utcnow())
        if rate_limit_rpm is not None:
            sets.append("rate_limit_rpm = ?")
            params.append(int(rate_limit_rpm))
        if allowed_plans is not None:
            sets.append("allowed_plans_json = ?")
            params.append(json.dumps(allowed_plans))
        if note is not None:
            sets.append("note = ?")
            params.append(note or None)
        params.append(key_id)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE api_keys SET {', '.join(sets)} WHERE id = ?", params)
        return self.get_api_key_by_id(key_id)

    def rotate_api_key(self, key_id: str, *, key_prefix: str, key_hash: str) -> Optional[dict[str, Any]]:
        now = _utcnow()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE api_keys
                   SET key_prefix = ?, key_hash = ?, status = 'active', revoked_at = NULL, updated_at = ?
                   WHERE id = ?""",
                (key_prefix, key_hash, now, key_id),
            )
            if int(cur.rowcount or 0) == 0:
                return None
        return self.get_api_key_by_id(key_id)

    def revoke_api_key(self, key_id: str) -> Optional[dict[str, Any]]:
        now = _utcnow()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE api_keys
                   SET status = 'revoked', revoked_at = COALESCE(revoked_at, ?), updated_at = ?
                   WHERE id = ?""",
                (now, now, key_id),
            )
            if int(cur.rowcount or 0) == 0:
                return None
        return self.get_api_key_by_id(key_id)

    def touch_api_key_usage(self, key_id: str) -> None:
        now = _utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE api_keys
                   SET total_requests = total_requests + 1,
                       last_used_at = ?
                   WHERE id = ?""",
                (now, key_id),
            )

    def bind_api_task(
        self,
        *,
        job_id: str,
        api_key_id: str,
        external_order_id: str = "",
        masked_email: str = "",
        plan: str,
        exit_country: str = "",
    ) -> None:
        now = _utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO api_tasks(
                    job_id, api_key_id, external_order_id, masked_email, plan, exit_country, created_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    external_order_id = COALESCE(NULLIF(api_tasks.external_order_id,''), excluded.external_order_id),
                    masked_email = COALESCE(NULLIF(api_tasks.masked_email,''), excluded.masked_email)""",
                (job_id, api_key_id, external_order_id or None, masked_email or None, plan, exit_country or None, now),
            )

    def get_api_task_ownership(self, job_id: str, *, api_key_id: str) -> Optional[dict[str, Any]]:
        if not job_id or not api_key_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_tasks WHERE job_id = ? AND api_key_id = ?",
                (job_id, api_key_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_api_tasks(
        self,
        *,
        api_key_id: str,
        status: str = "",
        external_order_id: str = "",
        search: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        if not api_key_id:
            return [], 0
        capped = max(1, min(int(limit), 100))
        skip = max(0, int(offset))
        clauses = ["t.api_key_id = ?"]
        params: list[Any] = [api_key_id]
        st = (status or "").strip().lower()
        if st and st != "all":
            clauses.append("COALESCE(j.status, 'queued') = ?")
            params.append(st)
        ext = (external_order_id or "").strip()
        if ext:
            clauses.append("t.external_order_id = ?")
            params.append(ext)
        q = (search or "").strip()
        if q:
            like = f"%{q}%"
            clauses.append(
                "(COALESCE(t.external_order_id,'') LIKE ? OR COALESCE(t.masked_email,'') LIKE ? "
                "OR t.job_id LIKE ? OR COALESCE(j.order_id,'') LIKE ?)"
            )
            params.extend([like, like, like, like])
        where_sql = "WHERE " + " AND ".join(clauses)
        with self._lock:
            total = int(
                self._conn.execute(
                    f"""SELECT COUNT(*)
                        FROM api_tasks t
                        LEFT JOIN quick_pay_jobs j ON j.id = t.job_id
                        {where_sql}""",
                    params,
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                f"""SELECT t.job_id, t.api_key_id, t.external_order_id, t.masked_email,
                           t.plan, t.exit_country, t.created_at,
                           j.status, j.stage, j.order_id, j.amount, j.currency,
                           j.failure_code, j.message, j.finished_at, j.updated_at,
                           o.auto_renew_status, o.auto_renew_canceled_at, o.auto_renew_error,
                           o.payment_status, o.delivery_status
                    FROM api_tasks t
                    LEFT JOIN quick_pay_jobs j ON j.id = t.job_id
                    LEFT JOIN orders o ON o.id = j.order_id
                    {where_sql}
                    ORDER BY t.created_at DESC
                    LIMIT ? OFFSET ?""",
                [*params, capped, skip],
            ).fetchall()
        return [dict(row) for row in rows], total

    def clear_all(self) -> None:
        """Test/reset helper; never called by a live request path."""
        with self._lock:
            for table in ("api_tasks", "api_keys", "order_sensitive_vault",
                          "portal_tasks", "portal_users", "order_events", "account_locks",
                          "idempotency_keys", "quick_pay_jobs", "orders"):
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
