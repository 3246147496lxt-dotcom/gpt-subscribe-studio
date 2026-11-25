"""结账能力。独立实现，直接请求 ChatGPT，不依赖其它本地项目。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import List, Optional

from gss_contracts import (
    AdapterCapabilities,
    BillingMode,
    CheckoutRequest,
    CheckoutResult,
    Plan,
)
from gss_contracts.errors import DomainError, ErrorCode
from gss_core.billing import address_for
from gss_core.verified_payer import checkout_address
from gss_core.chatgpt import ChatGPTClient, clamp_team_seats
from gss_core.config import ORDER_HTTP_TIMEOUT_S
from gss_core.session_token import parse_session
from gss_core.countries import country_codes, currency_for
from gss_core.proxies import (
    align_proxy_region,
    configured_proxy,
    rotate_proxy_sid,
    session_proxy,
)

logger = logging.getLogger("gss.checkout")
_STRIPE_PROXY_CHECK_URL = "https://api.stripe.com/v1/payment_methods"
_STRIPE_PROXY_CHECK_TIMEOUT = ORDER_HTTP_TIMEOUT_S


def _retryable_sentinel_transport(exc: DomainError) -> bool:
    """A dead proxy SID returns CONNECT 404/407 before any checkout is created."""
    if exc.code is not ErrorCode.CHECKOUT_UPSTREAM_ERROR:
        return False
    text = str(exc.message or "").lower()
    if "sentinel" not in text and "风控令牌" not in text:
        return False
    return any(marker in text for marker in (
        "connect tunnel failed", "curl: (7)", "curl: (35)", "curl: (56)",
        "proxy fetch:", "timed out", "proxyerror", "ssl_error_syscall",
        "ssl_connect", "boringssl",
    ))


def _probe_stripe_proxy(proxy: Optional[str], *, timeout: float = _STRIPE_PROXY_CHECK_TIMEOUT) -> bool:
    """通过选定代理访问 Stripe，接受鉴权失败而拒绝代理错误。"""
    try:
        from gss_core.chatgpt import _new_session

        http = _new_session(proxy=proxy, timeout=timeout)
        try:
            response = http.get(_STRIPE_PROXY_CHECK_URL, timeout=timeout)
        finally:
            close = getattr(http, "close", None)
            if callable(close):
                close()
        status = int(getattr(response, "status_code", 0) or 0)
        return 200 <= status < 300 or status in {401, 404}
    except Exception as exc:
        logger.warning("Team Stripe 代理预检失败 type=%s", type(exc).__name__)
        return False


_LIST_PRICE = {
    Plan.PLUS: Decimal("20.00"),
    Plan.GO: Decimal("8.00"),
    Plan.PRO_LITE: Decimal("100.00"),
    Plan.PRO: Decimal("200.00"),
    Plan.PRO_MAX: Decimal("500.00"),
    Plan.TEAM: Decimal("125.00"),
}


class CheckoutAdapterBase:
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            source="self-contained chatgpt.com payments/checkout",
            license="MIT (this repository)",
            supports={
                "plans": [p.value for p in Plan],
                "billing_modes": [m.value for m in BillingMode],
                "countries": country_codes(),
            },
            failure_modes={
                "session_expired": "401 → 重新粘贴 session",
                "cloudflare_403": "被拦 → 中止请求",
                "unusual_activity": "上游判定异常流量 → 中止请求，不重试",
                "sentinel_missing": "无法签发 Sentinel → 检查 Node 与出口代理",
            },
        )


@dataclass
class UpstreamCheckoutAdapter(CheckoutAdapterBase):
    """用客户 session 向 ChatGPT 创建结账会话，并取出 Stripe 公钥。"""

    timeout: float = ORDER_HTTP_TIMEOUT_S

    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult:
        if not req.session or not parse_session(req.session):
            raise DomainError(ErrorCode.INVALID_SESSION, "缺少 session，无法创建结账")
        # exit_country 决定代理出口与建单定价；billing_country 只决定地址/税额。
        # 已 pin 的 req.proxy 必须原样保留，不能再按 billing_country 改写。
        exit_country = req.exit_country or req.billing_country
        pinned_billing = None if req.plan is Plan.TEAM else checkout_address(
            exit_country, req.billing_country, req.verified_billing,
            profile=req.verified_billing if hasattr(req.verified_billing, "verification_id") else None,
        )
        proxy = req.proxy or session_proxy("checkout", exit_country) or configured_proxy("checkout")
        if not req.proxy:
            proxy = align_proxy_region(proxy, exit_country)
        if req.plan is Plan.TEAM:
            if not proxy or not await asyncio.to_thread(_probe_stripe_proxy, proxy,
                                                        timeout=_STRIPE_PROXY_CHECK_TIMEOUT):
                raise DomainError(
                    ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                    "订单代理连接Stripe失败，未创建结账/未付款",
                )
        if pinned_billing is not None:
            # Bind payer + attestation across the worker handoff, even for internal callers.
            req = replace(req, verified_billing=pinned_billing)
        # A dead proxy session fails inside Sentinel before the checkout POST.
        # Rotate only that SID and recreate the client; the retried request uses
        # the new exit for both Sentinel and the checkout, keeping one IP.
        created = None
        for attempt in range(2):
            try:
                created = await asyncio.to_thread(self._create, req, proxy)
                break
            except DomainError as exc:
                if attempt == 0 and proxy and _retryable_sentinel_transport(exc):
                    logger.warning("checkout_sentinel_proxy_rotated code=%s", exc.code.value)
                    proxy = rotate_proxy_sid(proxy, force=True)
                    continue
                raise
        if created is None:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "建单未返回结果")
        amount = created.amount
        # Team 托管长链的实际价格（席位、周期、优惠码）只在 Stripe 页面上有，拿不到报价就留空，
        # 不能拿写死的 25 × 席位数冒充应付金额。
        if amount is None and req.plan is not Plan.TEAM:
            amount = Decimal("0.00") if req.billing_mode is BillingMode.PROMO else _list_price(req)
        return CheckoutResult(
            checkout_url=created.checkout_url,
            plan=req.plan,
            currency=created.currency,
            amount=amount,
            upstream_request_id=created.checkout_session_id,
            checkout_session_id=created.checkout_session_id,
            publishable_key=created.publishable_key or None,
            customer_session_client_secret=created.customer_session_client_secret or None,
            billing=dict(pinned_billing if pinned_billing is not None else address_for(req.billing_country)),
            tax_delta=getattr(created, "tax_delta", None),
            checkout_kind=created.checkout_kind or ("hosted" if req.plan is Plan.TEAM else "custom"),
            confirm_return_url=created.confirm_return_url or "",
            effective_proxy=proxy,
        )

    def _create(self, req: CheckoutRequest, proxy: Optional[str]):
        client = ChatGPTClient.from_raw_session(req.session, proxy=proxy)
        client.timeout = float(self.timeout)
        client.purchase_attempt_id = str(getattr(req, "purchase_attempt_id", "") or "")
        if getattr(req, "browser_context", None) is not None:
            client.apply_browser_context(req.browser_context)
        try:
            return client.create_checkout(
                plan=str(req.plan),
                country=req.exit_country or req.billing_country,
                billing_country=req.billing_country,
                promo=req.billing_mode is BillingMode.PROMO,
                promo_code=req.promo_code,
                promo_campaign_id=req.promo_campaign_id,
                workspace_name=req.workspace_name,
                seat_quantity=req.seat_quantity,
                price_interval=req.price_interval,
                **({"verified_billing": req.verified_billing} if req.plan is not Plan.TEAM else {}),
            )
        finally:
            client.close()


@dataclass
class FakeCheckoutAdapter(CheckoutAdapterBase):
    calls: List[CheckoutRequest] = field(default_factory=list)

    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult:
        pinned_billing = None if req.plan is Plan.TEAM else checkout_address(
            req.exit_country or req.billing_country, req.billing_country, req.verified_billing,
            profile=req.verified_billing if hasattr(req.verified_billing, "verification_id") else None,
        )
        self.calls.append(req)
        currency = currency_for(req.exit_country or req.billing_country)
        amount = Decimal("0.00") if req.billing_mode is BillingMode.PROMO else _list_price(req)
        from gss_core.fake_receipts import checkout_id_for
        hosted = req.plan is Plan.TEAM
        checkout_id = checkout_id_for(hosted=hosted, amount=amount, currency=currency)
        return CheckoutResult(
            checkout_url=(
                f"https://pay.openai.com/c/pay/{checkout_id}"
                if hosted
                else f"https://chatgpt.com/checkout/fake/{len(self.calls)}"
            ),
            plan=req.plan,
            currency=currency,
            amount=amount,
            upstream_request_id="fake-checkout-req",
            checkout_session_id=checkout_id,
            publishable_key="pk_test_fake" if hosted else None,
            billing=dict(pinned_billing if pinned_billing is not None else address_for(req.billing_country)),
            checkout_kind="hosted" if hosted else "custom",
        )


def _list_price(req: CheckoutRequest) -> Decimal:
    if req.plan is Plan.TEAM:
        seats = clamp_team_seats(req.seat_quantity)
        return Decimal(str(25 * seats))
    return _LIST_PRICE.get(req.plan, Decimal("20.00"))


__all__ = ["CheckoutAdapterBase", "UpstreamCheckoutAdapter", "FakeCheckoutAdapter"]
