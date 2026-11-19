"""平台错误码与领域异常。

错误码同时用于 HTTP 响应与 UI 提示（见 contracts/openapi.yaml 的 ``Error``）。
"""

from __future__ import annotations

from enum import Enum

try:  # Python 3.11+
    from enum import StrEnum
except ImportError:  # Python 3.9 / 3.10 兼容
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        def __str__(self) -> str:
            return str(self.value)


class ErrorCode(StrEnum):
    """平台标准错误码。"""

    INVALID_SESSION = "invalid_session"
    SESSION_EXPIRED = "session_expired"
    PROMO_NOT_ACKNOWLEDGED = "promo_not_acknowledged"
    BILLING_COUNTRY_MISMATCH = "billing_country_mismatch"
    PAYMENT_PROXY_POOL_UNAVAILABLE = "payment_proxy_pool_unavailable"
    CHECKOUT_UPSTREAM_ERROR = "checkout_upstream_error"
    PAYMENT_DECLINED = "payment_declined"
    PAYMENT_UNKNOWN_STATE = "payment_unknown_state"
    SENTINEL_CHALLENGE_FAILED = "sentinel_challenge_failed"
    SUBSCRIPTION_LIMIT = "subscription_limit"
    SUBSCRIPTION_NOT_ACTIVE = "subscription_not_active"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    COMPLIANCE_BLOCKED = "compliance_blocked"


class UpstreamFailureCode(StrEnum):
    """Stable failure taxonomy for checkout diagnostics (additive, never auth)."""

    UNUSUAL_ACTIVITY = "unusual_activity"
    SENTINEL_CHALLENGE_FAILED = "sentinel_challenge_failed"
    PROXY_TUNNEL_FAILED = "proxy_tunnel_failed"
    PROXY_AUTHENTICATION_FAILED = "proxy_authentication_failed"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    BILLING_COUNTRY_MISMATCH = "billing_country_mismatch"
    BROWSER_DECODE_UNAVAILABLE = "browser_decode_unavailable"
    UNKNOWN_UPSTREAM = "unknown_upstream"


class DomainError(Exception):
    """带错误码的领域异常。``detail`` 必须已脱敏。"""

    def __init__(self, code: ErrorCode, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_payload(self) -> dict:
        """转为 OpenAPI ``Error`` schema。"""

        payload = {"code": str(self.code), "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


__all__ = ["ErrorCode", "UpstreamFailureCode", "DomainError"]
