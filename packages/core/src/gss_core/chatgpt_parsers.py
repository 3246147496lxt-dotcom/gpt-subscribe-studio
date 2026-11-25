"""Stateless ChatGPT / Stripe payload parsers, failure formatters, and safe logging helpers.

Extracted from :mod:`gss_core.chatgpt` so protocol transport and payload parsing
stay decoupled. All public and internal symbols are re-exported by
``gss_core.chatgpt`` for backwards compatibility.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.redaction import redact_sensitive

# Preserve the original logger channel so existing log filters and tests keep matching.
logger = logging.getLogger("gss_core.chatgpt")

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"


def _nested_first(payload: Any, keys: tuple[str, ...], *, depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in (
            "entitlement",
            "last_active_subscription",
            "subscription",
            "current_subscription",
            "data",
            "result",
        ):
            if key in payload:
                found = _nested_first(payload[key], keys, depth=depth + 1)
                if found:
                    return found
    elif isinstance(payload, list):
        for item in payload:
            found = _nested_first(item, keys, depth=depth + 1)
            if found:
                return found
    return ""


def _subscription_id(payload: Dict[str, Any]) -> str:
    return _nested_first(payload, ("subscription_id", "id"))


def _will_renew(payload: Dict[str, Any]) -> Optional[bool]:
    for block in (
        payload,
        payload.get("entitlement"),
        payload.get("last_active_subscription"),
        payload.get("subscription"),
    ):
        if isinstance(block, dict) and "will_renew" in block:
            return bool(block.get("will_renew"))
    return None


def _session_token_from_set_cookie(response: Any) -> str:
    """Return a rotated session token from Set-Cookie, joining chunked values."""
    headers = getattr(response, "headers", None)
    raw_values = []
    getter = getattr(headers, "get_list", None)
    if callable(getter):
        try:
            raw_values = list(getter("set-cookie") or [])
        except Exception:  # noqa: BLE001
            raw_values = []
    if not raw_values and headers is not None:
        try:
            raw = headers.get("set-cookie")
        except Exception:  # noqa: BLE001
            raw = None
        if raw:
            raw_values = [raw]
    plain = ""
    chunks: Dict[str, str] = {}
    for item in raw_values:
        for match in re.finditer(r"(__Secure-next-auth\.session-token(?:\.\d+)?)=([^;]+)", str(item)):
            name, value = match.group(1), match.group(2).strip()
            if name.rsplit(".", 1)[-1].isdigit():
                chunks[name] = value
            else:
                plain = value
    if chunks:
        return "".join(chunks[key] for key in sorted(chunks))
    return plain


def _json(response: Any) -> Dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


_CHECKOUT_AUTH_ERROR_CODES = frozenset({
    "access_denied",
    "authentication_error",
    "expired_token",
    "invalid_session",
    "invalid_token",
    "session_expired",
    "token_expired",
    "token_revoked",
    "unauthorized",
})


# A caller may pass a per-session path or an entire URL as `where`. Retain
# only fixed labels; neither response text nor a cleaned-up URL is safe to log.
_LOGGABLE_ENDPOINTS = frozenset({
    "/api/auth/session",
    "/backend-api/me",
    ACCOUNTS_CHECK_PATH,
    "/backend-api/payments/checkout",
    "/backend-api/payments/checkout/taxes",
    "/backend-api/payments/payment_method",
    "/backend-api/subscriptions",
    "/v1/confirmation_tokens",
})


def _log_upstream_failure(where: str, response: Any, *, level: int = logging.WARNING) -> None:
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        logger.log(
            level,
            "upstream_error where=%s status=%s",
            where if where in _LOGGABLE_ENDPOINTS else "upstream",
            status,
        )


def _log_checkout_unauthorized(response: Any, configured_headers: Any) -> None:
    observed_headers = getattr(getattr(response, "request", None), "headers", None)
    header_source = "request" if observed_headers is not None else "configured"
    headers = observed_headers if observed_headers is not None else configured_headers
    present = {name.lower() for name, value in headers.items() if value}

    response_headers = getattr(response, "headers", None) or {}
    content_type = response_headers.get("content-type") or response_headers.get("Content-Type") or ""
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type == "application/json" or media_type.endswith("+json"):
        body_kind = "json"
    elif media_type == "text/html":
        body_kind = "html"
    else:
        body_kind = "other"

    content = getattr(response, "content", None)
    body_bytes = len(content) if isinstance(content, bytes) else None
    error_code = "unknown"
    if body_kind == "json" and (body_bytes is None or body_bytes <= 65536):
        payload = _json(response)
        for error in (payload.get("error"), payload.get("detail"), payload):
            if not isinstance(error, dict):
                continue
            code = error.get("code")
            if isinstance(code, str) and code in _CHECKOUT_AUTH_ERROR_CODES:
                error_code = code
                break

    logger.warning(
        "checkout_unauthorized status_code=401 header_source=%s "
        "has_oai_device_id=%s has_user_agent=%s body_kind=%s body_bytes=%s upstream_error_code=%s",
        header_source,
        "oai-device-id" in present,
        "user-agent" in present,
        body_kind,
        body_bytes,
        error_code,
    )


def _account_id(payload: Dict[str, Any]) -> str:
    for key in ("account_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    account = payload.get("account")
    if isinstance(account, dict):
        for key in ("id", "account_id"):
            value = account.get(key)
            if isinstance(value, str) and value:
                return value
    orgs = payload.get("orgs") or payload.get("accounts")
    if isinstance(orgs, dict):
        data = orgs.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            value = data[0].get("id")
            if isinstance(value, str):
                return value
    return ""


def _processor(_country: str) -> str:
    # HAR：日本结账的 processor_entity 也是 openai_llc，不是 openai_ie。
    return "openai_llc"


def _processor_from_checkout_url(url: str) -> str:
    parts = [part for part in str(url or "").split("/") if part]
    if "checkout" not in parts:
        return ""
    index = parts.index("checkout")
    if index + 1 >= len(parts):
        return ""
    candidate = parts[index + 1]
    if candidate in {"verify", ""} or candidate.startswith("oaics_"):
        return ""
    return candidate


def _taxes_body(
    *,
    checkout_id: str,
    billing: Optional[Dict[str, str]] = None,
    processor: str = "",
    currency: str = "",
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"checkout_session_id": checkout_id, "tax_id": None}
    if processor:
        body["processor_entity"] = processor
    if currency:
        body["currency"] = currency
    if billing:
        body["billing_country"] = billing.get("country") or ""
        body["billing_name"] = billing.get("name") or ""
        body["billing_address"] = {
            "line1": billing.get("line1") or "",
            "city": billing.get("city") or "",
            "state": billing.get("state") or "",
            "postal_code": billing.get("postal_code") or "",
            "country": billing.get("country") or "",
        }
    return body


def intent_id_from_secret(secret: str) -> str:
    text = str(secret or "").strip()
    marker = "_secret_"
    if marker not in text:
        return ""
    prefix = text.split(marker, 1)[0]
    if prefix.startswith(("pi_", "seti_")):
        return prefix
    return ""


_CONFIRM_BLOCKED = frozenset({"blocked", "failed", "rejected", "not_approved", "declined"})


def checkout_confirm_blocked(payload: Dict[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("result") or "").strip().lower()
    return status in _CONFIRM_BLOCKED


def checkout_confirm_message(payload: Dict[str, Any]) -> str:
    for key in ("message", "detail", "reason", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in _CONFIRM_BLOCKED:
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("message") or value.get("code") or value.get("reason")
            if nested:
                return str(nested)
    return ""


def checkout_confirm_blocked_advice(
    country: str = "",
    *,
    upstream_reason: str = "",
    upstream_status: str = "blocked",
) -> str:
    """Report an upstream refusal without inferring a card, account or region cause.

    ``country`` is retained for call compatibility; country alone is not evidence.
    """
    status = str(upstream_status or "blocked").strip().lower()
    if status not in _CONFIRM_BLOCKED:
        status = "blocked"
    reason = redact_sensitive(str(upstream_reason or "").strip())
    if reason:
        return f"ChatGPT 未批准本次结账（{status}）。上游信息：{reason}。尚未确认扣款，请先核对账单。"
    return f"ChatGPT 未批准本次结账（{status}），上游未提供明确原因。尚未确认扣款，请先核对账单。"


def payment_intent_from_confirm(payload: Dict[str, Any]) -> tuple[str, str, str]:
    """ChatGPT checkout/confirm 成功时返回 status=success + pi client_secret，没有顶层 id。

    Stripe payment_pages/confirm 则把 PaymentIntent 嵌在 ``payment_intent`` 里，
    批准前也可能只给 ``pi_`` 字符串。
    """
    nested = _intent_object(payload)
    secret = str(payload.get("client_secret") or nested.get("client_secret") or "")
    intent_id = str(nested.get("id") or "")
    raw = payload.get("payment_intent")
    if not intent_id and isinstance(raw, str) and raw.startswith("pi_"):
        intent_id = raw
    if intent_id.startswith("cs_"):
        intent_id = ""
    intent_id = intent_id or intent_id_from_secret(secret)
    return_url = str(payload.get("confirm_return_url") or payload.get("return_url") or "")
    return intent_id, secret, return_url


def submission_state(payload: Dict[str, Any]) -> str:
    attempt = payload.get("submission_attempt") if isinstance(payload.get("submission_attempt"), dict) else {}
    return str(attempt.get("state") or attempt.get("status") or "").strip().lower()


_FAILURE_KEYS = ("decline_code", "code", "message", "reason", "failure_reason", "failure_code", "type")


def _failure_parts(source: Any) -> list[str]:
    if isinstance(source, str):
        return [source.strip()] if source.strip() else []
    if not isinstance(source, dict):
        return []
    parts: list[str] = []
    for key in _FAILURE_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}={value.strip()}")
    return parts


def submission_failure_detail(payload: Dict[str, Any]) -> str:
    """从失败的 Stripe 响应里尽量找出拒绝原因（decline_code / last_payment_error 等）。"""
    attempt = payload.get("submission_attempt") if isinstance(payload.get("submission_attempt"), dict) else {}
    nested = _intent_object(payload)
    sources = [
        attempt.get("error"),
        attempt.get("last_error"),
        attempt.get("failure"),
        attempt,
        payload.get("last_payment_error"),
        nested.get("last_payment_error"),
        payload.get("error"),
    ]
    seen: list[str] = []
    for source in sources:
        for part in _failure_parts(source):
            if part not in seen:
                seen.append(part)
    return "; ".join(seen[:6])


def checkout_requires_approval(payload: Dict[str, Any]) -> bool:
    return submission_state(payload) in {"requires_approval", "pending_approval"}


def nested_intent_status(payload: Dict[str, Any]) -> str:
    nested = _intent_object(payload)
    return str(nested.get("status") or "").strip().lower()


def payment_page_settled(payload: Dict[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("payment_object_status") or "").strip().lower()
    state = submission_state(payload)
    pi_status = nested_intent_status(payload)
    if status in {"complete", "completed", "expired", "succeeded", "requires_action"}:
        return True
    if state in {"succeeded", "complete", "completed", "failed", "declined", "canceled", "cancelled"}:
        return True
    if pi_status in {
        "succeeded",
        "processing",
        "requires_action",
        "requires_payment_method",
        "canceled",
        "cancelled",
    }:
        return True
    return False


def _intent_object(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("payment_intent", "setup_intent"):
        value = payload.get(key)
        if isinstance(value, dict) and (
            value.get("id") or value.get("client_secret") or value.get("status")
        ):
            return value
    attempt = payload.get("submission_attempt")
    if isinstance(attempt, dict):
        nested = _intent_object(attempt)
        if nested:
            return nested
    elements = payload.get("elements_session")
    if isinstance(elements, dict):
        nested = _intent_object(elements)
        if nested:
            return nested
    return {}


def expected_amount_minor(payload: Any) -> Optional[int]:
    """Stripe payment_pages/confirm 的 expected_amount：应付整数（最小货币单位）。

    init 响应里通常是 ``total_summary.due`` / ``invoice.amount_due``，不是顶层 amount_total。
    """
    if not isinstance(payload, dict):
        return None
    # Current custom checkout puts the payable total in checkout_state.total.total.
    for wrapper in (payload, payload.get("checkout_session"), payload.get("session")):
        if not isinstance(wrapper, dict):
            continue
        state = wrapper.get("checkout_state")
        if isinstance(state, dict) and isinstance(state.get("total"), dict):
            total = state["total"]
            bucket = total.get("total") if isinstance(total.get("total"), dict) else total
            if bucket.get("minorUnitsAmount") is not None:
                found = _as_minor_int(bucket["minorUnitsAmount"])
                if found is not None:
                    return found
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else None
    if snapshot:
        found = _as_minor_int(snapshot.get("amount_total") or snapshot.get("due"))
        if found is not None:
            return found
    wrappers = (
        payload,
        payload.get("session") if isinstance(payload.get("session"), dict) else None,
        payload.get("checkout_session") if isinstance(payload.get("checkout_session"), dict) else None,
        payload.get("payment_page") if isinstance(payload.get("payment_page"), dict) else None,
    )
    for wrapper in wrappers:
        if not isinstance(wrapper, dict):
            continue
        found = _expected_amount_from_object(wrapper)
        if found is not None:
            return found
    return None


def _as_minor_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            return None
        return int(value)
    text = str(value).strip()
    if not text or "." in text or "," in text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _expected_amount_from_object(payload: Dict[str, Any]) -> Optional[int]:
    summary = payload.get("total_summary")
    if isinstance(summary, dict):
        for key in ("due", "amount_due", "total", "amount"):
            found = _as_minor_int(summary.get(key))
            if found is not None:
                return found
    invoice = payload.get("invoice")
    if isinstance(invoice, dict):
        for key in ("amount_due", "total", "amount_paid"):
            found = _as_minor_int(invoice.get(key))
            if found is not None:
                return found
    for key in ("amount_total", "amount_due", "checkout_amount_minor"):
        found = _as_minor_int(payload.get(key))
        if found is not None:
            return found
    total = payload.get("total")
    if isinstance(total, dict):
        nested = total.get("total") if isinstance(total.get("total"), dict) else None
        if nested:
            found = _as_minor_int(nested.get("minorUnitsAmount") or nested.get("due"))
            if found is not None:
                return found
        for key in ("due", "minorUnitsAmount", "amount", "total"):
            found = _as_minor_int(total.get(key))
            if found is not None:
                return found
    items = payload.get("line_items") or payload.get("lineItems")
    if isinstance(items, list):
        running = 0
        saw = False
        for item in items:
            if not isinstance(item, dict):
                continue
            found = _as_minor_int(item.get("amount") or item.get("amount_total"))
            if found is None:
                continue
            running += found
            saw = True
        if saw:
            return running
    return None


def _stripe_unknown_param(payload: Dict[str, Any]) -> str:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    param = str(error.get("param") or "").strip()
    if param:
        return param
    message = str(error.get("message") or payload.get("message") or "")
    match = re.search(r"unknown parameter[:\s]+['\"]?([A-Za-z0-9_\[\]]+)", message, re.I)
    return match.group(1) if match else ""


_DECLINE_ADVICE = {
    "do_not_honor": "发卡行未说明原因。请联系银行确认该卡已开通境外/网上/订阅扣款、额度够用，并核对账单地址与卡登记地址一致；先别连续重试，可换一张卡。",
    "generic_decline": "发卡行未说明原因。请联系银行确认该卡可用于境外网上扣款，再重试或换卡。",
    "insufficient_funds": "余额或额度不足，请充值后重试或换卡。",
    "card_velocity_exceeded": "短时间内尝试次数或金额超限，请稍后再试，或联系银行放宽限额。",
    "lost_card": "发卡行标记该卡已挂失，请换卡。",
    "stolen_card": "发卡行标记该卡不可用，请换卡。",
    "expired_card": "卡已过期，请换卡。",
    "incorrect_cvc": "安全码（CVC）不正确，请重新输入。",
    "incorrect_number": "卡号不正确，请重新输入。",
    "invalid_cvc": "安全码（CVC）不正确，请重新输入。",
    "invalid_expiry_month": "有效期月份不正确，请重新输入。",
    "invalid_expiry_year": "有效期年份不正确，请重新输入。",
    "authentication_required": "发卡行要求 3DS 验证，请在银行 App 或短信里完成验证后重试。",
    "card_not_supported": "这张卡不支持该类扣款，请换卡。",
    "currency_not_supported": "这张卡不支持当前币种，请联系银行开通或换卡。",
    "transaction_not_allowed": "发卡行不允许这类交易，请联系银行或换卡。",
    "pickup_card": "发卡行要求不再使用该卡，请换卡。",
    "fraudulent": "发卡行判定为风险交易，请联系银行确认后再试。",
}


def decline_message(detail: str) -> str:
    """发卡行拒绝的统一文案：保留原始拒绝码，已知码附上处理建议。"""
    code = str(detail or "").strip()
    advice = _DECLINE_ADVICE.get(code.lower())
    return f"发卡行拒绝: {code}。{advice}" if advice else f"发卡行拒绝: {code}"


def _raise_stripe(payload: Dict[str, Any], status: int) -> None:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = str(error.get("code") or "")
    decline = str(error.get("decline_code") or "")
    message = str(error.get("message") or payload.get("message") or f"Stripe HTTP {status}")
    if "billing country must match" in message.lower():
        raise DomainError(
            ErrorCode.BILLING_COUNTRY_MISMATCH,
            "账单国家必须与出口代理国家一致，请用所选地区的 SID 重试。",
        )
    if "automatic payment methods" in message.lower() and "payment_method_types" in message.lower():
        raise DomainError(
            ErrorCode.PAYMENT_UNKNOWN_STATE,
            "支付方式采集模式和确认接口不一致。请刷新页面后重新创建结账，不要用上一单的输入框。",
        )
    if "expected amount" in message.lower() and "has to be provided" in message.lower():
        raise DomainError(
            ErrorCode.PAYMENT_UNKNOWN_STATE,
            "Stripe 确认缺少应付金额。请重新创建结账后再确认。",
        )
    if code == "card_declined" or decline:
        detail = decline or code or message
        raise DomainError(ErrorCode.PAYMENT_DECLINED, decline_message(redact_sensitive(detail)))
    raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, redact_sensitive(message))


_SETI_SECRET = re.compile(r"seti_[A-Za-z0-9]+_secret_[A-Za-z0-9]+")
_SETI_ID = re.compile(r"seti_[A-Za-z0-9]+")


def _walk(payload: Any):
    if isinstance(payload, dict):
        for value in payload.values():
            yield from _walk(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk(item)
    else:
        yield payload


def setup_intent_from_payload(payload: Any) -> tuple[str, str]:
    """从 ChatGPT /payments/payment_method 响应里抽出 seti + client_secret。"""
    secret = ""
    if isinstance(payload, dict):
        nested = payload.get("setup_intent")
        if isinstance(nested, dict):
            nested_id, nested_secret = setup_intent_from_payload(nested)
            if nested_id and nested_secret:
                return nested_id, nested_secret
        for key in ("client_secret", "clientSecret", "setup_intent_client_secret"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("seti_"):
                secret = value
                break
    if not secret:
        for value in _walk(payload):
            if isinstance(value, str) and _SETI_SECRET.search(value):
                secret = _SETI_SECRET.search(value).group(0)
                break
    if not secret:
        return "", ""
    match = _SETI_ID.search(secret)
    setup_id = match.group(0) if match else ""
    if isinstance(payload, dict):
        for key in ("id", "setup_intent_id", "setupIntentId"):
            value = str(payload.get(key) or "")
            if value.startswith("seti_") and "_secret_" not in value:
                setup_id = value
                break
    return setup_id, secret


__all__ = [
    "ACCOUNTS_CHECK_PATH",
    "_CHECKOUT_AUTH_ERROR_CODES",
    "_CONFIRM_BLOCKED",
    "_DECLINE_ADVICE",
    "_FAILURE_KEYS",
    "_LOGGABLE_ENDPOINTS",
    "_SETI_ID",
    "_SETI_SECRET",
    "_account_id",
    "_as_minor_int",
    "_expected_amount_from_object",
    "_failure_parts",
    "_intent_object",
    "_json",
    "_log_checkout_unauthorized",
    "_log_upstream_failure",
    "_nested_first",
    "_processor",
    "_processor_from_checkout_url",
    "_raise_stripe",
    "_session_token_from_set_cookie",
    "_stripe_unknown_param",
    "_subscription_id",
    "_taxes_body",
    "_walk",
    "_will_renew",
    "checkout_confirm_blocked",
    "checkout_confirm_blocked_advice",
    "checkout_confirm_message",
    "checkout_requires_approval",
    "decline_message",
    "expected_amount_minor",
    "intent_id_from_secret",
    "nested_intent_status",
    "payment_intent_from_confirm",
    "payment_page_settled",
    "setup_intent_from_payload",
    "submission_failure_detail",
    "submission_state",
]
