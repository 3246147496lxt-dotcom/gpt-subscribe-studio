"""Environment-backed route selection. Default A preserves 77d6c8d."""
from __future__ import annotations

import os
from typing import Mapping

from gss_contracts.route import RoutePlan, RouteProfile

DEFAULT_PROFILE = RouteProfile.A


def _env(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return env if env is not None else os.environ


def _on(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def route_plan_from_env(env: Mapping[str, str] | None = None) -> RoutePlan:
    values = _env(env)
    raw = str(values.get("GSS_ROUTE_PROFILE", DEFAULT_PROFILE.value)).strip().upper()
    try:
        profile = RouteProfile(raw)
    except ValueError:
        profile = DEFAULT_PROFILE
    # A decode flag cannot silently select C; profile is explicit.
    return RoutePlan.for_profile(profile)


def browser_decode_enabled(env: Mapping[str, str] | None = None) -> bool:
    return _on(_env(env).get("GSS_BROWSER_DECODE"))


def browser_decode_auth_enabled(env: Mapping[str, str] | None = None) -> bool:
    return _on(_env(env).get("GSS_BROWSER_DECODE_AUTH"))


def browser_decode_fallback_a(env: Mapping[str, str] | None = None) -> bool:
    return str(_env(env).get("GSS_BROWSER_DECODE_FALLBACK", "")).strip().upper() == "A"


def browser_decode_url(env: Mapping[str, str] | None = None) -> str:
    return str(_env(env).get("GSS_BROWSER_DECODE_URL", "https://chatgpt.com/")).strip() or "https://chatgpt.com/"


__all__ = [
    "route_plan_from_env",
    "browser_decode_enabled",
    "browser_decode_auth_enabled",
    "browser_decode_fallback_a",
    "browser_decode_url",
]
