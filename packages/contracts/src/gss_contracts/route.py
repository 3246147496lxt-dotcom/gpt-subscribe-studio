"""Route profiles for the checkout pipeline.

A is the historical curl_cffi path.  B/C add a real-browser decode step while
keeping every order/payment request on the locked order proxy.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover
    class StrEnum(str, Enum):
        def __str__(self) -> str:
            return str(self.value)


class RouteProfile(StrEnum):
    A = "A"
    B = "B"
    C = "C"


class RouteStage(StrEnum):
    DECODE = "decode"
    CHECKOUT = "checkout"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class RoutePlan:
    profile: RouteProfile = RouteProfile.A
    decode_via: str = "proxy"
    checkout_via: str = "proxy"
    confirm_via: str = "proxy"
    use_browser_context: bool = False

    @classmethod
    def for_profile(cls, profile: RouteProfile | str) -> "RoutePlan":
        try:
            value = RouteProfile(str(profile).upper())
        except ValueError:
            value = RouteProfile.A
        if value is RouteProfile.A:
            return cls(value, "proxy", "proxy", "proxy", False)
        return cls(value, "direct", "proxy", "proxy", True)


__all__ = ["RouteProfile", "RouteStage", "RoutePlan"]
