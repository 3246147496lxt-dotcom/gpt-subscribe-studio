"""Account subscription detail: credentials, upstream fan-out, normalization.

Only non-secret fields are returned or persisted. The access token and the
session cookie stay in memory for the duration of one request.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ACCOUNTS_CHECK_PATH, APP_BASE, ChatGPTClient, _json
from gss_core.config import ORDER_HTTP_TIMEOUT_S
from gss_core.countries import locale_for, timezone_for
from gss_core.proxies import (
    align_proxy_region,
    configured_proxy,
    cooling_sids,
    fresh_proxy_sid,
    rewrite_proxy_exit,
)
from gss_core.redaction import redact_sensitive
from gss_core.session_token import parse_session_bundle

from .stripe_portal_invoices import fetch_portal_invoices, safe_invoice_url

logger = logging.getLogger("gss.account_detail")

BEIJING = timezone(timedelta(hours=8))
# Bump when the normalized shape changes; old cache rows are ignored.
CACHE_VERSION = 9
SUBSCRIPTIONS_PATH = "/backend-api/subscriptions"
TRANSACTION_HISTORY_PATH = "/backend-api/payments/transaction-history"
INVOICES_PATH = "/backend-api/payments/invoices"
BILLING_HISTORY_PATH = "/backend-api/payments/billing_history"
RECEIPTS_PATH = "/backend-api/payments/receipts"

# Stripe zero-decimal currencies (minor unit == major unit; never divide by 100).
_ZERO_DECIMAL_CURRENCIES = frozenset({
    "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA",
    "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
})

_PLAN_LABELS = {
    "plus": "ChatGPT Plus",
    "chatgptplusplan": "ChatGPT Plus",
    "go": "ChatGPT Go",
    "chatgptgoplan": "ChatGPT Go",
    "pro_lite": "ChatGPT Pro Lite",
    "chatgptprolite": "ChatGPT Pro Lite",
    "pro": "ChatGPT Pro",
    "chatgptpro": "ChatGPT Pro",
    "pro_max": "ChatGPT Pro Max",
    "chatgptpromax": "ChatGPT Pro Max",
    "team": "ChatGPT Team",
    "chatgptteamplan": "ChatGPT Team",
    "free": "ChatGPT 免费版",
    "chatgptfreeplan": "ChatGPT 免费版",
}
_INVOICE_STATUS = {
    "paid": "已支付",
    "succeeded": "已支付",
    "open": "待支付",
    "pending": "待支付",
    "void": "已作废",
    "voided": "已作废",
    "uncollectible": "无法扣款",
    "refunded": "已退款",
    "failed": "支付失败",
}
_CHANNEL_LABELS = {
    "stripe": "网页支付",
    "web": "网页支付",
    "web_payment": "网页支付",
    "chatgpt_web": "网页支付",
    "apple": "App Store",
    "app_store": "App Store",
    "ios": "App Store",
    "chatgpt_ios": "App Store",
    "ios_app_store": "App Store",
    "mobile_ios": "App Store",
    "google": "Google Play",
    "google_play": "Google Play",
    "android": "Google Play",
    "chatgpt_android": "Google Play",
    "mobile_android": "Google Play",
}


class CredentialError(DomainError):
    def __init__(self, code: str, message: str, detail: Optional[dict] = None) -> None:
        super().__init__(ErrorCode.INVALID_SESSION, message, detail={"code": code, **(detail or {})})


def _jwt_claims(token: str) -> Dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        padded = parts[1] + ("=" * (-len(parts[1]) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_credentials(session: str) -> Dict[str, str]:
    """Return ``access_token`` / ``email`` / ``account_id`` or raise a typed error."""
    text = str(session or "").strip()
    if not text:
        raise CredentialError("session_required", "请先粘贴 Session JSON。")
    try:
        bundle = parse_session_bundle(text)
    except ValueError:
        raise CredentialError("invalid_session", "Session JSON 格式无效，请粘贴完整内容。") from None
    token = bundle.access_token
    if not token:
        raise CredentialError("invalid_session", "Session 中缺少 accessToken，请重新登录后导出。")
    claims = _jwt_claims(token)
    expires = claims.get("exp")
    if isinstance(expires, (int, float)) and expires <= datetime.now(timezone.utc).timestamp():
        raise CredentialError("session_expired", "accessToken 已过期，请重新登录 ChatGPT 后导出新 Session。")
    profile = claims.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, dict) else {}
    email = str(profile.get("email") or claims.get("email") or "").strip().lower()
    account_id = ""
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        account_id = str(auth.get("chatgpt_account_id") or "").strip()
    return {
        "access_token": token,
        "email": email,
        "account_id": account_id,
        "session_country": str(bundle.country or "").upper(),
    }


# ── upstream fan-out ──────────────────────────────────────────────────
def _header(response: Any, name: str) -> Optional[str]:
    headers = getattr(response, "headers", None)
    try:
        value = headers.get(name) if headers is not None else None
    except Exception:  # noqa: BLE001
        value = None
    if value is None and headers is not None:
        for key in getattr(headers, "keys", lambda: [])():
            if str(key).lower() == name.lower():
                try:
                    value = headers[key]
                except Exception:  # noqa: BLE001
                    value = None
                break
    return str(value)[:300] if value else None


def _safe_body(response: Any) -> str:
    raw = str(getattr(response, "text", "") or "")[:1500]
    return redact_sensitive(raw).replace("\n", " ").replace("\r", " ")[:800]


def _wire_code(payload: Dict[str, Any]) -> str:
    for block in (payload.get("error"), payload.get("detail"), payload):
        if isinstance(block, dict):
            value = block.get("code") or block.get("type")
            if isinstance(value, str) and value.strip():
                return value.strip()[:80]
    return ""


def _jwt_exp(token: str) -> Optional[str]:
    claims = _jwt_claims(token)
    value = claims.get("exp")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    return None


def _safe_headers(response: Any) -> Dict[str, str]:
    """Headers useful for diagnosis; credentials and cookies are dropped."""
    try:
        items = dict(getattr(response, "headers", None) or {})
    except Exception:  # noqa: BLE001
        return {}
    return {
        str(key): redact_sensitive(str(value))[:300]
        for key, value in items.items()
        if str(key).lower() not in {"set-cookie", "cookie", "authorization"}
    }


def _log_auth_failure(path: str, response: Any) -> None:
    logger.warning(
        "account_detail_auth path=%s status=%s cf_ray=%s headers=%s body=%s",
        path,
        int(getattr(response, "status_code", 0) or 0),
        _header(response, "cf-ray"),
        _safe_headers(response),
        _safe_body(response),
    )


def _auth_error(path: str, response: Any, client: ChatGPTClient,
                session_token_present: bool) -> DomainError:
    payload = _json(response)
    wire = _wire_code(payload)
    detail = {
        "code": wire or "upstream_unauthorized",
        "upstream_status": 401,
        "upstream_code": wire or None,
        "upstream_message": _safe_body(response)[:240],
        "cf_ray": _header(response, "cf-ray"),
        "www_authenticate": _header(response, "www-authenticate"),
        "jwt_expires_at": _jwt_exp(getattr(client, "access_token", "")),
        "session_token_present": session_token_present,
        "path": path,
    }
    if wire in {"token_expired", "expired_token"}:
        message = "accessToken 已过期，且当前 Session 无法自动续期，请重新登录。"
    elif wire == "token_revoked":
        message = "上游明确撤销了该 accessToken（token_revoked），请重新登录后导出新 Session。"
    elif session_token_present:
        message = "上游拒绝了当前 accessToken，已尝试用 Session 自动续期但仍失败。"
    else:
        message = "上游拒绝当前 accessToken（401），且 Session 中没有可续期的 sessionToken。"
    return DomainError(ErrorCode.INVALID_SESSION, message, detail=detail)


def _forbidden_error(path: str, response: Any) -> DomainError:
    body = _safe_body(response).lower()
    challenge = ("just a moment" in body or "cf-chl" in body
                 or "challenge-platform" in body or "cf-challenge" in body)
    detail = {
        "code": "cloudflare_challenge" if challenge else "upstream_forbidden",
        "upstream_status": 403,
        "cf_ray": _header(response, "cf-ray"),
        "upstream_message": _safe_body(response)[:240],
        "path": path,
    }
    message = ("Cloudflare 拦截了查询请求（403 challenge），请检查出口 IP 与请求头。"
               if challenge else "上游拒绝查询（HTTP 403）。")
    return DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, message, detail=detail)


def _structure(payload: Any, depth: int = 0) -> Any:
    """Keys and array lengths only: enough to verify a mapping, no values."""
    if depth >= 5:
        return "..."
    if isinstance(payload, dict):
        out: Dict[str, Any] = {"type": "object", "keys": sorted(payload)[:24]}
        for key in ("items", "invoices", "receipts", "data", "billing_history",
                    "accounts", "entitlement", "current_billing_period"):
            if key in payload:
                out[key] = _structure(payload[key], depth + 1)
        return out
    if isinstance(payload, list):
        return {"type": "array", "len": len(payload),
                "first": _structure(payload[0], depth + 1) if payload else None}
    return type(payload).__name__


def _request(client: ChatGPTClient, path: str, params: Optional[dict]):
    headers = client._route_headers(path)
    headers.update(client._identity_headers())
    response = client.http.get(APP_BASE + path, params=params, headers=headers, timeout=client.timeout)
    status = int(getattr(response, "status_code", 0) or 0)
    if status in {401, 403}:
        _log_auth_failure(path, response)
        return None, status, response
    if status == 404:
        logger.info("account_detail_upstream path=%s status=404", path)
        return None, status, response
    if status >= 400:
        logger.info("account_detail_upstream path=%s status=%s", path, status)
        return None, status, response
    payload = _json(response)
    logger.info("account_detail_upstream path=%s status=%s structure=%s",
                path, status, json.dumps(_structure(payload), ensure_ascii=False)[:1200])
    if path == SUBSCRIPTIONS_PATH:
        for key in ("current_billing_period", "scheduled_billing_period",
                    "last_active_subscription", "entitlement"):
            if isinstance(payload.get(key), (dict, list)):
                logger.info("account_detail_sub key=%s structure=%s", key,
                            json.dumps(_structure(payload[key]), ensure_ascii=False)[:700])
    if path == ACCOUNTS_CHECK_PATH and isinstance(payload.get("accounts"), dict):
        for name, item in list(payload["accounts"].items())[:2]:
            logger.info("account_detail_account key=%s structure=%s", name,
                        json.dumps(_structure(item), ensure_ascii=False)[:700])
    if not payload:
        logger.info("account_detail_upstream_empty path=%s", path)
    return payload, status, response


def _detail_read_proxy(country: str) -> Optional[str]:
    """查详情时用一条与充值出口不强相关的会话 SID。

    查订阅/账单详情若直接复用刚被建单绑定的那条出口 SID，等于把「读详情」
    和「充值」压在同一身份上；换个 SID 可以避免前序查询给后续建单连带风控。
    ``GSS_ACCOUNT_DETAIL_SID_ISOLATION=0`` 可关闭。
    """
    proxy = configured_proxy("checkout")
    if not proxy:
        return None
    if country:
        proxy = align_proxy_region(proxy, country)
    if os.environ.get("GSS_ACCOUNT_DETAIL_SID_ISOLATION", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return proxy
    try:
        return rewrite_proxy_exit(proxy, sid=fresh_proxy_sid(exclude=cooling_sids()))
    except Exception:  # noqa: BLE001
        return proxy


def _detail_read_client(session: str, proxy: Optional[str], *,
                        timeout: Optional[float] = None,
                        country: str = "") -> ChatGPTClient:
    """只读客户端：沿用 Session 自带的 device_id / UA 与地区语言。

    每次随机新 device_id + 随机 OS 会在建单前制造「同账号多台设备短时间跳变」
    的高危信号，还可能把用户原 AT 顶掉。只在 Session 缺字段时回退伪造档案。
    """
    bundle = parse_session_bundle(session)
    client = ChatGPTClient.from_raw_session(session, proxy=proxy, strip_ambient=True)
    if bundle.device_id:
        client.device_id = bundle.device_id
        client.extra_cookies["oai-did"] = bundle.device_id
    if bundle.user_agent:
        client.user_agent = bundle.user_agent
    effective_country = str(country or bundle.country or "").strip().upper()
    if effective_country:
        client.language = locale_for(effective_country)
        client.timezone = timezone_for(effective_country)
    if timeout is not None:
        client.timeout = timeout
    return client


def _fetch_serial(session: str, access_token: str, calls, *,
                  account_id: str = "", proxy: Optional[str], timeout: float,
                  include_invoices: bool = True, country: str = "") -> Dict[str, Any]:
    """One client, one exit, one device id, endpoints strictly in order.

    A 401 on the first request is retried once after exchanging the session
    cookie for a fresh access token; only a second failure is reported.
    When ChatGPT's direct billing endpoints return no invoices for a Stripe
    account, falls back to reading invoice history from the account's Stripe
    Customer Portal session (strictly read-only, no ChatGPT credentials sent
    to Stripe).
    """
    client = _detail_read_client(session, proxy, timeout=timeout, country=country)
    client.access_token = access_token
    merged: Dict[str, Any] = {}
    failures: List[str] = []
    refreshed = False
    try:
        for key, path, params in calls:
            # Skip legacy 404 invoice guesses once the official transaction-history endpoint answered 200.
            if merged.get("_tx_history_ok") and key in {
                "invoices", "billing_history", "receipts", "account_billing",
                "account_invoices", "transactions",
            }:
                continue
            call_params = dict(params) if isinstance(params, dict) else params
            if key == "transaction_history":
                resolved_aid = account_id or str(_deep(merged.get("accounts"), ("account_id",)) or "")
                call_params = {"limit": "20"}
                if resolved_aid:
                    call_params["account_id"] = resolved_aid
            try:
                payload, status, response = _request(client, path, call_params)
            except DomainError:
                raise
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{path}:{type(exc).__name__}")
                logger.warning("account_detail_transport path=%s error=%s",
                               path, type(exc).__name__)
                continue
            if status == 401 and not refreshed:
                refreshed = True
                session_token_present = bool(getattr(client, "session_token", ""))
                if session_token_present and client.refresh_session(
                        trust_statsig=False, reject_unauthorized=False):
                    logger.info("account_detail_token_refreshed path=%s", path)
                    try:
                        payload, status, response = _request(client, path, call_params)
                    except DomainError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        failures.append(f"{path}:{type(exc).__name__}")
                        logger.warning("account_detail_transport path=%s error=%s",
                                       path, type(exc).__name__)
                        continue
            if status == 401:
                raise _auth_error(path, response, client, bool(getattr(client, "session_token", "")))
            if status == 403:
                raise _forbidden_error(path, response)
            if status == 404:
                continue
            if status >= 400:
                failures.append(f"{path}:{status}")
                continue
            if payload:
                if key == "transaction_history" and isinstance(payload.get("transactions"), list):
                    tx_items = list(payload["transactions"])
                    next_cursor = payload.get("next_cursor")
                    # Follow at most one cursor page if OpenAI capped page 1 below 20 items.
                    if isinstance(next_cursor, str) and next_cursor.strip() and len(tx_items) < 20:
                        page2_params = dict(call_params or {})
                        page2_params["cursor"] = next_cursor.strip()
                        try:
                            p2, s2, _r2 = _request(client, path, page2_params)
                            if s2 == 200 and isinstance(p2, dict) and isinstance(p2.get("transactions"), list):
                                tx_items.extend(p2["transactions"])
                        except Exception:  # noqa: BLE001
                            pass
                    merged[key] = {"transactions": tx_items[:20]}
                    merged["_tx_history_ok"] = True
                else:
                    merged[key] = payload
        if failures and not merged:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                              "订阅接口暂时不可达，请稍后重试。",
                              detail={"code": "upstream_unavailable",
                                      "reason": failures[0][:80]})
        if not include_invoices:
            if _should_fetch_portal_invoices(merged):
                merged["_portal_meta"] = {"attempted": False, "stage": "deferred", "has_portal": True}
            return merged
        if _should_fetch_portal_invoices(merged):
            resolved_account_id = account_id or str(_deep(merged.get("accounts"), ("account_id",)) or "")
            try:
                portal_url, _source, _attempts = _request_portal_url(
                    client, resolved_account_id, timeout, allow_refresh=not refreshed,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("account_detail_portal_fallback_error type=%s", type(exc).__name__)
                merged["_portal_meta"] = {"attempted": True, "stage": "portal_error", "has_portal": False}
            else:
                if portal_url:
                    portal_items, stage = fetch_portal_invoices(
                        portal_url, proxy=proxy, timeout=max(float(timeout), 25.0),
                    )
                    if portal_items:
                        merged["portal_invoices"] = portal_items
                    merged["_portal_meta"] = {"attempted": True, "stage": stage, "has_portal": True}
                else:
                    merged["_portal_meta"] = {"attempted": True, "stage": "no_portal_url", "has_portal": False}
        return merged
    finally:
        client.close()


async def fetch_basic_snapshot(session: str, access_token: str, *, account_id: str,
                               country: str, timeout: float = ORDER_HTTP_TIMEOUT_S) -> Dict[str, Any]:
    """Fast phase-1 read: accounts/check + subscriptions + payments/transaction-history (~1-2s)."""
    proxy = _detail_read_proxy(country)
    account_params = {"account_id": account_id} if account_id else None
    tx_params = {"limit": "20", **({"account_id": account_id} if account_id else {})}
    calls = (
        ("accounts", ACCOUNTS_CHECK_PATH, {"timezone_offset_min": "0"}),
        ("subscriptions", SUBSCRIPTIONS_PATH, account_params),
        ("transaction_history", TRANSACTION_HISTORY_PATH, tx_params),
    )
    return await asyncio.to_thread(
        _fetch_serial,
        session,
        access_token,
        calls,
        account_id=account_id,
        proxy=proxy,
        timeout=timeout,
        include_invoices=False,
        country=country,
    )


def _fetch_billing_history_sync(
    session: str,
    access_token: str,
    *,
    account_id: str,
    country: str,
    default_plan: str,
    fallback_currency: str,
    timeout: float,
) -> Dict[str, Any]:
    proxy = _detail_read_proxy(country)
    retry_proxy = _detail_read_proxy(country)
    client = _detail_read_client(session, proxy, timeout=min(float(timeout), 20.0), country=country)
    client.access_token = access_token
    portal_url = ""
    tx_payload: Optional[Dict[str, Any]] = None
    try:
        tx_params = {"limit": "20", **({"account_id": account_id} if account_id else {})}
        try:
            payload, status, _resp = _request(client, TRANSACTION_HISTORY_PATH, tx_params)
            if status == 401 and getattr(client, "session_token", "") and client.refresh_session(
                trust_statsig=False, reject_unauthorized=False,
            ):
                payload, status, _resp = _request(client, TRANSACTION_HISTORY_PATH, tx_params)
            if status == 200 and isinstance(payload, dict) and isinstance(payload.get("transactions"), list):
                tx_payload = payload
        except Exception:  # noqa: BLE001
            tx_payload = None
        if tx_payload is None:
            portal_url, _source, _attempts = _request_portal_url(
                client, account_id, min(float(timeout), 20.0), allow_refresh=True,
            )
    finally:
        client.close()

    safe_default_plan = (
        ""
        if str(default_plan or "").strip() in {"", "免费套餐", "ChatGPT 免费版", "未知套餐"}
        else default_plan
    )

    if tx_payload is not None:
        records = _invoice_records({"transaction_history": tx_payload}, default_plan=safe_default_plan)
        currency = _pick_currency(records, {}, {})
        if currency == "-" and fallback_currency:
            currency = fallback_currency
        invoices = [{
            "plan": row["plan"],
            "paidAt": to_beijing(row["moment"]) or "-",
            "status": row["status"],
            "amount": _amount(
                row["amount_raw"],
                row["currency"] or currency,
                amount_unit=row.get("amount_unit", ""),
            ),
            "invoiceId": row.get("invoice_id") or None,
            "invoiceUrl": row.get("invoice_url"),
        } for row in records[:20]]
        return {
            "invoices": invoices,
            "billingSource": "upstream_api" if invoices else "none",
            "billingNote": None if invoices else "当前账号暂无历史账单记录。",
            "currency": currency,
            "retryable": False,
            "verified": True,
        }

    if not portal_url:
        return {
            "invoices": [],
            "billingSource": "none",
            "billingNote": "未获取到该账号的 Stripe 账单门户（可能是免费、团队或非 Stripe 渠道订阅）。",
            "currency": fallback_currency or "-",
            "retryable": False,
            "verified": False,
        }

    portal_items, stage = fetch_portal_invoices(
        portal_url, proxy=proxy, retry_proxy=retry_proxy, timeout=max(float(timeout), 25.0),
    )
    if portal_items:
        records = _invoice_records({"portal_invoices": portal_items}, default_plan=safe_default_plan)
        currency = _pick_currency(records, {}, {})
        if currency == "-" and fallback_currency:
            currency = fallback_currency
        invoices = [{
            "plan": row["plan"],
            "paidAt": to_beijing(row["moment"]) or "-",
            "status": row["status"],
            "amount": _amount(
                row["amount_raw"],
                row["currency"] or currency,
                amount_unit=row.get("amount_unit", ""),
            ),
            "invoiceId": row.get("invoice_id") or None,
            "invoiceUrl": row.get("invoice_url"),
        } for row in records[:20]]
        return {
            "invoices": invoices,
            "billingSource": "stripe_portal",
            "billingNote": None,
            "currency": currency,
            "retryable": False,
            "verified": True,
        }

    if stage == "browser_verified_empty":
        return {
            "invoices": [],
            "billingSource": "stripe_portal_empty",
            "billingNote": "Stripe 官方账单门户当前暂无历史账单记录（如免费试用或尚未出账）。",
            "currency": fallback_currency or "-",
            "retryable": False,
            "verified": True,
        }

    if stage == "browser_disabled":
        return {
            "invoices": [],
            "billingSource": "stripe_portal_empty",
            "billingNote": "已关闭自动抓取 Stripe 门户页面，可点击右上角「打开 Stripe 官方账单明细」查看。",
            "currency": fallback_currency or "-",
            "retryable": False,
            "verified": False,
        }

    return {
        "invoices": [],
        "billingSource": "stripe_portal_timeout",
        "billingNote": "官方账单门户加载超时（出口代理波动），可点击「重试读取账单」或右上角直接打开官方门户。",
        "currency": fallback_currency or "-",
        "retryable": True,
        "verified": False,
    }


async def fetch_billing_history(
    session: str,
    access_token: str,
    *,
    account_id: str,
    country: str,
    default_plan: str = "",
    fallback_currency: str = "",
    timeout: float = 30.0,
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _fetch_billing_history_sync,
        session,
        access_token,
        account_id=account_id,
        country=country,
        default_plan=default_plan,
        fallback_currency=fallback_currency,
        timeout=timeout,
    )


async def fetch_snapshot(session: str, access_token: str, *, account_id: str,
                         country: str, timeout: float = ORDER_HTTP_TIMEOUT_S) -> Dict[str, Any]:
    """Serial reads on one exit; parallel tunnels were rejected by the proxy."""
    proxy = _detail_read_proxy(country)
    account_params = {"account_id": account_id} if account_id else None
    tx_params = {"limit": "20", **({"account_id": account_id} if account_id else {})}
    calls = [
        ("accounts", ACCOUNTS_CHECK_PATH, {"timezone_offset_min": "0"}),
        ("subscriptions", SUBSCRIPTIONS_PATH, account_params),
        ("transaction_history", TRANSACTION_HISTORY_PATH, tx_params),
        ("invoices", INVOICES_PATH, account_params),
        ("billing_history", BILLING_HISTORY_PATH, account_params),
        ("receipts", RECEIPTS_PATH, account_params),
    ]
    if account_id:
        calls.append(("account_billing",
                      f"/backend-api/accounts/{account_id}/billing_history", None))
        calls.append(("account_invoices",
                      f"/backend-api/accounts/{account_id}/invoices", None))
    calls.append(("transactions", "/backend-api/payments/transactions", account_params))
    return await asyncio.to_thread(_fetch_serial, session, access_token, tuple(calls),
                                   account_id=account_id, proxy=proxy, timeout=timeout,
                                   country=country)


# ── normalization ────────────────────────────────────────────────────
def _deep(
    payload: Any,
    keys: tuple[str, ...],
    depth: int = 0,
    *,
    include_last_active: bool = True,
) -> Any:
    if depth > 6:
        return None
    if isinstance(payload, dict):
        for key in keys:
            if key in payload and payload[key] not in (None, "", [], {}):
                return payload[key]
        nested_keys = (
            ("entitlement", "subscription", "last_active_subscription",
             "current_subscription", "data", "result", "accounts", "default", "account")
            if include_last_active
            else ("entitlement", "subscription",
                  "current_subscription", "data", "result", "accounts", "default", "account")
        )
        for key in nested_keys:
            if key in payload:
                found = _deep(payload[key], keys, depth + 1, include_last_active=include_last_active)
                if found is not None:
                    return found
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _moment(value: Any) -> Optional[datetime]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10**12:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,13}", text):
        return _moment(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def to_beijing(value: Any) -> Optional[str]:
    moment = _moment(value)
    return moment.astimezone(BEIJING).strftime("%Y/%m/%d %H:%M") if moment else None


def _plan_label(value: Any, *, fallback: str = "") -> str:
    raw = str(value or "").strip()
    key = raw.lower()
    if key in _PLAN_LABELS:
        return _PLAN_LABELS[key]
    if key:
        if any(tok in key for tok in ("pro max", "pro_max", "pro-max", "promax", "chatgptpromax")):
            return "ChatGPT Pro Max"
        if any(tok in key for tok in ("pro lite", "pro_lite", "pro-lite", "prolite", "chatgptprolite", "pro standard", "pro 5x")):
            return "ChatGPT Pro Lite"
        if "chatgpt pro" in key or "chatgptpro" in key:
            return "ChatGPT Pro"
        if "chatgpt team" in key or "chatgpt business" in key or "chatgptteam" in key:
            return "ChatGPT Team"
        if "chatgpt go" in key or "chatgptgo" in key:
            return "ChatGPT Go"
        if "chatgpt plus" in key or "chatgptplus" in key:
            return "ChatGPT Plus"
        if "chatgpt free" in key or "chatgptfree" in key:
            return "ChatGPT 免费版"
        return redact_sensitive(raw)[:60]
    return fallback or "未知套餐"


def _channel_label(subs: Dict[str, Any], accounts: Dict[str, Any]) -> str:
    channel_source = str(
        _deep(subs, ("payment_provider", "payment_channel", "channel",
                     "payment_method_type", "purchase_origin_platform"))
        or _deep(accounts, ("payment_provider", "payment_channel", "purchase_origin_platform"))
        or ""
    ).strip().lower()
    if channel_source in _CHANNEL_LABELS:
        return _CHANNEL_LABELS[channel_source]
    if any(tok in channel_source for tok in ("apple", "ios", "app_store")):
        return "App Store"
    if any(tok in channel_source for tok in ("google", "android")):
        return "Google Play"
    if any(tok in channel_source for tok in ("stripe", "web")):
        return "网页支付"
    if _as_bool(_deep(subs, ("is_processor_stripe",))) is True:
        return "网页支付"
    return "网页支付" if not channel_source else channel_source


def _should_fetch_portal_invoices(merged: Dict[str, Any]) -> bool:
    """Decide whether to read historical bills from Stripe Customer Portal."""
    if merged.get("_tx_history_ok"):
        return False
    if _invoice_records(merged):
        return False
    subs = merged.get("subscriptions") if isinstance(merged.get("subscriptions"), dict) else {}
    accounts = merged.get("accounts") if isinstance(merged.get("accounts"), dict) else {}
    if not subs and not accounts:
        return False
    is_stripe = _as_bool(_deep(subs, ("is_processor_stripe",)))
    channel = _channel_label(subs, accounts)
    if channel in {"App Store", "Google Play"} and is_stripe is not True:
        return False
    if is_stripe is True:
        return True
    if _as_bool(_deep(subs, ("is_eligible_for_cp",))) is True:
        return True
    if _as_bool(_deep(subs, ("is_active", "active", "has_active_subscription"))) is True:
        return True
    if _deep(subs, ("subscription_id",)) or _deep(accounts, ("subscription_id",)):
        return True
    if isinstance(subs.get("last_active_subscription"), dict) and subs["last_active_subscription"]:
        return True
    if _deep(accounts, ("last_active_subscription",)):
        return True
    plan_source = (_deep(subs, ("plan_type", "subscription_plan", "plan", "plan_name"))
                   or _deep(accounts, ("plan_type", "subscription_plan")))
    plan_key = str(plan_source or "").strip().lower()
    return bool(plan_key and plan_key not in {"free", "chatgptfreeplan"})


def _amount(value: Any, currency: str, *, amount_unit: str = "") -> str:
    cur = "" if str(currency or "").strip() in {"", "-"} else str(currency).strip().upper()
    if isinstance(value, dict):
        if value.get("currency"):
            cur = str(value["currency"]).strip().upper()
        zero_decimal = cur in _ZERO_DECIMAL_CURRENCIES
        minor = value.get("minorUnitsAmount")
        if isinstance(minor, int) and not isinstance(minor, bool):
            number: Any = Decimal(minor) if zero_decimal else Decimal(minor) / 100
        else:
            number = value.get("amount")
            if number is None:
                number = value.get("value")
    else:
        zero_decimal = cur in _ZERO_DECIMAL_CURRENCIES
        number = value
    if number is None or number == "":
        return "-"
    try:
        amount = Decimal(str(number))
        if not isinstance(value, dict):
            text = str(number).strip()
            unit = str(amount_unit or "").strip().lower()
            if unit == "major":
                pass
            elif unit == "minor":
                if not zero_decimal and (
                    (isinstance(number, int) and not isinstance(number, bool))
                    or text.lstrip("-").isdigit()
                ):
                    amount = amount / 100
            else:
                # Billing endpoints report integer minor units (98214 -> 982.14).
                if not zero_decimal and text.isdigit() and amount >= 100:
                    amount = amount / 100
    except (InvalidOperation, ValueError):
        return f"{redact_sensitive(str(number))[:30]} {cur}".strip()
    if zero_decimal and amount == amount.to_integral_value():
        formatted = f"{int(amount)}"
    else:
        formatted = f"{amount.quantize(Decimal('0.01')):.2f}"
    return f"{formatted} {cur}".strip() if cur else formatted


def _invoice_items(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "invoices", "receipts", "data", "billing_history",
                "results", "history", "transactions"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _invoice_records(merged: Dict[str, Any], *, default_plan: str = "") -> List[dict]:
    """Merge every billing source, de-duplicate, newest first."""
    candidates: List[tuple[dict, str, str]] = []
    for item in _invoice_items(merged.get("transaction_history")):
        candidates.append((item, "upstream_api", "minor"))
    for key in ("invoices", "billing_history", "receipts", "account_billing",
                "account_invoices", "transactions"):
        for item in _invoice_items(merged.get(key)):
            candidates.append((item, "upstream_api", ""))
    for item in _invoice_items(merged.get("portal_invoices")):
        candidates.append((item, "stripe_portal", ""))
    subs = merged.get("subscriptions") if isinstance(merged.get("subscriptions"), dict) else {}
    period = subs.get("current_billing_period") if isinstance(subs, dict) else None
    if isinstance(period, dict) and _deep(period, ("amount_paid", "total", "amount",
                                                    "total_amount", "price")):
        candidates.append((period, "current_period", ""))
    records: List[dict] = []
    seen: Dict[tuple[str, ...], int] = {}
    for item, source, default_unit in candidates:
        moment = _moment(_deep(item, ("paid_at", "created", "date", "invoice_date",
                                      "created_at", "period_start")))
        amount_raw = _deep(item, ("amount_paid", "total", "amount", "total_amount", "price"))
        currency = str(_deep(item, ("currency", "currency_code")) or "").upper()
        status_key = str(_deep(item, ("status", "payment_status")) or "").strip().lower()
        invoice_id = str(item.get("id") or item.get("invoice_id") or item.get("number") or "").strip() or None
        invoice_url = safe_invoice_url(
            item.get("invoice_url") or item.get("hosted_invoice_url") or item.get("invoice_pdf")
        )
        signature = (
            ("id", invoice_id)
            if invoice_id
            else ("fields", moment.isoformat() if moment else "", str(amount_raw), currency)
        )
        if signature in seen:
            existing = records[seen[signature]]
            if invoice_url and not existing.get("invoice_url"):
                existing["invoice_url"] = invoice_url
            if invoice_id and not existing.get("invoice_id"):
                existing["invoice_id"] = invoice_id
            continue
        seen[signature] = len(records)
        product_obj = item.get("product") if isinstance(item.get("product"), dict) else {}
        raw_plan = (
            product_obj.get("plan")
            or product_obj.get("plan_type")
            or product_obj.get("subscription_plan")
            or product_obj.get("name")
            or _deep(item, ("plan_name", "plan", "subscription_plan",
                            "description", "product_name"))
        )
        records.append({
            "moment": moment,
            "plan": _plan_label(raw_plan, fallback=default_plan),
            "status": _INVOICE_STATUS.get(status_key, "未知"),
            "amount_raw": amount_raw,
            "amount_unit": str(item.get("amount_unit") or default_unit),
            "currency": currency,
            "invoice_id": invoice_id,
            "invoice_url": invoice_url,
            "source": source,
        })
    records.sort(key=lambda row: row["moment"] or datetime.min.replace(tzinfo=timezone.utc),
                 reverse=True)
    return records


def _pick_currency(records: List[dict], subs: Dict[str, Any], accounts: Dict[str, Any]) -> str:
    # A settled invoice is the strongest evidence of the billing currency.
    for row in records:
        if row["currency"]:
            return row["currency"]
    for source in (subs, accounts):
        value = str(_deep(source, ("billing_currency", "currency", "currency_code",
                                   "plan_currency")) or "").upper()
        if value:
            return value
    return "-"


def _billing_meta(
    records: List[dict],
    *,
    payment_channel: str,
    status: str,
    is_free: bool,
    portal_meta: Dict[str, Any],
) -> tuple[str, Optional[str]]:
    if records:
        sources = {row.get("source") for row in records}
        if "upstream_api" in sources:
            return "upstream_api", None
        if "stripe_portal" in sources:
            return "stripe_portal", None
        return "current_period", None
    if payment_channel in {"App Store", "Google Play"}:
        return (
            "mobile_store",
            f"当前订阅由 {payment_channel} 代扣，ChatGPT 网页端与 Stripe 门户不保存移动端历史账单，请在对应应用商店查看。",
        )
    if portal_meta.get("stage") == "deferred":
        return "loading", None
    if status == "未订阅" and is_free and not portal_meta.get("attempted"):
        return "none", "当前为免费账号，暂无付费账单记录。"
    if portal_meta.get("attempted"):
        stage = str(portal_meta.get("stage") or "")
        if stage == "browser_disabled":
            return (
                "stripe_portal_empty",
                "已关闭自动抓取 Stripe 门户页面，可点击右上角「打开 Stripe 官方账单明细」查看。",
            )
        if stage in {"browser_unavailable", "browser_error", "browser_timeout"}:
            return (
                "stripe_portal_timeout",
                "官方账单门户加载超时（出口代理波动），请点击重试或点击右上角「打开 Stripe 官方账单明细」查看。",
            )
        if stage in {"no_portal_url", "portal_error"}:
            return (
                "none",
                "未获取到该账号的 Stripe 账单门户（可能是免费、团队或非 Stripe 渠道订阅），可尝试点击右上角按钮或前往官网查看。",
            )
        return (
            "stripe_portal_empty",
            "Stripe 账单门户暂未返回历史账单（如免费试用、尚未出账或页面需要手动验证），可点击右上角「打开 Stripe 官方账单明细」直接查看。",
        )
    return "none", "上游未返回历史账单，可点击右上角「打开 Stripe 官方账单明细」查看。"


def normalize(merged: Dict[str, Any], *, email: str, account_id: str,
              now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    accounts = merged.get("accounts") if isinstance(merged.get("accounts"), dict) else {}
    subs = merged.get("subscriptions") if isinstance(merged.get("subscriptions"), dict) else {}
    portal_meta = merged.get("_portal_meta") if isinstance(merged.get("_portal_meta"), dict) else {}

    # Inspect current subscription state WITHOUT conflating historical `last_active_subscription`.
    plan_source = (
        _deep(subs, ("plan_type", "subscription_plan", "plan", "plan_name"), include_last_active=False)
        or _deep(accounts, ("plan_type", "subscription_plan"), include_last_active=False)
    )
    plan_key = str(plan_source or "").strip().lower()
    is_free = plan_key in {"", "free", "chatgptfreeplan"}

    active_flag = _as_bool(
        _deep(subs, ("is_active", "active", "has_active_subscription"), include_last_active=False)
    )
    if active_flag is None:
        active_flag = _as_bool(
            _deep(accounts, ("has_active_subscription", "is_active", "active"), include_last_active=False)
        )

    end = (
        _deep(
            subs,
            ("current_period_end", "expires_at", "active_until", "end_date", "renews_at", "expiration_time"),
            include_last_active=False,
        )
        or _deep(
            accounts,
            ("expires_at", "active_until", "current_period_end"),
            include_last_active=False,
        )
    )
    end_moment = _moment(end)

    # An account whose subscription is inactive, reverted to free, or past its expiry time
    # currently has no active paid plan — normalize cleanly to "未订阅 / 免费套餐 / 不适用"
    # while still retaining Stripe payment channel, currency, and historical invoices.
    if active_flag is False or is_free:
        active = False
    elif active_flag is True:
        active = True
    elif end_moment is not None and end_moment <= now:
        active = False
    else:
        active = not is_free

    status = "使用中" if active else "未订阅"

    remaining: Optional[int] = None
    if active and end_moment is not None and end_moment > now:
        remaining = max(1, math.ceil((end_moment - now).total_seconds() / 86400))

    if active:
        cancel = _as_bool(_deep(subs, ("cancel_at_period_end", "cancel_at_period_end_on")))
        will_renew = _as_bool(_deep(subs, ("will_renew", "auto_renew", "renews")))
        cancels_at = _deep(subs, ("cancels_at",))
        auto_renew = (
            "已关闭"
            if (cancel is True or will_renew is False or (bool(cancels_at) and will_renew is not True))
            else "已开启"
        )
        plan_name = _plan_label(plan_source)
    else:
        auto_renew = "不适用"
        plan_name = "免费套餐"
        end = None
        end_moment = None

    payment_channel = _channel_label(subs, accounts)

    default_invoice_plan = "" if not active or plan_name in {"未知套餐", "免费套餐", "ChatGPT 免费版"} else plan_name
    records = _invoice_records(merged, default_plan=default_invoice_plan)
    currency = _pick_currency(records, subs, accounts)
    invoices = [{
        "plan": row["plan"],
        "paidAt": to_beijing(row["moment"]) or "-",
        "status": row["status"],
        "amount": _amount(
            row["amount_raw"],
            row["currency"] or currency,
            amount_unit=row.get("amount_unit", ""),
        ),
        "invoiceId": row.get("invoice_id") or None,
        "invoiceUrl": row.get("invoice_url"),
    } for row in records[:20]]
    billing_source, billing_note = _billing_meta(
        records,
        payment_channel=payment_channel,
        status=status,
        is_free=not active,
        portal_meta=portal_meta,
    )

    # Start time: only meaningful for an active subscription.
    start_time: Optional[str] = None
    if active:
        start_moment = _moment(_deep(
            subs,
            ("current_period_start", "active_start", "start_date", "started_at",
             "subscription_start", "created_at", "created"),
            include_last_active=False,
        ))
        estimated = False
        if start_moment is None:
            start_moment = next((row["moment"] for row in records
                                 if row["status"] == "已支付" and row["moment"]), None)
        if start_moment is None and end_moment is not None:
            start_moment = end_moment - timedelta(days=30)
            estimated = True
        if start_moment is not None:
            start_time = start_moment.astimezone(BEIJING).strftime("%Y/%m/%d %H:%M")
            if estimated:
                start_time += "（约）"

    return {
        "email": email or str(_deep(accounts, ("email",)) or ""),
        "accountId": account_id or str(_deep(accounts, ("account_id",)) or ""),
        "status": status,
        "planName": plan_name,
        "startTime": start_time,
        "endTime": to_beijing(end),
        "remainingDays": remaining,
        "autoRenew": auto_renew,
        "paymentChannel": payment_channel,
        "currency": currency,
        "invoices": invoices,
        "billingSource": billing_source,
        "billingNote": billing_note,
        "queriedAt": now.astimezone(BEIJING).strftime("%Y/%m/%d %H:%M"),
        "cacheVersion": CACHE_VERSION,
        "cached": False,
        "stale": False,
        "warning": None,
    }


# ── Stripe billing portal ─────────────────────────────────────────────
PORTAL_PATHS = (
    # The GET route answers 200 while POST is 405.
    ("GET", "/backend-api/payments/customer_portal"),
    ("POST", "/backend-api/payments/customer_portal"),
    ("POST", "/backend-api/payments/billing_portal"),
)


def _portal_url(payload: Any, depth: int = 0) -> str:
    """Find the first allowed portal link anywhere in the response."""
    if depth > 6:
        return ""
    if isinstance(payload, str):
        return _safe_portal_url(payload) if payload.startswith("https://") else ""
    if isinstance(payload, dict):
        for key in ("url", "portal_url", "billing_portal_url", "customer_portal_url",
                    "redirect_url", "redirect"):
            value = payload.get(key)
            if isinstance(value, str):
                safe = _safe_portal_url(value)
                if safe:
                    return safe
        for value in payload.values():
            found = _portal_url(value, depth + 1)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload[:10]:
            found = _portal_url(item, depth + 1)
            if found:
                return found
    return ""


_OPENAI_PORTAL_HOSTS = frozenset({"pay.openai.com", "billing.openai.com", "invoice.openai.com"})
_PORTAL_PATH_HINTS = ("/billing", "/portal", "/account", "/p/session", "/payments")


def _safe_portal_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host == "stripe.com" or host.endswith(".stripe.com"):
        allowed = True
    elif host in _OPENAI_PORTAL_HOSTS:
        allowed = True
    elif (
        host == "chatgpt.com"
        or host.endswith(".chatgpt.com")
        or host == "openai.com"
        or host.endswith(".openai.com")
    ):
        # A return_url is not a portal: require a billing/portal/account path.
        allowed = any(part in parsed.path for part in _PORTAL_PATH_HINTS)
    else:
        allowed = False
    return url if parsed.scheme == "https" and allowed else ""


def _safe_url_hint(payload: Any) -> Dict[str, Any]:
    """Non-secret diagnostic summary of the candidate URL field (never logs tokens)."""
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    candidate = None
    found_key = ""
    for key in ("url", "portal_url", "billing_portal_url", "customer_portal_url",
                "redirect_url", "redirect"):
        if key in payload:
            found_key = key
            candidate = payload.get(key)
            break
    if not found_key:
        return {"key": None}
    if candidate is None:
        return {"key": found_key, "type": "NoneType"}
    if not isinstance(candidate, str):
        return {"key": found_key, "type": type(candidate).__name__}
    text = candidate.strip()
    if not text:
        return {"key": found_key, "type": "empty_str"}
    try:
        parsed = urlsplit(text)
    except ValueError:
        return {"key": found_key, "type": "invalid_url"}
    segments: List[str] = []
    for seg in [s for s in parsed.path.split("/") if s][:2]:
        if len(seg) <= 20 and re.fullmatch(r"[A-Za-z-]+", seg):
            segments.append(seg)
        else:
            segments.append("<id>")
    return {
        "key": found_key,
        "type": "str",
        "scheme": parsed.scheme or "",
        "host": (parsed.hostname or "").lower(),
        "path_prefix": "/" + "/".join(segments) if segments else "/",
    }


def _request_portal_url(
    client: ChatGPTClient,
    account_id: str,
    timeout: float,
    *,
    allow_refresh: bool = True,
) -> tuple[str, str, List[str]]:
    attempts: List[str] = []
    refreshed = not allow_refresh
    for method, path in PORTAL_PATHS:
        body = {"account_id": account_id} if account_id else {}
        for _attempt in range(2):
            headers = client._route_headers(path)
            headers.update(client._identity_headers())
            if method == "POST":
                headers["Content-Type"] = "application/json"
            try:
                if method == "POST":
                    response = client.http.post(APP_BASE + path, json=body, headers=headers,
                                                timeout=timeout)
                else:
                    response = client.http.get(APP_BASE + path, params=body or None,
                                               headers=headers, timeout=timeout)
            except DomainError:
                raise
            except Exception as exc:  # noqa: BLE001
                attempts.append(f"{path}:{type(exc).__name__}")
                response = None
                break
            status = int(getattr(response, "status_code", 0) or 0)
            attempts.append(f"{path}:{status}")
            if status == 401 and not refreshed:
                refreshed = True
                session_token_present = bool(getattr(client, "session_token", ""))
                if session_token_present and client.refresh_session(
                        trust_statsig=False, reject_unauthorized=False):
                    logger.info("account_detail_portal_token_refreshed path=%s", path)
                    continue
            if status == 401:
                raise _auth_error(path, response, client, bool(getattr(client, "session_token", "")))
            if status == 403:
                raise _forbidden_error(path, response)
            if status >= 400:
                logger.info("account_detail_portal path=%s status=%s", path, status)
                break
            payload = _json(response)
            url = _portal_url(payload)
            if url:
                host = (urlsplit(url).hostname or "").lower()
                logger.info("account_detail_portal path=%s status=%s ok host=%s", path, status, host)
                return url, path, attempts
            logger.info(
                "account_detail_portal path=%s status=%s no_url url_hint=%s structure=%s",
                path,
                status,
                json.dumps(_safe_url_hint(payload), ensure_ascii=False),
                json.dumps(_structure(payload), ensure_ascii=False)[:700],
            )
            break
    return "", "", attempts


def fetch_stripe_portal(session: str, access_token: str, *, account_id: str,
                        country: str, timeout: float = ORDER_HTTP_TIMEOUT_S) -> Dict[str, str]:
    """Create a Stripe customer-portal session on the bound exit."""
    proxy = _detail_read_proxy(country)
    client = _detail_read_client(session, proxy, timeout=timeout)
    client.access_token = access_token
    try:
        url, source, attempts = _request_portal_url(client, account_id, timeout, allow_refresh=True)
        if url:
            return {"url": url, "source": source}
    finally:
        client.close()
    raise DomainError(
        ErrorCode.CHECKOUT_UPSTREAM_ERROR,
        "未能获取 Stripe 账单门户链接，请稍后重试。",
        detail={"code": "stripe_portal_unavailable", "attempts": attempts[-4:]},
    )
