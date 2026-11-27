"""请求 / 响应 schema。"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationInfo, field_validator, model_serializer, model_validator


class AddressedRequest(BaseModel):
    """Preserve unknown-field presence, not the values ignored by Pydantic.

    Older Team clients retain their existing schema behavior. Non-Team writes
    explicitly reject unmodelled input (including line2/nested address aliases)
    rather than silently dropping part of the submitted billing address.
    """
    _unknown_fields: bool = PrivateAttr(default=False)

    @model_validator(mode="wrap")
    @classmethod
    def _inspect_keys(cls, value, handler):
        instance = handler(value)
        if isinstance(value, dict):
            instance._unknown_fields = any(key not in cls.model_fields for key in value)
        return instance

    def reject_unknown_fields(self) -> None:
        if self._unknown_fields:
            raise HTTPException(409, "账单请求包含无法核验的字段，未提交。")
        for name in type(self).model_fields:
            child = getattr(self, name)
            if isinstance(child, AddressedRequest):
                child.reject_unknown_fields()


class ModeIn(AddressedRequest):
    account_source: str = Field(default="customer_session")
    card_source: str = Field(default="customer")
    billing_mode: str = Field(default="standard")
    risk_acknowledged: bool = False


class OrderCreateRequest(AddressedRequest):
    # Only checked for non-Team. Do not echo malformed optional claims in a
    # validation error; the policy validates the full address with redacted errors.
    billing: Optional[Any] = Field(default=None, repr=False)
    plan: str
    billing_country: Optional[str] = Field(default=None, validate_default=True)
    # 代理出口 / Session 重签发国家；默认等于 billing_country。
    exit_country: Optional[str] = None
    mode: ModeIn = Field(default_factory=ModeIn)
    session: Optional[str] = None
    payment_method_token: Optional[str] = None
    promo_code: str = ""
    promo_campaign_id: str = ""
    workspace_name: str = ""
    seat_quantity: Optional[int] = None
    price_interval: str = "month"
    auto_cancel_renew: bool = False

    @field_validator("billing_country")
    @classmethod
    def _team_requires_region(cls, value: Optional[str], info: ValidationInfo) -> Optional[str]:
        # Only non-Team may omit this field. Team retains its required country.
        if info.data.get("plan") == "team" and value is None:
            raise ValueError("Team 需要创建结账地区")
        return value


class BillingIn(AddressedRequest):
    email: str = Field(default="", max_length=254)
    name: str = Field(min_length=1, max_length=100)
    line1: str = Field(min_length=1, max_length=200)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(default="", max_length=100)
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(min_length=2, max_length=2)


class CardIn(AddressedRequest):
    """Server-side card submission. Never persisted or logged."""
    number: str = Field(min_length=12, max_length=23)
    cvc: str = Field(min_length=3, max_length=4)
    exp_month: str = Field(min_length=1, max_length=2)
    exp_year: str = Field(min_length=2, max_length=4)


class OrderPayRequest(AddressedRequest):
    payment_method_token: str = ""
    billing: Optional[BillingIn] = None
    team_context_id: Optional[str] = Field(default=None, max_length=80)
    passive_captcha_token: Optional[str] = Field(default=None, max_length=20000)
    rv_timestamp: Optional[str] = Field(default=None, max_length=5000)
    card: Optional[CardIn] = None


class TeamQuoteRequest(BaseModel):
    billing: Optional[BillingIn] = None
    browser_locale: str = Field(default="en-US", max_length=30)
    browser_timezone: str = Field(default="UTC", max_length=80)


class TeamPrepareRequest(BaseModel):
    billing: BillingIn
    browser_locale: str = Field(default="en-US", max_length=30)
    browser_timezone: str = Field(default="UTC", max_length=80)


class SubscriptionOut(BaseModel):
    active: bool
    expires_at: Optional[str] = None
    channel: Optional[str] = None


class OrderOut(BaseModel):
    id: str
    status: str
    purchase_attempt_id: Optional[str] = None
    route_profile: Optional[str] = None
    failure_code: Optional[str] = None
    plan: str
    billing_country: str
    mode: ModeIn
    checkout_url: Optional[str] = None
    publishable_key: Optional[str] = None
    customer_session_client_secret: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    billing: Optional[Dict[str, str]] = None
    billing_verified: bool = False
    last_error: Optional[str] = None
    quote_error: Optional[str] = None
    next_action: Optional[Dict[str, Any]] = None
    subscription: Optional[SubscriptionOut] = None
    checkout_kind: Optional[str] = None
    seat_quantity: Optional[int] = None
    workspace_name: Optional[str] = None
    promo_code: Optional[str] = None
    promo_campaign_id: Optional[str] = None
    price_interval: Optional[str] = None
    confirm_return_url: Optional[str] = None
    # 从 accessToken(JWT) 解析出的账户邮箱，用于前端预填账单邮箱
    session_email: Optional[str] = None
    # 跨区充值：已丢弃旧 accessToken，在目标国家出口重签发 session
    session_reissued_for_country: bool = False
    exit_country: Optional[str] = None
    payment_status: str = "unpaid"
    delivery_status: str = "pending"
    payment_intent_id: Optional[str] = None
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

    @model_serializer(mode="wrap")
    def _preserve_team_contract(self, serialize):
        result = serialize(self)
        if self.plan == "team":
            result.pop("billing_verified", None)
        return result


class ErrorOut(BaseModel):
    code: str
    message: str
    detail: Optional[Dict[str, Any]] = None


class QuickPayRequest(AddressedRequest):
    """Checkout-only forbids card data; combined checkout/payment requires it."""

    model_config = ConfigDict(
        hide_input_in_errors=True,
        json_schema_extra={
            "if": {"required": ["debug_checkout_only"], "properties": {"debug_checkout_only": {"const": True}}},
            "then": {"not": {"required": ["card"]}},
            "else": {"required": ["card"], "properties": {"card": {"type": "object"}}},
        },
    )

    plan: str
    billing_country: Optional[str] = None
    exit_country: Optional[str] = None
    session: str = Field(repr=False)
    card: Optional[CardIn] = Field(default=None, repr=False)
    billing: Optional[BillingIn] = None
    mode: ModeIn = Field(default_factory=ModeIn)
    promo_code: str = ""
    promo_campaign_id: str = ""
    workspace_name: str = ""
    seat_quantity: Optional[int] = None
    price_interval: str = "month"
    auto_cancel_renew: bool = True
    debug_checkout_only: bool = Field(default=False, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _card_boundary(cls, data):
        if isinstance(data, dict):
            if data.get("debug_checkout_only") is True:
                if "card" in data:  # Reject even null before parsing nested card fields.
                    raise ValueError("仅建单请求不可携带 card。")
            elif data.get("card") is None:
                raise ValueError("付款请求必须提供 card。")
        return data


class QuickPayEventOut(BaseModel):
    state: str
    stage: str
    message: str = ""
    created_at: str


class QuickPayJobOut(BaseModel):
    job_id: str
    checkout_outcome: Optional[Literal["rejected_before_payment"]] = None
    lock_id: Optional[str] = None
    timings_ms: Dict[str, int] = Field(default_factory=dict)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    next_action: Optional[Dict[str, Any]] = None
    events: list[QuickPayEventOut] = Field(default_factory=list)
    status: str
    stage: str
    queue_position: Optional[int] = None
    attempts: int = 0
    reconcile_attempts: int = 0
    last_reconciled_at: Optional[str] = None
    order_id: Optional[str] = None
    purchase_attempt_id: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    failure_code: Optional[str] = None
    message: Optional[str] = None
    auto_renew_status: str = "disabled"
    auto_renew_canceled_at: Optional[str] = None
    auto_renew_error: Optional[str] = None
    proxy_rotations: int = 0
    proxy_last_sid: Optional[str] = None
    proxy_last_exit_ip: Optional[str] = None


class PreflightRequest(BaseModel):
    """Read-only pre-flight check input. Never accepts or stores card data."""

    model_config = ConfigDict(hide_input_in_errors=True)

    plan: str
    billing_country: Optional[str] = None
    exit_country: Optional[str] = None
    session: str = Field(default="", repr=False)
    debug_checkout_only: bool = False
    check_proxy_connectivity: bool = False
    check_account_upstream: bool = False


class PreflightCheckOut(BaseModel):
    key: str
    label: str
    status: Literal["pass", "warn", "fail"]
    code: str
    message: str


class QueueStatsOut(BaseModel):
    queued: int = 0
    running: int = 0
    max_concurrency: int = 3
    draining: bool = False


class QueueJobSummaryOut(BaseModel):
    job_id: str
    status: str
    stage: str
    queue_position: Optional[int] = None
    attempts: int = 0
    reconcile_attempts: int = 0
    last_reconciled_at: Optional[str] = None
    order_id: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    failure_code: Optional[str] = None
    message: Optional[str] = None
    timings_ms: Dict[str, int] = Field(default_factory=dict)
    created_at: str
    finished_at: Optional[str] = None


class QueueStatusOut(BaseModel):
    stats: QueueStatsOut
    jobs: list[QueueJobSummaryOut] = Field(default_factory=list)


class PreflightOut(BaseModel):
    ok: bool
    blocking_code: Optional[str] = None
    summary: str
    checks: list[PreflightCheckOut] = Field(default_factory=list)
    queue: QueueStatsOut
    elapsed_ms: int = 0


class OrderLedgerStatsOut(BaseModel):
    total: int = 0
    delivered: int = 0
    paid_pending_delivery: int = 0
    unknown: int = 0
    failed: int = 0
    active_locks: int = 0


class OrderLedgerItemOut(BaseModel):
    id: str
    source: str = "step"
    account_email: Optional[str] = None
    masked_email: Optional[str] = None
    portal_username: Optional[str] = None
    account_tag: Optional[str] = None
    card_brand: Optional[str] = None
    plan: str
    billing_country: str
    exit_country: Optional[str] = None
    status: str
    payment_status: str = "unpaid"
    delivery_status: str = "pending"
    amount: Optional[str] = None
    currency: Optional[str] = None
    tax_delta: Optional[str] = None
    failure_code: Optional[str] = None
    last_error: Optional[str] = None
    purchase_attempt_id: Optional[str] = None
    payment_intent_id: Optional[str] = None
    checkout_session_id: Optional[str] = None
    checkout_kind: Optional[str] = None
    route_profile: Optional[str] = None
    billing_mode: Optional[str] = None
    billing_verified: bool = False
    session_reissued: bool = False
    auto_cancel_renew: bool = False
    auto_renew_status: str = "disabled"
    auto_renew_attempts: int = 0
    auto_renew_canceled_at: Optional[str] = None
    auto_renew_error: Optional[str] = None
    auto_renew_http_status: Optional[int] = None
    proxy_rotations: int = 0
    proxy_last_sid: Optional[str] = None
    proxy_last_exit_ip: Optional[str] = None
    job_id: Optional[str] = None
    job_status: Optional[str] = None
    job_stage: Optional[str] = None
    job_attempts: int = 0
    reconcile_attempts: int = 0
    last_reconciled_at: Optional[str] = None
    active_lock_id: Optional[str] = None
    total_ms: Optional[int] = None
    timings_ms: Dict[str, int] = Field(default_factory=dict)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    unknown_since: Optional[str] = None
    finished_at: Optional[str] = None


class OrderLedgerOut(BaseModel):
    stats: OrderLedgerStatsOut
    total: int = 0
    limit: int = 20
    offset: int = 0
    items: list[OrderLedgerItemOut] = Field(default_factory=list)


class OrderFailureExchangeOut(BaseModel):
    title: str
    method: Optional[str] = None
    url: Optional[str] = None
    http_status: Optional[int] = None
    request: Optional[Any] = None
    response: Optional[Any] = None
    captured: bool = True


class OrderFailureDetailOut(BaseModel):
    order_id: str
    title: str = "开通失败详情"
    summary: str
    error_code: Optional[str] = None
    stage: Optional[str] = None
    upstream_status: Optional[int] = None
    upstream_code: Optional[str] = None
    cf_ray: Optional[str] = None
    request_id: Optional[str] = None
    exchange: Optional[OrderFailureExchangeOut] = None
    events: list[QuickPayEventOut] = Field(default_factory=list)


class PaymentLockSummaryOut(BaseModel):
    id: str
    lock_key_prefix: str
    lock_type: str
    order_id: Optional[str] = None
    job_id: Optional[str] = None
    payment_intent_id: Optional[str] = None
    state: str
    reason: str
    created_at: str
    released_at: Optional[str] = None
    released_by: Optional[str] = None
    release_note: Optional[str] = None
    order_plan: Optional[str] = None
    order_status: Optional[str] = None
    order_payment_status: Optional[str] = None
    order_delivery_status: Optional[str] = None
    order_amount: Optional[str] = None
    order_currency: Optional[str] = None
    order_billing_country: Optional[str] = None


class PaymentLocksOut(BaseModel):
    counts: Dict[str, int] = Field(default_factory=dict)
    total: int = 0
    limit: int = 50
    offset: int = 0
    items: list[PaymentLockSummaryOut] = Field(default_factory=list)


class ReconcileResultOut(BaseModel):
    order_id: Optional[str] = None
    job_id: Optional[str] = None
    outcome: str
    changed: bool = False
    order_status: Optional[str] = None
    payment_status: Optional[str] = None
    delivery_status: Optional[str] = None
    job_status: Optional[str] = None
    lock_state: Optional[str] = None
    reconcile_attempts: int = 0
    message: str = ""


class ReconcilerStatusOut(BaseModel):
    enabled: bool
    running: bool
    interval_s: float
    max_attempts: int
    pending_candidates: int = 0
    last_run_at: Optional[str] = None
    total_runs: int = 0
    resolved_count: int = 0


class WebhookConfigIn(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    enabled: bool = False
    url: str = Field(default="", max_length=1000)
    secret: Optional[str] = Field(default=None, max_length=256, repr=False)
    clear_secret: bool = False
    events: list[str] = Field(default_factory=lambda: [
        "succeeded", "failed", "unknown", "requires_action", "paid_pending_delivery", "reconcile_resolved",
    ])


class WebhookConfigOut(BaseModel):
    enabled: bool = False
    url_masked: str = ""
    has_url: bool = False
    has_secret: bool = False
    events: list[str] = Field(default_factory=list)
    supported_events: list[str] = Field(default_factory=list)


class WebhookDeliveryOut(BaseModel):
    id: str
    event: str
    ok: bool
    status_code: Optional[int] = None
    elapsed_ms: int = 0
    attempts: int = 1
    error: Optional[str] = None
    created_at: str


class WebhookSettingsOut(BaseModel):
    config: WebhookConfigOut
    deliveries: list[WebhookDeliveryOut] = Field(default_factory=list)


class WebhookTestOut(BaseModel):
    ok: bool
    status_code: Optional[int] = None
    elapsed_ms: int = 0
    error: Optional[str] = None
    delivery: Optional[WebhookDeliveryOut] = None


class BatchPreflightItemIn(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    label: str = Field(default="", max_length=60)
    plan: str = "plus"
    billing_country: Optional[str] = None
    exit_country: Optional[str] = None
    session: str = Field(default="", repr=False)
    check_proxy_connectivity: bool = False
    check_account_upstream: bool = False


class BatchPreflightRequest(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    items: list[BatchPreflightItemIn] = Field(min_length=1, max_length=20)


class BatchPreflightItemOut(BaseModel):
    index: int
    label: str
    ok: bool
    blocking_code: Optional[str] = None
    summary: str
    checks: list[PreflightCheckOut] = Field(default_factory=list)
    elapsed_ms: int = 0


class BatchPreflightOut(BaseModel):
    total: int
    passed: int
    failed: int
    elapsed_ms: int = 0
    items: list[BatchPreflightItemOut] = Field(default_factory=list)


class SystemCheckOut(BaseModel):
    key: str
    label: str
    status: Literal["pass", "warn", "fail"]
    code: str
    message: str


class SystemDiagnosticsOut(BaseModel):
    ok: bool
    python_version: str
    node_version: Optional[str] = None
    db_bytes: int = 0
    wal_bytes: int = 0
    table_counts: Dict[str, int] = Field(default_factory=dict)
    cached_orders: int = 0
    disk_free_mb: int = 0
    checks: list[SystemCheckOut] = Field(default_factory=list)


class MaintenanceOut(BaseModel):
    retention_days: int
    pruned_events: int
    db_bytes: int
    wal_bytes: int
    wal_checkpoint: Optional[Dict[str, int]] = None
    table_counts: Dict[str, int] = Field(default_factory=dict)


class AccountSubscriptionRequest(BaseModel):
    """Session JSON is accepted transiently and never echoed back."""

    session: str = ""
    account_id: str = Field(default="", alias="accountId")
    force_refresh: bool = Field(default=False, alias="forceRefresh")
    include_invoices: bool = Field(default=True, alias="includeInvoices")
    model_config = ConfigDict(populate_by_name=True)


class AccountBillingHistoryRequest(BaseModel):
    session: str = ""
    account_id: str = Field(default="", alias="accountId")
    plan_name: str = Field(default="", alias="planName")
    currency: str = ""
    model_config = ConfigDict(populate_by_name=True)


class AccountInvoiceOut(BaseModel):
    plan: str
    paidAt: str
    status: str
    amount: str
    invoiceId: Optional[str] = None
    invoiceUrl: Optional[str] = None


class AccountBillingHistoryOut(BaseModel):
    invoices: list[AccountInvoiceOut] = Field(default_factory=list)
    billingSource: Optional[str] = None
    billingNote: Optional[str] = None
    currency: str = "-"
    retryable: bool = False


class AccountSubscriptionOut(BaseModel):
    email: str
    accountId: str = ""
    status: str
    planName: str
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    remainingDays: Optional[int] = None
    autoRenew: str
    paymentChannel: str
    currency: str
    invoices: list[AccountInvoiceOut] = Field(default_factory=list)
    billingSource: Optional[str] = None
    billingNote: Optional[str] = None
    queriedAt: str
    cached: bool = False
    stale: bool = False
    warning: Optional[str] = None


class StripePortalRequest(BaseModel):
    session: str = ""
    account_id: str = Field(default="", alias="accountId")
    model_config = ConfigDict(populate_by_name=True)


class StripePortalOut(BaseModel):
    url: str
    source: str


class RuntimeFlagOptionOut(BaseModel):
    value: str
    label: str


class RuntimeFlagItemOut(BaseModel):
    key: str
    category: str
    label: str
    description: str
    kind: str
    env_var: str
    value: Any
    default: Any
    env_value: Any = None
    source: str
    options: list[RuntimeFlagOptionOut] = Field(default_factory=list)
    min_value: Optional[int] = None
    max_value: Optional[int] = None


class RuntimeFlagsOut(BaseModel):
    items: list[RuntimeFlagItemOut] = Field(default_factory=list)
    override_count: int = 0
    updated_at: Optional[str] = None
    fake_mode: bool = False


class RuntimeFlagsUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updates: Dict[str, Any] = Field(default_factory=dict)


class RuntimeFlagsResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: Optional[list[str]] = None


class FixedBillingAddressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    email: str
    line1: str
    city: str
    state: str
    postal_code: str
    country: str = "US"


class FixedBillingAddressOut(BaseModel):
    name: str
    email: str
    line1: str
    city: str
    state: str
    postal_code: str
    country: str


class FixedBillingStateOptionOut(BaseModel):
    value: str
    label: str


class FixedBillingSettingsOut(BaseModel):
    configured: bool
    valid: bool
    source: str
    storage_label: str
    can_delete: bool = False
    error: Optional[str] = None
    address: Optional[FixedBillingAddressOut] = None
    verification_id: Optional[str] = None
    updated_at: Optional[str] = None
    strict_exit_match: bool = False
    tax_free_states: list[FixedBillingStateOptionOut] = Field(default_factory=list)
    default_sample: FixedBillingAddressOut
