"""订阅校验。交付前必须确认订阅已生效。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from gss_contracts import (
    AdapterCapabilities,
    PromoCampaignInfo,
    PromoEligibility,
    SubscriptionInfo,
)
from gss_contracts.adapter import TEAM_PLAN_KINDS, current_verification_lease
from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ChatGPTClient
from gss_core.config import ORDER_HTTP_TIMEOUT_S
from gss_core.session_token import parse_session
from gss_core.proxies import configured_proxy



def _observed(record: dict, nested: dict, key: str, expected_type: type):
    """Missing and contradictory entitlement facts cannot qualify for delivery."""
    values = [part[key] for part in (record, nested) if key in part]
    if not values or any(type(v) is not expected_type or v != values[0] for v in values):
        return None
    return values[0] or None


class AccountAdapterBase:
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            source="self-contained chatgpt.com backend-api/subscriptions",
            license="MIT (this repository)",
            supports={
                "account_sources": ["customer_session"],
                "actions": ["subscription_status"],
            },
            failure_modes={
                "invalid_session": "session 无效 → 重新粘贴",
                "subscription_not_active": "订阅未生效 → 确认窗口内重试，不交付",
            },
        )


@dataclass
class UpstreamAccountAdapter(AccountAdapterBase):
    timeout: float = ORDER_HTTP_TIMEOUT_S

    async def subscription_status(self, session: str, *,
                                  credentials_current: Optional[Callable[[], bool]] = None) -> SubscriptionInfo:
        lease = credentials_current if credentials_current is not None else current_verification_lease()
        if lease is not None and not lease():
            raise DomainError(ErrorCode.SESSION_EXPIRED, "凭证已过期，停止权益核验")
        if not session or not parse_session(session):
            raise DomainError(ErrorCode.INVALID_SESSION, "缺少 session，无法查询订阅")
        client = ChatGPTClient.from_raw_session(session, proxy=configured_proxy("checkout"))
        client.timeout = float(self.timeout)
        client.credentials_current = lease
        try:
            payload = client.subscription_payload()
        finally:
            client.close()
        plan_type = str(
            payload.get("plan_type")
            or (payload.get("subscription") or {}).get("plan")
            or (payload.get("plan") or {}).get("type")
            or ""
        ).lower()
        status = str(
            (payload.get("subscription") or {}).get("status")
            or payload.get("status")
            or ""
        ).lower()
        expires_at = None
        expires_raw = (payload.get("subscription") or {}).get("expires_at")
        if expires_raw is None:
            expires_raw = payload.get("expires_at")
        if isinstance(expires_raw, str) and expires_raw:
            try:
                expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            except ValueError:
                expires_at = None
        sub = payload.get("subscription") or {}
        # Product labels and paid receipts are not entitlement activation. Reject an ambiguous
        # or expired validity window for every plan, even when another record says "active".
        expiries = [v for v in (payload.get("expires_at"), sub.get("expires_at")) if v is not None]
        current = not expiries or (
            all(v == expires_raw for v in expiries) and expires_at is not None
            and expires_at.tzinfo is not None and expires_at > datetime.now(timezone.utc)
        )
        states = (payload.get("status"), sub.get("status"))
        is_active = (status in {"active", "trialing"} and current
                     and all(str(v).lower() in {"active", "trialing"} for v in states if v))
        aliases = (TEAM_PLAN_KINDS, {"plus", "chatgptplusplan"}, {"go", "chatgptgoplan"},
                   {"pro", "chatgptpro", "chatgptproplan"}, {"pro_lite", "chatgptprolite"},
                   {"pro_max", "chatgptpromax"})
        kinds = next((group for group in aliases if plan_type in group), {plan_type})
        plans = (payload.get("plan_type"), sub.get("plan"), (payload.get("plan") or {}).get("type"))
        is_active = is_active and all(str(p).lower() in kinds for p in plans if p)
        return SubscriptionInfo(
            active=is_active,
            expires_at=expires_at,
            channel="card" if is_active else None,
            raw_kind=plan_type or status or "unknown",
            workspace_id=_observed(payload, sub, "workspace_id", str),
            workspace_name=_observed(payload, sub, "workspace_name", str),
            seat_quantity=_observed(payload, sub, "seat_quantity", int),
        )

    async def promo_eligibility(self, session: str) -> PromoEligibility:
        if not session or not parse_session(session):
            raise DomainError(ErrorCode.INVALID_SESSION, "缺少 session，无法查询优惠资格")
        client = ChatGPTClient.from_raw_session(session, proxy=configured_proxy("checkout"))
        client.timeout = float(self.timeout)
        try:
            result = client.promo_eligibility()
        finally:
            client.close()
        campaigns = [
            PromoCampaignInfo(
                key=str(item.get("key") or ""),
                campaign_id=str(item.get("id") or ""),
                discount_percentage=item.get("discount_percentage"),
                duration_num_periods=item.get("duration_num_periods"),
                duration_period=item.get("duration_period"),
                title=item.get("title"),
                summary=item.get("summary"),
                promotion_type=item.get("promotion_type"),
            )
            for item in result.get("campaigns", [])
            if isinstance(item, dict) and item.get("id")
        ]
        return PromoEligibility(
            account_id=result.get("account_id") or None,
            current_plan_type=result.get("current_plan_type"),
            subscription_plan=result.get("subscription_plan"),
            is_free=bool(result.get("is_free")),
            plus_eligible=bool(result.get("plus_eligible")),
            campaigns=campaigns,
        )

    async def register_account(self) -> Dict[str, Any]:
        raise NotImplementedError("本工具只开通客户自己的账号，不代注册")


@dataclass
class FakeAccountAdapter(AccountAdapterBase):
    active: bool = True
    sessions_seen: Optional[list] = None

    def __post_init__(self) -> None:
        if self.sessions_seen is None:
            self.sessions_seen = []

    async def subscription_status(self, session: str) -> SubscriptionInfo:
        assert self.sessions_seen is not None
        self.sessions_seen.append(session)
        return SubscriptionInfo(
            active=self.active,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=30) if self.active else None),
            channel="web" if self.active else None,
            raw_kind="plus" if self.active else None,
        )

    async def promo_eligibility(self, session: str) -> PromoEligibility:
        return PromoEligibility(
            account_id="fake-account",
            current_plan_type="free",
            subscription_plan="chatgptfreeplan",
            is_free=True,
            plus_eligible=True,
            campaigns=[
                PromoCampaignInfo(
                    key="plus",
                    campaign_id="plus-1-month-free",
                    discount_percentage=100,
                    duration_num_periods=1,
                    duration_period="month",
                    title="Try Plus free for 1 month",
                    promotion_type="1-month free trial",
                )
            ],
        )

    async def register_account(self) -> Dict[str, Any]:
        return {"session": "fake-session", "email": "fake@example.invalid"}


__all__ = ["AccountAdapterBase", "UpstreamAccountAdapter", "FakeAccountAdapter"]
