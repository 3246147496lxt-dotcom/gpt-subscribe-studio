"""Unified auto-renewal cancellation for ChatGPT web subscriptions.

Strategy (in strict priority order):
1. If ``GSS_CANCEL_AUTORENEW_PATH`` is explicitly configured in the environment,
   use the direct ChatGPT backend endpoint via ``ChatGPTClient.cancel_auto_renew()``.
2. Otherwise, check ``GET /backend-api/subscriptions`` (idempotent: returns
   immediately if already canceled or non-Stripe mobile store), fetch the
   official Stripe Customer Portal session (``GET /backend-api/payments/customer_portal``
   -> ``https://pay.openai.com/p/session/...``), execute the official Stripe
   Customer Portal cancellation flow via headless Playwright, verify via
   ``GET /backend-api/subscriptions``, and update the local snapshot cache.

Only non-secret status fields are logged or returned. Raw sessions and tokens
never leave memory.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ChatGPTClient
from gss_core.chatgpt_parsers import _will_renew
from gss_core.config import ORDER_HTTP_TIMEOUT_S, use_fake_adapters
from gss_core.proxies import align_proxy_region, configured_proxy

from .account_detail import (
    BEIJING,
    CACHE_VERSION,
    _channel_label,
    _deep,
    _request_portal_url,
    parse_credentials,
    to_beijing,
)
from .persistence import get_db_optional
from .stripe_portal_invoices import cancel_portal_subscription

logger = logging.getLogger("gss.auto_renew")


def _extract_end_time(sub_payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(sub_payload, dict):
        return None
    end = _deep(
        sub_payload,
        ("current_period_end", "expires_at", "active_until", "end_date", "renews_at", "cancels_at"),
    )
    return to_beijing(end)


def _sync_cached_snapshot(
    *,
    account_id: str,
    email: str,
    auto_renew: str,
    end_time: Optional[str] = None,
) -> None:
    db = get_db_optional()
    if db is None:
        return
    key = account_id or email.lower()
    if not key:
        return
    try:
        cached = db.get_account_snapshot(key)
        if not cached:
            return
        updated = {
            **cached,
            "autoRenew": auto_renew,
            "queriedAt": datetime.now(timezone.utc).astimezone(BEIJING).strftime("%Y/%m/%d %H:%M"),
            "cacheVersion": CACHE_VERSION,
        }
        if end_time and not updated.get("endTime"):
            updated["endTime"] = end_time
        db.save_account_snapshot(
            key,
            email=updated.get("email") or email,
            account_id=updated.get("accountId") or account_id,
            detail=updated,
            queried_at=updated["queriedAt"],
        )
    except Exception:  # noqa: BLE001
        logger.debug("auto_renew_cache_sync_skipped", exc_info=True)


def _success_message(end_time: Optional[str]) -> str:
    if end_time:
        return f"自动续费已关闭，本期仍可使用至 {end_time}。"
    return "自动续费已关闭，本期订阅仍可正常使用。"


def _fetch_sub_with_refresh(client: ChatGPTClient) -> Dict[str, Any]:
    try:
        return client.subscription_payload()
    except DomainError as exc:
        if exc.code is ErrorCode.INVALID_SESSION and getattr(client, "session_token", ""):
            if client.refresh_session(trust_statsig=False, reject_unauthorized=False):
                return client.subscription_payload()
        raise


def _verify_canceled_on_chatgpt(
    client: ChatGPTClient,
    *,
    attempts: int = 3,
    delay_s: float = 1.4,
) -> tuple[Optional[bool], Optional[str]]:
    """Poll ChatGPT subscriptions endpoint briefly to observe Stripe webhook sync."""
    will_renew: Optional[bool] = None
    end_time: Optional[str] = None
    for idx in range(max(1, attempts)):
        if idx > 0:
            time.sleep(delay_s)
        try:
            payload = _fetch_sub_with_refresh(client)
        except Exception:  # noqa: BLE001
            continue
        end_time = _extract_end_time(payload) or end_time
        will_renew = _will_renew(payload)
        if will_renew is False:
            return False, end_time
    return will_renew, end_time


def _manual_message(stage: str) -> str:
    if stage == "portal_cancel_disabled":
        return "官方账单门户未开放直接退订按钮，请点击下方链接前往官方页面手动关闭续费。"
    if stage in {"browser_timeout", "browser_error"}:
        return "连接 Stripe 账单门户超时（出口代理波动），请重试或点击下方链接手动关闭。"
    if stage == "browser_disabled":
        return "服务端已关闭自动打开账单门户，请点击下方链接手动关闭自动续费。"
    if stage == "browser_unavailable":
        return "当前环境缺少浏览器自动化运行时，请点击下方链接前往官方门户关闭续费。"
    return "未能自动确认关闭续费，请点击下方链接前往官方 Stripe 账单门户完成关闭。"


def cancel_account_auto_renew(
    session: str,
    *,
    plan: str = "",
    account_id: str = "",
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """Cancel next billing renewal for a ChatGPT web subscription."""
    credentials = parse_credentials(session)
    email = credentials["email"]
    resolved_account_id = account_id.strip() or credentials["account_id"]
    country = credentials["session_country"] or "US"

    if use_fake_adapters():
        return {
            "canceled": True,
            "will_renew": False,
            "manual_required": False,
            "portal_url": None,
            "end_time": None,
            "stage": "fake_adapter",
            "message": "自动续费已关闭，本期订阅仍可正常使用。",
        }

    resolved_proxy = proxy
    if resolved_proxy is None:
        base_proxy = configured_proxy("checkout")
        resolved_proxy = align_proxy_region(base_proxy, country) if (base_proxy and country) else base_proxy

    client = ChatGPTClient.from_raw_session(session, proxy=resolved_proxy, strip_ambient=True)
    client.access_token = credentials["access_token"]
    if resolved_account_id and not getattr(client, "account_id", ""):
        client.account_id = resolved_account_id
    client.timeout = ORDER_HTTP_TIMEOUT_S

    try:
        # Path 1: Explicit direct ChatGPT endpoint when configured via env.
        if os.environ.get("GSS_CANCEL_AUTORENEW_PATH", "").strip():
            res = client.cancel_auto_renew(plan_type=plan)
            end_time: Optional[str] = None
            try:
                end_time = _extract_end_time(_fetch_sub_with_refresh(client))
            except Exception:  # noqa: BLE001
                end_time = None
            _sync_cached_snapshot(
                account_id=resolved_account_id or getattr(client, "account_id", ""),
                email=email,
                auto_renew="已关闭",
                end_time=end_time,
            )
            return {
                "canceled": True,
                "will_renew": False,
                "manual_required": False,
                "portal_url": None,
                "end_time": end_time,
                "stage": "direct_endpoint",
                "subscription_id": res.get("subscription_id"),
                "message": _success_message(end_time),
            }

        # Path 2: Inspect live subscription state first (idempotent & channel-aware).
        sub_payload = _fetch_sub_with_refresh(client)
        resolved_account_id = resolved_account_id or getattr(client, "account_id", "")
        end_time = _extract_end_time(sub_payload)
        channel = _channel_label(sub_payload, {})

        is_active = _deep(sub_payload, ("is_active", "active", "has_active_subscription"))
        plan_source = str(_deep(sub_payload, ("plan_type", "subscription_plan", "plan")) or "").strip().lower()
        is_free = plan_source in {"", "free", "chatgptfreeplan"}
        if is_active is False or (is_active is None and is_free):
            return {
                "canceled": False,
                "will_renew": False,
                "manual_required": False,
                "portal_url": None,
                "end_time": end_time,
                "stage": "not_subscribed",
                "message": "当前账号暂无有效订阅，无需关闭自动续费。",
            }

        if _will_renew(sub_payload) is False:
            _sync_cached_snapshot(
                account_id=resolved_account_id,
                email=email,
                auto_renew="已关闭",
                end_time=end_time,
            )
            return {
                "canceled": True,
                "will_renew": False,
                "manual_required": False,
                "portal_url": None,
                "end_time": end_time,
                "stage": "already_canceled",
                "message": _success_message(end_time),
            }

        is_stripe = _deep(sub_payload, ("is_processor_stripe",))
        if channel in {"App Store", "Google Play"} and is_stripe is not True:
            return {
                "canceled": False,
                "will_renew": True,
                "manual_required": True,
                "portal_url": None,
                "end_time": end_time,
                "stage": "mobile_store",
                "message": f"当前订阅由 {channel} 代扣，需在对应手机应用商店的订阅管理中关闭自动续费。",
            }

        # Obtain authenticated Stripe Customer Portal session URL (pay.openai.com/p/session/...)
        portal_url, _source, _attempts = _request_portal_url(
            client, resolved_account_id, float(ORDER_HTTP_TIMEOUT_S), allow_refresh=True,
        )
        if not portal_url:
            return {
                "canceled": False,
                "will_renew": True,
                "manual_required": True,
                "portal_url": None,
                "end_time": end_time,
                "stage": "no_portal_url",
                "message": "未能获取到官方 Stripe 账单门户链接（可能为非网页渠道或团队账号），请前往官网设置手动关闭。",
            }

        retry_proxy = configured_proxy("checkout")
        portal_res = cancel_portal_subscription(
            portal_url,
            proxy=resolved_proxy,
            timeout=max(float(ORDER_HTTP_TIMEOUT_S), 35.0),
            retry_proxy=retry_proxy,
        )
        stage = str(portal_res.get("stage") or "unknown")

        if portal_res.get("canceled"):
            # Verify via ChatGPT /backend-api/subscriptions so we confirm upstream webhook sync.
            verified_will_renew, verified_end = _verify_canceled_on_chatgpt(
                client,
                attempts=3 if stage != "already_canceled" else 1,
                delay_s=1.4,
            )
            final_end = verified_end or end_time
            if verified_will_renew is False or portal_res.get("xhr_confirmed") or stage == "already_canceled":
                if verified_will_renew is not False:
                    logger.info("auto_renew_portal_confirmed_webhook_pending stage=%s", stage)
                _sync_cached_snapshot(
                    account_id=resolved_account_id,
                    email=email,
                    auto_renew="已关闭",
                    end_time=final_end,
                )
                return {
                    "canceled": True,
                    "will_renew": False,
                    "manual_required": False,
                    "portal_url": None,
                    "end_time": final_end,
                    "stage": stage,
                    "message": _success_message(final_end),
                }

            return {
                "canceled": False,
                "will_renew": True,
                "manual_required": True,
                "portal_url": portal_url,
                "end_time": final_end,
                "stage": "verify_pending",
                "message": "尚未确认自动续费已关闭，请稍后重新查询订阅状态或点击下方链接核对。",
            }

        return {
            "canceled": False,
            "will_renew": True,
            "manual_required": True,
            "portal_url": portal_url,
            "end_time": end_time,
            "stage": stage,
            "message": _manual_message(stage),
        }
    finally:
        client.close()


__all__ = ["cancel_account_auto_renew"]
