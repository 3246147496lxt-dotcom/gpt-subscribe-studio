"""Classify upstream failures without exposing credentials or raw payloads."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gss_contracts.errors import UpstreamFailureCode


@dataclass(frozen=True)
class UpstreamFailure:
    code: UpstreamFailureCode
    stage: str = "checkout"
    status: int | None = None
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": str(self.code),
            "failure_stage": self.stage,
            "message": self.message,
        }
        if self.status is not None:
            payload["upstream_status"] = self.status
        if self.detail:
            payload["diagnostics"] = dict(self.detail)
        return payload


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    return str(value or "")


def classify_upstream_failure(
    *,
    status: int | None = None,
    body: Any = "",
    exc: BaseException | None = None,
    stage: str = "checkout",
) -> UpstreamFailure:
    name = type(exc).__name__ if exc is not None else ""
    text = (_text(body) + " " + _text(exc)).lower()
    if "browser_decode_unavailable" in text or name in {"BrowserDecodeUnavailable", "PlaywrightError"}:
        code = UpstreamFailureCode.BROWSER_DECODE_UNAVAILABLE
    elif any(token in name.lower() for token in ("timeout", "timedout")) or "timed out" in text:
        code = UpstreamFailureCode.UPSTREAM_TIMEOUT
    elif any(token in name.lower() for token in ("proxy", "sslerror", "connecterror")) or "connect 404" in text or "proxy" in text:
        code = UpstreamFailureCode.PROXY_AUTHENTICATION_FAILED if "407" in text else UpstreamFailureCode.PROXY_TUNNEL_FAILED
    elif "unusual activity" in text or "unusual_activity" in text or "异常流量" in text:
        code = UpstreamFailureCode.UNUSUAL_ACTIVITY
    elif status == 403 and ("sentinel" in text or "challenge" in text or "安全验证" in text):
        code = UpstreamFailureCode.SENTINEL_CHALLENGE_FAILED
    elif "insufficient_funds" in text or "insufficient funds" in text or "余额不足" in text:
        code = UpstreamFailureCode.INSUFFICIENT_FUNDS
    elif any(token in text for token in (
            "card_declined", "card was declined", "card's balance",
            "card_velocity_exceeded", "do_not_honor", "generic_decline",
            "pickup_card", "发卡行拒付", "卡片被拒")):
        code = UpstreamFailureCode.CARD_DECLINED
    elif "subscription_limit" in text or "subscription limit" in text:
        code = UpstreamFailureCode.SUBSCRIPTION_LIMIT
    elif "billing_country" in text or "billing country" in text:
        code = UpstreamFailureCode.BILLING_COUNTRY_MISMATCH
    else:
        code = UpstreamFailureCode.UNKNOWN_UPSTREAM
    return UpstreamFailure(code=code, stage=stage, status=status, message=_text(body)[:240])


__all__ = ["UpstreamFailure", "classify_upstream_failure"]
