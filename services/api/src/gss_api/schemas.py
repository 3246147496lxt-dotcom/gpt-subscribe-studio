"""请求 / 响应 schema。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, PrivateAttr, ValidationInfo, field_validator, model_serializer, model_validator


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
    """One-call quick-pay input. The card is request-memory only."""

    plan: str
    billing_country: Optional[str] = None
    exit_country: Optional[str] = None
    session: str = Field(repr=False)
    card: CardIn = Field(repr=False)
    billing: BillingIn
    mode: ModeIn = Field(default_factory=ModeIn)
    promo_code: str = ""
    promo_campaign_id: str = ""
    workspace_name: str = ""
    seat_quantity: Optional[int] = None
    price_interval: str = "month"
    debug_checkout_only: bool = False


class QuickPayJobOut(BaseModel):
    job_id: str
    status: str
    stage: str
    order_id: Optional[str] = None
    purchase_attempt_id: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    failure_code: Optional[str] = None
    message: Optional[str] = None
