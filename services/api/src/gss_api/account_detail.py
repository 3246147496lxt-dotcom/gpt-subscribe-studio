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
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ACCOUNTS_CHECK_PATH, APP_BASE, ChatGPTClient, _json
from gss_core.config import ORDER_HTTP_TIMEOUT_S
from gss_core.proxies import align_proxy_region, configured_proxy
from gss_core.redaction import redact_sensitive
from gss_core.session_token import parse_session_bundle

logger = logging.getLogger("gss.account_detail")

BEIJING = timezone(timedelta(hours=8))
SUBSCRIPTIONS_PATH = "/backend-api/subscriptions"
INVOICES_PATH = "/backend-api/payments/invoices"

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
    "refunded": "已退款",
    "failed": "支付失败",
}
_CHANNEL_LABELS = {
    "stripe": "网页支付",
    "web": "网页支付",
    "web_payment": "网页支付",
    "apple": "App Store",
    "app_store": "App Store",
    "ios": "App Store",
    "google": "Google Play",
    "google_play": "Google Play",
    "android": "Google Play",
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
def _upstream_get(client: ChatGPTClient, path: str, params: Optional[dict]) -> Optional[Dict[str, Any]]:
    headers = client._route_headers(path)
    headers.update(client._identity_headers())
    response = client.http.get(APP_BASE + path, params=params, headers=headers, timeout=client.timeout)
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 401:
        payload = _json(response)
        wire = ""
        for block in (payload.get("error"), payload.get("detail"), payload):
            if isinstance(block, dict) and isinstance(block.get("code"), str):
                wire = block["code"][:60]
                break
        message = ("Session 已被上游撤销（token_revoked），请重新登录后导出新 Session。"
                   if wire == "token_revoked" else "Session 无效或已过期，请重新登录。")
        raise DomainError(ErrorCode.INVALID_SESSION, message,
                          detail={"code": "token_revoked" if wire == "token_revoked" else "invalid_session",
                                  "upstream_status": status})
    if status == 404:
        return None
    if status >= 400:
        logger.info("account_detail_upstream path=%s status=%s", path, status)
        return None
    return _json(response)


def _fetch_one(session: str, access_token: str, path: str, params: Optional[dict],
               *, proxy: Optional[str], timeout: float) -> Optional[Dict[str, Any]]:
    client = ChatGPTClient.from_raw_session(session, proxy=proxy, strip_ambient=True)
    client.access_token = access_token
    client.timeout = timeout
    try:
        return _upstream_get(client, path, params)
    finally:
        client.close()


async def fetch_snapshot(session: str, access_token: str, *, account_id: str,
                         country: str, timeout: float = ORDER_HTTP_TIMEOUT_S) -> Dict[str, Any]:
    """Fan out to the three read-only endpoints with independent clients."""
    proxy = align_proxy_region(configured_proxy("checkout"), country) if country else configured_proxy("checkout")
    account_params = {"account_id": account_id} if account_id else None
    calls = (
        ("accounts", ACCOUNTS_CHECK_PATH, {"timezone_offset_min": "0"}),
        ("subscriptions", SUBSCRIPTIONS_PATH, account_params),
        ("invoices", INVOICES_PATH, account_params),
    )
    tasks = [
        asyncio.to_thread(_fetch_one, session, access_token, path, params,
                          proxy=proxy, timeout=timeout)
        for _, path, params in calls
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    merged: Dict[str, Any] = {}
    failures: List[str] = []
    for (key, _, _), item in zip(calls, results):
        if isinstance(item, BaseException):
            if isinstance(item, DomainError):
                raise item
            failures.append(type(item).__name__)
            continue
        if item is not None:
            merged[key] = item
    if failures and not merged:
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                          "订阅接口暂时不可达，请稍后重试。",
                          detail={"code": "upstream_unavailable", "reason": failures[0]})
    return merged


# ── normalization ────────────────────────────────────────────────────
def _deep(payload: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(payload, dict):
        for key in keys:
            if key in payload and payload[key] not in (None, "", [], {}):
                return payload[key]
        for key in ("entitlement", "subscription", "last_active_subscription",
                    "current_subscription", "data", "result", "accounts", "default",
                    "account"):
            if key in payload:
                found = _deep(payload[key], keys, depth + 1)
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


def _plan_label(value: Any) -> str:
    key = str(value or "").strip().lower()
    return _PLAN_LABELS.get(key, key or "未知套餐")


def _amount(value: Any, currency: str) -> str:
    cur = (currency or "USD").upper()
    if isinstance(value, dict):
        minor = value.get("minorUnitsAmount")
        if isinstance(minor, int) and not isinstance(minor, bool):
            amount = Decimal(minor) / 100
        else:
            amount = value.get("amount")
            cur = str(value.get("currency") or cur).upper()
    else:
        amount = value
    if amount is None or amount == "":
        return "-"
    try:
        number = Decimal(str(amount))
        if not isinstance(value, dict):
            number = number / 100 if number >= 100 and str(amount).isdigit() else number
    except (InvalidOperation, ValueError):
        return f"{redact_sensitive(str(amount))[:30]} {cur}".strip()
    quantized = number.quantize(Decimal("0.01"))
    return f"{quantized} {cur}"


def _invoice_items(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "invoices", "items", "billing_history", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def normalize(merged: Dict[str, Any], *, email: str, account_id: str,
              now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    accounts = merged.get("accounts") if isinstance(merged.get("accounts"), dict) else {}
    subs = merged.get("subscriptions") if isinstance(merged.get("subscriptions"), dict) else {}

    plan_source = (_deep(subs, ("plan_type", "subscription_plan", "plan", "plan_name"))
                   or _deep(accounts, ("plan_type", "subscription_plan")))
    plan_key = str(plan_source or "").strip().lower()
    is_free = plan_key in {"", "free", "chatgptfreeplan"}

    active = _as_bool(_deep(subs, ("is_active", "active", "has_active_subscription")))
    if active is None:
        active = not is_free
    status = "使用中" if active else ("已过期" if plan_key and not is_free else "未订阅")

    start = _deep(subs, ("current_period_start", "start_date", "started_at",
                         "subscription_start", "created_at", "created"))
    end = _deep(subs, ("current_period_end", "expires_at", "active_until",
                       "end_date", "renews_at", "expiration_time"))
    end_moment = _moment(end)
    remaining = None
    if end_moment is not None:
        remaining = max(0, math.ceil((end_moment - now).total_seconds() / 86400))

    cancel = _as_bool(_deep(subs, ("cancel_at_period_end", "cancel_at_period_end_on")))
    will_renew = _as_bool(_deep(subs, ("will_renew", "auto_renew", "renews")))
    auto_renew = "已关闭" if (cancel is True or will_renew is False) else "已开启"

    channel_source = str(_deep(subs, ("payment_provider", "payment_channel", "channel",
                                       "payment_method_type")) or "").strip().lower()
    payment_channel = _CHANNEL_LABELS.get(channel_source, "网页支付" if not channel_source else channel_source)

    invoices_raw = _invoice_items(merged.get("invoices"))
    currency = str(_deep(subs, ("currency", "plan_currency")) or "").upper()
    if not currency and invoices_raw:
        currency = str(_deep(invoices_raw[0], ("currency",)) or "").upper()
    currency = currency or "USD"

    invoices = []
    for item in invoices_raw:
        invoice_currency = str(_deep(item, ("currency",)) or currency).upper()
        amount_value = _deep(item, ("amount_paid", "total", "amount", "total_amount", "price"))
        invoices.append({
            "plan": _plan_label(_deep(item, ("plan_name", "plan", "subscription_plan", "description"))),
            "paidAt": to_beijing(_deep(item, ("paid_at", "created", "date", "invoice_date",
                                              "created_at", "period_start"))) or "-",
            "status": _INVOICE_STATUS.get(
                str(_deep(item, ("status", "payment_status")) or "").strip().lower(), "未知"),
            "amount": _amount(amount_value, invoice_currency),
        })

    return {
        "email": email or str(_deep(accounts, ("email",)) or ""),
        "accountId": account_id or str(_deep(accounts, ("account_id",)) or ""),
        "status": status,
        "planName": _plan_label(plan_source),
        "startTime": to_beijing(start),
        "endTime": to_beijing(end),
        "remainingDays": remaining,
        "autoRenew": auto_renew,
        "paymentChannel": payment_channel,
        "currency": currency,
        "invoices": invoices,
        "queriedAt": now.astimezone(BEIJING).strftime("%Y/%m/%d %H:%M"),
        "cached": False,
        "stale": False,
        "warning": None,
    }
