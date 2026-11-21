"""Browser-derived environment for Route C.

The object is held in memory only.  ``repr`` intentionally omits cookies and
Sentinel values so a stray log line cannot leak a session.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(frozen=True)
class BrowserContext:
    user_agent: str = ""
    sec_ch_ua: str = ""
    sec_ch_ua_platform: str = ""
    locale: str = "en-US"
    timezone_id: str = "America/New_York"
    viewport_width: int = 1440
    viewport_height: int = 900
    device_scale_factor: float = 1.0
    cookies: Dict[str, str] = field(default_factory=dict, repr=False)
    sentinel_token: str = field(default="", repr=False)
    storage_state: Dict[str, Any] | None = field(default=None, repr=False)
    source: str = "playwright"
    expires_at: float | None = None

    def headers(self) -> dict[str, str]:
        # Only browser environment headers. Never Cookie/Authorization/Host or
        # HTTP/2 pseudo-headers: curl_cffi owns those and duplicate values break
        # the request (or can overwrite the freshly reissued Authorization).
        out: dict[str, str] = {}
        if self.user_agent:
            out["User-Agent"] = self.user_agent
        if self.sec_ch_ua:
            out["sec-ch-ua"] = self.sec_ch_ua
        if self.sec_ch_ua_platform:
            out["sec-ch-ua-platform"] = f'"{self.sec_ch_ua_platform}"'
        if self.locale:
            out["Accept-Language"] = f"{self.locale},{self.locale.split('-')[0]};q=0.9,en;q=0.8"
        return {k: v for k, v in out.items() if not k.startswith(":")}

    def merged_cookies(self, base: Dict[str, str] | None = None) -> Dict[str, str]:
        result = dict(base or {})
        result.update(safe_browser_cookies(self.cookies))
        return result

    def vault_dict(self) -> dict[str, Any]:
        """In-memory vault form. Never log or serialize this to API output."""
        data = self.to_dict()
        data["cookies"] = dict(self.cookies)
        data["sentinel_token"] = self.sentinel_token
        return data

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_agent": self.user_agent,
            "sec_ch_ua": self.sec_ch_ua,
            "sec_ch_ua_platform": self.sec_ch_ua_platform,
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "device_scale_factor": self.device_scale_factor,
            "source": self.source,
            "expires_at": self.expires_at,
            # cookies/sentinel are deliberately excluded from the public dict
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "BrowserContext | None":
        if not isinstance(raw, dict):
            return None
        return cls(
            user_agent=str(raw.get("user_agent") or ""),
            sec_ch_ua=str(raw.get("sec_ch_ua") or ""),
            sec_ch_ua_platform=str(raw.get("sec_ch_ua_platform") or ""),
            locale=str(raw.get("locale") or "en-US"),
            timezone_id=str(raw.get("timezone_id") or "America/New_York"),
            viewport_width=int(raw.get("viewport_width") or 1440),
            viewport_height=int(raw.get("viewport_height") or 900),
            device_scale_factor=float(raw.get("device_scale_factor") or 1.0),
            cookies={str(k): str(v) for k, v in (raw.get("cookies") or {}).items()},
            sentinel_token=str(raw.get("sentinel_token") or ""),
            source=str(raw.get("source") or "playwright"),
            expires_at=raw.get("expires_at"),
        )


# Browser context is captured direct, while checkout uses the order proxy.
# Never carry IP-bound Cloudflare/device cookies across that boundary.
BROWSER_COOKIE_DENYLIST = frozenset({
    "__cf_bm", "cf_clearance", "_cfuvid", "cf_chl_2", "cf_chl_prog",
    "oai-did", "oai-device-id", "oai-sc", "__Secure-next-auth.session-token",
})


def safe_browser_cookies(cookies: dict[str, str] | None) -> dict[str, str]:
    return {
        str(name): str(value)
        for name, value in (cookies or {}).items()
        if name and value is not None and str(name) not in BROWSER_COOKIE_DENYLIST
    }


__all__ = ["BrowserContext", "BROWSER_COOKIE_DENYLIST", "safe_browser_cookies"]
