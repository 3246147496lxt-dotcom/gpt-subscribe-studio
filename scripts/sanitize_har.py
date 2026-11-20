#!/usr/bin/env python3
"""Reduce a browser HAR to a small, redacted business-API flow.

Usage:
    python scripts/sanitize_har.py --input tmp/chatgpt.har
    python scripts/sanitize_har.py --input tmp/chatgpt.har --output tmp/sanitized_flow.json

The output deliberately contains only:
  - method + path (no query values)
  - HTTP status + total time
  - sanitized JSON key skeleton, with enum/business values retained
  - no headers, cookies, bodies, tokens, cards, emails or addresses
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

REDACTED = "***REDACTED***"
STATIC_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".avif",
    ".js", ".mjs", ".css", ".map", ".woff", ".woff2", ".ttf", ".otf",
    ".eot", ".mp4", ".webm", ".mp3", ".wav", ".pdf", ".zip", ".wasm",
}
API_HINTS = (
    "/backend-api/", "/api/", "/v1/", "/payments/", "/checkout/",
    "/auth/session", "/sentinel/", "/stripe/",
)
SENSITIVE_KEY = re.compile(
    r"(?i)(cookie|authorization|auth|bearer|token|secret|password|passwd|"
    r"session|card|cvc|cvv|pan|expiry|expir|exp_month|exp_year|"
    r"email|phone|address|line1|line2|postal|zip|name|tax_id|did|device)"
)
# Values that are safe and useful for baseline analysis.
KEEP_VALUE_KEYS = {
    "status", "state", "payment_status", "delivery_status", "intent_status",
    "code", "failure_code", "decline_code", "currency", "plan", "plan_name",
    "plan_type", "billing_country", "country", "exit_country", "checkout_kind",
    "checkout_ui_mode", "type", "next_action_type", "requires_action",
    "amount", "amount_total", "amount_subtotal", "tax", "tax_amount",
    "total", "subtotal", "unit_amount", "quantity", "minorUnitsAmount",
    "created", "livemode",
}
SAFE_HEADER_NAMES = {
    "content-type", "accept", "accept-language",
    "x-openai-target-path", "x-openai-target-route",
    "originator", "x-openai-web-frontend",
    "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
}


def _is_static(entry: dict[str, Any], path: str) -> bool:
    suffix = Path(path).suffix.lower()
    if suffix in STATIC_EXT:
        return True
    mime = str((entry.get("response", {}).get("content", {}) or {}).get("mimeType") or "").lower()
    if mime.startswith(("image/", "font/", "text/css", "text/javascript", "application/javascript")):
        return True
    return False


def _is_api(entry: dict[str, Any], path: str) -> bool:
    if _is_static(entry, path):
        return False
    mime = str((entry.get("response", {}).get("content", {}) or {}).get("mimeType") or "").lower()
    if "json" in mime or "event-stream" in mime:
        return True
    return any(hint in path for hint in API_HINTS)


def _safe_path(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    return parsed.path or "/"


def _safe_scalar(value: Any, key: str = "") -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if value is None:
        return None
    text = str(value)
    if key in KEEP_VALUE_KEYS and len(text) <= 80:
        return text
    return REDACTED


def _sanitize_json(value: Any, key: str = "", depth: int = 0) -> Any:
    if depth > 12:
        return REDACTED
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for child_key, child in value.items():
            name = str(child_key)
            if SENSITIVE_KEY.search(name) and name not in KEEP_VALUE_KEYS:
                result[name] = REDACTED
            else:
                result[name] = _sanitize_json(child, name, depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize_json(item, key, depth + 1) for item in value[:100]]
    return _safe_scalar(value, key)


def _parse_json_text(text: Any) -> Any:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _sanitize_form(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        return {}
    parsed = parse_qs(text, keep_blank_values=True)
    result = {}
    for key, values in parsed.items():
        safe = REDACTED if SENSITIVE_KEY.search(key) and key not in KEEP_VALUE_KEYS else (
            values[0] if key in KEEP_VALUE_KEYS else REDACTED
        )
        result[key] = safe
    return result


def _sanitize_body(entry: dict[str, Any]) -> Any:
    request = entry.get("request", {}) or {}
    post = request.get("postData") or {}
    text = post.get("text")
    mime = str(post.get("mimeType") or "").lower()
    if not text:
        return None
    if "json" in mime or str(text).lstrip().startswith(("{", "[")):
        parsed = _parse_json_text(text)
        return _sanitize_json(parsed) if parsed is not None else REDACTED
    if "x-www-form-urlencoded" in mime:
        return _sanitize_form(text)
    return REDACTED


def _sanitize_response(entry: dict[str, Any]) -> Any:
    content = (entry.get("response", {}) or {}).get("content", {}) or {}
    text = content.get("text")
    mime = str(content.get("mimeType") or "").lower()
    if not text:
        return None
    if "json" in mime or str(text).lstrip().startswith(("{", "[")):
        parsed = _parse_json_text(text)
        return _sanitize_json(parsed) if parsed is not None else REDACTED
    return REDACTED


def sanitize(har: dict[str, Any]) -> dict[str, Any]:
    entries = (har.get("log", {}) or {}).get("entries", [])
    result = []
    for entry in entries:
        request = entry.get("request", {}) or {}
        response = entry.get("response", {}) or {}
        path = _safe_path(request.get("url", ""))
        if not _is_api(entry, path):
            continue
        headers = {}
        for header in request.get("headers", []) or []:
            name = str(header.get("name") or "").lower()
            if name in SAFE_HEADER_NAMES:
                headers[name] = REDACTED if name == "cookie" else str(header.get("value") or "")
        result.append({
            "method": str(request.get("method") or "GET").upper(),
            "path": path,
            "status": int(response.get("status") or 0),
            "time_ms": round(float(entry.get("time") or 0), 3),
            "request_headers": headers,
            "request_json": _sanitize_body(entry),
            "response_json": _sanitize_response(entry),
        })
    return {
        "meta": {
            "source": "sanitized HAR",
            "entries": len(result),
            "note": "Only method/path/status/time and sanitized JSON skeleton are retained.",
        },
        "entries": result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="tmp/chatgpt.har", help="input HAR path")
    parser.add_argument("--output", default="tmp/sanitized_flow.json", help="output JSON path")
    args = parser.parse_args()
    source = Path(args.input)
    if not source.exists():
        parser.error(f"HAR 不存在: {source}")
    try:
        har = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        parser.error(f"HAR JSON 无法解析: {exc}")
    clean = sanitize(har)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {target} ({clean['meta']['entries']} business API entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
