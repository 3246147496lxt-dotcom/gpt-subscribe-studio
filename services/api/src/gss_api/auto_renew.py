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
from gss_core.countries import locale_for, timezone_for
from gss_core.proxies import (
    align_proxy_region,
    configured_proxy,
    cooling_sids,
    fresh_proxy_sid,
    rewrite_proxy_exit,
)
from gss_core.session_token import parse_session_bundle

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
            try:
                refreshed = bool(client.refresh_session(
                    trust_statsig=False, reject_unauthorized=False, force_refresh=True,
                ))
            except TypeError:
                refreshed = bool(client.refresh_session(trust_statsig=False, reject_unauthorized=False))
            if refreshed:
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
            "reset by peer", "EOF", "curl: (35)", "curl: (56)", "curl: (28)",
        )
    )


def _fresh_exit(proxy_url: Optional[str]) -> Optional[str]:
    if not proxy_url:
        return None
    try:
        return rewrite_proxy_exit(proxy_url, sid=fresh_proxy_sid(exclude=cooling_sids()))
    except Exception:  # noqa: BLE001
        return proxy_url


def _candidate_proxies(
    explicit_proxy: Optional[str],
    country: str,
    *,
    retries: int = 2,
) -> list[Optional[str]]:
    if explicit_proxy is not None:
        if not explicit_proxy:
            return [None]
        candidates: list[Optional[str]] = [explicit_proxy]
        for _ in range(max(0, retries)):
            nxt = _fresh_exit(explicit_proxy)
            if nxt and nxt not in candidates:
                candidates.append(nxt)
        return candidates
    base_proxy = configured_proxy("checkout")
    if not base_proxy:
        return [None]
    aligned = align_proxy_region(base_proxy, country) if country else base_proxy
    candidates = []
    for _ in range(max(1, 1 + retries)):
        nxt = _fresh_exit(aligned)
        if nxt and nxt not in candidates:
            candidates.append(nxt)
    return candidates or [aligned]


def _build_auto_renew_client(
    session: str,
    *,
    credentials: Dict[str, str],
    resolved_account_id: str,
    country: str,
    proxy: Optional[str],
) -> ChatGPTClient:
    client = ChatGPTClient.from_raw_session(session, proxy=proxy, strip_ambient=True)
    try:
        bundle = parse_session_bundle(session)
    except Exception:  # noqa: BLE001
        bundle = None
    if bundle is not None:
        if bundle.device_id and hasattr(client, "device_id"):
            client.device_id = bundle.device_id
            if isinstance(getattr(client, "extra_cookies", None), dict):
                client.extra_cookies["oai-did"] = bundle.device_id
        if bundle.user_agent and hasattr(client, "user_agent"):
            client.user_agent = bundle.user_agent
        effective_country = str(country or bundle.country or "").strip().upper()
        if effective_country:
            if hasattr(client, "language"):
                client.language = locale_for(effective_country)
            if hasattr(client, "timezone"):
                client.timezone = timezone_for(effective_country)
    client.access_token = credentials["access_token"]
    if resolved_account_id and not getattr(client, "account_id", ""):
        client.account_id = resolved_account_id
    client.timeout = ORDER_HTTP_TIMEOUT_S
    return client


def _portal_attempts_transport_failed(attempts: list[str]) -> bool:
    if not attempts:
        return False
    transport_suffixes = (
        "SSLError", "ProxyError", "ConnectionError", "ConnectError",
        "Timeout", "TimeoutError", "ReadTimeout", "ConnectTimeout",
        "CurlError", "RequestException",
    )
    return all(any(str(a).endswith(s) for s in transport_suffixes) for a in attempts)


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

    candidates = _candidate_proxies(proxy, country, retries=2)
    active_proxy = candidates[0]
    client: Optional[ChatGPTClient] = None

    try:
        # Path 1: Explicit direct ChatGPT endpoint when configured via env.
        if os.environ.get("GSS_CANCEL_AUTORENEW_PATH", "").strip():
            client = _build_auto_renew_client(
                session,
                credentials=credentials,
                resolved_account_id=resolved_account_id,
                country=country,
                proxy=active_proxy,
            )
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

        # Path 2: Inspect live subscription state first (idempotent, channel-aware, dead-SID resilient).
        sub_payload: Optional[Dict[str, Any]] = None
        last_transport_exc: Optional[Exception] = None
        for idx, candidate_proxy in enumerate(candidates):
            attempt_client = _build_auto_renew_client(
                session,
                credentials=credentials,
                resolved_account_id=resolved_account_id,
                country=country,
                proxy=candidate_proxy,
            )
            try:
                sub_payload = _fetch_sub_with_refresh(attempt_client)
                client = attempt_client
                active_proxy = candidate_proxy
                last_transport_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                attempt_client.close()
                if _is_proxy_transport_error(exc):
                    last_transport_exc = exc
                    logger.warning(
                        "auto_renew_proxy_retry stage=subscriptions attempt=%s/%s type=%s",
                        idx + 1, len(candidates), type(exc).__name__,
                    )
                    continue
                raise

        if sub_payload is None or client is None:
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                "出口代理连接 ChatGPT 失败（已自动切换节点重试仍断连），尚未提交关闭续费，请稍后重试。",
                detail={
                    "code": "proxy_tunnel_failed",
                    "stage": "subscriptions",
                    "reason": type(last_transport_exc).__name__ if last_transport_exc else "transport_error",
                },
            )

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

        # Path 2A (Primary 2026 official web flow): ChatGPT first-party
        # POST /backend-api/subscriptions/cancel with {"account_id": resolved_account_id}.
        # OpenAI disabled cancellation inside Stripe Customer Portal (features.subscription_cancel.enabled=false)
        # and moved cancellation to ChatGPT Settings -> Billing -> CancellationDialog.
        direct_cancel_fn = getattr(client, "cancel_subscription_direct", None)
        direct_error_code = ""
        if callable(direct_cancel_fn):
            direct_res: Optional[Dict[str, Any]] = None
            try:
                direct_res = direct_cancel_fn(account_id=resolved_account_id)
            except DomainError:
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_proxy_transport_error(exc):
                    # POST may or may not have landed upstream; read-only check before deciding.
                    logger.warning("auto_renew_direct_transport_error type=%s", type(exc).__name__)
                    verified_will_renew, verified_end = _verify_canceled_on_chatgpt(
                        client, attempts=2, delay_s=1.2,
                    )
                    if verified_will_renew is False:
                        final_end = verified_end or end_time
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
                            "stage": "chatgpt_api_canceled",
                            "message": _success_message(final_end),
                        }
                    raise DomainError(
                        ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                        "提交关闭自动续费时出口代理中断，请先重新查询账号详情确认自动续费状态后再决定是否重试。",
                        detail={"code": "proxy_tunnel_failed", "stage": "subscriptions_cancel"},
                    )
                raise

            if isinstance(direct_res, dict) and direct_res.get("canceled"):
                verified_will_renew, verified_end = _verify_canceled_on_chatgpt(
                    client, attempts=3, delay_s=1.2,
                )
                final_end = verified_end or end_time
                if verified_will_renew is False:
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
                        "stage": "chatgpt_api_canceled",
                        "message": _success_message(final_end),
                    }
                # Upstream POST /backend-api/subscriptions/cancel returned 2xx; Stripe webhook sync may lag briefly.
                logger.info("auto_renew_direct_api_2xx_webhook_pending")
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
                    "stage": "chatgpt_api_canceled",
                    "message": _success_message(final_end),
                }

            if isinstance(direct_res, dict):
                direct_error_code = str(direct_res.get("error_code") or "")
                if not direct_res.get("fallback_portal"):
                    return {
                        "canceled": False,
                        "will_renew": True,
                        "manual_required": True,
                        "portal_url": None,
                        "end_time": end_time,
                        "stage": f"chatgpt_api_failed:{direct_error_code or 'error'}",
                        "message": "ChatGPT 退订接口返回失败，请稍后重试或前往 ChatGPT 设置 → 账单页面手动取消。",
                    }

        # Path 2B (Legacy fallback): Stripe Customer Portal session URL (pay.openai.com/p/session/...)
        portal_url, _source, portal_attempts = _request_portal_url(
            client, resolved_account_id, float(ORDER_HTTP_TIMEOUT_S), allow_refresh=True,
        )
        if not portal_url and _portal_attempts_transport_failed(portal_attempts):
            for idx, fallback_proxy in enumerate(candidates):
                if fallback_proxy == active_proxy:
                    continue
                logger.warning(
                    "auto_renew_proxy_retry stage=customer_portal attempt=%s",
                    idx + 1,
                )
                retry_client = _build_auto_renew_client(
                    session,
                    credentials={**credentials, "access_token": getattr(client, "access_token", credentials["access_token"])},
                    resolved_account_id=resolved_account_id,
                    country=country,
                    proxy=fallback_proxy,
                )
                if getattr(client, "session_token", "") and hasattr(retry_client, "session_token"):
                    retry_client.session_token = client.session_token
                try:
                    portal_url, _source, portal_attempts = _request_portal_url(
                        retry_client, resolved_account_id, float(ORDER_HTTP_TIMEOUT_S), allow_refresh=True,
                    )
                except Exception:  # noqa: BLE001
                    retry_client.close()
                    continue
                if portal_url:
                    client.close()
                    client = retry_client
                    active_proxy = fallback_proxy
                    break
                retry_client.close()
                if not _portal_attempts_transport_failed(portal_attempts):
                    break
        if not portal_url:
            if _portal_attempts_transport_failed(portal_attempts):
                raise DomainError(
                    ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                    "获取 Stripe 账单门户链接时出口代理断连，尚未提交关闭续费，请稍后重试。",
                    detail={"code": "proxy_tunnel_failed", "stage": "customer_portal"},
                )
            return {
                "canceled": False,
                "will_renew": True,
                "manual_required": True,
                "portal_url": None,
                "end_time": end_time,
                "stage": "no_portal_url",
                "message": "未能获取到官方 Stripe 账单门户链接（可能为非网页渠道或团队账号），请前往官网设置手动关闭。",
            }

        retry_proxy = _fresh_exit(active_proxy) if active_proxy else configured_proxy("checkout")
        portal_res = cancel_portal_subscription(
            portal_url,
            proxy=active_proxy,
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
        if client is not None:
            client.close()


__all__ = ["cancel_account_auto_renew"]
