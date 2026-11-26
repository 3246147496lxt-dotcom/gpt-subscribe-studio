"""订单：粘贴 session → 生成结账 → 用浏览器 PaymentMethod 开通。"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import random
import re
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from gss_contracts import (
    AccountSource,
    BillingMode,
    CardSource,
    Job,
    PaymentStatus,
    Mode,
    ModeValidationError,
    Order,
    OrderStatus,
    Plan,
    RoutePlan,
)
from gss_contracts.adapter import receipt_matches_order, team_entitlement_matches, verification_lease
from gss_contracts.errors import DomainError, ErrorCode
from gss_account import FakeAccountAdapter
from gss_checkout import FakeCheckoutAdapter
from gss_core.checkout_diagnostics import safe_checkout_diagnostics
from gss_core.failure_trace import load_redacted_http_trace
from gss_core.billing import address_for
from gss_core.fixed_billing import BILLING_COUNTRY
from gss_core.config import ORDER_HTTP_TIMEOUT_S, load_settings, use_fake_adapters
from gss_core.browser_context import BrowserContext
from gss_core.browser_decode import BrowserDecodeUnavailable, PlaywrightBrowserDecoder
from gss_core.countries import currency_for, is_supported, locale_for, normalize_country, timezone_for
from gss_core.billing_profiles import load_billing_profiles
from gss_core.purchase_attempt import new_purchase_attempt_id
from gss_core.upstream_errors import classify_upstream_failure
from gss_core.route_profiles import (
    browser_decode_enabled,
    browser_decode_fallback_a,
    browser_decode_url,
    route_plan_from_env,
)
from gss_core.hosted import TeamHostedClient
from gss_core.plus_protocol import PlusProtocol
from gss_core.plus_har_protocol import PlusHarProtocol
from gss_core.stripe_protocol import StripeProtocol, PaymentOutcome
from gss_core.proxies import (
    ProxyPreflightError,
    checkout_proxy_for,
    configured_exit_country,
    cool_proxy,
    proxy_exit_sid,
    rotate_proxy_sid,
    verify_refresh_egress,
)
from gss_core.redaction import redact_sensitive
from gss_core import runtime_flags
from gss_core.chatgpt import (
    ChatGPTClient,
    _pick_profile,
    clamp_team_seats,
    profile_from_session_ua,
)
from gss_core.session_token import (
    email_from_access_token,
    is_access_token_expired,
    parse_session,
    parse_session_bundle,
)
from gss_core.vault import OrderSecrets, vault
from gss_core.verified_payer import MissingProfile, ObservedBillingConflict, split_exit_billing_allowed
from gss_core.saved_card_protocol import _minor
from gss_core.saved_cards import verification_session
from gss_orchestrator.pipeline import apply_payment_result, assert_deliverable
from gss_payment.adapter import FakePaymentAdapter, hosted_payment_result

from ..runtime import build_pipeline
from ..verified_billing import require_verified_billing
from ..public_payment import detect_card_brand, issuer_action, normalize_card_brand, read_text, receipt_view
from ..schemas import (
    AddressedRequest,
    BillingIn,
    CardIn,
    ErrorOut,
    ModeIn,
    OrderCreateRequest,
    OrderFailureDetailOut,
    OrderFailureExchangeOut,
    OrderLedgerItemOut,
    OrderLedgerOut,
    OrderLedgerStatsOut,
    OrderOut,
    OrderPayRequest,
    QuickPayEventOut,
    ReconcileResultOut,
    SubscriptionOut,
    TeamPrepareRequest,
    TeamQuoteRequest,
)
from ..persistence import get_db_optional, load_checkout_diagnostics
from ..store import store
from .. import locks, purchase_claims
from .events import notify_order_update

router = APIRouter(prefix="/api", tags=["orders"])
logger = logging.getLogger("gss.orders")
PAYMENT_UNCONFIRMED = "扣款结果待核对，请勿重复支付"
PAID_AWAITING_ENTITLEMENT = "已扣款，待核验"
_team_locks: dict[str, asyncio.Lock] = {}
_creating_keys: set[str] = set()



def _aligned_billing(
    billing: dict, country: str, preferred: dict | None = None
) -> dict:
    """Session 签发地存在时，账单国家与默认地址模板一起对齐。

    preferred 是建单时已生成的地址（vault.billing）；存在时复用它，
    避免 prepare / pay 两次调用随机模板导致地址不一致。
    """
    code = normalize_country(country)
    current = dict(billing or {})
    if not code:
        return current
    if (
        normalize_country(str(current.get("country") or "")) == code
        and current.get("line1")
        and current.get("city")
        and current.get("postal_code")
    ):
        current["country"] = code
        return current
    preferred = dict(preferred or {})
    if (
        normalize_country(str(preferred.get("country") or "")) == code
        and preferred.get("line1")
        and preferred.get("city")
        and preferred.get("postal_code")
    ):
        result = dict(preferred)
        result["email"] = str(current.get("email") or preferred.get("email") or "")
        if current.get("name"):
            result["name"] = str(current["name"])
        return result
    template = address_for(code)
    template["email"] = str(current.get("email") or "")
    if current.get("name"):
        template["name"] = str(current["name"])
    return template


def _billing_claim(payload):
    payload.reject_unknown_fields()
    if isinstance(payload.billing, BillingIn):
        return payload.billing.model_dump(exclude_unset=True)
    if payload.billing is None and "billing" in payload.model_fields_set:
        raise HTTPException(409, "提交的账单不完整，未提交。")
    return payload.billing


_OFFLINE_TEST_INSTRUMENT = "offline-verified-instrument"


def _unverified_instrument():
    return HTTPException(409, "无法核验此外部付款工具的账单与本单属于同一付款主体，未提交。")


def _require_offline_instrument(pipeline, token):
    # No provider-issued opaque method carries a trusted payer/address binding
    # at these endpoints. Only the explicit local simulator instrument can pay,
    # and only on an entirely simulated pipeline (never trust an environment flag).
    if (token != _OFFLINE_TEST_INSTRUMENT
            or type(pipeline.deps.payment) is not FakePaymentAdapter
            or type(pipeline.deps.checkout) is not FakeCheckoutAdapter
            or type(pipeline.deps.account) is not FakeAccountAdapter):
        raise _unverified_instrument()


def _verified_order_billing(order, secrets: OrderSecrets, *claims):
    view = store.view(order.id)
    if (not view.billing_verified or not view.billing_verification_id
            or not view.billing or not secrets.billing):
        raise HTTPException(409, "原订单没有有效的账单核验记录，不能继续付款。")
    supplied = []
    for claim in claims:
        if isinstance(claim, BillingIn):
            claim.reject_unknown_fields()
            claim = claim.model_dump(exclude_unset=True)
        if claim is not None:
            supplied.append(claim)
    verified = require_verified_billing(*supplied, secrets.billing, order_billing=view.billing,
                                        verification_id=view.billing_verification_id)
    codes = [order.billing_country]
    if not split_exit_billing_allowed():
        # Strict mode only: the exit is part of the billing assertion.
        codes += [view.exit_country, secrets.exit_country]
    if any(code != verified["country"] for code in codes):
        raise HTTPException(409, "订单账单国家与核验记录不一致，未提交。")
    return verified


def _assert_saved_card_resolved(secrets: OrderSecrets) -> None:
    context = secrets.saved_card_context
    if context and context.get("protocol_flow"):
        raise HTTPException(409, "本订单已进入协议绑卡支付，请使用协议状态查询")
    if context and context.get("state") not in {"saved", "checkout_ready", "canceled"}:
        raise HTTPException(409, "请先完成保存卡片的验证与账号同步，再提交付款")


def _verify_saved_checkout(pipeline, order_id: str, secrets: OrderSecrets):
    if vault.get(order_id) is not secrets:
        raise DomainError(ErrorCode.SESSION_EXPIRED, "订单凭证已过期")
    client = ChatGPTClient.from_raw_session(secrets.session, proxy=secrets.proxy or None)
    client.timeout = ORDER_HTTP_TIMEOUT_S
    try:
        session = verification_session(
            client,
            secrets.saved_card_context["account_id"],
            trust_statsig=not getattr(secrets, "session_reissued", False),
            credentials_current=lambda: vault.get(order_id) is secrets,
        )
    finally:
        client.close()
    # The upstream account adapter uses blocking HTTP; keep the entire lookup off the API loop.
    if vault.get(order_id) is not secrets:
        raise DomainError(ErrorCode.SESSION_EXPIRED, "订单凭证已过期")
    with verification_lease(lambda: vault.get(order_id) is secrets):
        return session, asyncio.run(pipeline.verify(session=session))


def _to_mode(mode_in: ModeIn) -> Mode:
    return Mode(
        account_source=AccountSource(mode_in.account_source),
        card_source=CardSource(mode_in.card_source),
        billing_mode=BillingMode(mode_in.billing_mode),
        risk_acknowledged=mode_in.risk_acknowledged,
    )


def _default_status_summary(payment_status: str, delivery_status: str) -> str:
    if payment_status == "succeeded":
        if delivery_status == "delivered":
            return "扣款成功，订阅已生效"
        return PAID_AWAITING_ENTITLEMENT
    if payment_status == "processing":
        return "支付处理中，请勿重复提交"
    if payment_status == "failed":
        return "支付失败，未完成扣款"
    return "待支付"


def _receipt_for(order: Order, view) -> dict:
    amount, currency = view.paid_amount, view.paid_currency  # quotes are not receipts
    summary = view.status_summary or _default_status_summary(view.payment_status, view.delivery_status)
    return {
        "payment_intent_id": view.payment_intent_id,
        "amount": str(amount) if amount is not None else None,
        "currency": currency,
        "tax_delta": view.tax_delta,
        "plan_type": view.plan_type or str(order.plan),
        "payment_status": view.payment_status,
        "delivery_status": view.delivery_status,
        "summary": summary,
    }


def _set_payment_status(
    order_id: str,
    status: str,
    *,
    intent_id: Optional[str] = None,
    amount: Optional[str] = None,
    currency: Optional[str] = None,
    tax_delta: Optional[str] = None,
    plan_type: Optional[str] = None,
    summary: Optional[str] = None,
) -> None:
    view = store.view(order_id)
    view.payment_status = status
    if intent_id:
        view.payment_intent_id = intent_id
    if amount is not None:
        view.paid_amount = str(amount)
    if currency:
        view.paid_currency = currency
    if tax_delta is not None:
        view.tax_delta = str(tax_delta)
    if plan_type:
        view.plan_type = plan_type
    view.status_summary = redact_sensitive(summary) if summary else _default_status_summary(status, view.delivery_status)
    order = store.get_order(order_id)
    if order is not None:
        view.receipt = _receipt_for(order, view)


def _set_delivery_status(order_id: str, status: str, summary: Optional[str] = None) -> None:
    view = store.view(order_id)
    view.delivery_status = status
    view.status_summary = summary or _default_status_summary(view.payment_status, status)
    order = store.get_order(order_id)
    if order is not None:
        view.receipt = _receipt_for(order, view)


async def _mark_payment_unknown(order: Order) -> None:
    """提交后的异常不是拒付凭据；查询和重复提交都不得重新扣款。"""
    store.update_status(order.id, OrderStatus.UNKNOWN)
    view = store.view(order.id)
    view.last_error = None
    view.next_action = None
    _set_payment_status(order.id, "processing", summary=PAYMENT_UNCONFIRMED)
    await notify_order_update(order.id, OrderStatus.UNKNOWN, {"payment_status": "processing"})


def _accept_receipt(order: Order, evidence) -> bool:
    view = store.view(order.id)
    if not receipt_matches_order(evidence, checkout_id=view.checkout_session_id,
                                 amount=view.amount, currency=view.currency,
                                 payment_id=view.observed_payment_id):
        return False
    view.observed_payment_id = evidence.payment_id
    view.receipt_evidence = evidence
    _set_payment_status(order.id, "succeeded", intent_id=evidence.payment_id,
                        amount=str(evidence.amount), currency=evidence.currency,
                        plan_type=str(order.plan), summary=PAID_AWAITING_ENTITLEMENT)
    return True


def _plan_matches(order: Order, info, view) -> bool:
    if order.plan is Plan.TEAM:
        return team_entitlement_matches(info, view.receipt_evidence,
                                        workspace_name=view.workspace_name, seat_quantity=view.seat_quantity)
    aliases = {"plus": {"plus", "chatgptplusplan"}, "go": {"go", "chatgptgoplan"},
               "pro": {"pro", "chatgptpro", "chatgptproplan"},
               "pro_lite": {"pro_lite", "chatgptprolite"}, "pro_max": {"pro_max", "chatgptpromax"}}
    return info.active and (info.raw_kind or "").strip().lower() in aliases.get(str(order.plan), set())


async def _verify_after_payment(order: Order, view) -> bool:
    """付款事实已经确定；仅凭匹配的权益证据完成交付。"""
    if order.status in {OrderStatus.AWAITING_PAYMENT, OrderStatus.REQUIRES_ACTION, OrderStatus.UNKNOWN}:
        # Reconciliation observes an existing payment attempt; no provider write here.
        store.update_status(order.id, OrderStatus.PAYING)
    if order.status is OrderStatus.PAYING:
        store.update_status(order.id, OrderStatus.VERIFYING)
    view.last_error = view.next_action = view.subscription = None
    _set_delivery_status(order.id, "verify_delayed", PAID_AWAITING_ENTITLEMENT)
    await notify_order_update(order.id, order.status, {
        "payment_status": "succeeded", "delivery_status": "verify_delayed",
    })
    # Do not carry a credential lease across the preceding payment/poll/notification.
    secrets = vault.get(order.id)
    if secrets is None:
        return False
    try:
        with verification_lease(lambda: vault.get(order.id) is secrets):
            info = await build_pipeline().verify(session=secrets.session)
    except Exception as exc:
        # Account lookup is read-only. Never release custody or downgrade a paid receipt here.
        logger.warning("entitlement_lookup_delayed id=%s type=%s", order.id, type(exc).__name__)
        return False
    if not _plan_matches(order, info, view):
        return False
    assert_deliverable(OrderStatus.ACTIVE, info)
    view.subscription = info
    view.plan_type = info.raw_kind or view.plan_type
    store.update_status(order.id, OrderStatus.ACTIVE)
    await _mark_delivered(order.id)
    return True


def _to_out(order: Order) -> OrderOut:
    view = store.view(order.id)
    team = order.plan is Plan.TEAM
    public_billing = view.billing if team else None
    if not team and view.billing_verified and view.billing_verification_id and view.billing:
        try:
            active_profile = None
            if view.currency:
                try:
                    active_profile = load_billing_profiles().resolve_for_country(
                        normalize_country(order.billing_country), view.currency)
                except MissingProfile:
                    active_profile = None
            public_billing = require_verified_billing(
                order_billing=view.billing, verification_id=view.billing_verification_id,
                profile=active_profile,
            )
        except HTTPException:
            # Revocation/rotation hides the old payer's address, not the receipt.
            pass
    subscription = None
    if view.subscription is not None:
        subscription = SubscriptionOut(
            active=view.subscription.active,
            expires_at=view.subscription.expires_at.isoformat() if view.subscription.expires_at else None,
            channel=view.subscription.channel,
        )
    # The Team workflow uses its session email. Non-Team only shows verified payer details.
    session_email = ""
    try:
        _sec = vault.get(order.id) if team else None
        if _sec and _sec.session:
            session_email = email_from_access_token(parse_session(_sec.session))
    except Exception:
        session_email = ""

    return OrderOut(
        id=order.id,
        status=str(order.status),
        purchase_attempt_id=getattr(view, "purchase_attempt_id", None) or getattr(order, "purchase_attempt_id", None),
        route_profile=getattr(view, "route_profile", None),
        failure_code=getattr(view, "failure_code", None),
        plan=str(order.plan),
        billing_country=order.billing_country,
        mode=ModeIn(
            account_source=str(order.mode.account_source),
            card_source=str(order.mode.card_source),
            billing_mode=str(order.mode.billing_mode),
            risk_acknowledged=order.mode.risk_acknowledged,
        ),
        checkout_url=view.checkout_url if team else None,
        publishable_key=view.publishable_key,
        customer_session_client_secret=view.customer_session_client_secret if team else None,
        amount=view.amount if view.amount is not None else (str(order.amount) if order.amount is not None else None),
        currency=view.currency,
        billing=public_billing,
        billing_verified=not team and public_billing is not None,
        last_error=view.last_error if team else read_text(view.last_error),
        quote_error=view.quote_error if team else read_text(view.quote_error),
        next_action=view.next_action if team else issuer_action(
            view.next_action, allow_redirect=public_billing is not None and order.status is OrderStatus.REQUIRES_ACTION,
            checkout_url=view.checkout_url),
        subscription=subscription,
        checkout_kind=view.checkout_kind,
        seat_quantity=view.seat_quantity,
        workspace_name=view.workspace_name,
        promo_code=view.promo_code,
        promo_campaign_id=view.promo_campaign_id,
        price_interval=view.price_interval,
        confirm_return_url=view.confirm_return_url if team else None,
        session_email=session_email or None,
        session_reissued_for_country=bool(getattr(view, "session_reissued_for_country", False)),
        exit_country=getattr(view, "exit_country", None),
        payment_status=view.payment_status,
        delivery_status=view.delivery_status,
        payment_intent_id=view.payment_intent_id,
        paid_amount=view.paid_amount,
        paid_currency=view.paid_currency,
        tax_delta=view.tax_delta,
        plan_type=view.plan_type or str(order.plan),
        status_summary=(view.status_summary if team else read_text(view.status_summary))
        or _default_status_summary(view.payment_status, view.delivery_status),
        receipt=(view.receipt or None) if team else receipt_view(view.receipt),
        auto_cancel_renew=view.auto_cancel_renew,
        auto_renew_status=view.auto_renew_status,
        auto_renew_attempts=view.auto_renew_attempts,
        auto_renew_canceled_at=view.auto_renew_canceled_at,
        auto_renew_error=view.auto_renew_error,
        auto_renew_http_status=view.auto_renew_http_status,
        proxy_rotations=view.proxy_rotations,
        proxy_last_sid=mask_sid(view.proxy_last_sid),
        proxy_last_exit_ip=view.proxy_last_exit_ip,
    )


def _error(status_code: int, code: ErrorCode, message: str, *, session_reissued: bool = False,
           diagnostics: dict | None = None) -> HTTPException:
    detail = {"session_reissued_for_country": True} if session_reissued else {}
    detail.update(safe_checkout_diagnostics(diagnostics))
    return HTTPException(
        status_code=status_code,
        detail=ErrorOut(
            code=str(code),
            message=redact_sensitive(message),
            detail=detail or None,
        ).model_dump(),
    )


def _from_domain(exc: DomainError, *, session_reissued: bool = False, nonteam: bool = False) -> HTTPException:
    status_code = status.HTTP_400_BAD_REQUEST
    if exc.code in {ErrorCode.INVALID_SESSION, ErrorCode.SESSION_EXPIRED}:
        status_code = status.HTTP_401_UNAUTHORIZED
    # 只报告已发生的重签发；同时保留真实失败原因，避免把上游错误伪装成代理错误。
    # 上游错误可能截断并回显 Cookie 值，不能把原始 message 直接拼进响应。
    raw_reason = redact_sensitive(exc.message or "")
    if re.search(r"(?i)(?:session-token|set-cookie|cookie)", raw_reason):
        raw_reason = "上游拒绝建单（凭证内容已隐藏）"
    message = (f"会话已重新签发，但后续步骤失败：{raw_reason}"
               if session_reissued else raw_reason)
    return _error(status_code, exc.code, read_text(message) if nonteam else message,
                  session_reissued=session_reissued, diagnostics=exc.detail)


def mask_sid(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value)
    if len(text) <= 8:
        return "***"
    return text[:4] + "***" + text[-4:]


def normalize_plan_name(value: str) -> str:
    """Accept competitor-facing aliases while keeping Plan.pro_lite internal."""
    name = str(value or "").strip().lower()
    return "pro_lite" if name == "pro5" else name


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _reissue_on_exit_enabled() -> bool:
    """默认在真实上游模式下，只要含 ST/refresh_token 就在当前充值出口重颁新 AT。

    优先级：面板覆盖（runtime-flags.json） > 环境变量 GSS_REISSUE_SESSION_ON_EXIT > 默认值。
    """
    return runtime_flags.get_bool("reissue_session_on_exit")


def _reissue_keep_ua() -> bool:
    return runtime_flags.get_bool("reissue_keep_ua")


async def _human_pause_async(interval: tuple[float, float] = (1.5, 2.5)) -> None:
    """重颁 → 建单之间的拟人停顿。

    真人不会在拿到新 AT 的同一毫秒就去建单；0ms 抢跑是明显的脚本特征
    （实测失败单 b08cc2ad）。``GSS_WARMUP_INTERVAL=0`` 供单测/离线模式零延迟。
    """
    configured = os.environ.get("GSS_WARMUP_INTERVAL", "").strip()
    if configured:
        try:
            delay = max(0.0, float(configured))
        except ValueError:
            delay = random.uniform(*interval)
    else:
        delay = random.uniform(*interval)
    if delay:
        await asyncio.sleep(delay)


def _quarantine_exit(exit_proxy: str | None, failure_code: str) -> None:
    """把触发风控的出口 SID 放进冷却黑名单，避免下个订单立刻复用。

    与 plus_har_protocol 的 ``_note_exit_rejection`` 保持同一处置口径：
    绝不重试本单，只隔离这条出口。
    """
    if not exit_proxy:
        return
    try:
        seconds = int(os.environ.get("GSS_PROXY_COOLDOWN_SECONDS", "1800") or 1800)
    except ValueError:
        seconds = 1800
    try:
        result = cool_proxy(exit_proxy, reason=f"checkout:{failure_code}", seconds=max(1, seconds))
    except Exception:  # noqa: BLE001
        logger.warning("checkout_proxy_cooldown_failed code=%s", failure_code)
        return
    logger.info("checkout_proxy_cooled code=%s cooled=%s", failure_code, bool(result.get("cooled")))


def _mark(progress: Callable[[str], None] | None, stage: str) -> None:
    if progress is not None:
        try:
            progress(stage)
        except Exception:  # noqa: BLE001
            pass


def _checkout_failure_code(exc: DomainError) -> str:
    # An explicit rejection outranks incidental transport words in its message.
    detail = safe_checkout_diagnostics(exc.detail)
    upstream = str(detail.get("upstream_code", "")).lower()
    text = exc.message.lower()
    if (exc.code in {ErrorCode.INVALID_SESSION, ErrorCode.SESSION_EXPIRED}
            or detail.get("upstream_status") == 401
            or upstream in {"invalid_session", "token_revoked", "invalid_token", "session_expired"}
            or any(x in text for x in ("token_revoked", "token revoked", "invalidated oauth token",
                                       "invalid_session", "invalid session"))):
        return "invalid_session"
    if upstream == "unusual_activity" or any(x in text for x in ("unusual_activity", "unusual activity", "异常流量")):
        return "unusual_activity"
    if exc.code is not ErrorCode.CHECKOUT_UPSTREAM_ERROR:
        return str(exc.code)
    if upstream in {"already_subscribed", "subscription_limit", "billing_country_mismatch"}:
        return upstream
    # Some adapters report only a message. A subscription refusal is still
    # terminal even when the same message also mentions a proxy tunnel.
    if upstream == "already_paid" or any(x in text for x in (
        "already_subscribed", "already subscribed", "already paid", "已是付费订阅", "已有订阅",
    )):
        return "already_subscribed"
    inferred = str(classify_upstream_failure(exc=exc, stage="checkout").code)
    if exc.detail and inferred == "proxy_tunnel_failed":
        return "unknown_upstream"  # presence of a response is not a transport diagnosis
    return inferred


def _is_retryable_checkout_tunnel(exc: DomainError) -> bool:
    # Do not replay a checkout POST after a reply (even a partial/malformed one).
    # "proxy" by itself is not evidence that a connection was never established.
    if exc.code is not ErrorCode.CHECKOUT_UPSTREAM_ERROR or exc.detail or _checkout_failure_code(exc) != "proxy_tunnel_failed":
        return False
    return any(marker in exc.message.lower() for marker in (
        "proxy_tunnel_failed", "proxy tunnel failed", "connect tunnel failed",
    ))


def _release_create_failure(claim_dir, order_id: str, idempotency_key: str | None) -> None:
    if idempotency_key:
        store.forget_idempotency(idempotency_key)
    if claim_dir is not None:
        purchase_claims.release_checkout(claim_dir, order_id)


async def _guard_refresh_egress(proxy: str | None, country: str) -> None:
    if not runtime_flags.get_bool("proxy_preflight"):
        return
    if use_fake_adapters():
        # Fake adapters do not implement authentication. Never hit a real refresh
        # endpoint just because offline mode skipped the network checks.
        raise _error(503, ErrorCode.CHECKOUT_UPSTREAM_ERROR, "离线模式不发送会话凭证")
    try:
        await asyncio.to_thread(verify_refresh_egress, proxy or "", country)
    except ProxyPreflightError as exc:
        logger.warning("session_preflight_aborted status=%s", exc.status_code)
        raise HTTPException(exc.status_code, detail={
            "code": "proxy_tunnel_failed",
            "message": f"{exc}；未发送会话凭证。",
        }) from None


async def _create_checkout_once(pipeline, *, proxy: str, **kwargs):
    # A timeout is ambiguous: the upstream may already have accepted this POST.
    # Do not resend it, even on the same connection.
    try:
        return await pipeline.create_checkout(proxy=proxy, pin_proxy=True, **kwargs)
    except DomainError as exc:
        logger.warning(
            "create_checkout_domain_error type=%s code=%s", type(exc).__name__, str(exc.code),
        )
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_checkout_exception type=%s", type(exc).__name__)
        raise DomainError(
            ErrorCode.CHECKOUT_UPSTREAM_ERROR, "建单请求未获确认，已停止且不自动重试。",
        ) from None


def _csv_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


_HEX_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16,64}$")
_TIMING_KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def _derive_order_source(row: dict[str, Any]) -> str:
    if row.get("portal_task_job_id"):
        return "portal"
    if row.get("job_id"):
        return "quick_pay"
    return "step"


def _safe_account_tag(fingerprint: Optional[str]) -> Optional[str]:
    fp = (fingerprint or "").strip().lower()
    if _HEX_FINGERPRINT_RE.fullmatch(fp):
        return fp[:8]
    return None


def _parse_timings_ms(raw: Any) -> dict[str, int]:
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in data.items():
        if isinstance(k, str) and _TIMING_KEY_RE.fullmatch(k) and isinstance(v, (int, float)) and not isinstance(v, bool):
            ival = int(v)
            if 0 <= ival <= 86_400_000:
                out[k] = ival
    return out


def _compute_total_ms(row: dict[str, Any], timings_ms: dict[str, int]) -> Optional[int]:
    if "total_ms" in timings_ms:
        return timings_ms["total_ms"]
    start_raw = row.get("created_at")
    end_raw = row.get("finished_at") or row.get("job_finished_at")
    if not start_raw or not end_raw:
        return None
    try:
        start = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
        delta_ms = int((end - start).total_seconds() * 1000)
        return delta_ms if 0 <= delta_ms <= 86_400_000 else None
    except Exception:
        return None


def _ledger_item_from_row(row: dict[str, Any]) -> OrderLedgerItemOut:
    timings_ms = _parse_timings_ms(row.get("timings_json"))
    diagnostics = load_checkout_diagnostics(row.get("diagnostics_json"))
    raw_http = row.get("auto_renew_http_status")
    auto_renew_http = int(raw_http) if isinstance(raw_http, int) and not isinstance(raw_http, bool) else None
    return OrderLedgerItemOut(
        id=row["id"],
        source=_derive_order_source(row),
        account_email=row.get("account_email") or None,
        masked_email=row.get("masked_email") or None,
        portal_username=row.get("portal_username") or None,
        account_tag=_safe_account_tag(row.get("account_fingerprint")),
        card_brand=normalize_card_brand(row.get("card_brand")),
        plan=row["plan"],
        billing_country=row["billing_country"],
        exit_country=row.get("exit_country"),
        status=row["status"],
        payment_status=row.get("payment_status") or "unpaid",
        delivery_status=row.get("delivery_status") or "pending",
        amount=row.get("amount"),
        currency=row.get("currency"),
        tax_delta=row.get("tax_delta"),
        failure_code=row.get("failure_code"),
        last_error=row.get("last_error"),
        purchase_attempt_id=row.get("purchase_attempt_id"),
        payment_intent_id=row.get("payment_intent_id"),
        checkout_session_id=row.get("checkout_session_id"),
        checkout_kind=row.get("checkout_kind"),
        route_profile=row.get("route_profile"),
        billing_mode=row.get("billing_mode"),
        billing_verified=bool(row.get("billing_verified")),
        session_reissued=bool(row.get("session_reissued")),
        auto_cancel_renew=bool(row.get("auto_cancel_renew")),
        auto_renew_status=row.get("auto_renew_status") or "disabled",
        auto_renew_attempts=int(row.get("auto_renew_attempts") or 0),
        auto_renew_canceled_at=row.get("auto_renew_canceled_at"),
        auto_renew_error=row.get("auto_renew_error"),
        auto_renew_http_status=auto_renew_http,
        proxy_rotations=int(row.get("proxy_rotations") or 0),
        proxy_last_sid=mask_sid(row.get("proxy_last_sid")),
        proxy_last_exit_ip=row.get("proxy_last_exit_ip"),
        job_id=row.get("job_id"),
        job_status=row.get("job_status"),
        job_stage=row.get("job_stage"),
        job_attempts=int(row.get("job_attempts") or 0),
        reconcile_attempts=int(row.get("reconcile_attempts") or 0),
        last_reconciled_at=row.get("last_reconciled_at"),
        active_lock_id=row.get("active_lock_id"),
        total_ms=_compute_total_ms(row, timings_ms),
        timings_ms=timings_ms,
        diagnostics=diagnostics,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        unknown_since=row.get("unknown_since"),
        finished_at=row.get("finished_at") or row.get("job_finished_at"),
    )


@router.get("/orders", response_model=OrderLedgerOut)
async def list_orders_ledger(
    status_filter: str = Query(default="all", alias="status", max_length=40),
    plan: str = Query(default="all", max_length=40),
    source: str = Query(default="all", max_length=24),
    search: str = Query(default="", max_length=80),
    date_from: str = Query(default="", max_length=40),
    date_to: str = Query(default="", max_length=40),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> OrderLedgerOut:
    """Server-side paginated read-only order ledger (zero secrets)."""
    db = get_db_optional()
    if db is None:
        return OrderLedgerOut(stats=OrderLedgerStatsOut(), total=0, limit=limit, offset=offset, items=[])
    rows, total = db.list_orders_page(
        status=status_filter, plan=plan, source=source, search=search,
        date_from=date_from, date_to=date_to, limit=limit, offset=offset,
    )
    stats = OrderLedgerStatsOut(**db.order_summary_stats())
    items = [_ledger_item_from_row(row) for row in rows]
    return OrderLedgerOut(stats=stats, total=total, limit=limit, offset=offset, items=items)


@router.get("/orders/export.csv")
async def export_orders_csv(
    status_filter: str = Query(default="all", alias="status", max_length=40),
    plan: str = Query(default="all", max_length=40),
    source: str = Query(default="all", max_length=24),
    search: str = Query(default="", max_length=80),
    date_from: str = Query(default="", max_length=40),
    date_to: str = Query(default="", max_length=40),
) -> StreamingResponse:
    """Export redacted server-side ledger as UTF-8 BOM CSV with formula-injection protection."""
    db = get_db_optional()
    rows = (
        db.export_orders_rows(
            status=status_filter, plan=plan, source=source, search=search,
            date_from=date_from, date_to=date_to,
        )
        if db is not None
        else []
    )
    columns = (
        "created_at", "finished_at", "total_ms",
        "id", "source", "account_email", "portal_username", "masked_email", "account_tag",
        "plan", "card_brand", "exit_country", "billing_country", "checkout_kind", "route_profile",
        "billing_verified", "session_reissued",
        "amount", "currency", "tax_delta",
        "status", "payment_status", "delivery_status",
        "auto_renew_status", "auto_renew_attempts", "auto_renew_canceled_at",
        "proxy_rotations", "proxy_last_sid", "proxy_last_exit_ip",
        "failure_code", "upstream_status", "upstream_code",
        "job_id", "purchase_attempt_id", "checkout_session_id", "payment_intent_id",
        "reconcile_attempts", "active_lock_id", "updated_at",
    )
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        timings_ms = _parse_timings_ms(row.get("timings_json"))
        diag = load_checkout_diagnostics(row.get("diagnostics_json"))
        enriched = {
            **row,
            "finished_at": row.get("finished_at") or row.get("job_finished_at"),
            "total_ms": _compute_total_ms(row, timings_ms),
            "source": _derive_order_source(row),
            "account_email": row.get("account_email") or None,
            "card_brand": normalize_card_brand(row.get("card_brand")),
            "account_tag": _safe_account_tag(row.get("account_fingerprint")),
            "billing_verified": "true" if row.get("billing_verified") else "false",
            "session_reissued": "true" if row.get("session_reissued") else "false",
            "proxy_last_sid": mask_sid(row.get("proxy_last_sid")),
            "upstream_status": diag.get("upstream_status"),
            "upstream_code": diag.get("upstream_code"),
        }
        writer.writerow([_csv_safe(enriched.get(col)) for col in columns])
    content = buf.getvalue().encode("utf-8")
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="gss-order-ledger.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/orders/{order_id}/events", response_model=list[QuickPayEventOut])
async def get_order_events(order_id: str) -> list[QuickPayEventOut]:
    db = get_db_optional()
    if db is None:
        return []
    return [QuickPayEventOut(**ev) for ev in db.list_events(order_id=order_id)]


_FAILURE_SUMMARY_BY_CODE: Dict[str, str] = {
    "subscription_limit": "充值账单未通过确认（账号已达订阅上限），本次尚未提交付款。",
    "already_subscribed": "当前账号已存在有效订阅，本次未创建新账单也未提交付款。",
    "unusual_activity": "上游风控判定异常流量并拒绝了本次请求，尚未提交付款。请更换同地区出口节点或重新导出 Session 后重试。",
    "invalid_session": "账号 Session 已失效或被撤销，本次尚未提交付款。请重新登录 ChatGPT 导出完整 Session 后重试。",
    "session_expired": "账号 Session 已过期，本次尚未提交付款。请重新登录 ChatGPT 导出最新 Session。",
    "card_declined": "发卡行或 Stripe 拒绝了本次扣款，请核对卡片状态、余额或更换卡片后重试。",
    "insufficient_funds": "卡片可用余额不足，Stripe 未完成扣款，请充值或更换卡片后重试。",
    "sentinel_challenge_failed": "上游安全验证未通过，系统已熔断停止，本次未继续扣款。",
    "proxy_tunnel_failed": "出口代理连接失败，请求未送达上游，请检查代理节点可用性后重试。",
    "proxy_authentication_failed": "出口代理认证失败，请检查代理账号密码与地区配置。",
    "billing_country_mismatch": "上游校验账单国家与预期不一致，本次尚未提交付款。",
    "browser_decode_unavailable": "浏览器解码环境不可用，已停止建单，请检查 Chromium / Playwright 配置。",
    "checkout_upstream_error": "结账请求未获上游确认，本次尚未提交付款，请核对下方上游返回信息。",
    "payment_unknown_state": "支付确认结果暂未明确，系统已自动加锁防止重复扣款，请先执行只读对账或核对银行账单。",
    "upstream_timeout": "上游响应超时，请勿立即重复扣款，请先核对订单状态或执行只读对账。",
}

_STAGE_ENDPOINT_DEFAULTS: Dict[str, tuple[str, str, str]] = {
    "creating_checkout": ("Checkout 建单", "POST", "https://chatgpt.com/backend-api/payments/checkout"),
    "checkout": ("Checkout 建单", "POST", "https://chatgpt.com/backend-api/payments/checkout"),
    "payment_confirm": ("Checkout 确认", "POST", "https://chatgpt.com/backend-api/payments/checkout/confirm"),
    "confirming_payment": ("Checkout 确认", "POST", "https://chatgpt.com/backend-api/payments/checkout/confirm"),
    "payment": ("Checkout 确认", "POST", "https://chatgpt.com/backend-api/payments/checkout/confirm"),
    "payment_tokenize": ("Stripe 卡片令牌化", "POST", "https://api.stripe.com/v1/confirmation_tokens"),
    "reissuing": ("Session 跨区重签发", "GET", "https://chatgpt.com/api/auth/session"),
    "session_refresh": ("Session 刷新", "GET", "https://chatgpt.com/api/auth/session"),
    "session_auth": ("账号鉴权校验", "GET", "https://chatgpt.com/backend-api/me"),
    "preflight": ("前置自动体检", "CHECK", "local://preflight"),
}


@router.get("/orders/{order_id}/failure-detail", response_model=OrderFailureDetailOut)
async def get_order_failure_detail(order_id: str) -> OrderFailureDetailOut:
    """Admin-only structured failure diagnostics with strictly redacted request/response payloads."""
    db = get_db_optional()
    if db is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "订单账本未就绪"})
    row = db.get_order_failure_row(order_id)
    if row is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "订单不存在"})

    events_raw = db.list_events(order_id=order_id)
    events = [QuickPayEventOut(**ev) for ev in events_raw]
    diag = load_checkout_diagnostics(row.get("diagnostics_json"))
    trace = load_redacted_http_trace(row.get("failure_trace_json"))

    raw_code = (
        row.get("failure_code")
        or row.get("job_failure_code")
        or diag.get("upstream_code")
        or None
    )
    code_norm = str(raw_code).strip().lower() if raw_code else ""
    error_code_display = str(raw_code).strip().upper() if raw_code else None

    stage = (
        (trace or {}).get("stage")
        or diag.get("stage")
        or next(
            (ev["stage"] for ev in reversed(events_raw) if ev.get("stage") and ev["stage"] not in {"failed", "unknown"}),
            None,
        )
        or (row.get("job_stage") if row.get("job_stage") not in {None, "", "failed"} else None)
        or ("creating_checkout" if not row.get("checkout_kind") else "payment_confirm")
    )

    upstream_status = (
        (trace or {}).get("http_status")
        or diag.get("upstream_status")
        or row.get("auto_renew_http_status")
    )
    cf_ray = (trace or {}).get("cf_ray") or diag.get("cf_ray")
    request_id = (trace or {}).get("request_id") or diag.get("request_id")

    base_summary = _FAILURE_SUMMARY_BY_CODE.get(code_norm)
    recorded_error = (row.get("last_error") or row.get("job_message") or row.get("auto_renew_error") or "").strip()
    if base_summary:
        summary = base_summary
    elif recorded_error:
        summary = recorded_error
    else:
        summary = "该订单未成功完成，请结合下方请求与响应信息排查。"

    if trace is not None:
        exchange = OrderFailureExchangeOut(
            title=trace["title"],
            method=trace.get("method"),
            url=trace.get("url"),
            http_status=trace.get("http_status") or upstream_status,
            request=trace.get("request"),
            response=trace.get("response"),
            captured=True,
        )
    else:
        step_title, step_method, step_url = _STAGE_ENDPOINT_DEFAULTS.get(
            str(stage or ""),
            ("上游请求", "POST", "https://chatgpt.com/backend-api/payments/checkout"),
        )
        req_params: dict[str, Any] = {
            "plan": row.get("plan"),
            "billing_country": row.get("billing_country"),
        }
        if row.get("exit_country"):
            req_params["exit_country"] = row.get("exit_country")
        if row.get("route_profile"):
            req_params["route_profile"] = row.get("route_profile")
        if row.get("billing_mode"):
            req_params["billing_mode"] = row.get("billing_mode")
        if row.get("checkout_kind"):
            req_params["checkout_kind"] = row.get("checkout_kind")

        resp_summary: dict[str, Any] = {}
        if diag.get("upstream_code"):
            resp_summary["code"] = diag["upstream_code"]
        elif raw_code:
            resp_summary["code"] = str(raw_code)
        if recorded_error:
            resp_summary["message"] = recorded_error
        if cf_ray:
            resp_summary["cf_ray"] = cf_ray
        if request_id:
            resp_summary["request_id"] = request_id
        if not resp_summary:
            resp_summary["status"] = row.get("status") or "failed"

        exchange = OrderFailureExchangeOut(
            title=step_title,
            method=step_method,
            url=step_url,
            http_status=int(upstream_status) if isinstance(upstream_status, int) else None,
            request={"json": req_params},
            response=resp_summary,
            captured=False,
        )

    return OrderFailureDetailOut(
        order_id=row["id"],
        title="开通失败详情",
        summary=summary,
        error_code=error_code_display,
        stage=stage,
        upstream_status=int(upstream_status) if isinstance(upstream_status, int) else None,
        upstream_code=diag.get("upstream_code"),
        cf_ray=cf_ray,
        request_id=request_id,
        exchange=exchange,
        events=events,
    )


@router.post("/orders/{order_id}/reconcile", response_model=ReconcileResultOut)
async def reconcile_order_endpoint(order_id: str) -> ReconcileResultOut:
    from ..services import reconciler
    result = await reconciler.reconcile_order(order_id, force=True)
    if result.get("outcome") == "order_not_found":
        raise HTTPException(404, detail={"code": "not_found", "message": "订单不存在"})
    return ReconcileResultOut(**result)


@router.post(
    "/orders",
    status_code=status.HTTP_201_CREATED,
    response_model=OrderOut,
    responses={400: {"model": ErrorOut}, 401: {"model": ErrorOut}},
)
async def create_order(
    payload: OrderCreateRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> OrderOut:
    # Single-process API: no await between the check and reservation. A concurrent
    # duplicate must not spend the incoming session or start a second checkout.
    idempotency_key = (idempotency_key or "").strip()
    if not idempotency_key:
        if payload.payment_method_token:
            raise _error(400, ErrorCode.IDEMPOTENCY_CONFLICT,
                         "建单并付款必须提供稳定的 Idempotency-Key；重试须沿用同一键。")
        return await _create_order(payload, None)
    if len(idempotency_key) > 255:
        raise _error(400, ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency-Key 长度不能超过 255")
    if idempotency_key in _creating_keys:
        raise _error(409, ErrorCode.IDEMPOTENCY_CONFLICT, "同一订单正在处理，请勿重复提交。")
    _creating_keys.add(idempotency_key)
    try:
        return await _create_order(payload, idempotency_key)
    finally:
        _creating_keys.discard(idempotency_key)


async def _create_order(payload: OrderCreateRequest, idempotency_key: str | None,
                        progress: Callable[[str], None] | None = None) -> OrderOut:
    # Preserve signatures for callers that did not previously send a billing claim.
    non_team = payload.plan != str(Plan.TEAM)
    if non_team and any(field in payload.model_fields_set and not (getattr(payload, field) or "").strip()
                        for field in ("billing_country", "exit_country")):
        raise HTTPException(409, "地区声明为空，未提交；由服务端确定时请省略该字段。")
    claim = _billing_claim(payload) if non_team else None
    omit = {"billing"} if not non_team or "billing" not in payload.model_fields_set else set()
    tag = (purchase_claims.request_tag(idempotency_key, payload.model_dump_json(exclude=omit))
           if idempotency_key else None)
    existing = store.find_by_idempotency(idempotency_key)
    if existing is not None:
        if tag != store.view(existing.id).create_request_tag:
            raise _error(409, ErrorCode.IDEMPOTENCY_CONFLICT, "此幂等键已绑定不同的订单请求")
        return _to_out(existing)

    try:
        plan = Plan(normalize_plan_name(payload.plan))
    except ValueError:
        raise _error(status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED, f"未知套餐: {payload.plan}")

    try:
        mode = _to_mode(payload.mode)
        mode.validate()
    except ModeValidationError as exc:
        try:
            code = ErrorCode(exc.code)
        except ValueError:
            code = ErrorCode.COMPLIANCE_BLOCKED
        raise _error(status.HTTP_400_BAD_REQUEST, code, exc.message)
    except ValueError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED, f"模式非法: {exc}")

    if mode.billing_mode is BillingMode.PROMO and not load_settings().enable_promo_mode:
        raise _error(status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED, "促销模式尚未启用")

    if mode.account_source is AccountSource.CUSTOMER_SESSION and not payload.session:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.INVALID_SESSION,
            "请粘贴 ChatGPT session",
        )

    raw_session = (payload.session or "").strip()
    try:
        bundle = parse_session_bundle(raw_session)
    except ValueError as exc:
        raise _error(status.HTTP_401_UNAUTHORIZED, ErrorCode.INVALID_SESSION, str(exc))
    fingerprint = locks.account_fingerprint(raw_session)
    held = locks.find_active_lock(fingerprint)
    if held is not None:
        raise HTTPException(409, detail={
            "code": "payment_unknown_lock",
            "message": "存在未核销的 unknown 订单，请先人工核对账单",
            **locks.lock_payload(held),
        })
    
    if payload.billing_country is not None and not is_supported(payload.billing_country):
        raise _error(
            status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED,
            f"不支持的地区: {payload.billing_country}" if not non_team else "不支持的地区，未创建订单。",
        )
    if payload.exit_country and not is_supported(payload.exit_country):
        raise _error(
            status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED,
            f"不支持的出口地区: {normalize_country(payload.exit_country)}" if not non_team else "不支持的出口地区，未创建订单。",
        )
    original_country = normalize_country(bundle.country)
    if non_team:
        # 出口自由：显式出口 → Session 签发地 → 已配置网关地区 → 账单国兜底。
        # 固定账单（美国免税地址）不参与出口选择。
        exit_country = (normalize_country(payload.exit_country) or original_country
                        or configured_exit_country()
                        or normalize_country(payload.billing_country) or BILLING_COUNTRY)
    else:
        # Team 保持原流程：出口跟随其显式结账地区。
        exit_country = normalize_country(payload.exit_country or payload.billing_country)
    billing_profile = None
    if non_team:
        try:
            profile_currency = currency_for(exit_country)
            billing_profile = load_billing_profiles().resolve_for_country(BILLING_COUNTRY, profile_currency)
        except MissingProfile:
            raise HTTPException(503, detail={
                "code": "BILLING_PROFILE_UNAVAILABLE",
                "message": "已核验账单资料不可用，不能提交新的结账或付款。",
            }) from None
    verified = (require_verified_billing(*([claim] if claim is not None else []), profile=billing_profile)
                if non_team else None)
    # Never derive the payer's country from a route, browser locale, or session.
    # The Team schema still requires its original explicit country.
    country = verified["country"] if verified is not None else normalize_country(payload.billing_country)
    if verified is not None and payload.billing_country is not None and normalize_country(payload.billing_country) != country:
        # 旧客户端会把出口国当账单国提交；固定账单优先，静默忽略这个提示。
        logger.info("billing_country_ignored submitted=%s fixed=%s",
                    normalize_country(payload.billing_country), country)
    if verified is not None and exit_country != country and not split_exit_billing_allowed():
        raise HTTPException(409, "当前为严格模式：出口必须与固定账单国家一致，未创建订单。")
    cross_country = bool(original_country and original_country != exit_country)
    at_missing = not bundle.access_token
    at_expired = bool(
        bundle.access_token and is_access_token_expired(bundle.access_token, leeway_seconds=60)
    )
    proactive_exit_reissue = bool(
        not cross_country and not at_missing and _reissue_on_exit_enabled()
    )
    need_st_reissue = bool(
        bundle.session_token
        and (cross_country or at_missing or at_expired or proactive_exit_reissue)
    )
    need_oauth_reissue = bool(
        not need_st_reissue
        and bundle.refresh_token
        and (cross_country or at_missing or at_expired or _reissue_on_exit_enabled())
    )
    pipeline = None
    if non_team:
        if payload.payment_method_token:
            pipeline = build_pipeline()
            _require_offline_instrument(pipeline, payload.payment_method_token)
            # The simulator adapters do not replace the router's Session client.
            # Do not let a simulated instrument trigger a real refresh/preflight.
            if need_st_reissue or need_oauth_reissue:
                raise HTTPException(409, "离线付款工具不能触发上游会话刷新，未提交。")
    session_reissued = False

    # 出口选择不授权更改已核验账单。
    logger.info(
        "checkout_country exit_country=%s billing_country=%s session_country=%s",
        exit_country,
        country,
        original_country or "-",
    )
    exit_proxy = checkout_proxy_for(
        "checkout",
        session_country=exit_country,
        billing_country=exit_country,
    )

    seats = 5
    workspace = str(payload.workspace_name or "").strip()
    promo_code = str(payload.promo_code or "").strip()
    promo_campaign_id = str(payload.promo_campaign_id or "").strip()
    interval = "year" if str(payload.price_interval or "").lower() == "year" else "month"
    if plan is Plan.TEAM:
        if payload.seat_quantity is None:
            raise _error(status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED, "Team 需要填写席位，最少 2 人")
        try:
            seats = clamp_team_seats(payload.seat_quantity)
        except ValueError as exc:
            raise _error(status.HTTP_400_BAD_REQUEST, ErrorCode.COMPLIANCE_BLOCKED, str(exc))
        if not workspace:
            workspace = "Team Workspace"

    order = Order(
        idempotency_key=idempotency_key or "",
        purchase_attempt_id=new_purchase_attempt_id(),
        plan=plan,
        billing_country=country,
        mode=mode,
        status=OrderStatus.CREATED,
    )

    def _session_json(client: ChatGPTClient, country: str) -> str:
        resolved_ua = getattr(client, "user_agent", "") or getattr(client, "ua", "")
        data: Dict[str, Any] = {
            "accessToken": client.access_token,
            "sessionToken": client.extra_cookies.get("__Secure-next-auth.session-token", client.session_token),
            "deviceId": client.device_id,
            "sessionId": client.oai_session_id,
            "userAgent": resolved_ua,
            "cookies": client.extra_cookies,
            "statsigContext": {
                "deviceId": client.device_id,
                "userAgent": resolved_ua,
                "country": country,
            },
        }
        if getattr(client, "refresh_token", ""):
            data["refresh_token"] = client.refresh_token
        return json.dumps(data)

    if original_country and original_country != exit_country and not bundle.session_token and not bundle.refresh_token:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.INVALID_SESSION,
            "跨区充值需要完整 Session（缺少 sessionToken），请重新导出整段 session JSON",
        )

    claim_dir = load_settings().data_dir if idempotency_key else None
    if claim_dir is not None:
        try:
            first = await asyncio.to_thread(purchase_claims.reserve, claim_dir, idempotency_key,
                                            order.id, includes_payment=bool(payload.payment_method_token))
        except purchase_claims.ClaimUnavailable:
            raise _error(503, ErrorCode.IDEMPOTENCY_CONFLICT, "暂时无法确认原订单，请勿重新提交付款") from None
        if not first:
            # A restarted/other worker cannot authenticate a replay from this key
            # alone: never return someone else's receipt or create another charge.
            raise _error(409, ErrorCode.IDEMPOTENCY_CONFLICT, "已有原订单记录，请核对原付款，勿重复提交")

    def remember(snapshot: str, ua: str, device_id: str) -> OrderSecrets:
        # Commit the snapshot in RAM before closing the refreshing client, deriving
        # checkout metadata or invoking an adapter. No public credential handle.
        item = OrderSecrets(
            session=snapshot, user_agent=ua, device_id=device_id,
            proxy=exit_proxy or "", session_country=original_country, exit_country=exit_country,
            session_reissued=session_reissued, purchase_attempt_id=order.purchase_attempt_id,
        )
        vault.put(order.id, item)
        store.create_order(order, idempotency_key)
        view = store.view(order.id)
        view.account_fingerprint = fingerprint
        try:
            view.account_email = (
                email_from_access_token(parse_session(snapshot))
                or email_from_access_token(bundle.access_token)
                or None
            )
        except Exception:
            view.account_email = email_from_access_token(bundle.access_token) or None
        view.auto_cancel_renew = bool(payload.auto_cancel_renew)
        view.auto_renew_status = "pending" if payload.auto_cancel_renew else "disabled"
        view.create_request_tag = tag
        view.purchase_attempt_id = order.purchase_attempt_id
        view.session_reissued_for_country = session_reissued
        view.exit_country = exit_country
        # 记录实际命中的出口 SID（输出层统一经 mask_sid() 打码），
        # 便于排查是哪批 SID 触发风控。
        try:
            view.proxy_last_sid = proxy_exit_sid(exit_proxy)
        except Exception:  # noqa: BLE001
            view.proxy_last_sid = None
        store.persist(order.id)
        return item

    route_plan = route_plan_from_env()
    browser_context: BrowserContext | None = None
    access_token = bundle.access_token
    stage_started = time.perf_counter()
    if need_st_reissue or need_oauth_reissue:
        _mark(progress, "reissuing" if cross_country else "session_refresh")
        try:
            await _guard_refresh_egress(exit_proxy, exit_country)
        except HTTPException:
            _release_create_failure(claim_dir, order.id, idempotency_key)
            raise
        use_clean_st_reissue = bool(need_st_reissue)
        if need_oauth_reissue:
            try:
                client = await asyncio.to_thread(
                    ChatGPTClient.reauthorize_oauth, raw_session,
                    proxy=exit_proxy, country=exit_country,
                )
            except DomainError as exc:
                _release_create_failure(claim_dir, order.id, idempotency_key)
                raise _from_domain(exc, nonteam=plan is not Plan.TEAM)
            except Exception:
                _release_create_failure(claim_dir, order.id, idempotency_key)
                raise _error(503, ErrorCode.CHECKOUT_UPSTREAM_ERROR, "会话请求中断，已停止。") from None
        elif use_clean_st_reissue:
            client = None
            for reauth_attempt in range(2):
                reauth_kwargs: Dict[str, Any] = {"proxy": exit_proxy, "country": exit_country}
                if _reissue_keep_ua():
                    reauth_kwargs["preserve_user_agent"] = True
                try:
                    client = await asyncio.to_thread(
                        ChatGPTClient.reauthorize_for_country, raw_session, **reauth_kwargs,
                    )
                    break
                except DomainError as exc:
                    detail = exc.detail if isinstance(exc.detail, dict) else {}
                    # 仅在匿名边缘握手阶段（未发送任何会话凭证）遇代理断连时换一次新 SID；
                    # 一旦已向 /api/auth/session 发送过凭证，绝不自动重放。
                    if (
                        reauth_attempt == 0
                        and exit_proxy
                        and runtime_flags.get_bool("checkout_rotate")
                        and exc.code is ErrorCode.CHECKOUT_UPSTREAM_ERROR
                        and detail.get("credentials_sent") is False
                    ):
                        logger.warning("reissue_warmup_proxy_rotating code=proxy_tunnel_failed")
                        exit_proxy = rotate_proxy_sid(exit_proxy, force=True)
                        continue
                    _release_create_failure(claim_dir, order.id, idempotency_key)
                    raise _from_domain(exc, nonteam=plan is not Plan.TEAM)
                except Exception:
                    _release_create_failure(claim_dir, order.id, idempotency_key)
                    raise _error(503, ErrorCode.CHECKOUT_UPSTREAM_ERROR, "会话请求中断，已停止。") from None
            if client is None:
                _release_create_failure(claim_dir, order.id, idempotency_key)
                raise _error(503, ErrorCode.CHECKOUT_UPSTREAM_ERROR, "会话请求中断，已停止。")
        else:
            client = ChatGPTClient.from_raw_session(raw_session, proxy=exit_proxy, strip_ambient=True)
            client.language = locale_for(exit_country)
            client.timezone = timezone_for(exit_country)
            client.session_country = exit_country
        try:
            access_token = client.access_token
            if not access_token:
                _release_create_failure(claim_dir, order.id, idempotency_key)
                raise _error(401, ErrorCode.INVALID_SESSION, "会话刷新未返回 accessToken")
            session_reissued = cross_country
            # Persist the refreshed credential before any further upstream call.
            raw_session = _session_json(client, exit_country)
            if not fingerprint:
                fingerprint = locks.account_fingerprint(raw_session)
                held = locks.find_active_lock(fingerprint)
                if held is not None:
                    _release_create_failure(claim_dir, order.id, idempotency_key)
                    raise HTTPException(409, detail={
                        "code": "payment_unknown_lock",
                        "message": "存在未核销的 unknown 订单，请先人工核对账单",
                        **locks.lock_payload(held),
                    })
            resolved_ua = getattr(client, "user_agent", "") or getattr(client, "ua", "")
            item = remember(raw_session, resolved_ua, client.device_id)
            if runtime_flags.get_bool("warm_account"):
                # Optional lab warm-up. Default off keeps the proven 00331ce
                # main path free of extra account-endpoint requests.
                await _human_pause_async((1.0, 1.5))
                try:
                    await asyncio.to_thread(client.warm_account)
                except DomainError as exc:
                    store.view(order.id).last_error = exc.message
                    store.update_status(order.id, OrderStatus.REJECTED)
                    _release_create_failure(claim_dir, order.id, idempotency_key)
                    raise _from_domain(exc, session_reissued=session_reissued, nonteam=plan is not Plan.TEAM)
                except Exception:
                    logger.warning("warm_account_failed_continue")
                raw_session = _session_json(client, exit_country)
                item.session = raw_session
                item.user_agent = client.user_agent
                item.device_id = client.device_id
                vault.put(order.id, item)
        finally:
            client.close()
            logger.info("stage_timing stage=reissue duration_ms=%.1f", (time.perf_counter() - stage_started) * 1000)
        _mark(progress, "reissued" if cross_country else "session_refreshed")
    else:
        if not access_token:
            _release_create_failure(claim_dir, order.id, idempotency_key)
            raise _error(401, ErrorCode.INVALID_SESSION, "JSON 中没有 accessToken")
        if at_expired:
            _release_create_failure(claim_dir, order.id, idempotency_key)
            raise _error(
                401,
                ErrorCode.SESSION_EXPIRED,
                "accessToken 已过期且缺少 sessionToken/refresh_token，请重新登录导出完整 Session",
            )
        item = remember(raw_session, bundle.user_agent, bundle.device_id)

    view = store.view(order.id)
    view.route_profile = str(route_plan.profile)
    if route_plan.use_browser_context:
        _mark(progress, "browser_decode")
        decode_started = time.perf_counter()
        if not browser_decode_enabled():
            if not browser_decode_fallback_a():
                store.update_status(order.id, OrderStatus.REJECTED)
                view.failure_code = "browser_decode_unavailable"
                view.last_error = "浏览器解码未启用，Route C 已停止。"
                _release_create_failure(claim_dir, order.id, idempotency_key)
                raise HTTPException(503, detail={
                    "code": "browser_decode_unavailable",
                    "message": "浏览器解码未启用，Route C 已停止。",
                })
            route_plan = RoutePlan.for_profile("A")
            view.route_profile = "A"
        else:
            try:
                decoder = PlaywrightBrowserDecoder(
                    headless=os.environ.get("GSS_BROWSER_HEADLESS", "0").strip().lower()
                    in {"1", "true", "yes", "on"},
                    timeout_ms=int(os.environ.get("GSS_BROWSER_TIMEOUT_MS", "30000") or "30000"),
                    target_url=browser_decode_url(),
                )
                browser_context = await decoder.decode(target_url=browser_decode_url())
            except BrowserDecodeUnavailable:
                if not browser_decode_fallback_a():
                    store.update_status(order.id, OrderStatus.REJECTED)
                    view.failure_code = "browser_decode_unavailable"
                    view.last_error = "浏览器解码失败，Route C 已停止。"
                    _release_create_failure(claim_dir, order.id, idempotency_key)
                    raise HTTPException(503, detail={
                        "code": "browser_decode_unavailable",
                        "message": "浏览器解码失败，Route C 已停止。",
                    })
                route_plan = RoutePlan.for_profile("A")
                view.route_profile = "A"
                browser_context = None
            if browser_context is not None:
                item.browser_context = browser_context.vault_dict()
                item.purchase_attempt_id = order.purchase_attempt_id
                vault.put(order.id, item)
        logger.info("stage_timing stage=browser_decode duration_ms=%.1f", (time.perf_counter() - decode_started) * 1000)
        _mark(progress, "browser_decoded" if browser_context is not None else "browser_decode_skipped")
    # 重颁完成到发起建单之间保持拟人节奏（默认 1.5–2.5s，可被 GSS_WARMUP_INTERVAL 覆盖）。
    if (need_st_reissue or need_oauth_reissue) and not use_fake_adapters():
        await _human_pause_async()
    store.add_job(Job(order_id=order.id, stage="checkout"))
    item.profile = profile_from_session_ua(item.user_agent) or _pick_profile(item.device_id)
    if verified is not None:
        item.billing = dict(verified)
    else:
        item.billing = address_for(country)
        item.billing["email"] = email_from_access_token(access_token) or ""

    if pipeline is None:
        pipeline = build_pipeline()
    checkout_started = time.perf_counter()
    _mark(progress, "creating_checkout")
    try:
        if verified is not None:
            require_verified_billing(order_billing=verified, verification_id=verified.verification_id,
                                    profile=verified)
        checkout = None
        for checkout_attempt in range(2):
            try:
                checkout = await _create_checkout_once(
                    pipeline,
                    proxy=exit_proxy or "",
                    session_reissued=session_reissued,
                    session=raw_session,
                    plan=plan,
                    billing_country=country,
                    exit_country=exit_country,
                    mode=mode,
                    promo_code=promo_code,
                    promo_campaign_id=promo_campaign_id,
                    workspace_name=workspace,
                    seat_quantity=seats,
                    price_interval=interval,
                    browser_context=browser_context,
                    purchase_attempt_id=order.purchase_attempt_id,
                    **({"verified_billing": verified} if verified is not None else {}),
                )
                break
            except DomainError as exc:
                if (checkout_attempt == 0 and exit_proxy and runtime_flags.get_bool("checkout_rotate")
                        and _is_retryable_checkout_tunnel(exc)):
                    logger.warning("checkout_proxy_rotating code=proxy_tunnel_failed")
                    exit_proxy = rotate_proxy_sid(exit_proxy, force=True)
                    # Re-issue AT on the newly rotated exit IP when ST/OAuth is available
                    # so OpenAI does not see an AT minted on the previous SID's IP.
                    if (not use_fake_adapters()
                            and (need_st_reissue or need_oauth_reissue or _reissue_on_exit_enabled())):
                        cur_bundle = parse_session_bundle(raw_session)
                        if cur_bundle.session_token or cur_bundle.refresh_token:
                            reissued_client = None
                            try:
                                if cur_bundle.session_token:
                                    rkw: Dict[str, Any] = {"proxy": exit_proxy, "country": exit_country}
                                    if _reissue_keep_ua():
                                        rkw["preserve_user_agent"] = True
                                    reissued_client = await asyncio.to_thread(
                                        ChatGPTClient.reauthorize_for_country, raw_session, **rkw,
                                    )
                                else:
                                    reissued_client = await asyncio.to_thread(
                                        ChatGPTClient.reauthorize_oauth, raw_session,
                                        proxy=exit_proxy, country=exit_country,
                                    )
                                raw_session = _session_json(reissued_client, exit_country)
                                resolved_ua = (
                                    getattr(reissued_client, "user_agent", "")
                                    or getattr(reissued_client, "ua", "")
                                )
                                item.user_agent = resolved_ua
                                item.device_id = reissued_client.device_id
                                item.profile = (
                                    profile_from_session_ua(item.user_agent)
                                    or _pick_profile(item.device_id)
                                )
                            finally:
                                if reissued_client is not None:
                                    reissued_client.close()
                            await _human_pause_async()
                    item.proxy = exit_proxy
                    item.session = raw_session
                    vault.put(order.id, item)
                    view.proxy_rotations += 1
                    view.proxy_last_sid = proxy_exit_sid(exit_proxy)
                    view.proxy_last_exit_ip = None
                    store.persist(order.id)
                    _mark(progress, "checkout_retry_started")
                    continue
                raise
        if checkout is None:
            raise _error(502, ErrorCode.CHECKOUT_UPSTREAM_ERROR, "建单未返回结果")
    except HTTPException:
        store.update_status(order.id, OrderStatus.REJECTED)
        raise
    except DomainError as exc:
        logger.info("stage_timing stage=create_checkout duration_ms=%.1f", (time.perf_counter() - checkout_started) * 1000)
        view = store.view(order.id)
        view.failure_code = _checkout_failure_code(exc)
        if isinstance(getattr(exc, "_http_trace", None), dict):
            view.failure_trace = exc._http_trace
        if view.failure_code == "already_subscribed":
            error = _error(400, ErrorCode.ALREADY_SUBSCRIBED, "该账号已有订阅，已停止建单。",
                           session_reissued=session_reissued, diagnostics=exc.detail)
        elif view.failure_code in {"unusual_activity", "invalid_session"}:
            # 该出口已被上游按风控拒绝：隔离 SID，但绝不重试本单。
            _quarantine_exit(exit_proxy, view.failure_code)
            # Never persist upstream prose or advice to replay a rejected session.
            invalid = view.failure_code == "invalid_session"
            error = _error(401 if invalid else 400,
                           ErrorCode.INVALID_SESSION if invalid else ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                           "会话已失效，已停止建单。" if invalid else "上游报告异常流量，已停止建单。",
                           session_reissued=session_reissued, diagnostics=exc.detail)
        else:
            error = _from_domain(exc, session_reissued=session_reissued, nonteam=plan is not Plan.TEAM)
        error._checkout_order_id = order.id  # internal correlation only; not in the HTTP body
        view.last_error = error.detail["message"]
        store.update_status(order.id, OrderStatus.REJECTED)  # persists failure fields together
        logger.info(
            "checkout_failed id=%s plan=%s country=%s code=%s",
            order.id,
            plan,
            country,
            exc.code.value,
        )
        raise error

    logger.info("stage_timing stage=create_checkout duration_ms=%.1f", (time.perf_counter() - checkout_started) * 1000)
    _mark(progress, "checkout_created")
    if verified is not None:
        # Adapters must return the exact billing they used. A missing/mismatched
        # response or revoked payer profile is never turned into an accepted order.
        try:
            if not getattr(checkout, "billing", None):
                raise HTTPException(409, "未取得结账账单核对结果，已停止付款。")
            require_verified_billing(checkout.billing, order_billing=verified,
                                     verification_id=verified.verification_id, profile=verified)
        except HTTPException:
            store.update_status(order.id, OrderStatus.REJECTED)
            raise
    view = store.view(order.id)
    view.billing_verified = verified is not None
    view.billing_verification_id = verified.verification_id if verified is not None else None
    view.checkout_url = checkout.checkout_url
    view.checkout_session_id = checkout.checkout_session_id
    view.publishable_key = checkout.publishable_key
    view.customer_session_client_secret = checkout.customer_session_client_secret
    view.billing = dict(verified if verified is not None else (checkout.billing or {}))
    view.amount = str(checkout.amount) if checkout.amount is not None else None
    view.currency = checkout.currency
    view.payment_status = "unpaid"
    view.delivery_status = "pending"
    view.tax_delta = str(checkout.tax_delta) if getattr(checkout, "tax_delta", None) is not None else None
    view.status_summary = _default_status_summary("unpaid", "pending")
    view.receipt = _receipt_for(order, view)
    view.checkout_kind = checkout.checkout_kind or ("hosted" if plan is Plan.TEAM else "custom")
    view.confirm_return_url = checkout.confirm_return_url or None
    view.promo_code = promo_code or None
    view.promo_campaign_id = promo_campaign_id or None
    if plan is Plan.TEAM:
        view.seat_quantity = seats
        view.workspace_name = workspace
        view.price_interval = interval
    order.amount = checkout.amount
    secrets = vault.get(order.id)
    if secrets is not None:
        secrets.checkout_session_id = checkout.checkout_session_id or ""
        secrets.publishable_key = checkout.publishable_key or ""
        secrets.billing = dict(view.billing)
        effective_proxy = getattr(checkout, "effective_proxy", None)
        if effective_proxy is not None:
            secrets.proxy = effective_proxy
        effective_session = getattr(checkout, "effective_session", None)
        if effective_session:
            secrets.session = effective_session
            eff_ua = getattr(checkout, "effective_user_agent", None)
            if eff_ua:
                secrets.user_agent = eff_ua
            eff_did = getattr(checkout, "effective_device_id", None)
            if eff_did:
                secrets.device_id = eff_did
            secrets.profile = profile_from_session_ua(secrets.user_agent) or _pick_profile(secrets.device_id)
        vault.put(order.id, secrets)
    store.update_status(order.id, OrderStatus.PENDING_CHECKOUT)
    store.update_status(order.id, OrderStatus.AWAITING_PAYMENT)
    logger.info(
        "checkout_created id=%s plan=%s country=%s amount=%s currency=%s kind=%s seats=%s",
        order.id,
        plan,
        country,
        view.amount,
        view.currency,
        view.checkout_kind,
        view.seat_quantity,
    )

    if payload.payment_method_token:
        assert claim_dir is not None  # validated at the public entry point
        try:
            dispatch = await asyncio.to_thread(purchase_claims.begin_payment, claim_dir, order.id)
        except purchase_claims.ClaimUnavailable:
            raise _error(503, ErrorCode.IDEMPOTENCY_CONFLICT, "暂时无法确认原订单，请勿重新提交付款") from None
        if not dispatch:
            raise _error(409, ErrorCode.IDEMPOTENCY_CONFLICT, "请查询原付款，不要重复确认")
        try:
            return await pay_order(order.id, OrderPayRequest(payment_method_token=payload.payment_method_token))
        finally:
            observed = ("unknown" if order.status is OrderStatus.UNKNOWN else store.view(order.id).payment_status)
            if observed in {"processing", "unknown", "succeeded", "failed"}:
                try:
                    await asyncio.to_thread(purchase_claims.record_observation, claim_dir, order.id, observed)
                except purchase_claims.ClaimUnavailable:
                    # A lost ledger update must not invalidate an already paid receipt.
                    logger.warning("purchase_observation_unavailable order=%s", order.id)
    return _to_out(store.get_order(order.id))  # type: ignore[arg-type]


async def _mark_delivered(order_id: str) -> None:
    """权益已核验后交付，并清掉内存里的 session。"""
    current = store.get_order(order_id)
    if current is None or current.status is not OrderStatus.ACTIVE:
        raise DomainError(ErrorCode.SUBSCRIPTION_NOT_ACTIVE, "权益尚未核验，不能交付")
    view = store.view(order_id)
    if view.payment_status != "succeeded" or (current.plan is Plan.TEAM and (
        not view.subscription or not _plan_matches(current, view.subscription, view)
        or not receipt_matches_order(view.receipt_evidence, checkout_id=view.checkout_session_id,
                                     amount=view.amount, currency=view.currency,
                                     payment_id=view.observed_payment_id)
    )):
        raise DomainError(ErrorCode.SUBSCRIPTION_NOT_ACTIVE, "付款或本单权益尚未核验")
    store.update_status(order_id, OrderStatus.DELIVERED)
    view.last_error = None
    view.next_action = None
    view.delivery_status = "delivered"
    view.status_summary = "扣款成功，订阅已生效"
    view.receipt = _receipt_for(current, view)
    vault.drop(order_id)
    await notify_order_update(order_id, OrderStatus.DELIVERED, {"payment_confirmed": True})


def _team_client(secrets: OrderSecrets, country: str = "") -> ChatGPTClient:
    """Build a client for this order, optionally exiting from the billing country.

    支付出口必须与创建结账时完全一致：Stripe 把 checkout session 绑定在
    创建时的出口上，按账单国家重新对齐会换掉 IP，导致验证码/风控判定异常。
    country 参数保留仅为兼容既有调用方，不再参与出口选择。
    """
    client = ChatGPTClient.from_raw_session(secrets.session, proxy=secrets.proxy or None)
    client.timeout = ORDER_HTTP_TIMEOUT_S
    return client


def _leased_team_client(order_id, secrets, *args):
    if vault.get(order_id) is not secrets:
        raise DomainError(ErrorCode.SESSION_EXPIRED, "订单凭证已过期")
    client = _team_client(secrets, *args)
    client.credentials_current = lambda: vault.get(order_id) is secrets
    return client


def _team_quote(view, context: dict) -> None:
    code = context["currency"].upper()
    zero = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
    divisor = 1 if code in zero else 100
    view.amount = str(Decimal(context["amount_minor"]) / divisor)
    view.currency = code
    view.quote_error = None


@router.post("/orders/{order_id}/team/quote", response_model=OrderOut)
async def quote_team(order_id: str, payload: TeamQuoteRequest) -> OrderOut:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        order = store.get_order(order_id)
        if order is None or order.plan is not Plan.TEAM:
            raise HTTPException(404, "Team 订单不存在")
        if order.status is not OrderStatus.AWAITING_PAYMENT:
            raise HTTPException(409, "支付已提交，请查询支付状态")
        secrets = vault.get(order_id)
        if secrets is None:
            raise _error(401, ErrorCode.SESSION_EXPIRED, "session 已过期")
        view = store.view(order_id)
        if use_fake_adapters():
            return _to_out(order)
        billing = {k: v.strip() for k, v in payload.billing.model_dump().items()} if payload.billing else None
        if billing:
            billing = _aligned_billing(billing, normalize_country(order.billing_country), getattr(secrets, "billing", None))
            billing["country"] = billing["country"].upper()
            if not is_supported(billing["country"]):
                raise HTTPException(400, "不支持的支付账单国家")
        started = time.monotonic()
        client = _leased_team_client(order_id, secrets)
        try:
            quote = await asyncio.to_thread(TeamHostedClient(client).quote,
                session_id=secrets.checkout_session_id, key=secrets.publishable_key,
                billing=billing, currency=view.currency or "", browser_locale=payload.browser_locale,
                browser_timezone=payload.browser_timezone)
            _team_quote(view, quote)
            logger.info("team_quote_ok id=%s amount=%s currency=%s elapsed=%.2f",
                        order_id, view.amount, view.currency, time.monotonic() - started)
        except DomainError as exc:
            view.quote_error = redact_sensitive(exc.message)
            logger.warning("team_quote_failed id=%s code=%s elapsed=%.2f error=%s", order_id,
                           exc.code.value, time.monotonic() - started, redact_sensitive(exc.message)[:160])
        except Exception as exc:
            view.quote_error = "读取 Stripe 报价时连接中断，本步骤未提交付款确认"
            logger.warning("team_quote_error id=%s type=%s elapsed=%.2f", order_id, type(exc).__name__,
                           time.monotonic() - started)
        finally:
            client.close()
        return _to_out(order)


@router.post("/orders/{order_id}/team/prepare", tags=["orders"])
async def prepare_team(order_id: str, payload: TeamPrepareRequest) -> dict:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        order = store.get_order(order_id)
        if order is None or order.plan is not Plan.TEAM:
            raise HTTPException(404, "Team 订单不存在")
        if order.status is not OrderStatus.AWAITING_PAYMENT:
            raise HTTPException(409, "已提交的订单请查询支付状态，不要重新提交")
        secrets = vault.get(order_id)
        if secrets is None:
            raise _error(401, ErrorCode.SESSION_EXPIRED, "session 已过期")
        billing = {k: v.strip() for k, v in payload.billing.model_dump().items()}
        billing = _aligned_billing(billing, normalize_country(order.billing_country), getattr(secrets, "billing", None))
        billing["country"] = billing["country"].upper()
        if not is_supported(billing["country"]):
            raise HTTPException(400, "不支持的支付账单国家")
        if use_fake_adapters():
            context = {"context_id": "fixture-context", "session_id": secrets.checkout_session_id,
                "publishable_key": secrets.publishable_key, "eid": "NA", "amount_minor": 115000,
                "currency": "egp", "billing": billing, "prepared_at": time.time(),
                "checkout_config_id": "fixture-config", "elements_session_id": "elements_fixture",
                "client_session_id": "fixture-client", "guid": "fixture-guid", "muid": "fixture-muid", "sid": "fixture-sid",
                "passive_captcha": {}}
        else:
            started = time.monotonic()
            client = _leased_team_client(order_id, secrets)
            try:
                context = await asyncio.to_thread(TeamHostedClient(client).prepare,
                    session_id=secrets.checkout_session_id, key=secrets.publishable_key, billing=billing,
                    currency=store.view(order_id).currency or "", browser_locale=payload.browser_locale,
                    browser_timezone=payload.browser_timezone,
                    on_quote=lambda quote: _team_quote(store.view(order_id), quote))
                logger.info("team_prepare_ok id=%s amount=%s currency=%s elapsed=%.2f",
                            order_id, store.view(order_id).amount, store.view(order_id).currency,
                            time.monotonic() - started)
            except DomainError as exc:
                store.view(order_id).last_error = redact_sensitive(exc.message)
                logger.warning("team_prepare_failed id=%s code=%s elapsed=%.2f confirm_submitted=False error=%s",
                               order_id, exc.code.value, time.monotonic() - started,
                               redact_sensitive(exc.message)[:160])
                raise _from_domain(exc)
            finally:
                client.close()
        secrets.team_context = context
        secrets.billing = billing
        vault.put(order_id, secrets)
        view = store.view(order_id)
        view.billing = billing
        view.last_error = None
        _team_quote(view, context)
        return {"order": _to_out(order).model_dump(), "context": {k: context[k] for k in (
            "context_id", "session_id", "publishable_key", "eid", "amount_minor", "currency",
            "checkout_config_id", "elements_session_id", "client_session_id", "guid", "muid", "sid",
            "passive_captcha", "customer_email") if k in context}}


@router.post("/orders/{order_id}/team/poll", response_model=OrderOut)
async def poll_team(order_id: str) -> OrderOut:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        return await _observe_team_locked(order_id)


async def _observe_team_locked(order_id: str) -> OrderOut:
    """The caller owns the order lock; observing the hosted checkout never submits it."""
    order = store.get_order(order_id)
    if order is None or order.plan is not Plan.TEAM:
        raise HTTPException(404, "Team 订单不存在")
    if order.status in {OrderStatus.DELIVERED, OrderStatus.FAILED, OrderStatus.REJECTED}:
        return _to_out(order)
    view = store.view(order_id)
    secrets = vault.get(order_id)
    paid = view.payment_status == "succeeded"
    if secrets is None:
        if paid or view.payment_status == "processing" or order.status is OrderStatus.UNKNOWN:
            return _to_out(order)  # retain the last observation; no authenticated I/O
        raise _error(401, ErrorCode.SESSION_EXPIRED, "session 已过期")
    if paid and (use_fake_adapters() or getattr(view.receipt_evidence, "workspace_id", None)):
        await _verify_after_payment(order, view)
        return _to_out(order)
    if use_fake_adapters():
        return _to_out(order)
    client = _leased_team_client(order_id, secrets)
    def read_original():
        if vault.get(order_id) is not secrets:
            raise DomainError(ErrorCode.SESSION_EXPIRED, "订单凭证已过期")
        return TeamHostedClient(client).poll(session_id=secrets.checkout_session_id, key=secrets.publishable_key)

    try:
        payload = await asyncio.to_thread(read_original)
    except Exception as exc:
        if paid or view.payment_status == "processing" or order.status is OrderStatus.UNKNOWN:
            logger.warning("team_recheck_unavailable id=%s type=%s", order_id, type(exc).__name__)
            return _to_out(order)
        if isinstance(exc, DomainError):
            raise _from_domain(exc)
        raise _error(502, ErrorCode.CHECKOUT_UPSTREAM_ERROR, "暂时无法查询原付款，请稍后查询") from None
    finally:
        client.close()
    result = hosted_payment_result(payload, secrets.checkout_session_id, view.checkout_url or "")
    # Poll only observes payment; it must never submit a challenge or confirm a charge.
    if paid:
        # Missing workspace linkage may arrive later; a stale poll must not undo settled money.
        if result.status is PaymentStatus.SUCCEEDED and _accept_receipt(order, result.receipt_evidence):
            await _verify_after_payment(order, view)
        return _to_out(order)
    if result.status is PaymentStatus.SUCCEEDED:
        if not _accept_receipt(order, result.receipt_evidence):
            await _mark_payment_unknown(order)
            return _to_out(order)
        await _verify_after_payment(order, view)
    elif result.hosted_open_unpaid:
        # Only explicit untouched checkout evidence can leave a new order payable.
        return _to_out(order)
    elif result.status is PaymentStatus.ERROR:
        await _mark_payment_unknown(order)
    elif (result.provider_ref and result.provider_ref.startswith("pi_") and view.observed_payment_id
          and result.provider_ref != view.observed_payment_id):
        await _mark_payment_unknown(order)
    elif result.status is PaymentStatus.DECLINED:
        store.update_status(order_id, OrderStatus.FAILED)
        view.last_error = redact_sensitive(result.message) if result.message else None
        view.next_action = None
        _set_payment_status(order_id, "failed", summary=view.last_error)
        await notify_order_update(order_id, OrderStatus.FAILED, {"payment_status": "failed"})
    elif result.status is PaymentStatus.REQUIRES_ACTION:
        if order.status in {OrderStatus.AWAITING_PAYMENT, OrderStatus.UNKNOWN}:
            store.update_status(order_id, OrderStatus.PAYING)
        store.update_status(order_id, OrderStatus.REQUIRES_ACTION)
        view.next_action = result.next_action
        _set_payment_status(order_id, "processing")
        view.last_error = redact_sensitive(result.message) if result.message else None
    else:
        if order.status is OrderStatus.AWAITING_PAYMENT:
            store.update_status(order_id, OrderStatus.PAYING)
        if view.payment_status != "processing":
            _set_payment_status(order_id, "processing")
    if (result.status in {PaymentStatus.PROCESSING, PaymentStatus.REQUIRES_ACTION}
            and result.provider_ref and result.provider_ref.startswith("pi_")
            and not view.observed_payment_id):
        view.observed_payment_id = result.provider_ref
    # An abandoned/challenged attempt is not a fresh checkout, even on a later open poll.
    return _to_out(order)


@router.post(
    "/orders/{order_id}/pay",
    response_model=OrderOut,
    responses={400: {"model": ErrorOut}, 401: {"model": ErrorOut}, 404: {"model": ErrorOut}},
)
async def pay_order(order_id: str, payload: OrderPayRequest) -> OrderOut:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        return await _pay_order(order_id, payload)


async def _pay_order(order_id: str, payload: OrderPayRequest) -> OrderOut:
    order = store.get_order(order_id)
    if order is None:
        raise _error(status.HTTP_404_NOT_FOUND, ErrorCode.COMPLIANCE_BLOCKED, "订单不存在")
    if order.status in {OrderStatus.DELIVERED, OrderStatus.ACTIVE}:
        return _to_out(order)
    early_view = store.view(order_id)
    if early_view.payment_status == "succeeded":
        # 已扣款成功：无论交付是否完成，都不允许再次提交支付。
        return _to_out(order)
    if order.status is not OrderStatus.AWAITING_PAYMENT:
        raise HTTPException(409, "支付已提交，请查询状态，不要重复确认")
    secrets = vault.get(order_id)
    if secrets is None:
        raise _error(status.HTTP_401_UNAUTHORIZED, ErrorCode.SESSION_EXPIRED, "session 已过期，请重新提交")
    _assert_saved_card_resolved(secrets)
    verified = None
    pipeline = None
    if order.plan is not Plan.TEAM:
        verified = _verified_order_billing(order, secrets, _billing_claim(payload))
        pipeline = build_pipeline()
        _require_offline_instrument(pipeline, payload.payment_method_token)

    if order.plan is Plan.TEAM and not use_fake_adapters():
        context = secrets.team_context
        address = {k: v.strip() for k, v in payload.billing.model_dump().items()} if payload.billing else dict(secrets.billing)
        address = _aligned_billing(address, normalize_country(order.billing_country), getattr(secrets, "billing", None))
        address["country"] = address.get("country", "").upper()
        address.setdefault("email", "")
        if not context or context.get("context_id") != payload.team_context_id:
            raise HTTPException(409, "Team 会话上下文不匹配，请重新初始化")
        if time.time() - context["prepared_at"] > 600 or address != context["billing"]:
            raise HTTPException(409, "Team 账单或会话已变更，请重新初始化")
        # Server-side card submission acquires its own pm_ and may run without a browser captcha token.
        if (payload.card is None and context.get("passive_captcha", {}).get("site_key")
                and not payload.passive_captcha_token):
            raise HTTPException(400, "请先完成当前会话的 Stripe 验证")

        if payload.card is not None and payload.payment_method_token:
            raise HTTPException(400, "卡号与 payment_method 只能二选一")

        observed = await _observe_team_locked(order_id)
        if order.status is not OrderStatus.AWAITING_PAYMENT:
            return observed  # The hosted page may have paid without first polling this API.

        if payload.card is not None:
            detected_brand = detect_card_brand(payload.card.number)
            if detected_brand:
                store.view(order_id).card_brand = detected_brand
                store.persist(order_id)
            started = time.monotonic()
            client = _leased_team_client(order_id, secrets, address.get("country", ""))
            try:
                pm_id = await asyncio.to_thread(
                    TeamHostedClient(client).tokenize,
                    card=payload.card.model_dump(), billing=address,
                    publishable_key=secrets.publishable_key, session_id=secrets.checkout_session_id,
                    checkout_config_id=context.get("checkout_config_id", ""),
                    device={"guid": context.get("guid", ""), "muid": context.get("muid", ""),
                            "sid": context.get("sid", ""),
                            "client_session_id": context.get("client_session_id", "")},
                    captcha_token=payload.passive_captcha_token or "",
                )
                logger.info("team_tokenize_ok id=%s elapsed=%.2f", order_id, time.monotonic() - started)
            except DomainError as exc:
                logger.warning("team_tokenize_failed id=%s code=%s elapsed=%.2f error=%s", order_id,
                               exc.code.value, time.monotonic() - started,
                               redact_sensitive(exc.message)[:160])
                raise _from_domain(exc)
            finally:
                client.close()
            payload = payload.model_copy(update={"payment_method_token": pm_id})

    if order.plan is Plan.TEAM and payload.billing is not None:
        custom = {k: v.strip() for k, v in payload.billing.model_dump().items()}
        custom = _aligned_billing(custom, normalize_country(order.billing_country), getattr(secrets, "billing", None))
        custom["country"] = custom["country"].upper()
        # order.billing_country 是创建结账的地区（决定币种）；
        # custom.country 是支付账单国家，独立用于 PaymentMethod 和 Stripe tax_region。
        if order.plan is Plan.TEAM:
            if not is_supported(custom["country"]):
                raise _error(
                    status.HTTP_400_BAD_REQUEST,
                    ErrorCode.BILLING_COUNTRY_MISMATCH,
                    f"不支持的账单国家: {custom['country']}",
                )
        secrets.billing = custom
        vault.put(order_id, secrets)

    view = store.view(order_id)
    if order.plan is Plan.TEAM and payload.billing is not None:
        view.billing = dict(secrets.billing)
    if vault.get(order_id) is not secrets:
        raise _error(401, ErrorCode.SESSION_EXPIRED, "付款尚未提交，订单凭证已过期")
    if pipeline is None:
        pipeline = build_pipeline()
    store.update_status(order_id, OrderStatus.PAYING)
    _set_payment_status(order_id, "processing")
    try:
        payment = await pipeline.pay(
            checkout_url=view.checkout_url or "",
            country=order.billing_country,
            mode=order.mode,
            payment_method_token=payload.payment_method_token,
            credentials_current=lambda: vault.get(order_id) is secrets,
            session=secrets.session,
            checkout_session_id=view.checkout_session_id or secrets.checkout_session_id,
            publishable_key=view.publishable_key or secrets.publishable_key,
            checkout_proxy=secrets.proxy or None,
            approve_proxy=secrets.proxy or None,
            billing=verified if verified is not None else (secrets.billing or None),
            plan=str(order.plan), team_context=secrets.team_context,
            passive_captcha_token=payload.passive_captcha_token, rv_timestamp=payload.rv_timestamp,
            session_reissued=getattr(secrets, "session_reissued", False),
            exit_country=getattr(secrets, "exit_country", "") or None,
        )
    except asyncio.CancelledError:
        # Cancellation cannot retract a confirmation already sent to the provider.
        await _mark_payment_unknown(order)
        raise
    except DomainError as exc:
        if exc.code is not ErrorCode.PAYMENT_DECLINED:
            await _mark_payment_unknown(order)
            return _to_out(order)
        store.update_status(order_id, OrderStatus.FAILED)
        error = _from_domain(exc, session_reissued=view.session_reissued_for_country, nonteam=order.plan is not Plan.TEAM)
        message = error.detail["message"]
        _set_payment_status(order_id, "failed", summary=f"支付失败：{message[:120]}")
        view.last_error = message
        await notify_order_update(order_id, OrderStatus.FAILED, {"error": message})
        raise error
    except Exception as exc:
        logger.warning("payment_unconfirmed id=%s type=%s", order_id, type(exc).__name__)
        await _mark_payment_unknown(order)
        return _to_out(order)

    new_status = apply_payment_result(OrderStatus.PAYING, payment.status)
    if new_status is OrderStatus.UNKNOWN:
        await _mark_payment_unknown(order)
        return _to_out(order)
    store.update_status(order_id, new_status)
    view.next_action = payment.next_action
    view.last_error = (
        redact_sensitive(payment.message)
        if payment.message and payment.status is not PaymentStatus.SUCCEEDED else None
    )
    if payment.status is PaymentStatus.SUCCEEDED:
        if not _accept_receipt(order, payment.receipt_evidence):
            await _mark_payment_unknown(order)
            return _to_out(order)
    elif payment.status in {PaymentStatus.PROCESSING, PaymentStatus.REQUIRES_ACTION}:
        if payment.provider_ref and payment.provider_ref.startswith("pi_"):
            view.observed_payment_id = payment.provider_ref
        _set_payment_status(order_id, "processing", summary="支付处理中，请勿重复提交")
    elif payment.status is PaymentStatus.DECLINED:
        _set_payment_status(order_id, "failed", summary=view.last_error or "支付失败")
    logger.info(
        "pay_finished id=%s country=%s payment=%s order=%s error=%s",
        order_id,
        order.billing_country,
        payment.status,
        new_status,
        view.last_error or "",
    )
    
    if new_status is OrderStatus.VERIFYING:
        await _verify_after_payment(order, view)
    else:
        await notify_order_update(order_id, new_status, {"payment_status": view.payment_status})
    return _to_out(store.get_order(order_id))  # type: ignore[arg-type]


@router.post(
    "/orders/{order_id}/verify",
    response_model=OrderOut,
    responses={401: {"model": ErrorOut}, 404: {"model": ErrorOut}},
)
async def verify_order(order_id: str, paid: bool = False) -> OrderOut:
    order = store.get_order(order_id)
    if order is None:
        raise _error(status.HTTP_404_NOT_FOUND, ErrorCode.COMPLIANCE_BLOCKED, "订单不存在")
    if order.status in {OrderStatus.ACTIVE, OrderStatus.DELIVERED}:
        return _to_out(order)
    if order.plan is Plan.TEAM and not use_fake_adapters():
        return await poll_team(order_id)
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        return await _verify_order(order_id, paid=paid)


async def _verify_order(order_id: str, paid: bool = False) -> OrderOut:
    order = store.get_order(order_id)
    if order is None:
        raise _error(404, ErrorCode.COMPLIANCE_BLOCKED, "订单不存在")
    if order.status in {OrderStatus.ACTIVE, OrderStatus.DELIVERED}:
        return _to_out(order)
    view = store.view(order_id)
    secrets = vault.get(order_id)
    if view.payment_status == "succeeded":
        await _verify_after_payment(order, view)
        return _to_out(order)
    if (secrets and secrets.saved_card_context and secrets.saved_card_context.get("protocol_flow")
            and order.status is not OrderStatus.UNKNOWN and view.payment_status == "processing"):
        raise HTTPException(409, "请从原协议入口查询这笔付款")
    if (view.payment_status == "processing" or order.status in {
        OrderStatus.UNKNOWN, OrderStatus.FAILED, OrderStatus.REJECTED
    } or order.plan is Plan.TEAM):
        # Generic subscription activity cannot resolve an unknown charge or an unpaid Team order.
        return _to_out(order)
    if secrets is None:
        raise _error(status.HTTP_401_UNAUTHORIZED, ErrorCode.SESSION_EXPIRED, "session 已过期，请重新提交")
    pipeline = build_pipeline()
    if secrets.saved_card_context:
        if secrets.saved_card_context.get("protocol_flow"):
            raise HTTPException(409, "本订单已进入协议绑卡支付，请使用协议状态查询")
        if secrets.saved_card_context.get("state") != "checkout_ready":
            raise HTTPException(409, "请先完成保存卡片并刷新结账链接，再查询订阅")
        # A status query before payment must leave this checkout available.
        try:
            refreshed_session, info = await asyncio.to_thread(_verify_saved_checkout, pipeline, order_id, secrets)
            secrets.session = refreshed_session
        except DomainError as exc:
            if exc.code in {ErrorCode.INVALID_SESSION, ErrorCode.SESSION_EXPIRED, ErrorCode.CHECKOUT_UPSTREAM_ERROR}:
                raise _from_domain(exc, nonteam=order.plan is not Plan.TEAM)
            view.last_error = "订阅状态暂未读到，请稍后重新查询"
            return _to_out(order)
        # Activity on the account alone cannot attest payment for this checkout.
        if info.active:
            view.subscription = None
            await _mark_payment_unknown(order)
        else:
            view.subscription = info
            view.last_error = "尚未核实本单付款"
    # An unpaid checkout or a client-supplied paid flag provides no settlement evidence.
    return _to_out(order)




@router.post("/orders/{order_id}/auto-renew/cancel", response_model=OrderOut)
async def cancel_auto_renew(order_id: str) -> OrderOut:
    """Manual retry of the post-payment auto-renew cancellation.

    Deliberately does not accept a new session: the operation must reuse the
    original session/proxy, otherwise it can touch the wrong account or region.
    """
    order = store.get_order(order_id)
    if order is None:
        raise _error(status.HTTP_404_NOT_FOUND, ErrorCode.COMPLIANCE_BLOCKED, "订单不存在")
    view = store.view(order_id)
    if view.payment_status != "succeeded":
        raise HTTPException(409, detail={"code": "auto_renew_not_paid",
                                         "message": "订单尚未确认扣款成功，不能取消续费"})
    if not view.auto_cancel_renew:
        view.auto_renew_status = "disabled"
        return _to_out(order)
    if view.auto_renew_status == "canceled":
        return _to_out(order)
    secrets = vault.get(order_id)
    if secrets is None:
        view.auto_renew_status = "manual_required"
        view.auto_renew_error = "vault_expired"
        store.persist(order_id)
        raise HTTPException(409, detail={"code": "vault_expired",
                                         "message": "原订单会话上下文已过期，请人工登录账号取消续费"})
    view.auto_renew_status = "canceling_auto_renew"
    view.auto_renew_attempts = 0
    store.persist(order_id)
    last_error = ""
    for attempt in range(3):
        view.auto_renew_attempts = attempt + 1
        try:
            client = await asyncio.to_thread(
                ChatGPTClient.from_raw_session, secrets.session, proxy=secrets.proxy,
            )
            try:
                await asyncio.to_thread(client.cancel_auto_renew,
                                        plan_type=normalize_plan_name(str(order.plan)))
            finally:
                client.close()
            view.auto_renew_status = "canceled"
            view.auto_renew_canceled_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            view.auto_renew_error = None
            store.persist(order_id)
            return _to_out(order)
        except Exception as exc:  # noqa: BLE001
            last_error = redact_sensitive(str(exc))[:300]
            if "未配置" in last_error:
                try:
                    from ..auto_renew import cancel_account_auto_renew
                    portal_res = await asyncio.to_thread(
                        cancel_account_auto_renew,
                        secrets.session,
                        plan=normalize_plan_name(str(order.plan)),
                        proxy=secrets.proxy,
                    )
                    if portal_res.get("canceled"):
                        view.auto_renew_status = "canceled"
                        view.auto_renew_canceled_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        view.auto_renew_error = None
                        store.persist(order_id)
                        return _to_out(order)
                    last_error = redact_sensitive(str(portal_res.get("message") or last_error))[:300]
                except Exception as portal_exc:  # noqa: BLE001
                    last_error = redact_sensitive(str(portal_exc))[:300]
                break
            if "Session" in last_error:
                break
            if attempt < 2:
                await asyncio.sleep(1 if attempt == 0 else 4)
    view.auto_renew_status = "manual_required"
    view.auto_renew_error = last_error or "cancel_failed"
    store.persist(order_id)
    return _to_out(order)


@router.get(
    "/orders/{order_id}",
    response_model=OrderOut,
    responses={404: {"model": ErrorOut}},
)
async def get_order(order_id: str) -> OrderOut:
    order = store.get_order(order_id)
    if order is None:
        raise _error(status.HTTP_404_NOT_FOUND, ErrorCode.COMPLIANCE_BLOCKED, "订单不存在")
    return _to_out(order)

# ── 纯协议支付（不依赖浏览器） ────────────────────────────────
class ProtocolStepsIn(AddressedRequest):
    """协议链路的可选环节。全部默认关闭 = 与 scripts/run_cards.py 一致的批量模式。"""

    fingerprint: bool = False   # m.stripe.com/6 × 4 指纹注册 + fetch_allowed_origins
    dwell: bool = False         # init 后停留 10–16s
    elements: bool = False      # elements/sessions
    tax_region: bool = False    # 提交 tax_region[country]


class ProtocolPayRequest(AddressedRequest):
    """协议支付的入参。

    两种取 pm_ 的方式，二选一：
      · card              服务端直接向 Stripe 提交卡号（全协议）
      · payment_method_id 浏览器 Stripe 输入框已换好的 pm_（混合模式）
        卡数据走 Stripe iframe，与真实用户一致，hCaptcha/Radar 判定更宽松。
    """
    card: Optional[CardIn] = None
    payment_method_id: Optional[str] = Field(default=None, max_length=80)
    billing: BillingIn
    proxy: Optional[str] = Field(default=None, max_length=300)
    publishable_key: Optional[str] = Field(default=None, max_length=120)
    steps: ProtocolStepsIn = ProtocolStepsIn()


def resolve_protocol_payment_method(payload: "ProtocolPayRequest") -> str:
    """校验 card / payment_method_id 只给一个，返回浏览器给的 pm_（没有就空串）。

    单独抽出来是为了能离线测：pay-protocol 本身会真的打 Stripe，集成测试不该碰。
    """
    pm_id = (payload.payment_method_id or "").strip()
    if not payload.card and not pm_id:
        raise HTTPException(400, "需要 card 或 payment_method_id 其一")
    if payload.card and pm_id:
        raise HTTPException(400, "card 与 payment_method_id 只能二选一")
    if pm_id and not pm_id.startswith("pm_"):
        raise HTTPException(400, "payment_method_id 格式不对（应以 pm_ 开头）")
    return pm_id


class ProtocolPayOut(BaseModel):
    """纯协议支付的结果。"""
    state: str
    code: Optional[str] = None
    decline_code: Optional[str] = None
    message: Optional[str] = None
    intent_status: Optional[str] = None
    charge_id: Optional[str] = None
    payment_method_id: Optional[str] = None
    payment_status: str = "unpaid"
    delivery_status: str = "pending"
    payment_intent_id: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    tax_delta: Optional[str] = None
    plan_type: Optional[str] = None
    status_summary: Optional[str] = None
    receipt: Optional[Dict[str, Any]] = None


def _protocol_out(order_id: str, *, state: str, message: Optional[str] = None, **extra: Any) -> ProtocolPayOut:
    order = store.get_order(order_id)
    view = store.view(order_id)
    summary = view.status_summary or _default_status_summary(view.payment_status, view.delivery_status)
    receipt = _receipt_for(order, view) if order else None
    if order and order.plan is not Plan.TEAM:
        receipt = receipt_view(receipt)
        extra.pop("payment_method_id", None)
        extra = {k: read_text(v) for k, v in extra.items()}
        message, summary = read_text(message), read_text(summary)
    return ProtocolPayOut(
        state=state,
        message=redact_sensitive(message) if message is not None else summary,
        payment_status=view.payment_status,
        delivery_status=view.delivery_status,
        payment_intent_id=view.payment_intent_id,
        amount=view.paid_amount or view.amount,
        currency=view.paid_currency or view.currency,
        tax_delta=view.tax_delta,
        plan_type=view.plan_type or (str(order.plan) if order else None),
        status_summary=summary,
        receipt=receipt,
        **extra,
    )


@router.post(
    "/orders/{order_id}/pay-protocol",
    response_model=ProtocolPayOut,
    responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut},
               409: {"model": ErrorOut}, 502: {"model": ErrorOut}},
)
async def pay_order_protocol(order_id: str, payload: ProtocolPayRequest) -> ProtocolPayOut:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        return await _pay_order_protocol(order_id, payload)


async def _pay_order_protocol(order_id: str, payload: ProtocolPayRequest) -> ProtocolPayOut:
    """纯 HTTP 协议完成 Stripe Checkout 支付，不需要浏览器。

    链路：指纹注册 → init → elements/sessions → 逐字段地址
          → hCaptcha(passive + challenge) → payment_methods → confirm → poll
    """
    order = store.get_order(order_id)
    if order is None:
        raise _error(status.HTTP_404_NOT_FOUND, ErrorCode.COMPLIANCE_BLOCKED, "订单不存在")
    view = store.view(order_id)
    if view.payment_status == "succeeded":
        state = "paid" if view.delivery_status == "delivered" else "pending"
        return _protocol_out(order_id, state=state, message=view.status_summary)
    if order.status is not OrderStatus.AWAITING_PAYMENT:
        raise HTTPException(409, "支付已提交，请查询订单状态，不要重复确认")
    checkout_url = (getattr(view, "checkout_url", "") or "") if view else ""
    if not checkout_url:
        raise HTTPException(409, "订单缺少 checkout_url，请先创建结账")

    settings = load_settings()
    captcha_key = getattr(settings, "captcha_api_key", "") or ""
    captcha_url = getattr(settings, "captcha_api_url", "https://api.yescaptcha.com")

    secrets = vault.get(order_id)
    if secrets is None:
        raise _error(401, ErrorCode.SESSION_EXPIRED, "订单凭证已过期，请重新创建结账")
    _assert_saved_card_resolved(secrets)
    verified = None
    if order.plan is not Plan.TEAM:
        verified = _verified_order_billing(order, secrets, _billing_claim(payload))
        if payload.payment_method_id:
            # This route constructs a protocol client directly, even in fake mode.
            raise _unverified_instrument()
    session_raw = secrets.session or ""
    billing_country = normalize_country(order.billing_country)
    exit_country = normalize_country(
        getattr(secrets, "exit_country", "") or configured_exit_country() or billing_country
    )
    # 全链路粘性：优先复用建单时 pin 的出口，禁止每次支付换 SID。
    # 只有老订单没有 proxy 时才按 exit_country 新建一次。
    proxy = secrets.proxy or payload.proxy or checkout_proxy_for(
        "checkout",
        session_country=exit_country,
        billing_country=exit_country,
    )
    logger.info(
        "protocol_pay_country exit_country=%s billing_country=%s pinned=%s",
        exit_country or "-",
        billing_country or "-",
        bool(secrets.proxy),
    )
    publishable_key = (
        (payload.publishable_key or "")
        or (getattr(view, "publishable_key", "") or "")
        or ((secrets.publishable_key if secrets else "") or "")
    )
    currency = (getattr(view, "currency", "") or "") or "USD"

    pm_id = resolve_protocol_payment_method(payload)
    card = payload.card.model_dump() if payload.card else {}
    if payload.card is not None:
        detected_brand = detect_card_brand(payload.card.number)
        if detected_brand:
            view.card_brand = detected_brand
            store.persist(order_id)
    if verified is not None:
        billing = dict(verified)
    else:
        billing = {k: (v or "").strip() for k, v in payload.billing.model_dump().items()}
        billing = _aligned_billing(billing, normalize_country(order.billing_country), getattr(secrets, "billing", None))
        billing["country"] = (billing.get("country") or "US").upper()

    # custom checkout 的会话 id 有两种：oaics_（ChatGPT 内部）与 cs_live_（Stripe 自定义）。
    # 单看 URL 前缀会漏掉后者，所以以订单的 checkout_kind 为准。
    is_custom_checkout = (
        str(getattr(view, "checkout_kind", "") or "").lower() == "custom"
        or bool(re.search(r"oaics_[A-Za-z0-9]+", checkout_url))
    )

    def credentials_current() -> bool:
        return vault.get(order_id) is secrets

    def _run() -> PaymentOutcome:
        if not credentials_current():
            raise DomainError(ErrorCode.SESSION_EXPIRED, "订单凭证已过期")
        har_checkout_id = str(getattr(view, "checkout_session_id", "") or "")
        if is_custom_checkout and not pm_id and not har_checkout_id.startswith("cs_"):
            # 协议模式：2026-10-29 HAR 逐请求复刻（指纹 + 遥测，设备每次随机）
            hp = PlusHarProtocol(
                session=session_raw, checkout_url=checkout_url,
                publishable_key=publishable_key, card=card, billing=billing,
                proxy=proxy, currency=currency,
                profile=getattr(secrets, "profile", None),
                customer_session_client_secret=(
                    getattr(view, "customer_session_client_secret", "") or ""),
                email=billing.get("email") or "",
                country=billing.get("country") or "",
                account_id=secrets.account_id,
                checkout_id=getattr(view, "checkout_session_id", "") or "",
                confirm_return_url=getattr(view, "confirm_return_url", "") or "",
                user_agent=getattr(secrets, "user_agent", "") or "",
                device_id=getattr(secrets, "device_id", "") or "",
                exit_country=getattr(secrets, "exit_country", "") or "",
                pin_proxy=True,
                browser_context=BrowserContext.from_dict(getattr(secrets, "browser_context", None)),
                purchase_attempt_id=getattr(secrets, "purchase_attempt_id", ""),
            )
            hp.credentials_current = credentials_current
            hp.receipt_expected_minor = _minor(view.amount, currency) if view.amount is not None else None
            try:
                return hp.pay_from_url()
            finally:
                if isinstance(getattr(hp, "last_http_trace", None), dict):
                    view.failure_trace = hp.last_http_trace
                hp.close()
        if is_custom_checkout:
            # 输入框模式：浏览器已换好 pm_，服务端只做确认，不接触卡号
            pp = PlusProtocol(
                session=session_raw, checkout_url=checkout_url,
                publishable_key=publishable_key, card=card, billing=billing,
                proxy=proxy, currency=currency,
                profile=getattr(secrets, "profile", None),
                user_agent=getattr(secrets, "user_agent", "") or "",
                pin_proxy=True,
            )
            if verified is not None:
                pp.verified_quote_billing = verified
            pp.credentials_current = credentials_current
            pp.external_pm = pm_id
            try:
                return pp.pay_from_url()
            finally:
                pp.close()
        sp = StripeProtocol(
            proxy=proxy, captcha_key=captcha_key, captcha_url=captcha_url,
            card=card, billing=billing,
            profile=getattr(secrets, "profile", None),
            user_agent=getattr(secrets, "user_agent", "") or "",
            pin_proxy=True,
            publishable_key=publishable_key,
            currency=currency,
            exit_country=exit_country,
        )
        # 默认全关（批量模式）；网页上勾选后逐项打开
        sp.step_fingerprint = payload.steps.fingerprint
        sp.step_dwell = payload.steps.dwell
        sp.step_elements = payload.steps.elements
        sp.step_tax_region = payload.steps.tax_region
        # 浏览器已经换好 pm_ 时复用它，服务端不再提交卡号
        sp.credentials_current = credentials_current
        sp.external_pm = pm_id
        try:
            return sp.pay_from_url(checkout_url)
        finally:
            sp.close()

    if order.plan is Plan.TEAM and not use_fake_adapters():
        await _observe_team_locked(order_id)
        if order.status is not OrderStatus.AWAITING_PAYMENT:
            state = ("paid" if view.delivery_status == "delivered" else
                     "unknown" if order.status is OrderStatus.UNKNOWN else
                     "card_declined" if view.payment_status == "failed" else "pending")
            return _protocol_out(order_id, state=state, message=view.status_summary)

    store.update_status(order_id, OrderStatus.PAYING)
    _set_payment_status(order_id, "processing", summary="支付处理中，请勿重复提交")
    try:
        outcome = await asyncio.to_thread(_run)
    except ObservedBillingConflict as exc:
        store.update_status(order_id, OrderStatus.REJECTED)
        _set_payment_status(order_id, "unpaid", summary=exc.message)
        raise HTTPException(409, exc.message) from None
    except asyncio.CancelledError:
        # Cancellation cannot retract a confirmation already sent to the provider.
        await _mark_payment_unknown(order)
        raise
    except DomainError as exc:
        if isinstance(getattr(exc, "_http_trace", None), dict):
            view.failure_trace = exc._http_trace
        if exc.code is ErrorCode.PAYMENT_DECLINED:
            store.update_status(order_id, OrderStatus.FAILED)
            _set_payment_status(order_id, "failed")
            raise _from_domain(exc, session_reissued=view.session_reissued_for_country, nonteam=order.plan is not Plan.TEAM)
        await _mark_payment_unknown(order)
        return _protocol_out(order_id, state="unknown", message=PAYMENT_UNCONFIRMED)
    except Exception as exc:
        logger.warning("protocol_pay_unconfirmed id=%s type=%s", order_id, type(exc).__name__)
        await _mark_payment_unknown(order)
        return _protocol_out(order_id, state="unknown", message=PAYMENT_UNCONFIRMED)

    logger.info(
        "protocol_pay id=%s state=%s decline=%s code=%s intent=%s",
        order_id, outcome.state, outcome.decline_code, outcome.code, outcome.intent_id,
    )
    payment_succeeded = outcome.state == "paid" or (
        outcome.intent_status == "succeeded" and (outcome.intent_id or "").startswith("pi_")
    )
    if payment_succeeded:
        # Older protocol adapters return a mix of observations. Never synthesize a receipt
        # by combining a bare success flag with this order's quote or requested identity.
        observed = []
        for envelope in (outcome.raw.get("poll"), outcome.raw.get("confirm"), outcome.raw.get("checkout")):
            if not isinstance(envelope, dict):
                continue
            body = envelope.get("body", envelope)
            if not isinstance(body, dict):
                continue
            result = hosted_payment_result(body, view.checkout_session_id or "", view.checkout_url or "")
            if result.receipt_evidence is not None:
                observed.append(result.receipt_evidence)
        if (not observed or any(item != observed[0] for item in observed)
                or (outcome.intent_id and observed[0].payment_id != outcome.intent_id)
                or not _accept_receipt(order, observed[0])):
            await _mark_payment_unknown(order)
            return _protocol_out(order_id, state="unknown", message=PAYMENT_UNCONFIRMED)
        delivered = await _verify_after_payment(order, view)
        response_state = "paid" if delivered else "pending"
        message = view.status_summary
    elif outcome.state == "card_declined":
        _set_payment_status(order_id, "failed", summary=outcome.message or "支付失败")
        store.update_status(order_id, OrderStatus.FAILED)
        response_state, message = "card_declined", outcome.message
    elif outcome.state == "challenge_required":
        _set_payment_status(order_id, "processing", summary="支付需要继续验证")
        store.update_status(order_id, OrderStatus.REQUIRES_ACTION)
        response_state, message = "challenge_required", outcome.message
    elif outcome.state == "pending":
        _set_payment_status(order_id, "processing", summary=outcome.message or "支付处理中")
        store.update_status(order_id, OrderStatus.PAYING)
        response_state, message = "pending", outcome.message
    else:
        await _mark_payment_unknown(order)
        response_state, message = "unknown", PAYMENT_UNCONFIRMED
    return _protocol_out(
        order_id,
        state=response_state,
        message=message,
        code=outcome.code,
        decline_code=outcome.decline_code,
        intent_status=outcome.intent_status,
        charge_id=outcome.charge_id,
        payment_method_id=outcome.payment_method_id,
    )

