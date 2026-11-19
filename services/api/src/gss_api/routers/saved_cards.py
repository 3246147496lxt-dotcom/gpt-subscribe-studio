"""Read historical saved-card status; unscoped legacy financial writes are disabled."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from gss_contracts import OrderStatus, Plan
from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ChatGPTClient
from gss_core.saved_cards import inspect_account_setup
from gss_core.vault import vault

from ..public_payment import saved_view
from ..store import store
from .orders import _error, _from_domain, _team_locks, _to_out

router = APIRouter(prefix="/api/orders", tags=["saved-cards"])
logger = logging.getLogger("gss.saved_cards")


def _load(order_id: str):
    order = store.get_order(order_id)
    if order is None or order.plan is not Plan.PLUS:
        raise HTTPException(404, "Plus 订单不存在")
    if order.status is not OrderStatus.AWAITING_PAYMENT:
        raise HTTPException(409, "支付已提交，请先查询原订单状态")
    secrets = vault.get(order_id)
    if secrets is None:
        raise _error(401, ErrorCode.SESSION_EXPIRED, "订单凭证已过期，请重新创建结账")
    if secrets.saved_card_context and secrets.saved_card_context.get("protocol_flow"):
        raise HTTPException(409, "本订单已进入协议绑卡支付，请使用协议状态查询")
    return order, secrets, store.view(order_id)


def _client(secrets) -> ChatGPTClient:
    client = ChatGPTClient.from_raw_session(secrets.session, proxy=secrets.proxy or None)
    client.timeout = 30.0
    return client


def _leased_client(order_id, secrets):
    if vault.get(order_id) is not secrets:
        raise DomainError(ErrorCode.SESSION_EXPIRED, "订单凭证已过期")
    client = _client(secrets)
    client.credentials_current = lambda: vault.get(order_id) is secrets
    return client


def _inspect(order_id, secrets) -> dict[str, Any]:
    client = _leased_client(order_id, secrets)
    try:
        return inspect_account_setup(client, secrets.saved_card_context)
    finally:
        client.close()


@router.post("/{order_id}/saved-card/prepare")
async def prepare_saved_card(order_id: str) -> dict:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        _load(order_id)
        # This legacy SetupIntent hands a writable client_secret to the browser;
        # neither a new nor an existing secret is scoped to the verified address.
        raise HTTPException(409, "此保存卡片入口无法锁定已核验账单，未提交请求")


@router.get("/{order_id}/saved-card")
async def saved_card_status(order_id: str) -> dict:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        _, secrets, _ = _load(order_id)
        context = secrets.saved_card_context
        if not context:
            return {"state": "not_started"}
        if not context.get("setup_intent_id") or not context.get("client_secret"):
            return {"state": "prepare_unknown", "message": "保存卡片会话的准备结果待确认，本次未重复发送请求"}
        try:
            return saved_view(await asyncio.to_thread(_inspect, order_id, secrets))
        except DomainError as exc:
            raise _from_domain(exc, nonteam=True)
        except Exception:
            logger.warning("saved_card_status_failed order=%s", order_id)
            raise HTTPException(502, "读取保存卡片状态中断，请重新查询；本次未提交验证或付款")


@router.post("/{order_id}/saved-card/refresh-checkout")
async def refresh_saved_card_checkout(order_id: str) -> dict:
    async with _team_locks.setdefault(order_id, asyncio.Lock()):
        order, secrets, view = _load(order_id)
        context = secrets.saved_card_context or {}
        if (context.get("refreshed_checkout_id")
                and context["refreshed_checkout_id"] == view.checkout_session_id
                and context.get("payment_method_id") and context.get("card")):
            # An already-completed operation is a local result, not a new
            # checkout authorization. Do not touch upstream to return it.
            return {"order": _to_out(order), **saved_view({"card": context["card"]})}
        raise HTTPException(409, "此旧入口不能验证已保存支付方式的账单，未新建或替换结账")
