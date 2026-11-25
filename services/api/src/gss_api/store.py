"""订单存储。session / 卡字段绝不进入这里。"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import os
from typing import Any, Dict, List, Optional

from gss_contracts.adapter import PaymentReceiptEvidence
from gss_contracts import Job, Order, OrderStatus, SubscriptionInfo

_SETTLED_STATUSES = {OrderStatus.DELIVERED, OrderStatus.FAILED, OrderStatus.REJECTED, OrderStatus.REFUNDED}


def _store_max_rehydrate() -> int:
    raw = os.environ.get("GSS_STORE_MAX_REHYDRATE", "500").strip()
    try:
        return max(10, min(10000, int(raw)))
    except ValueError:
        return 500


def _store_max_cached() -> int:
    raw = os.environ.get("GSS_STORE_MAX_CACHED", "1000").strip()
    try:
        return max(50, min(20000, int(raw)))
    except ValueError:
        return 1000


@dataclass
class OrderView:
    """API 展示用的附加字段（不含凭证）。"""

    # RAM-only keyed equality check; never part of the public order projection.
    create_request_tag: Optional[bytes] = field(default=None, repr=False)
    purchase_attempt_id: Optional[str] = None
    account_fingerprint: Optional[str] = field(default=None, repr=False)
    route_profile: str = "A"
    failure_code: Optional[str] = None
    checkout_url: Optional[str] = None
    checkout_session_id: Optional[str] = None
    publishable_key: Optional[str] = None
    customer_session_client_secret: Optional[str] = None
    billing: Optional[Dict[str, str]] = None
    billing_verified: bool = False
    billing_verification_id: Optional[str] = field(default=None, repr=False)
    amount: Optional[str] = None
    currency: Optional[str] = None
    last_error: Optional[str] = None
    quote_error: Optional[str] = None
    next_action: Optional[Dict[str, Any]] = None
    subscription: Optional[SubscriptionInfo] = None
    checkout_kind: Optional[str] = None
    seat_quantity: Optional[int] = None
    workspace_name: Optional[str] = None
    promo_code: Optional[str] = None
    promo_campaign_id: Optional[str] = None
    price_interval: Optional[str] = None
    confirm_return_url: Optional[str] = None
    protocol_saved_result: Optional[Dict[str, Any]] = None
    session_reissued_for_country: bool = False
    exit_country: Optional[str] = None
    # 支付状态与交付状态独立：支付成功后任何订阅查询失败都不能回退支付结果。
    payment_status: str = "unpaid"        # unpaid | processing | succeeded | failed
    delivery_status: str = "pending"     # pending | delivered | verify_delayed
    payment_intent_id: Optional[str] = None
    observed_payment_id: Optional[str] = None
    receipt_evidence: Optional[PaymentReceiptEvidence] = None
    paid_amount: Optional[str] = None
    paid_currency: Optional[str] = None
    tax_delta: Optional[str] = None
    plan_type: Optional[str] = None
    status_summary: Optional[str] = None
    receipt: Optional[Dict[str, Any]] = None
    auto_cancel_renew: bool = False
    auto_renew_status: str = "disabled"
    auto_renew_attempts: int = 0
    auto_renew_canceled_at: Optional[str] = None
    auto_renew_error: Optional[str] = None
    auto_renew_http_status: Optional[int] = None
    proxy_rotations: int = 0
    proxy_last_sid: Optional[str] = None
    proxy_last_exit_ip: Optional[str] = None
    unknown_since: Optional[str] = None
    finished_at: Optional[str] = None


class InMemoryStore:
    def __init__(self) -> None:
        self.orders: OrderedDict[str, Order] = OrderedDict()
        self.jobs: Dict[str, Job] = {}
        self.views: Dict[str, OrderView] = {}
        self._idempotency: Dict[str, str] = {}
        self.db = None
        self._db_loaded = False

    def _evict_if_needed(self) -> None:
        limit = _store_max_cached()
        if len(self.orders) <= limit:
            return
        for oid in list(self.orders.keys()):
            if len(self.orders) <= limit:
                break
            order = self.orders.get(oid)
            if order is not None and order.status in _SETTLED_STATUSES:
                self.orders.pop(oid, None)
                self.views.pop(oid, None)
                if order.idempotency_key:
                    self._idempotency.pop(order.idempotency_key, None)

    def _load_one_from_db(self, order_id: str) -> Optional[Order]:
        if self.db is None or not order_id:
            return None
        try:
            loaded = self.db.get_order_by_id(order_id)
        except Exception:
            return None
        if loaded is None:
            return None
        order, view, fingerprint = loaded
        self.orders[order.id] = order
        self.orders.move_to_end(order.id)
        self.views[order.id] = view
        if fingerprint:
            view.account_fingerprint = fingerprint
        if order.idempotency_key:
            self._idempotency[order.idempotency_key] = order.id
        self._evict_if_needed()
        return order

    def attach_db(self, db) -> None:
        """Attach SQLite persistence and rehydrate active + newest bounded orders once per process."""
        if self.db is db and self._db_loaded:
            return
        self.db = db
        for order, view, fingerprint in db.load_orders(limit=_store_max_rehydrate()):
            self.orders[order.id] = order
            self.views[order.id] = view
            if fingerprint:
                view.account_fingerprint = fingerprint
            if order.idempotency_key:
                self._idempotency[order.idempotency_key] = order.id
        for row in db.load_idempotency():
            if row.get("order_id") and row["order_id"] in self.orders:
                self._idempotency[row["key"]] = row["order_id"]
        self._db_loaded = True

    def persist(self, order_id: str) -> None:
        if self.db is None:
            return
        order = self.orders.get(order_id)
        if order is None:
            return
        view = self.views.setdefault(order_id, OrderView())
        try:
            self.db.save_order(order, view, account_fingerprint=view.account_fingerprint)
        except Exception:
            # Persistence failure must never crash a financial request.
            pass

    def forget_idempotency(self, key: Optional[str]) -> None:
        if key:
            self._idempotency.pop(key, None)

    def find_by_idempotency(self, key: Optional[str]) -> Optional[Order]:
        if not key:
            return None
        order_id = self._idempotency.get(key)
        if order_id and order_id in self.orders:
            self.orders.move_to_end(order_id)
            return self.orders[order_id]
        if self.db is not None:
            try:
                found_id = self.db.find_order_id_by_idempotency(key)
            except Exception:
                found_id = None
            if found_id:
                self._idempotency[key] = found_id
                return self.get_order(found_id)
        return None

    def create_order(self, order: Order, idempotency_key: Optional[str] = None) -> Order:
        self.orders[order.id] = order
        self.orders.move_to_end(order.id)
        self.views.setdefault(order.id, OrderView())
        if idempotency_key:
            self._idempotency[idempotency_key] = order.id
        self.persist(order.id)
        self._evict_if_needed()
        return order

    def get_order(self, order_id: str) -> Optional[Order]:
        order = self.orders.get(order_id)
        if order is not None:
            self.orders.move_to_end(order_id)
            return order
        return self._load_one_from_db(order_id)

    def view(self, order_id: str) -> OrderView:
        if order_id not in self.views and order_id not in self.orders:
            self._load_one_from_db(order_id)
        return self.views.setdefault(order_id, OrderView())

    def update_status(self, order_id: str, status: OrderStatus) -> Order:
        order = self.get_order(order_id)
        if order is None:
            raise KeyError(order_id)
        order.status = status
        if self.db is not None:
            try:
                self.db.save_idempotency(order.idempotency_key, "order_create",
                                         order_id=order.id, state=str(status))
            except Exception:
                pass
        self.persist(order_id)
        self._evict_if_needed()
        return order

    def add_job(self, job: Job) -> Job:
        self.jobs[job.id] = job
        return job

    def jobs_for_order(self, order_id: str) -> List[Job]:
        return [job for job in self.jobs.values() if job.order_id == order_id]

    def clear(self) -> None:
        self.orders.clear()
        self.jobs.clear()
        self.views.clear()
        self._idempotency.clear()
        if self.db is not None:
            try:
                self.db.clear_all()
            except Exception:
                pass


store = InMemoryStore()

__all__ = ["InMemoryStore", "OrderView", "store"]
