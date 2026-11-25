"""卡支付。浏览器只提交 PaymentMethod 标识，本模块向 Stripe / ChatGPT 确认。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Dict, List, Optional

from gss_contracts import AdapterCapabilities, PaymentRequest, PaymentResult, PaymentStatus
from gss_contracts.adapter import PaymentReceiptEvidence
from gss_core.money import _ZERO_DECIMAL
from gss_contracts.errors import DomainError, ErrorCode
from gss_core.fixed_billing import BILLING_COUNTRY, fixed_billing_address
from gss_core.verified_payer import checkout_address
from gss_core.hosted import TeamHostedClient
from gss_core.chatgpt import (
    APP_BASE,
    ChatGPTClient,
    checkout_confirm_blocked,
    checkout_confirm_message,
    _intent_object,
    decline_message,
    nested_intent_status,
    payment_intent_from_confirm,
    public_checkout_id,
    redact_sensitive,
    stripe_verify_url,
    submission_failure_detail,
    submission_state,
    _processor_from_checkout_url,
)
from gss_core.config import ORDER_HTTP_TIMEOUT_S, load_settings
from gss_core.session_token import parse_session
from gss_core.countries import currency_for, locale_for, timezone_for
from gss_core.proxies import (
    align_proxy_region,
    configured_proxy,
    proxy_exit_region,
    session_proxy,
)


class PaymentAdapterBase:
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            source="self-contained chatgpt.com + api.stripe.com",
            license="MIT (this repository)",
            supports={
                "providers": ["direct_card"],
                "card_sources": ["customer"],
                "proxy_roles": ["checkout", "approve"],
            },
            failure_modes={
            "payment_declined": "上游明确拒付 → 显示拒付状态",
            "requires_action": "3DS → 在浏览器完成验证",
            "payment_unknown_state": "扣款结果不确定 → 只读核对原付款，不重复支付",
            },
        )


logger = logging.getLogger("gss.payment")

def _check_approval_result(payload: Dict[str, Any]) -> None:
    if "checkout_approval_internal_error" in submission_failure_detail(payload):
        # Absence of a PaymentIntent in an error envelope is not proof of non-payment.
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "扣款结果未知，请核对账单，不要重复支付")


def _setup_only(payload: Dict[str, Any], intent: Dict[str, Any], intent_id: str) -> bool:
    return (
        payload.get("mode") == "setup" or intent.get("object") == "setup_intent"
        or intent_id.startswith("seti_")
        or (bool(intent) and intent is payload.get("setup_intent"))
    )


@dataclass
class UpstreamPaymentAdapter(PaymentAdapterBase):
    timeout: float = ORDER_HTTP_TIMEOUT_S

    async def pay(self, req: PaymentRequest) -> PaymentResult:
        if not req.payment_method_token and not req.card_id:
            raise DomainError(
                ErrorCode.PAYMENT_DECLINED,
                "缺少支付方式：需要浏览器端 payment_method_token",
            )
        if not req.session or not parse_session(req.session):
            raise DomainError(ErrorCode.INVALID_SESSION, "支付阶段缺少 session")
        checkout_id = req.checkout_session_id or ""
        if not checkout_id:
            raise DomainError(ErrorCode.PAYMENT_DECLINED, "缺少 checkout_session_id")

        # The exit follows the session issuer (statsigContext.country); billing country
        # is only a fallback. A cross-border exit looks inconsistent to Stripe and
        # forces an extra verification step.
        pinned_billing = (None if str(req.plan or "").lower() == "team" else
                          checkout_address(req.exit_country or req.country, req.country, req.billing))
        billing_country = str((req.billing or {}).get("country") or "").upper()
        # Non-Team: the fixed billing does not choose the exit. Team keeps its
        # declared billing country as the payment target.
        target_country = (billing_country if str(req.plan or "").lower() == "team" and billing_country
                          else (req.exit_country or req.country))
        pinned_proxy = req.checkout_proxy or req.approve_proxy
        if pinned_proxy:
            # 已 pin 的出口（可能是 PH 出口 + US 账单）不能被 billing_country 改写。
            proxy = pinned_proxy
        else:
            proxy = align_proxy_region(
                session_proxy("checkout", target_country)
                or configured_proxy("checkout")
                or configured_proxy("approve"),
                target_country,
            )
        if req.credentials_current is not None and not req.credentials_current():
            raise DomainError(ErrorCode.SESSION_EXPIRED, "付款凭证已过期")
        client = ChatGPTClient.from_raw_session(req.session, proxy=proxy)
        client.credentials_current = req.credentials_current
        client.timeout = float(self.timeout)
        client_country = req.exit_country or proxy_exit_region(proxy) or target_country
        client.language = locale_for(client_country)
        client.timezone = timezone_for(client_country)
        if str(req.plan or "").lower() == "team" and public_checkout_id(checkout_id).startswith("cs_"):
            try:
                return await asyncio.to_thread(self._pay_team, client, req)
            finally:
                client.close()
        page_url = req.checkout_url if str(req.checkout_url or "").startswith(APP_BASE) else f"{APP_BASE}/"
        client.warmup(page_url=page_url)
        pk = req.publishable_key or ""
        if not pk:
            raise DomainError(
                ErrorCode.PAYMENT_UNKNOWN_STATE,
                "缺少 Stripe 公钥。若是 Team 长链订单，请打开长链手动支付后查询订阅。",
            )

        billing = dict(pinned_billing if pinned_billing is not None
                       else req.billing or fixed_billing_address())
        checkout_id = public_checkout_id(checkout_id)
        stripe_custom = checkout_id.startswith("cs_")
        confirm_token = _confirmation_token(client, publishable_key=pk, token=req.payment_method_token or "")
        confirmed = client.confirm_checkout(
            checkout_session_id=checkout_id, confirm_token=confirm_token, checkout_url=page_url,
            billing=billing, currency=currency_for(req.exit_country or req.country or BILLING_COUNTRY),
            publishable_key=pk,
        )
        if req.credentials_current is not None and not req.credentials_current():
            # The last request ran under a valid lease. Keep its actual evidence; no follow-up.
            return hosted_payment_result(confirmed, checkout_id)
        _check_approval_result(confirmed)
        if stripe_custom:
            confirmed = client.finalize_stripe_custom_checkout(
                checkout_session_id=checkout_id, publishable_key=pk, checkout_url=page_url,
                processor_entity=_processor_from_checkout_url(page_url), confirmed=confirmed,
            )
            if req.credentials_current is not None and not req.credentials_current():
                return hosted_payment_result(confirmed, checkout_id)
            _check_approval_result(confirmed)
        intent_id, intent_secret, return_url = payment_intent_from_confirm(confirmed)
        def from_original_intent(observation: dict) -> PaymentResult:
            result = _result_from_intent(observation, intent_id, intent_secret)
            if result.status is PaymentStatus.SUCCEEDED and observation.get("id") == intent_id:
                receipt = _hosted_receipt({**confirmed, "payment_intent": observation}, checkout_id)
                return replace(result, receipt_evidence=receipt)
            return result

        chatgpt_status = str(confirmed.get("status") or confirmed.get("result") or "")
        kind = str(confirmed.get("type") or "")
        if checkout_confirm_blocked(confirmed):
            # Application rejection alone says nothing about the original charge.
            evidence = hosted_payment_result(confirmed, checkout_id)
            if evidence.status in {PaymentStatus.SUCCEEDED, PaymentStatus.DECLINED}:
                return evidence
            return PaymentResult(status=PaymentStatus.ERROR, provider_ref=intent_id or checkout_id,
                                 message="扣款结果待核对，请勿重复支付")
        state = submission_state(confirmed)
        if state in {"failed", "declined", "canceled", "cancelled"}:
            extra = checkout_confirm_message(confirmed) or state
            detail = submission_failure_detail(confirmed)
            if detail:
                extra = f"{extra} ({detail})"
            return PaymentResult(
                status=PaymentStatus.DECLINED,
                provider_ref=intent_id or checkout_id,
                message=f"Stripe 未完成确认: {extra}",
            )
        nested = _intent_object(confirmed)
        if _setup_only(confirmed, nested, intent_id):
            return PaymentResult(status=PaymentStatus.PROCESSING, provider_ref=checkout_id,
                                 message="尚无扣款确认，请核对账单")
        pi_status = nested_intent_status(confirmed)
        if pi_status in {"requires_payment_method", "canceled", "cancelled"} or nested.get("last_payment_error"):
            return _result_from_intent(nested or {"status": pi_status or "requires_payment_method"}, intent_id)
        if intent_secret.startswith("pi_") or kind == "payment_intent" or intent_id.startswith("pi_"):
            if pi_status == "requires_action" and intent_secret:
                live = client.retrieve_stripe_intent(
                    intent_id=intent_id,
                    client_secret=intent_secret,
                    publishable_key=pk,
                )
                if live:
                    return from_original_intent(live)
            if pi_status in {"succeeded", "processing", "requires_action"}:
                return from_original_intent(nested if nested else {"status": pi_status})
            if intent_secret and pi_status == "requires_confirmation":
                if not return_url:
                    return_url = stripe_verify_url(
                        checkout_id,
                        processor=_processor_from_checkout_url(page_url),
                        plan=str(req.plan or ""),
                    )
                final = client.confirm_stripe_intent(
                    intent_id=intent_id,
                    client_secret=intent_secret,
                    publishable_key=pk,
                    confirmation_token=confirm_token,
                    return_url=return_url,
                )
                return from_original_intent(final)
        if confirmed.get("payment_status") == "paid":
            return PaymentResult(
                status=PaymentStatus.SUCCEEDED,
                provider_ref=intent_id or None,
                receipt_evidence=_hosted_receipt(confirmed, checkout_id),
                message="支付成功",
            )
        if stripe_custom and (chatgpt_status in {"open", ""} or state in {"requires_approval", "processing", ""}):
            return PaymentResult(
                status=PaymentStatus.PROCESSING,
                provider_ref=intent_id or checkout_id,
                message="支付已提交，正在等待 Stripe 完成确认。请稍后刷新订单，不要立刻再点确认。",
            )
        return PaymentResult(
            status=PaymentStatus.ERROR,
            provider_ref=intent_id or checkout_id,
            message=f"未知支付状态: {chatgpt_status or state or 'empty'}",
        )


    def _pay_team(self, client: ChatGPTClient, req: PaymentRequest) -> PaymentResult:
        if not req.publishable_key:
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "缺少 Stripe 公钥，请打开官方长链")
        hosted = TeamHostedClient(client)
        context = req.team_context
        if not context:
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "请先初始化 Team Hosted Checkout")
        confirmed = hosted.confirm(context, req.payment_method_token or "",
                                   passive_captcha_token=req.passive_captcha_token or "",
                                   rv_timestamp=req.rv_timestamp or "")
        result = hosted_payment_result(confirmed, context["session_id"], req.checkout_url)
        # Hand the challenge back to the caller. Polling only observes the original payment.
        if result.status is PaymentStatus.REQUIRES_ACTION:
            pending = self._challenge_payload(confirmed)
            if pending:
                import dataclasses
                result = dataclasses.replace(result, next_action={**(result.next_action or {}), **pending})
        if (result.status is PaymentStatus.PROCESSING
                and (req.credentials_current is None or req.credentials_current())):
            polled = hosted.poll(session_id=context["session_id"], key=context["publishable_key"])
            result = hosted_payment_result(polled, context["session_id"], req.checkout_url)
        return result


    @staticmethod
    def _challenge_payload(confirmed: dict) -> dict:
        from gss_core.captcha import challenge_from_intent
        from gss_core.chatgpt import _intent_object
        challenge = challenge_from_intent(_intent_object(confirmed))
        return dict(challenge) if challenge else {}

    def _solve_intent_challenge(self, client: ChatGPTClient, confirmed: dict,
                                context: dict, req: PaymentRequest):
        """Solve Stripe's intent_confirmation_challenge and re-read the intent."""
        from gss_contracts import PaymentResult, PaymentStatus
        from gss_core.captcha import challenge_from_intent, solve_hcaptcha, verify_challenge
        from gss_core.chatgpt import _intent_object, payment_intent_from_confirm
        intent = _intent_object(confirmed)
        challenge = challenge_from_intent(intent)
        if not challenge:
            return None
        settings = load_settings()
        if not settings.captcha_api_key:
            logger.warning("challenge_present captcha_key_missing challenge=stripe")
            return None
        intent_id, client_secret, _ = payment_intent_from_confirm(confirmed)
        if not intent_id or not client_secret:
            return None
        logger.info("challenge_solving site_key_present=%s rqdata_present=%s",
                    bool(challenge.get("site_key")), bool(challenge.get("rqdata")))
        try:
            token = solve_hcaptcha(client.stripe_http, challenge, api_key=settings.captcha_api_key,
                                   api_url=settings.captcha_api_url, proxy=client.proxy or "",
                                   user_agent=client.ua)
            verified = verify_challenge(client.stripe_http, intent_id=intent_id, client_secret=client_secret,
                                        token=token, publishable_key=req.publishable_key or "",
                                        api_version="2026-03-25.dahlia",
                                        user_agent=client.ua)
        except DomainError as exc:
            logger.warning("challenge_failed code=%s error=%s", exc.code.value,
                           redact_sensitive(exc.message)[:200])
            return None
        if str(verified.get("status") or "") == "succeeded":
            return PaymentResult(status=PaymentStatus.SUCCEEDED, provider_ref=intent_id, message="支付成功")
        if verified.get("last_payment_error"):
            return _result_from_intent(verified, intent_id, client_secret)
        return PaymentResult(status=PaymentStatus.PROCESSING, provider_ref=intent_id,
                             message="验证通过，等待 Stripe 确认扣款")


def _checkout_references(payload: dict) -> list:
    ids = [payload[k] for k in ("session_id", "checkout_session_id", "checkout_id") if k in payload]
    if str(payload.get("id") or "").startswith("cs_"):
        ids.append(payload["id"])
    return ids


def _hosted_receipt(payload: dict, session_id: str) -> Optional[PaymentReceiptEvidence]:
    # Never infer amount_received from an authorization, checkout quote or caller input.
    refs = _checkout_references(payload)
    pi = payload.get("payment_intent")
    if (not refs or any(ref != session_id for ref in refs) or not isinstance(pi, dict)
            or pi.get("status") != "succeeded" or pi.get("object", "payment_intent") != "payment_intent"
            or payload.get("payment_status") not in (None, "paid")):
        return None
    intent_id, received, currency = pi.get("id"), pi.get("amount_received"), pi.get("currency")
    if (not isinstance(intent_id, str) or not intent_id.startswith("pi_") or "_secret_" in intent_id
            or type(received) is not int or received <= 0
            or not isinstance(currency, str) or len(currency) != 3 or not currency.isascii()
            or not currency.isalpha()):
        return None
    # Independent references in the same response must agree, even when the outer
    # checkout and amount happen to match. A contradictory nested PI is not evidence.
    if any(pi[k] != session_id for k in ("checkout_id", "checkout_session_id", "session_id") if k in pi):
        return None
    if any(payload[k] != intent_id for k in ("payment_intent_id", "intent_id") if k in payload):
        return None
    currency = currency.upper()
    if "currency" in payload and str(payload["currency"]).upper() != currency:
        return None
    amount = Decimal(received).scaleb(0 if currency in _ZERO_DECIMAL else -2)
    # A checkout-confirmed workspace ID joins the independent entitlement lookup.
    # If absent or inconsistent, money may be confirmed but delivery must wait.
    workspace = payload.get("workspace_id")
    if (not isinstance(workspace, str) or not workspace.strip()
            or ("workspace_id" in pi and pi["workspace_id"] != workspace)):
        workspace = None
    return PaymentReceiptEvidence(refs[0], intent_id, amount, currency, workspace)


def hosted_payment_result(payload: dict, session_id: str, checkout_url: str = ""):
    refs = _checkout_references(payload)
    if any(ref != session_id for ref in refs):
        return PaymentResult(status=PaymentStatus.ERROR, message="付款回执与原结账不符")
    object_status = str(payload.get("payment_object_status") or "").lower()
    state = str(payload.get("state") or "").lower()
    status = str(payload.get("status") or "").lower()
    intent = _intent_object(payload)
    intent_id, secret, _ = payment_intent_from_confirm(payload)
    if _setup_only(payload, intent, intent_id):
        return PaymentResult(status=PaymentStatus.PROCESSING, provider_ref=None,
                             message="尚无扣款确认，请核对账单")
    # Completing a Checkout or successfully setting up a payment method is not a charge.
    payment_intent = intent_id.startswith("pi_") or intent.get("object") == "payment_intent"
    if payload.get("payment_status") == "paid" or (payment_intent and intent.get("status") == "succeeded"):
        return PaymentResult(status=PaymentStatus.SUCCEEDED, provider_ref=intent_id or None,
                             receipt_evidence=_hosted_receipt(payload, session_id), message="支付成功")
    if intent.get("status") == "requires_action":
        next_action = intent.get("next_action") or {}
        sdk = next_action.get("use_stripe_sdk") or {}
        kind = str(sdk.get("type") or next_action.get("type") or "")
        result = _result_from_intent(intent, intent_id, secret)
        action = dict(result.next_action or {})
        action["challenge_type"] = kind
        action["checkout_url"] = checkout_url
        # Preserve the action for the original payment; polling never performs it.
        action["payment_intent_id"] = intent_id
        action["client_secret"] = secret or action.get("client_secret")
        return PaymentResult(status=PaymentStatus.REQUIRES_ACTION, provider_ref=intent_id or None,
                             next_action=action, message="需要完成 Stripe 验证" if kind == "intent_confirmation_challenge" else "需要完成支付验证")
    if payment_intent and (intent.get("status") in {"requires_payment_method", "canceled", "cancelled"}
                           or intent.get("last_payment_error")):
        return _result_from_intent(intent, intent_id, secret)
    if payment_intent and object_status in {"failed", "canceled"}:
        return PaymentResult(status=PaymentStatus.DECLINED, provider_ref=intent_id, message="付款未完成")
    if {state, status, object_status} & {"failed", "canceled", "expired"}:
        return PaymentResult(status=PaymentStatus.ERROR, provider_ref=intent_id or None,
                             message="扣款结果待核对，请勿重复支付")
    open_unpaid = (bool(refs) and status == "open" and payload.get("payment_status") == "unpaid"
                   and state in {"", "active", "open"} and not object_status
                   and not any(payload.get(k) for k in (
                       "payment_intent", "setup_intent", "submission_attempt", "payment_object",
                       "payment_intent_id", "client_secret", "last_payment_error", "charge",
                   )))
    return PaymentResult(status=PaymentStatus.PROCESSING, provider_ref=intent_id or None,
                         hosted_open_unpaid=open_unpaid, message="Stripe 正在处理，请等待状态确认")


def _confirmation_token(client: ChatGPTClient, *, publishable_key: str, token: str) -> str:
    text = str(token or "").strip()
    if text.startswith("ctoken_"):
        return text
    if text.startswith("pm_"):
        return client.create_confirmation_token(publishable_key=publishable_key, payment_method=text)
    raise DomainError(ErrorCode.PAYMENT_DECLINED, "支付标识无效")


def _result_from_intent(payload: Dict[str, Any], intent_id: str, client_secret: str = "") -> PaymentResult:
    status = str(payload.get("status") or "")
    if status == "requires_action":
        next_action = payload.get("next_action") if isinstance(payload.get("next_action"), dict) else {}
        url = (next_action or {}).get("redirect_to_url", {}).get("url") or (next_action or {}).get("url")
        secret = str(payload.get("client_secret") or client_secret or "")
        return PaymentResult(
            status=PaymentStatus.REQUIRES_ACTION,
            # 有跳转链接就直接给链接；3DS2（use_stripe_sdk）没有链接，
            # 需要浏览器用 PI 的 client_secret 调 stripe.handleNextAction 弹出验证窗口。
            next_action={
                "type": "3ds_redirect" if url else "use_stripe_sdk",
                "url": url,
                "client_secret": secret or None,
            },
            provider_ref=intent_id,
            message="需要完成 3DS 验证",
        )
    if status == "succeeded":
        return PaymentResult(status=PaymentStatus.SUCCEEDED, provider_ref=intent_id, message="支付成功")
    if status in {"processing", "pending"}:
        return PaymentResult(status=PaymentStatus.PROCESSING, provider_ref=intent_id, message="支付处理中")
    error = payload.get("last_payment_error") if isinstance(payload.get("last_payment_error"), dict) else {}
    if (status in {"requires_payment_method", "canceled", "cancelled"} or error.get("decline_code")
            or error.get("code") in {"card_declined", "expired_card", "incorrect_cvc", "incorrect_number"}):
        detail = str(error.get("decline_code") or error.get("code") or error.get("message") or status)
        return PaymentResult(
            status=PaymentStatus.DECLINED,
            provider_ref=intent_id,
            message=decline_message(detail) if error else f"支付未完成: {status}",
        )
    return PaymentResult(
        status=PaymentStatus.ERROR,
        provider_ref=intent_id,
        message=f"未知支付状态: {status or 'empty'}",
    )


@dataclass
class FakePaymentAdapter(PaymentAdapterBase):
    scripted: List[PaymentStatus] = field(default_factory=lambda: [PaymentStatus.SUCCEEDED])
    calls: List[PaymentRequest] = field(default_factory=list)

    async def pay(self, req: PaymentRequest) -> PaymentResult:
        self.calls.append(req)
        if not self.scripted:
            raise AssertionError("FakePaymentAdapter 脚本已耗尽")
        status = self.scripted.pop(0)
        next_action: Optional[Dict[str, Any]] = None
        if status is PaymentStatus.REQUIRES_ACTION:
            next_action = {"type": "3ds_redirect", "url": "https://3ds.example.invalid/verify"}
        from gss_core.fake_receipts import settlement_for
        receipt = settlement_for(req.checkout_session_id or "") if status is PaymentStatus.SUCCEEDED else None
        return PaymentResult(
            status=status,
            next_action=next_action,
            receipt_evidence=receipt,
            provider_ref=receipt.payment_id if receipt else "fake-payment-ref",
            message=f"scripted:{status}",
        )


__all__ = ["PaymentAdapterBase", "UpstreamPaymentAdapter", "FakePaymentAdapter"]
