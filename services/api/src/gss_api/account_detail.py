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
# Bump when the normalized shape changes; old cache rows are ignored.
CACHE_VERSION = 2
SUBSCRIPTIONS_PATH = "/backend-api/subscriptions"
INVOICES_PATH = "/backend-api/payments/invoices"
BILLING_HISTORY_PATH = "/backend-api/payments/billing_history"
RECEIPTS_PATH = "/backend-api/payments/receipts"

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


def _fetch_serial(session: str, access_token: str, calls, *,
                  proxy: Optional[str], timeout: float) -> Dict[str, Any]:
    """One client, one exit, one device id, endpoints strictly in order.

    A 401 on the first request is retried once after exchanging the session
    cookie for a fresh access token; only a second failure is reported.
    """
    client = ChatGPTClient.from_raw_session(session, proxy=proxy, strip_ambient=True)
    client.access_token = access_token
    client.timeout = timeout
    merged: Dict[str, Any] = {}
    failures: List[str] = []
    refreshed = False
    try:
        for key, path, params in calls:
            try:
                payload, status, response = _request(client, path, params)
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
                        payload, status, response = _request(client, path, params)
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
                merged[key] = payload
        if failures and not merged:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                              "订阅接口暂时不可达，请稍后重试。",
                              detail={"code": "upstream_unavailable",
                                      "reason": failures[0][:80]})
        return merged
    finally:
        client.close()


async def fetch_snapshot(session: str, access_token: str, *, account_id: str,
                         country: str, timeout: float = ORDER_HTTP_TIMEOUT_S) -> Dict[str, Any]:
    """Serial reads on one exit; parallel tunnels were rejected by the proxy."""
    proxy = align_proxy_region(configured_proxy("checkout"), country) if country else configured_proxy("checkout")
    account_params = {"account_id": account_id} if account_id else None
    calls = [
        ("accounts", ACCOUNTS_CHECK_PATH, {"timezone_offset_min": "0"}),
        ("subscriptions", SUBSCRIPTIONS_PATH, account_params),
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
                                   proxy=proxy, timeout=timeout)


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
    cur = "" if str(currency or "").strip() in {"", "-"} else str(currency).upper()
    if isinstance(value, dict):
        minor = value.get("minorUnitsAmount")
        if isinstance(minor, int) and not isinstance(minor, bool):
            number: Any = Decimal(minor) / 100
        else:
            number = value.get("amount")
            if number is None:
                number = value.get("value")
            if value.get("currency"):
                cur = str(value["currency"]).upper()
    else:
        number = value
    if number is None or number == "":
        return "-"
    try:
        amount = Decimal(str(number))
        if not isinstance(value, dict):
            text = str(number).strip()
            # Billing endpoints report integer minor units (98214 -> 982.14).
            if text.isdigit() and amount >= 100:
                amount = amount / 100
    except (InvalidOperation, ValueError):
        return f"{redact_sensitive(str(number))[:30]} {cur}".strip()
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


def _invoice_records(merged: Dict[str, Any]) -> List[dict]:
    """Merge every billing source, de-duplicate, newest first."""
    candidates: List[dict] = []
    for key in ("invoices", "billing_history", "receipts", "account_billing",
                "account_invoices", "transactions"):
        candidates.extend(_invoice_items(merged.get(key)))
    subs = merged.get("subscriptions") if isinstance(merged.get("subscriptions"), dict) else {}
    period = subs.get("current_billing_period") if isinstance(subs, dict) else None
    if isinstance(period, dict) and _deep(period, ("amount_paid", "total", "amount",
                                                    "total_amount", "price")):
        candidates.append(period)
    records: List[dict] = []
    seen = set()
    for item in candidates:
        moment = _moment(_deep(item, ("paid_at", "created", "date", "invoice_date",
                                      "created_at", "period_start")))
        amount_raw = _deep(item, ("amount_paid", "total", "amount", "total_amount", "price"))
        currency = str(_deep(item, ("currency", "currency_code")) or "").upper()
        status_key = str(_deep(item, ("status", "payment_status")) or "").strip().lower()
        signature = (moment.isoformat() if moment else "", str(amount_raw), currency)
        if signature in seen:
            continue
        seen.add(signature)
        records.append({
            "moment": moment,
            "plan": _plan_label(_deep(item, ("plan_name", "plan", "subscription_plan",
                                             "description", "product_name"))),
            "status": _INVOICE_STATUS.get(status_key, "未知"),
            "amount_raw": amount_raw,
            "currency": currency,
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

    records = _invoice_records(merged)
    currency = _pick_currency(records, subs, accounts)
    invoices = [{
        "plan": row["plan"],
        "paidAt": to_beijing(row["moment"]) or "-",
        "status": row["status"],
        "amount": _amount(row["amount_raw"], row["currency"] or currency),
    } for row in records]

    # Start time: subscription period start, else the latest settled invoice,
    # else a labelled one-month estimate before the expiry.
    start_moment = _moment(_deep(subs, ("current_period_start", "active_start",
                                        "start_date", "started_at", "subscription_start",
                                        "created_at", "created")))
    estimated = False
    if start_moment is None:
        start_moment = next((row["moment"] for row in records
                             if row["status"] == "已支付" and row["moment"]), None)
    if start_moment is None and end_moment is not None:
        start_moment = end_moment - timedelta(days=30)
        estimated = True
    start_time = None
    if start_moment is not None:
        start_time = start_moment.astimezone(BEIJING).strftime("%Y/%m/%d %H:%M")
        if estimated:
            start_time += "（约）"

    return {
        "email": email or str(_deep(accounts, ("email",)) or ""),
        "accountId": account_id or str(_deep(accounts, ("account_id",)) or ""),
        "status": status,
        "planName": _plan_label(plan_source),
        "startTime": start_time,
        "endTime": to_beijing(end),
        "remainingDays": remaining,
        "autoRenew": auto_renew,
        "paymentChannel": payment_channel,
        "currency": currency,
        "invoices": invoices,
        "queriedAt": now.astimezone(BEIJING).strftime("%Y/%m/%d %H:%M"),
        "cacheVersion": CACHE_VERSION,
        "cached": False,
        "stale": False,
        "warning": None,
    }
