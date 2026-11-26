"""Strictly allowlisted, redacted HTTP exchange trace for admin failure diagnostics.

Security invariant (enforced by ``test_checkout_refusal_roundtrip.py``):
Neither raw upstream response bodies, arbitrary provider-controlled prose,
unvalidated codes/headers, nor any credential/card/session strings may ever
enter SQLite or diagnostic payloads. Only explicitly allowlisted structural
keys, validated enum codes, and ``[REDACTED]`` placeholders are retained.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from gss_core.checkout_diagnostics import safe_checkout_diagnostics

REDACTED = "[REDACTED]"

# Request keys whose presence is structurally useful to show as "[REDACTED]".
_REQUEST_REDACTED_KEYS = frozenset({
    "checkout_session_id",
    "stripe_checkout_session_id",
    "confirm_token",
    "confirmation_token",
    "client_secret",
    "customer_session_client_secret",
    "publishable_key",
    "key",
    "payment_method",
    "payment_method_id",
    "billing_details",
    "card",
    "return_url",
    "cancel_url",
    "purchase_attempt_id",
    "client_context[customer]",
    "payment_method_data[billing_details][email]",
    "payment_method_data[billing_details][name]",
    "payment_method_data[card][number]",
    "payment_method_data[card][cvc]",
    "payment_method_data[card][exp_month]",
    "payment_method_data[card][exp_year]",
})

# Request keys whose scalar non-secret values are safe to retain (bounded & validated).
_REQUEST_SAFE_SCALAR_KEYS = frozenset({
    "plan",
    "plan_name",
    "selected_payment_method_type",
    "payment_method_type",
    "save_payment_method_for_reuse",
    "supports_saved_payment_link",
    "checkout_ui_mode",
    "checkout_source",
    "checkout_appearance",
    "entry_point",
    "processor_entity",
    "currency",
    "country",
    "billing_country",
    "exit_country",
    "price_interval",
    "interval",
    "seat_quantity",
    "billing_mode",
    "route_profile",
    "checkout_kind",
    "_stripe_version",
})

_SAFE_SCALAR_STR_RE = re.compile(r"^[A-Za-z0-9._-]{1,48}$")

# Strictly allowlisted structured upstream status values.
_SAFE_RESPONSE_STATUSES = frozenset({
    "subscription_limit",
    "already_subscribed",
    "already_paid",
    "blocked",
    "failed",
    "error",
    "rejected",
    "declined",
    "card_declined",
    "requires_payment_method",
    "requires_action",
    "requires_confirmation",
    "requires_capture",
    "canceled",
    "open",
    "complete",
    "expired",
    "unpaid",
    "paid",
    "no_payment_required",
    "processing",
    "succeeded",
})

# Strictly allowlisted structured upstream error / decline codes.
_SAFE_RESPONSE_CODES = frozenset({
    "unusual_activity",
    "invalid_session",
    "token_revoked",
    "invalid_token",
    "expired_token",
    "token_expired",
    "session_expired",
    "unauthorized",
    "authentication_error",
    "access_denied",
    "already_subscribed",
    "already_paid",
    "subscription_limit",
    "billing_country_mismatch",
    "invalid_request",
    "invalid_request_error",
    "rate_limit_exceeded",
    "rate_limit_error",
    "too_many_requests",
    "insufficient_quota",
    "server_error",
    "internal_error",
    "internal_server_error",
    "service_unavailable",
    "forbidden",
    "permission_denied",
    "account_deactivated",
    "account_disabled",
    "unsupported_country",
    "payment_required",
    "payment_method_required",
    "card_declined",
    "insufficient_funds",
    "expired_card",
    "incorrect_cvc",
    "incorrect_number",
    "invalid_cvc",
    "invalid_number",
    "invalid_expiry_month",
    "invalid_expiry_year",
    "processing_error",
    "authentication_required",
    "payment_intent_authentication_failure",
    "payment_intent_unexpected_state",
    "setup_intent_authentication_failure",
    "do_not_honor",
    "generic_decline",
    "fraudulent",
    "lost_card",
    "stolen_card",
    "pickup_card",
    "restricted_card",
    "security_violation",
    "try_again_later",
    "card_velocity_exceeded",
    "currency_not_supported",
    "transaction_not_allowed",
    "not_permitted",
    "checkout_confirm_failed",
    "sentinel_challenge_failed",
})

_SAFE_ERROR_TYPES = frozenset({
    "card_error",
    "invalid_request_error",
    "api_error",
    "authentication_error",
    "rate_limit_error",
    "idempotency_error",
    "validation_error",
    "permission_error",
})

_SAFE_ERROR_PARAMS = frozenset({
    "card",
    "number",
    "exp_month",
    "exp_year",
    "cvc",
    "billing_details",
    "payment_method",
    "confirmation_token",
    "checkout_session_id",
    "plan_name",
    "country",
    "currency",
    "promo_code",
})

_SAFE_NEXT_ACTION_TYPES = frozenset({
    "use_stripe_sdk",
    "redirect_to_url",
    "3ds_redirect",
    "requires_action",
})

_URL_SECRET_SEGMENT_RE = re.compile(
    r"/(?:oaics|cs_live|cs_test|pi|seti|pm|ctoken|cuss)_[A-Za-z0-9_]+",
    re.I,
)

_HTML_RE = re.compile(r"^\s*(?:<!doctype\s+html|<html[\s>])", re.I)

_SAFE_HOSTS = frozenset({
    "chatgpt.com",
    "api.stripe.com",
    "auth.openai.com",
    "checkout.stripe.com",
})


def redact_trace_url(url: Optional[str]) -> Optional[str]:
    if not isinstance(url, str) or not url.strip():
        return None
    raw = url.strip()
    try:
        parts = urlsplit(raw)
    except Exception:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host not in _SAFE_HOSTS:
        return None
    safe_path = _URL_SECRET_SEGMENT_RE.sub(f"/{REDACTED}", parts.path or "/")
    if not re.fullmatch(r"/[A-Za-z0-9._/\-\[\]]{0,180}", safe_path):
        safe_path = "/"
    return urlunsplit((parts.scheme, host, safe_path, "", ""))


def _project_request_mapping(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        low = k.strip().lower()
        if low in _REQUEST_REDACTED_KEYS:
            out[k] = REDACTED
        elif low in _REQUEST_SAFE_SCALAR_KEYS:
            if isinstance(v, bool):
                out[k] = v
            elif isinstance(v, int) and -10_000 <= v <= 100_000:
                out[k] = v
            elif isinstance(v, str) and _SAFE_SCALAR_STR_RE.fullmatch(v.strip()):
                out[k] = v.strip()
    return out or None


def _project_response_error_block(block: Any) -> dict[str, Any]:
    if not isinstance(block, Mapping):
        return {}
    out: dict[str, Any] = {}
    err_type = block.get("type")
    if isinstance(err_type, str) and err_type.strip().lower() in _SAFE_ERROR_TYPES:
        out["type"] = err_type.strip().lower()
    code = block.get("code")
    if isinstance(code, str) and code.strip().lower() in _SAFE_RESPONSE_CODES:
        out["code"] = code.strip().lower()
    decline = block.get("decline_code")
    if isinstance(decline, str) and decline.strip().lower() in _SAFE_RESPONSE_CODES:
        out["decline_code"] = decline.strip().lower()
    param = block.get("param")
    if isinstance(param, str) and param.strip().lower() in _SAFE_ERROR_PARAMS:
        out["param"] = param.strip().lower()
    return out


def _project_response_body(
    response_json: Any = None,
    response_text: Any = None,
    *,
    http_status: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Project ONLY allowlisted structured fields; never persist raw upstream text."""
    out: dict[str, Any] = {}

    if isinstance(response_json, Mapping):
        raw_status = response_json.get("status")
        if isinstance(raw_status, str) and raw_status.strip().lower() in _SAFE_RESPONSE_STATUSES:
            out["status"] = raw_status.strip().lower()

        top_code = response_json.get("code")
        if isinstance(top_code, str) and top_code.strip().lower() in _SAFE_RESPONSE_CODES:
            out["code"] = top_code.strip().lower()

        err_proj = _project_response_error_block(response_json.get("error"))
        if err_proj:
            out["error"] = err_proj

        detail_proj = _project_response_error_block(response_json.get("detail"))
        if detail_proj:
            out["detail"] = detail_proj

        next_action = response_json.get("next_action")
        if isinstance(next_action, Mapping):
            na_type = next_action.get("type")
            if isinstance(na_type, str) and na_type.strip().lower() in _SAFE_NEXT_ACTION_TYPES:
                out["next_action"] = {"type": na_type.strip().lower()}

        # Preserve safe local-derived enums on SQLite round-trip (never arbitrary text).
        reason = response_json.get("reason")
        if isinstance(reason, str) and reason in {
            "unusual_activity",
            "billing_country_mismatch",
            "already_subscribed",
            "internal_checkout_session_required",
            "html_challenge_page",
        }:
            out["reason"] = reason
        match = response_json.get("match")
        if isinstance(match, str) and match in {"body_match", "structured_code"}:
            out["match"] = match
        if response_json.get("content_type") == "text/html":
            out["content_type"] = "text/html"
        if response_json.get("body_filtered") is True:
            out["body_filtered"] = True
            if isinstance(http_status, int) and 100 <= http_status <= 599:
                out["http_status"] = http_status

    if not out:
        text = ""
        if isinstance(response_text, bytes):
            text = response_text.decode("utf-8", "ignore")[:1000]
        elif isinstance(response_text, str):
            text = response_text[:1000]
        low = text.lower()
        if text and _HTML_RE.match(text):
            out = {"content_type": "text/html", "reason": "html_challenge_page"}
        elif "unusual activity" in low or "unusual_activity" in low:
            out = {"reason": "unusual_activity", "match": "body_match"}
        elif "billing country must match" in low:
            out = {"reason": "billing_country_mismatch", "match": "body_match"}
        elif "already paid" in low or "already subscribed" in low:
            out = {"reason": "already_subscribed", "match": "body_match"}
        elif "subscription_limit" in low or "subscription limit" in low:
            out = {"status": "subscription_limit"}
        elif "only for internal checkout sessions" in low:
            out = {"reason": "internal_checkout_session_required"}
        elif http_status is not None:
            out = {"http_status": http_status, "body_filtered": True}

    return out or None


def build_redacted_http_trace(
    *,
    title: str,
    method: str,
    url: str,
    http_status: Optional[int] = None,
    stage: Optional[str] = None,
    request_json: Any = None,
    request_form: Any = None,
    request_params: Any = None,
    response_json: Any = None,
    response_text: Any = None,
    cf_ray: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build a strictly allowlisted, redacted HTTP exchange dict for admin audit."""
    request_block: dict[str, Any] = {}
    if request_json is not None:
        proj = _project_request_mapping(request_json)
        if proj:
            request_block["json"] = proj
    if request_form is not None:
        proj = _project_request_mapping(request_form)
        if proj:
            request_block["form"] = proj
    if request_params is not None:
        proj = _project_request_mapping(request_params)
        if proj:
            request_block["params"] = proj

    safe_meta = safe_checkout_diagnostics({
        "upstream_status": http_status,
        "cf_ray": cf_ray,
        "request_id": request_id,
    })
    safe_status = safe_meta.get("upstream_status")

    trace: dict[str, Any] = {
        "title": str(title or "上游请求")[:80],
        "method": str(method or "POST").upper()[:12],
        "url": redact_trace_url(url),
        "http_status": safe_status,
        "stage": str(stage)[:64] if stage and _SAFE_SCALAR_STR_RE.fullmatch(str(stage)) else None,
        "request": request_block or None,
        "response": _project_response_body(
            response_json=response_json,
            response_text=response_text,
            http_status=safe_status,
        ),
    }
    if safe_meta.get("cf_ray"):
        trace["cf_ray"] = safe_meta["cf_ray"]
    if safe_meta.get("request_id"):
        trace["request_id"] = safe_meta["request_id"]
    return trace


def load_redacted_http_trace(raw: Any) -> Optional[dict[str, Any]]:
    """Re-validate a stored trace JSON blob on read with strict allowlists."""
    data = raw
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
    if not isinstance(data, dict) or not data.get("title"):
        return None
    req = data.get("request") if isinstance(data.get("request"), Mapping) else {}
    return build_redacted_http_trace(
        title=str(data.get("title") or "上游请求"),
        method=str(data.get("method") or "POST"),
        url=str(data.get("url") or ""),
        http_status=data.get("http_status") if isinstance(data.get("http_status"), int) else None,
        stage=data.get("stage") if isinstance(data.get("stage"), str) else None,
        request_json=req.get("json"),
        request_form=req.get("form"),
        request_params=req.get("params"),
        response_json=data.get("response") if isinstance(data.get("response"), Mapping) else None,
        cf_ray=data.get("cf_ray") if isinstance(data.get("cf_ray"), str) else None,
        request_id=data.get("request_id") if isinstance(data.get("request_id"), str) else None,
    )


__all__ = [
    "REDACTED",
    "build_redacted_http_trace",
    "load_redacted_http_trace",
    "redact_trace_url",
]
