"""Single-order server-side card setup, checkout refresh, and payment protocol."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import hmac
import json
from decimal import Decimal, InvalidOperation
from secrets import token_bytes
from threading import Lock

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from gss_contracts import BillingMode, OrderStatus, Plan
from gss_contracts.errors import ErrorCode
from gss_core.chatgpt import ChatGPTClient
from gss_core.config import use_fake_adapters
from gss_core.checkout_protocol import run_checkout_protocol
from gss_core.chatgpt import _processor_from_checkout_url
from gss_core.vault import vault

from ..schemas import BillingIn, CardIn
from ..store import store
from .events import notify_order_update
from .orders import (
    PAID_AWAITING_ENTITLEMENT, PAYMENT_UNCONFIRMED,
    _error, _mark_delivered, _receipt_for, _set_delivery_status,
    _set_payment_status, _team_locks, _to_out,
)

logger = logging.getLogger("gss.protocol_cards")
router = APIRouter(prefix="/api/orders", tags=["card-protocol"])


class BindProtocolRequest(BaseModel):
    card: CardIn = Field(repr=False)
    billing: BillingIn
    expected_amount: str = Field(min_length=1, max_length=40)
    expected_currency: str = Field(min_length=3, max_length=3)


def _run(secrets, context, spec, *, card=None, billing=None, read_only=False):
    guard = context["operation_lock"]
    if not guard.acquire(blocking=False):
        return {"state": "pending", "stage": "payment" if context.get("app_confirmation_started") else "checkout",
                "message": "原协议请求仍在处理，请查询协议状态", "intent_status": "processing"}
    try:
        client = ChatGPTClient.from_raw_session(secrets.session, proxy=secrets.proxy or None)
        client.timeout = 45.0
        try:
            return run_checkout_protocol(client, context, spec, card=card, billing=billing, read_only=read_only)
        finally:
            client.close()
    finally:
        guard.release()


def _cached(order_id: str) -> dict | None:
    order = store.get_order(order_id)
    if order is None or order.plan is not Plan.PLUS:
        raise HTTPException(404, "Plus 订单不存在")
    view = store.view(order_id)
    if order.status in {OrderStatus.ACTIVE, OrderStatus.DELIVERED, OrderStatus.FAILED}:
        result = dict(view.protocol_saved_result or {})
        if not result:
            raise HTTPException(409, "订单已有最终结果，请查询原订单")
        return {**result, "order": _to_out(order)}
    return None


def _context(order_id: str, *, start: bool):
    order = store.get_order(order_id)
    if order is None or order.plan is not Plan.PLUS:
        raise HTTPException(404, "Plus 订单不存在")
    secrets = vault.get(order_id)
    if secrets is None:
        raise _error(401, ErrorCode.SESSION_EXPIRED, "订单凭证已过期，请重新创建订单")
    context = secrets.saved_card_context
    if context and (not context.get("protocol_flow") or context.get("protocol_kind") != "checkout_v2"):
        raise HTTPException(409, "本订单已开始旧版绑卡流程，请核对原结果；新建订单才使用当前直付协议")
    if not context and start:
        if order.status is not OrderStatus.AWAITING_PAYMENT:
            raise HTTPException(409, "订单已提交，请先查询状态")
        context = {"protocol_flow": True, "protocol_kind": "checkout_v2", "state": "preparing", "request_key": token_bytes(32), "operation_lock": Lock()}
        secrets.saved_card_context = context
    return order, secrets, store.view(order_id), context


def _spec(order, view, secrets, payload=None):
    return {
        "account_id": secrets.account_id, "country": order.billing_country,
        "exit_country": getattr(secrets, "exit_country", "") or "",
        "promo": order.mode.billing_mode is BillingMode.PROMO,
        "promo_code": view.promo_code or "", "promo_campaign_id": view.promo_campaign_id or "",
        "expected_amount": payload.expected_amount if payload else view.amount,
        "expected_currency": payload.expected_currency if payload else view.currency,
        "checkout_id": view.checkout_session_id or secrets.checkout_session_id,
        "checkout_url": view.checkout_url, "publishable_key": view.publishable_key or secrets.publishable_key,
        "amount": view.amount, "currency": view.currency,
        "customer_session_client_secret": view.customer_session_client_secret or "",
        "processor": _processor_from_checkout_url(view.checkout_url or ""),
        "confirm_return_url": view.confirm_return_url or "",
    }


async def _apply(order, secrets, view, context: dict, result: dict) -> dict:
    if context.get("quoted"):
        view.amount = str(context["amount"])
        view.currency = context["currency"]
        view.quote_error = None
        order.amount = context["amount"]
    if context.get("account_id"):
        secrets.account_id = context["account_id"]
    result = dict(result)
    result["payment_method_id"] = context.get("payment_method_id")
    payment_succeeded = (
        result["stage"] == "payment"
        and (result["state"] == "paid" or result.get("intent_status") == "succeeded")
    )
    if view.payment_status == "succeeded" and not payment_succeeded:
        # 已完成扣款后，只读核验返回的 unknown/pending 不能把支付状态降级。
        result.update({
            "state": "pending",
            "message": view.status_summary,
            "payment_status": "succeeded",
            "delivery_status": view.delivery_status,
            "payment_intent_id": view.payment_intent_id,
            "tax_delta": view.tax_delta,
            "plan_type": view.plan_type or str(order.plan),
            "status_summary": view.status_summary,
            "receipt": _receipt_for(order, view),
        })
        view.protocol_saved_result = result
        return {**result, "order": _to_out(order)}
    if (order.status is OrderStatus.UNKNOWN and not payment_succeeded
            and (result["stage"], result["state"]) != ("payment", "card_declined")):
        # A stale/incomplete observation cannot re-open an ambiguous financial attempt.
        result.update(stage="payment", state="unknown", message=PAYMENT_UNCONFIRMED)
        result.pop("next_action", None)
    if result["stage"] == "checkout" and result["state"] == "pending":
        store.update_status(order.id, OrderStatus.AWAITING_PAYMENT)
    if payment_succeeded:
        _set_payment_status(
            order.id,
            "succeeded",
            intent_id=result.get("payment_intent_id") or result.get("charge_id"),
            amount=view.amount,
            currency=view.currency,
            tax_delta=view.tax_delta,
            plan_type=str(order.plan),
            summary="扣款成功，正在核验订阅",
        )
        if result["state"] == "paid":
            # HAR 自身已确认订阅生效，直接交付；不再重复查订阅。
            if order.status in {OrderStatus.AWAITING_PAYMENT, OrderStatus.REQUIRES_ACTION, OrderStatus.UNKNOWN}:
                store.update_status(order.id, OrderStatus.PAYING)
            if order.status is OrderStatus.PAYING:
                store.update_status(order.id, OrderStatus.VERIFYING)
            store.update_status(order.id, OrderStatus.ACTIVE)
            await _mark_delivered(order.id)
        else:
            # 扣款已成功但 HAR 尚未看到订阅生效：只标记核验延迟，
            # 由 /verify 或下一次只读查询推进交付。
            if order.status in {OrderStatus.AWAITING_PAYMENT, OrderStatus.REQUIRES_ACTION, OrderStatus.UNKNOWN}:
                store.update_status(order.id, OrderStatus.PAYING)
            store.update_status(order.id, OrderStatus.VERIFYING)
            _set_delivery_status(order.id, "verify_delayed", PAID_AWAITING_ENTITLEMENT)
            result["state"] = "pending"
            result["message"] = view.status_summary
    elif result["stage"] == "payment":
        state = result["state"]
        if state == "card_declined":
            _set_payment_status(order.id, "failed", summary=result.get("message") or "支付失败")
            store.update_status(order.id, OrderStatus.FAILED)
        elif state == "challenge_required":
            _set_payment_status(order.id, "processing", summary=result.get("message") or "支付需要继续验证")
            store.update_status(order.id, OrderStatus.REQUIRES_ACTION)
        elif state == "unknown":
            result["message"] = PAYMENT_UNCONFIRMED
            _set_payment_status(order.id, "processing", summary=PAYMENT_UNCONFIRMED)
            store.update_status(order.id, OrderStatus.UNKNOWN)
        elif context.get("app_confirmation_started"):
            _set_payment_status(order.id, "processing")
            store.update_status(order.id, OrderStatus.PAYING)
    result.update({
        "payment_status": view.payment_status,
        "delivery_status": view.delivery_status,
        "payment_intent_id": view.payment_intent_id,
        "tax_delta": view.tax_delta,
        "plan_type": view.plan_type or str(order.plan),
        "status_summary": view.status_summary,
        "receipt": _receipt_for(order, view),
    })
    view.protocol_saved_result = result
    if not payment_succeeded and result["state"] != "paid":
        view.last_error = result.get("message")
        view.next_action = result.get("next_action")
        await notify_order_update(order.id, order.status, {"protocol_stage": result["stage"], "protocol_state": result["state"]})
    return {**result, "order": _to_out(order)}


@router.post("/{order_id}/bind-and-pay-protocol")
async def bind_and_pay_protocol(order_id: str, payload: BindProtocolRequest) -> dict:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        cached = _cached(order_id)
        if cached:
            return cached
        if use_fake_adapters():
            raise HTTPException(409, "离线模式不会提交真实绑卡或付款请求")
        # Validate the quote before creating or mutating a binding context.
        order = store.get_order(order_id)
        view = store.view(order_id)
        if view.payment_status == "succeeded" or order.status is OrderStatus.UNKNOWN:
            # 无论凭证/上下文是否仍在，只允许查询原付款，不再传入卡资料。
            unknown = view.payment_status != "succeeded"
            secrets = vault.get(order_id)
            context = secrets.saved_card_context if secrets is not None else None
            if secrets is not None and context and context.get("protocol_kind") == "checkout_v2":
                try:
                    result = await asyncio.to_thread(
                        _run, secrets, context, _spec(order, view, secrets), read_only=True
                    )
                    return await _apply(order, secrets, view, context, result)
                except HTTPException:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("bind_recheck_failed id=%s type=%s", order_id, type(exc).__name__)
            return {
                "state": "unknown" if unknown else ("paid" if view.delivery_status == "delivered" else "pending"),
                "stage": "payment",
                "message": PAYMENT_UNCONFIRMED if unknown else view.status_summary,
                "payment_status": view.payment_status,
                "delivery_status": view.delivery_status,
                "payment_intent_id": view.payment_intent_id,
                "tax_delta": view.tax_delta,
                "plan_type": view.plan_type or str(order.plan),
                "status_summary": view.status_summary,
                "receipt": _receipt_for(order, view),
                "order": _to_out(order),
            }
        try:
            amount = Decimal(payload.expected_amount)
            current_amount = Decimal(str(view.amount))
        except (InvalidOperation, ValueError):
            raise HTTPException(409, "尚未读到可确认的订单金额")
        if not amount.is_finite() or amount < 0 or amount != current_amount or payload.expected_currency.upper() != str(view.currency or "").upper():
            raise HTTPException(409, "订单报价已改变，请重新确认当前金额")
        billing = {key: value.strip() for key, value in payload.billing.model_dump().items()}
        billing["country"] = billing["country"].upper()
        order, secrets, view, context = _context(order_id, start=True)
        card = payload.card.model_dump()
        request = json.dumps({"card": card, "billing": billing}, sort_keys=True, separators=(",", ":")).encode()
        fingerprint = hmac.new(context["request_key"], request, hashlib.sha256).digest()
        previous = context.get("request_fingerprint")
        if previous and not hmac.compare_digest(previous, fingerprint):
            raise HTTPException(409, "本订单已开始处理另一份支付资料，请查询原结果或重新创建订单")
        context["request_fingerprint"] = fingerprint
        context["protocol_billing"] = billing
        secrets.billing = billing
        view.billing = dict(billing)
        try:
            result = await asyncio.to_thread(_run, secrets, context, _spec(order, view, secrets, payload), card=card, billing=billing)
        except asyncio.CancelledError:
            # The worker can still complete after the HTTP request is cancelled.
            await _apply(order, secrets, view, context, {"stage": "payment", "state": "unknown"})
            raise
        except Exception:  # A missing response is not evidence that the worker did not charge.
            result = {"stage": "payment", "state": "unknown"}
        return await _apply(order, secrets, view, context, result)


@router.get("/{order_id}/bind-and-pay-protocol")
async def get_bind_protocol_status(order_id: str) -> dict:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        cached = _cached(order_id)
        if cached:
            return cached
        order, secrets, view, context = _context(order_id, start=False)
        if not context:
            return {"state": "not_started", "stage": "checkout", "message": "协议流程尚未开始", "order": _to_out(order)}
        if use_fake_adapters():
            raise HTTPException(409, "离线模式不会查询真实支付上下文")
        result = await asyncio.to_thread(_run, secrets, context, _spec(order, view, secrets), read_only=True)
        return await _apply(order, secrets, view, context, result)
