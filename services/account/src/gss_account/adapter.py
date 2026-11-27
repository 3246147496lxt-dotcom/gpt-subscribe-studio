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
from gss_core.proxies import configured_proxy, cooling_sids, fresh_proxy_sid, rewrite_proxy_exit


def _is_proxy_transport_error(exc: Exception) -> bool:
    if isinstance(exc, DomainError):
        return False
    name = type(exc).__name__
    if name in {
        "SSLError", "ProxyError", "ConnectionError", "ConnectError",
        "Timeout", "TimeoutError", "ReadTimeout", "ConnectTimeout",
        "RemoteProtocolError", "CurlError", "RequestException", "HTTPError",
    }:
        return True
    msg = str(exc)
    return any(
        tok in msg
        for tok in (
            "SSL", "SSL_ERROR_SYSCALL", "Connection closed", "timed out",
            "reset by peer", "EOF", "proxy", "Proxy", "curl: (35)", "curl: (56)", "curl: (28)",
        )
    )



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
        base_proxy = configured_proxy("checkout")
        candidate_proxies: list[Optional[str]] = [base_proxy]
        if base_proxy:
            for _ in range(2):
                try:
                    rotated = rewrite_proxy_exit(base_proxy, sid=fresh_proxy_sid(exclude=cooling_sids()))
                except Exception:  # noqa: BLE001
                    break
                if rotated and rotated not in candidate_proxies:
                    candidate_proxies.append(rotated)

        payload: Dict[str, Any] = {}
        last_transport_exc: Optional[Exception] = None
        for idx, proxy_url in enumerate(candidate_proxies):
            if lease is not None and not lease():
                raise DomainError(ErrorCode.SESSION_EXPIRED, "凭证已过期，停止权益核验")
            client = ChatGPTClient.from_raw_session(session, proxy=proxy_url)
            client.timeout = float(self.timeout)
            client.credentials_current = lease
            try:
                try:
                    payload = client.subscription_payload()
                except DomainError as exc:
                    if exc.code is ErrorCode.INVALID_SESSION and getattr(client, "session_token", ""):
                        refresh = getattr(client, "refresh_session", None)
                        refreshed = False
                        if callable(refresh):
                            try:
                                refreshed = bool(refresh(
                                    trust_statsig=False, reject_unauthorized=False, force_refresh=True,
                                ))
                            except TypeError:
                                refreshed = bool(refresh())
                        if refreshed:
                            if lease is not None and not lease():
                                raise DomainError(ErrorCode.SESSION_EXPIRED, "凭证已过期，停止权益核验")
                            payload = client.subscription_payload()
                        else:
                            raise
                    else:
                        raise
                last_transport_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                if _is_proxy_transport_error(exc) and idx + 1 < len(candidate_proxies):
                    last_transport_exc = exc
                    continue
                raise
            finally:
                client.close()
        if last_transport_exc is not None:
            raise last_transport_exc
        entitlement = payload.get("entitlement") if isinstance(payload.get("entitlement"), dict) else {}
        sub = payload.get("subscription") if isinstance(payload.get("subscription"), dict) else {}
        plan_type = str(
            payload.get("plan_type")
            or entitlement.get("subscription_plan")
            or sub.get("plan")
            or (payload.get("plan") or {}).get("type")
            or ""
        ).lower()
        status = str(
            sub.get("status")
            or payload.get("status")
            or ""
        ).lower()
        expires_at = None
        expires_raw = sub.get("expires_at")
        if expires_raw is None:
            expires_raw = payload.get("expires_at")
        if expires_raw is None:
            expires_raw = entitlement.get("expires_at")
        if expires_raw is None:
            expires_raw = payload.get("active_until")
        if isinstance(expires_raw, str) and expires_raw:
            try:
                expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            except ValueError:
                expires_at = None
        # Product labels and paid receipts are not entitlement activation. Reject an ambiguous
        # or expired validity window for every plan, even when another record says "active".
        expiries = [
            v for v in (payload.get("expires_at"), sub.get("expires_at"), entitlement.get("expires_at"))
            if v is not None
        ]
        current = not expiries or (
            all(v == expires_raw for v in expiries) and expires_at is not None
            and expires_at.tzinfo is not None and expires_at > datetime.now(timezone.utc)
        )
        if not expiries and isinstance(payload.get("active_until"), str) and payload.get("active_until"):
            current = (
                expires_at is not None
                and expires_at.tzinfo is not None
                and expires_at > datetime.now(timezone.utc)
            )
        states = [str(v).lower() for v in (payload.get("status"), sub.get("status")) if v]
        active_flags = [
            v for v in (payload.get("is_active"), entitlement.get("has_active_subscription"))
            if v is not None
        ]
        delinquent = any(
            rec.get("is_delinquent") is True for rec in (payload, entitlement, sub)
        )
        if states:
            status_ok = status in {"active", "trialing"} and all(v in {"active", "trialing"} for v in states)
        else:
            status_ok = bool(active_flags) and all(v is True for v in active_flags)
        if any(v is False for v in active_flags) or delinquent:
            status_ok = False
        is_active = status_ok and current
        aliases = (
            TEAM_PLAN_KINDS,
            {"plus", "chatgptplusplan"},
            {"go", "chatgptgoplan"},
            {"pro", "chatgptpro", "chatgptproplan"},
            {"pro_lite", "prolite", "chatgptprolite", "chatgptproliteplan"},
            {"pro_max", "promax", "chatgptpromax", "chatgptpromaxplan"},
        )
        kinds = next((group for group in aliases if plan_type in group), {plan_type})
        plans = (
            payload.get("plan_type"),
            entitlement.get("subscription_plan"),
            sub.get("plan"),
            (payload.get("plan") or {}).get("type"),
        )
        is_active = is_active and bool(plan_type) and all(str(p).lower() in kinds for p in plans if p)
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
