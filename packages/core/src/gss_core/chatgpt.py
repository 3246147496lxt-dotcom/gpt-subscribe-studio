"""本项目自己的 ChatGPT / Stripe 客户端。建单协议对齐公开的直卡提链实现。"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from http.cookiejar import CookieJar
from typing import Any, Callable, Dict, NoReturn, Optional
from urllib.parse import quote, urlencode, urlsplit

from gss_contracts.errors import DomainError, ErrorCode

from . import ua_ch
from gss_core.verified_payer import checkout_address, check_observed_billing
from gss_core.checkout_diagnostics import safe_checkout_diagnostics
from gss_core.failure_trace import build_redacted_http_trace
from gss_core.config import ORDER_HTTP_TIMEOUT_S
from gss_core.countries import currency_for, locale_for, timezone_for
from gss_core.money import quote_from_payload
from gss_core.redaction import redact_sensitive
from gss_core.sentinel import (
    extract_attestation,
    extract_build,
    extract_sentinel_version,
    header_value,
    mint,
    so_header_value,
)
from gss_core.session_token import (
    SessionBundle,
    identity_cookies_for_reissue,
    parse_session_bundle,
    parse_statsig_context,
)

APP_BASE = "https://chatgpt.com"
ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
STRIPE_BASE = "https://api.stripe.com"
# OAuth token endpoint for refresh_token-granted sessions (Codex/CLI exports).
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = os.environ.get("GSS_OAUTH_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann")
_IMPERSONATE = "chrome150"
_SUPPORTED_CHROME_IMPERSONATES = (
    (150, "chrome150"),
    (146, "chrome146"),
    (145, "chrome145"),
    (142, "chrome142"),
    (136, "chrome136"),
    (133, "chrome133a"),
)


def impersonate_for_major(major: str) -> str:
    """Map a Chrome major version string to the nearest curl_cffi TLS target."""
    try:
        target = int(str(major or "").split(".", 1)[0])
    except (TypeError, ValueError):
        return _IMPERSONATE
    return min(_SUPPORTED_CHROME_IMPERSONATES, key=lambda row: abs(row[0] - target))[1]


def impersonate_for_ua(ua: str) -> str:
    match = _UA_CHROME_VERSION.search(str(ua or ""))
    return impersonate_for_major(match.group(1)) if match else _IMPERSONATE


_CLIENT_VERSION = "prod-db390ebea64862bf1899c420a4c736e0cf639747"
# 旧版本硬编码的 7904904 只在没有从页面解析到真实构建号时使用（见 _identity_headers），
# 默认不再无条件发送过期 build number。
_CLIENT_BUILD = ""
# ── 真实 Chrome profile（大版本严格对齐 curl_cffi TLS impersonate 目标）────
_PROFILES = {
    "windows": {
        "builds": ["150.0.7871.189", "150.0.7871.187",
                   "146.0.7680.180", "146.0.7680.178",
                   "145.0.7632.116", "142.0.7444.175"],
        "platform": "Win32",
        "label": "Windows",
        "screen": (1920, 1080, 1032),
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/{build} Safari/537.36"),
        "hints": {
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"10.0.0"',
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
        },
    },
    "macos": {
        "builds": ["150.0.7871.189", "150.0.7871.187",
                   "146.0.7680.180", "146.0.7680.177",
                   "145.0.7632.116", "142.0.7444.175"],
        "platform": "MacIntel",
        "label": "macOS",
        "screen": (1512, 982, 938),
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/{build} Safari/537.36"),
        "hints": {
            "sec-ch-ua-platform": '"macOS"',
            "sec-ch-ua-platform-version": '"14.6.1"',
            "sec-ch-ua-arch": '"arm"',
            "sec-ch-ua-bitness": '"64"',
        },
    },
    "linux": {
        "builds": ["150.0.7871.186", "150.0.7871.181",
                   "146.0.7680.177", "146.0.7680.164",
                   "145.0.7632.116", "142.0.7444.175"],
        "platform": "Linux x86_64",
        "label": "Linux",
        "screen": (1920, 1080, 1053),
        "ua": ("Mozilla/5.0 (X11; Linux x86_64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/{build} Safari/537.36"),
        "hints": {"sec-ch-ua-platform": '"Linux"'},
    },
}

# 默认只用 Windows Chrome：实测 macOS / Linux 档案在支付风控里显著更差
# （linux.do #80：苹果设备风控明显更高）。需要沿用真实 Session UA 时由
# `reissue_keep_ua=True` 走 profile_from_session_ua，不受此表限制。
_PROFILE_ORDER = ["windows"]


def _hash8(seed: str, salt: str = "") -> int:
    import hashlib as _hl
    return int(_hl.sha256((str(seed) + salt).encode("utf-8")).hexdigest()[:8], 16)


def _pick_profile(device_id: str) -> dict:
    """按 device_id 稳定选定平台与版本，同一账号永远同一组合。"""
    seed = str(device_id or "anon")
    key = _PROFILE_ORDER[_hash8(seed, "os") % len(_PROFILE_ORDER)]
    prof = dict(_PROFILES[key])
    prof["key"] = key
    prof["build"] = prof["builds"][_hash8(seed, "b") % len(prof["builds"])]
    return prof


_UA_CHROME_VERSION = re.compile(r"(?:Chrome|CriOS)/([0-9][0-9.]*)")


def _profile_from_session_ua(ua: str) -> Optional[dict]:
    """从 Session 的真实 UA 推导平台/版本，保证请求头与哨兵子进程同源。

    返回 None 表示 UA 不可识别，调用方回退到 deviceId 推导的伪造档案。
    """
    text = str(ua or "").strip()
    if not text:
        return None
    low = text.lower()
    if "windows" in low:
        key = "windows"
    elif "macintosh" in low or "mac os x" in low:
        key = "macos"
    elif "android" in low:
        key = "android"
    elif "cros" in low:
        key = "cros"
    elif "linux" in low or "x11" in low:
        key = "linux"
    else:
        return None
    prof = dict(_PROFILES.get(key, _PROFILES["linux"]))
    if key == "android":
        prof.update({"platform": "Linux armv8l", "label": "Android"})
        prof["hints"] = {
            "sec-ch-ua-platform": '"Android"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-arch": '"arm"',
            "sec-ch-ua-bitness": '"64"',
        }
    elif key == "cros":
        prof.update({"platform": "Linux x86_64", "label": "ChromeOS"})
        prof["hints"] = {"sec-ch-ua-platform": '"Chrome OS"'}
    match = _UA_CHROME_VERSION.search(text)
    prof["key"] = key
    prof["build"] = match.group(1) if match else prof["builds"][0]
    return prof


# 公开别名：建单 / 支付协议用它从 Session UA 推导同一份指纹档案。
profile_from_session_ua = _profile_from_session_ua


def _profile_from_ua(ua: str) -> dict:
    """兼容旧调用：从 UA 推导 platform / 屏幕 / 核心数。"""
    low = (ua or "").lower()
    if "windows" in low:
        return {"platform": "Win32", "platform_label": "Windows",
                "screen_w": 1920, "screen_h": 1080, "avail_h": 1032, "cores": 8}
    if "macintosh" in low or "mac os x" in low:
        return {"platform": "MacIntel", "platform_label": "macOS",
                "screen_w": 1512, "screen_h": 982, "avail_h": 938, "cores": 8}
    return {"platform": "Linux x86_64", "platform_label": "Linux",
            "screen_w": 1920, "screen_h": 1080, "avail_h": 1053, "cores": 8}


def _ua_mac(build: str) -> str:
    """兼容旧调用：默认 Linux UA。"""
    return _PROFILES["linux"]["ua"].format(build=build)


_CHROME = _PROFILES["linux"]["ua"].format(build=_PROFILES["linux"]["builds"][0])
_SEC_CH_UA = ua_ch.sec_ch_ua("150")
# GREASE 品牌轮换表已收敛到 gss_core.ua_ch（低熵/高熵/JS 侧共用同一来源）。

STRIPE_API_VERSION = "2025-03-31.basil"
STRIPE_CHECKOUT_API_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
logger = logging.getLogger("gss.chatgpt")
_PLAN_NAME = {
    "plus": "chatgptplusplan",
    "go": "chatgptgoplan",
    "pro_lite": "chatgptprolite",
    "pro": "chatgptpro",
    "pro_max": "chatgptpromax",
    "team": "chatgptteamplan",
}
_PLAN_TYPE = {
    "plus": "plus",
    "go": "go",
    "pro_lite": "prolite",
    "pro": "pro",
    "pro_max": "promax",
    "team": "team",
    "chatgptplusplan": "plus",
    "chatgptgoplan": "go",
    "chatgptprolite": "prolite",
    "chatgptpro": "pro",
    "chatgptpromax": "promax",
    "chatgptteamplan": "team",
}

# Stripe Checkout 托管页片段（公开 UI 状态，不是密钥）。Team 长链与 153 同源协议。
_HOSTED_CHECKOUT_FRAGMENT = (
    "#fidnandhYHdWcXxpYCc%2FJ2FgY2RwaXEnKSdpamZkaWAnPyd%2FbScpJ3ZwZ3Zmd2x1cWxqa1Brb"
    "HRwYGtgdnZAa2RnaWBhJz9jZGl2YCknYnBkZmRoamlgU2R3bGRrcSc%2FJ2Zqa3F3amknKSdkdWxO"
    "YHwnPyd1blppbHNgWjA0TUp3VnJGM200a31Cakw2aVFEYldvXFN3fzFhUDZjU0pkZ3xGZk5XNnVnQ"
    "E9icEZTRGl0Rn1hfUZQc2pXbTRdUnJXZGZTbGpzUDZuSU5zdW5vbTJMdG5SNTVsXVR2b2o2aycpJ2"
    "N3amhWYHdzYHcnP3F3cGApJ2dkZm5id2pwa2FGamlqdyc%2FJyZjY2NjY2MnKSdpZHxqcHFRfHVgJ"
    "z8ndmxrYmlgWmxxYGgnKSdga2RnaWBVaWRmYG1qaWFgd3YnP3F3cGB4JSUl"
)
_CS_ID = re.compile(r"cs_(?:live|test)_[A-Za-z0-9]+")
_OAICS_ID = re.compile(r"oaics_[A-Za-z0-9]+")


def _stripe_device_id() -> str:
    """文档 8.2：guid/muid/sid = UUID + 6 位 hex，共 42 字符。"""
    return str(uuid.uuid4()) + uuid.uuid4().hex[:6]


def _elements_session_client_fields(
    *,
    stripe_js_id: str,
    elements_session_id: str = "",
    locale: str = "en",
) -> Dict[str, str]:
    fields = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": locale or "en",
        "elements_session_client[is_aggregation_expected]": "false",
    }
    if elements_session_id:
        fields["elements_session_client[session_id]"] = elements_session_id
    return fields


def clamp_team_seats(value: Any) -> int:
    try:
        seats = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Team 席位必须是整数") from exc
    if seats < 2:
        raise ValueError("Team 最少 2 个席位")
    if seats > 999:
        raise ValueError("Team 最多 999 个席位")
    return seats


def _first_id(pattern: re.Pattern[str], *blobs: Any) -> str:
    for blob in blobs:
        if isinstance(blob, dict):
            for value in _walk(blob):
                if isinstance(value, str):
                    match = pattern.search(value)
                    if match:
                        return match.group(0)
        elif isinstance(blob, str):
            match = pattern.search(blob)
            if match:
                return match.group(0)
    return ""


def stripe_checkout_id(*blobs: Any) -> str:
    return _first_id(_CS_ID, *blobs)


def openai_internal_checkout_id(*blobs: Any) -> str:
    """ChatGPT `/payments/checkout/confirm` 只接受内部 oaics_ 会话。"""
    return _first_id(_OAICS_ID, *blobs)


def public_checkout_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("http"):
        text = text.rstrip("/").split("/")[-1]
    return text.split("?")[0].split("#")[0]


def stripe_verify_url(
    checkout_id: str,
    *,
    processor: str = "",
    plan: str = "",
    source: str = "chatgpt_web_merged",
) -> str:
    """HAR 里 PI confirm 的 return_url：/checkout/verify?stripe_session_id=oaics_…"""
    session_id = str(checkout_id or "").strip().rstrip("/").split("/")[-1]
    if session_id.startswith("http") or not session_id:
        return f"{APP_BASE}/"
    query = [f"stripe_session_id={session_id}"]
    entity = str(processor or "").strip()
    if entity and entity not in {"verify", "c", "pay"}:
        query.append(f"processor_entity={entity}")
    if source:
        query.append(f"source={source}")
    plan_type = _PLAN_TYPE.get(str(plan), "")
    if plan_type:
        query.append(f"plan_type={plan_type}")
    return f"{APP_BASE}/checkout/verify?{'&'.join(query)}"


def hosted_checkout_url(url: str = "", session_id: str = "") -> str:
    """Team 托管长链：pay.openai.com/c/pay/{cs_live} + Checkout 片段。"""
    value = str(url or "").strip()
    cs_id = session_id if str(session_id).startswith("cs_") else stripe_checkout_id(value, session_id)
    if value.startswith("https://checkout.stripe.com/c/pay/"):
        value = "https://pay.openai.com" + value[len("https://checkout.stripe.com") :]
    if value.startswith("https://pay.openai.com/c/pay/"):
        if "#" not in value:
            return value + _HOSTED_CHECKOUT_FRAGMENT
        return value
    if cs_id:
        return f"https://pay.openai.com/c/pay/{cs_id}{_HOSTED_CHECKOUT_FRAGMENT}"
    return value


# 参考 HAR 的 payments/* 请求里带一批前端标识。其中三项是 macOS Codex WebView
# 特有的客户端身份（originator: Codex Browser / x-openai-web-frontend: codex_webview /
# x-openai-codex-window-type: browser）：本客户端伪装的是 Windows Chrome 的 ChatGPT Web，
# 混发这组头会让同一条会话在服务端同时呈现「Windows Chrome Web」与「macOS Codex
# WebView」两种客户端身份，是最容易被按 origin 聚类的矛盾。
# 因此只保留与客户端身份无关、且在 Web 侧同样存在的两项。
_WEB_CHECKOUT_HEADERS = {
    "x-openai-checkout-source": "chatgpt_web_merged",
    "x-openai-rate-limit-join-workspace": "1",
}

# 顶层文档导航（GET /）上真 Chrome 不会发送的 API 专有头。
# 旧实现把 _identity_headers() 直接复用到文档导航上，于是 oai-did / oai-device-id /
# oai-language / oai-session-id / oai-client-version / chatgpt-account-id 出现在
# `sec-fetch-dest: document` 的请求里，同时 `sec-fetch-site: none` 还带着 Referer
# —— 真浏览器在没有 initiator 的导航里绝不带 Referer。
_DOC_NAV_OMIT_HEADERS = (
    "authorization",
    "origin",
    "content-type",
    "oai-did",
    "oai-device-id",
    "oai-language",
    "oai-session-id",
    "oai-client-version",
    "oai-client-build-number",
    "chatgpt-account-id",
    "priority",
    "referer",
)

# 高熵 UA-CH：冷导航（sec-fetch-site: none）上真 Chrome 不会发送，只有站点通过
# Accept-CH 持久化策略后才会在后续导航里补上。
_DOC_NAV_HIGH_ENTROPY_HINTS = (
    "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-arch",
    "sec-ch-ua-bitness",
    "sec-ch-ua-platform-version",
    "sec-ch-ua-model",
)

_PLATFORM_CH_DEFAULTS = {
    "macOS": ('"arm"', '"64"', '"14.6.1"'),
    "Windows": ('"x86"', '"64"', '"10.0.0"'),
    "Linux": ('"x86"', '"64"', '""'),
}

_TASKBAR_PX = {
    "macOS": 25,
    "Windows": 40,
    "Linux": 24,
}


def _merge_headers_ci(base: Dict[str, str], extra: Dict[str, str]) -> None:
    """Case-insensitive header update so mixed-case dicts never emit duplicate wire headers."""
    if not extra:
        return
    index = {k.lower(): k for k in base}
    for key, value in extra.items():
        low = key.lower()
        existing = index.get(low)
        if existing is not None and existing != key:
            base.pop(existing, None)
        base[key] = value
        index[low] = key


@contextmanager
def _without_session_headers(http: Any, omit_lower: set[str], *, prefix_oai: bool = False):
    http_headers = getattr(http, "headers", None)
    if http_headers is None:
        inner = getattr(http, "_inner", None)
        http_headers = getattr(inner, "headers", None) if inner is not None else None
    saved: list[tuple[str, Any]] = []
    if http_headers is not None and hasattr(http_headers, "pop"):
        for key in list(http_headers.keys()):
            low_key = str(key).lower()
            if low_key in omit_lower or (prefix_oai and low_key.startswith("oai-")):
                old = http_headers.pop(key, None)
                if old is not None:
                    saved.append((key, old))
    try:
        yield
    finally:
        if http_headers is not None and saved:
            for key, old in saved:
                try:
                    http_headers[key] = old
                except Exception:  # noqa: BLE001
                    pass


def _account_check_item(payload: Dict[str, Any], account_id: str = "") -> tuple[Dict[str, Any], str]:
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, dict):
        raise ValueError("响应缺少 accounts 对象")
    requested = str(account_id or "").strip()
    if requested and isinstance(accounts.get(requested), dict):
        return accounts[requested], requested
    default = accounts.get("default")
    if isinstance(default, dict):
        account = default.get("account")
        return default, str((account or {}).get("account_id") or "default")
    for key, value in accounts.items():
        if key != "default" and isinstance(value, dict):
            return value, str(key)
    raise ValueError("未找到可解析的账号条目")


def parse_account_check(payload: Dict[str, Any], *, account_id: str = "") -> Dict[str, Any]:
    """解析 accounts/check/v4 响应，保留可用于 checkout 的活动 id。"""
    item, account_key = _account_check_item(payload, account_id)
    account = item.get("account") or {}
    entitlement = item.get("entitlement") or {}
    campaigns = item.get("eligible_promo_campaigns") or {}
    if not isinstance(campaigns, dict):
        campaigns = {}
    plan_type = str(account.get("plan_type") or "").strip().lower()
    subscription_plan = str(entitlement.get("subscription_plan") or "").strip().lower()
    is_free = plan_type == "free" or subscription_plan == "chatgptfreeplan"
    parsed_campaigns = []
    for key, raw in campaigns.items():
        if not isinstance(raw, dict):
            continue
        metadata = raw.get("metadata") or {}
        discount = metadata.get("discount") or {}
        duration = metadata.get("duration") or {}
        parsed_campaigns.append(
            {
                "key": str(key),
                "id": str(raw.get("id") or ""),
                "discount_percentage": discount.get("percentage"),
                "duration_num_periods": duration.get("num_periods"),
                "duration_period": duration.get("period"),
                "title": metadata.get("title"),
                "summary": metadata.get("summary"),
                "promotion_type": metadata.get("promotion_type_label"),
            }
        )
    if not is_free:
        parsed_campaigns = []
    plus_campaigns = [campaign for campaign in parsed_campaigns if campaign["key"] == "plus"]
    return {
        "account_id": str(account.get("account_id") or account_key or ""),
        "current_plan_type": plan_type or None,
        "subscription_plan": subscription_plan or None,
        "is_free": is_free,
        "plus_eligible": bool(is_free and plus_campaigns),
        "campaigns": parsed_campaigns,
    }


def _checkout_body(
    *,
    plan: str,
    country: str,
    currency: str,
    billing_country: str = "",
    promo: bool = False,
    promo_code: str = "",
    promo_campaign_id: str = "",
    workspace_name: str = "",
    seat_quantity: int = 5,
    price_interval: str = "month",
    cancel_url: str = "",
) -> Dict[str, Any]:
    if str(plan) == "team":
        seats = clamp_team_seats(seat_quantity)
        interval = "year" if str(price_interval).lower() == "year" else "month"
        body: Dict[str, Any] = {
            "entry_point": "team_workspace_purchase_modal",
            "plan_name": "chatgptteamplan",
            "billing_details": {"country": billing_country or country, "currency": currency},
            "cancel_url": f"{APP_BASE}/",
            "checkout_ui_mode": "redirect",
            "check_card_proxy": True,
            "team_plan_data": {
                "workspace_name": (workspace_name or "Team Workspace")[:80],
                "price_interval": interval,
                "seat_quantity": seats,
            },
        }
        code = str(promo_code or "").strip()
        if code:
            body["promo_code"] = code[:80]
        return body
    body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": _PLAN_NAME.get(str(plan), "chatgptplusplan"),
        # 实测 HAR（docs/evidence/1029plus-payment-subset.json）：建单时
        # billing_details 记为「当前选中地区」= 出口国 + 出口币种
        # （例：JP 出口 → {"country": "JP", "currency": "JPY"}）。
        # 固定美国免税地址在下一步 POST /payments/checkout/taxes 才提交。
        "billing_details": {"country": (country or billing_country).upper(), "currency": currency},
        "checkout_source": "chatgpt_web_merged",
        "supports_saved_payment_link": True,
        "checkout_appearance": "light",
        "cancel_url": cancel_url or f"{APP_BASE}/?cta_tab=personal",
        "purchase_attempt_id": str(uuid.uuid4()),
        "checkout_ui_mode": "custom",
    }
    campaign_id = str(promo_campaign_id or "").strip()[:120]
    if promo or campaign_id:
        body["promo_campaign"] = {
            "promo_campaign_id": campaign_id or "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }
    # Personal Plus/Go campaigns use promo_campaign_id. Keep promo_code for the
    # hosted Team flow, whose checkout accepts an explicit code field.
    return body


class CheckoutAmountChanged(DomainError):
    """A quote changed before any checkout confirmation was submitted."""
    def __init__(self, amount_minor: int, currency: str):
        super().__init__(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "结账金额已变化，请重新确认")
        self.amount_minor = amount_minor
        self.currency = currency


@dataclass
class CheckoutSession:
    checkout_session_id: str
    publishable_key: str
    account_id: str
    checkout_url: str
    currency: str
    amount: Optional[Decimal] = None
    processor_entity: str = ""
    attestation: str = ""
    client_build_number: str = ""
    client_version: str = ""
    oai_session_id: str = ""
    customer_session_client_secret: str = ""
    checkout_kind: str = "custom"
    confirm_return_url: str = ""
    tax_delta: Optional[Decimal] = None


def jwt_account_id(access_token: str) -> str:
    parts = str(access_token or "").split(".")
    if len(parts) < 2:
        return ""
    try:
        padded = parts[1] + ("=" * (-len(parts[1]) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        value = auth.get("chatgpt_account_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _accept_language(language: str) -> str:
    """Format Accept-Language without duplicating 'en' on en-* locales."""
    loc = str(language or "en-US").strip() or "en-US"
    primary = loc.split("-", 1)[0]
    value = f"{loc},{primary};q=0.9" if primary and primary != loc else loc
    if primary != "en":
        value += ",en;q=0.8"
    return value


def _new_session(*, proxy: Optional[str], timeout: float, impersonate: str = _IMPERSONATE):
    try:
        from curl_cffi.requests import Session as CurlSession
    except ImportError as exc:  # pragma: no cover
        raise DomainError(
            ErrorCode.CHECKOUT_UPSTREAM_ERROR,
            "缺少 curl_cffi，无法用浏览器指纹请求 ChatGPT",
        ) from exc
    try:
        session = CurlSession(impersonate=impersonate, timeout=timeout, default_headers=False)
    except Exception:
        session = CurlSession(impersonate="chrome150", timeout=timeout, default_headers=False)
    if hasattr(session, "trust_env"):
        session.trust_env = False
    proxy = str(proxy or "").strip()
    session.proxies = {"http": proxy, "https": proxy} if proxy else {}
    return session


@dataclass
class ChatGPTClient:
    access_token: str
    proxy: Optional[str] = None
    timeout: float = ORDER_HTTP_TIMEOUT_S
    account_id: str = ""
    attestation: str = ""
    client_build_number: str = ""
    client_version: str = ""
    oai_session_id: str = ""
    session_token: str = ""
    # OAuth refresh token; never serialized, logged or persisted.
    refresh_token: str = field(default="", repr=False)
    device_id: str = ""
    user_agent: str = ""
    session_country: str = ""
    extra_cookies: Dict[str, str] = field(default_factory=dict)
    language: str = "en-US"
    timezone: str = "America/New_York"
    sentinel_token: str = ""
    sentinel_so: str = ""
    # 页面里解析到的 sentinel 脚本版本（sv）；空则回退桥内置默认值。
    sentinel_version: str = ""
    purchase_attempt_id: str = ""
    browser_context: Any = field(default=None, repr=False)
    _sec_ch_ua_override: str = field(default="", repr=False)
    _http: Any = field(default=None, repr=False)
    _stripe_http: Any = field(default=None, repr=False)
    credentials_current: Optional[Callable[[], bool]] = field(default=None, repr=False, compare=False, kw_only=True)
    _stripe_js_id: str = field(default="", repr=False)
    _elements_session_id: str = field(default="", repr=False)
    _strip_ambient: bool = field(default=False, repr=False)
    _last_refresh_transport_error: Optional[tuple[str, Optional[int]]] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        ctx = self.browser_context
        if ctx is not None:
            if getattr(ctx, "user_agent", ""):
                self.user_agent = str(ctx.user_agent)
            if getattr(ctx, "sec_ch_ua", ""):
                self._sec_ch_ua_override = str(ctx.sec_ch_ua)
            if self.language == "en-US" and getattr(ctx, "locale", ""):
                self.language = str(ctx.locale)
            if self.timezone == "America/New_York" and getattr(ctx, "timezone_id", ""):
                self.timezone = str(ctx.timezone_id)

    @classmethod
    def from_raw_session(
        cls, raw: str, proxy: Optional[str] = None, *, strip_ambient: bool = False
    ) -> "ChatGPTClient":
        bundle: SessionBundle = parse_session_bundle(raw, strip_ambient=strip_ambient)
        token = bundle.access_token
        # 只粘贴 session token 时 accessToken 为空，改用 session token 派生设备标识，避免所有用户撞同一个 id。
        seed = token or bundle.session_token
        if strip_ambient:
            # A fresh device per attempt: the imported one belongs to the exit
            # that exported the session, not to this order's proxy.
            device = str(uuid.uuid4())
            session_id = str(uuid.uuid4())
        else:
            device = bundle.device_id or str(uuid.uuid5(uuid.NAMESPACE_URL, "dcp-did:" + seed))
            session_id = bundle.oai_session_id or str(uuid.uuid5(uuid.NAMESPACE_URL, "dcp-sid:" + seed))
        return cls(
            access_token=token,
            proxy=proxy,
            account_id=jwt_account_id(token),
            session_token=bundle.session_token,
            refresh_token=bundle.refresh_token,
            device_id=device,
            user_agent=bundle.user_agent,
            session_country=bundle.country,
            extra_cookies=dict(bundle.cookies),
            oai_session_id=session_id,
            _strip_ambient=strip_ambient,
        )

    @classmethod
    def reauthorize_for_country(
        cls,
        raw: str,
        *,
        proxy: Optional[str],
        country: str,
        preserve_user_agent: bool = False,
        warmup_edge: bool = True,
    ) -> "ChatGPTClient":
        """在目标出口用 sessionToken（ST）重新签发 accessToken（AT）。

        旧 accessToken / 旧 statsigContext / 旧边缘 Cookie 一律丢弃；
        在目标出口 IP 下使用与当前出口匹配的语言/时区与干净设备指纹
        （可选保留原 UA）重新请求 `/api/auth/session`，拿到绑定当前环境的新 AT。
        """
        bundle = parse_session_bundle(raw)
        clean_st = str(bundle.session_token or "").replace("\r", "").replace("\n", "").strip()
        if not clean_st:
            raise DomainError(
                ErrorCode.INVALID_SESSION,
                "跨区充值需要完整 Session（缺少 __Secure-next-auth.session-token）",
            )
        device = str(uuid.uuid4())
        profile = _pick_profile(device)
        user_agent = (
            bundle.user_agent
            if preserve_user_agent and bundle.user_agent
            else profile["ua"].format(build=profile["build"])
        )
        session_cookie = "__Secure-next-auth.session-token"
        client = cls(
            access_token="",
            proxy=proxy,
            session_token=clean_st,
            refresh_token=bundle.refresh_token,
            device_id=device,
            user_agent=user_agent,
            oai_session_id=str(uuid.uuid4()),
            session_country=country,
            language=locale_for(country),
            timezone=timezone_for(country),
            extra_cookies=identity_cookies_for_reissue(
                {session_cookie: clean_st}
            ),
            _strip_ambient=True,
        )
        try:
            # 冷连接裸发 session-token 容易在边缘被判成异常：先在同一连接上
            # 做一次匿名首页文档导航（不带 session-token 凭证），拿到当前出口 IP
            # 对应的 __cf_bm / _cfuvid；若随机 SID 本身死隧道则直接抛传输错误，
            # 此时尚未发送任何会话凭证，调用方可安全轮换一次新 SID。
            if warmup_edge:
                client.warmup(
                    reject_unauthorized=False,
                    anonymous=True,
                    raise_on_transport_error=bool(proxy),
                )
                client._warm_wait((0.4, 1.0))
            # _strip_ambient=True already blocks echoed statsig overrides inside refresh_session.
            if not client.refresh_session(reject_unauthorized=True):
                if client._last_refresh_transport_error is not None:
                    raise DomainError(
                        ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                        "重签发阶段出口代理连接中断（未确认会话失效），请稍后重试。",
                        detail={
                            "code": "proxy_tunnel_failed",
                            "stage": "reissue",
                            "credentials_sent": True,
                        },
                    )
                raise DomainError(
                    ErrorCode.INVALID_SESSION,
                    f"无法在目标国家 {country} 用 session token 重签发 Session，请检查 session token 或该国家出口代理是否可用",
                )
            # 注：重颁完成到建单之间的拟人停顿由调用方（orders.py _human_pause_async /
            # PlusHarProtocol warm_exit_session）统一执行，避免在线程池内重复背靠背 sleep。
            rotated = client.extra_cookies.get(session_cookie, "")
            if rotated:
                client.session_token = rotated
            # 刷新响应可能回带旧 oai-did；强制回到本次新生成的设备标识。
            client.extra_cookies["oai-did"] = client.device_id
            client.session_country = country
            if client._http is not None:
                client._http.headers.update(client._identity_headers())
            return client
        except Exception:
            client.close()
            raise

    @classmethod
    def reauthorize_oauth(
        cls, raw: str, *, proxy: Optional[str], country: str
    ) -> "ChatGPTClient":
        """在目标出口用 OAuth refresh_token 换取全新 access_token，并生成干净设备身份。"""
        bundle = parse_session_bundle(raw)
        if not bundle.refresh_token:
            raise DomainError(
                ErrorCode.INVALID_SESSION,
                "缺少 OAuth refresh_token，无法重新签发访问令牌",
            )
        device = str(uuid.uuid4())
        profile = _pick_profile(device)
        user_agent = profile["ua"].format(build=profile["build"])
        client = cls(
            access_token="",
            proxy=proxy,
            session_token=bundle.session_token,
            refresh_token=bundle.refresh_token,
            device_id=device,
            user_agent=user_agent,
            oai_session_id=str(uuid.uuid4()),
            session_country=country,
            language=locale_for(country),
            timezone=timezone_for(country),
            extra_cookies={"oai-did": device},
            _strip_ambient=True,
        )
        try:
            if not client.refresh_oauth_token() or not client.access_token:
                raise DomainError(
                    ErrorCode.INVALID_SESSION,
                    f"无法在目标国家 {country} 通过 OAuth refresh_token 签发新访问令牌",
                )
            return client
        except Exception:
            client.close()
            raise

    def _cookie_header(self, *, include_session_token: bool = True) -> str:
        values: Dict[str, str] = {}
        for name, value in self.extra_cookies.items():
            clean_name = str(name or "").replace("\r", "").replace("\n", "").strip()
            if not clean_name or value is None:
                continue
            if not include_session_token and clean_name == "__Secure-next-auth.session-token":
                continue
            values[clean_name] = str(value).replace("\r", "").replace("\n", "").strip()
        if include_session_token and self.session_token:
            clean_st = str(self.session_token).replace("\r", "").replace("\n", "").strip()
            if clean_st:
                values.setdefault("__Secure-next-auth.session-token", clean_st)
        if self.device_id:
            clean_did = str(self.device_id).replace("\r", "").replace("\n", "").strip()
            if clean_did:
                values["oai-did"] = clean_did
        return "; ".join(f"{name}={value}" for name, value in values.items() if name)

    @property
    def profile(self) -> dict:
        # Session 里带真实 UA 时以其为准，HTTP 头与 Node 哨兵子进程共享同一份档案。
        return _profile_from_session_ua(self.user_agent) or _pick_profile(
            getattr(self, "device_id", "") or ""
        )

    @property
    def chrome_build(self) -> str:
        return self.profile["build"]

    @property
    def chrome_major(self) -> str:
        return self.chrome_build.split(".", 1)[0]

    @property
    def impersonate(self) -> str:
        return impersonate_for_major(self.chrome_major)

    def _grease_brand(self) -> tuple[str, str]:
        """本档案的 GREASE 品牌与版本（低熵/高熵 UA-CH 共用同一来源）。"""
        return ua_ch.grease_brand(self.chrome_major, self._sec_ch_ua_override)

    @property
    def sec_ch_ua(self) -> str:
        return ua_ch.sec_ch_ua(self.chrome_major, self._sec_ch_ua_override)

    @property
    def sec_ch_ua_full_list(self) -> str:
        return ua_ch.sec_ch_ua_full_version_list(self.chrome_build, self._sec_ch_ua_override)

    @property
    def ua(self) -> str:
        if self.user_agent:
            return self.user_agent
        return self.profile["ua"].format(build=self.chrome_build)

    def _effective_hints(self) -> Dict[str, str]:
        hints = dict(self.profile.get("hints") or {})
        context = self.browser_context
        if context is not None:
            platform = str(getattr(context, "sec_ch_ua_platform", "") or "").strip('"')
            if platform in _PLATFORM_CH_DEFAULTS:
                ctx_headers = {k.lower(): str(v) for k, v in (context.headers() or {}).items()}
                def_arch, def_bits, def_ver = _PLATFORM_CH_DEFAULTS[platform]
                hints["sec-ch-ua-platform"] = f'"{platform}"'
                hints["sec-ch-ua-arch"] = ctx_headers.get("sec-ch-ua-arch") or def_arch
                hints["sec-ch-ua-bitness"] = ctx_headers.get("sec-ch-ua-bitness") or def_bits
                hints["sec-ch-ua-platform-version"] = ctx_headers.get("sec-ch-ua-platform-version") or def_ver
        return hints

    @property
    def sentinel_profile(self) -> dict:
        p = self.profile
        hints = self._effective_hints()
        profile = {"platform": p["platform"], "platform_label": p["label"],
                   "screen_w": p["screen"][0], "screen_h": p["screen"][1],
                   "avail_h": p["screen"][2], "cores": 8,
                   # 与主站请求同一份高熵 Client Hints，避免哨兵请求缺 arch/bitness/版本
                   "sec_ch_ua_arch": str(hints.get("sec-ch-ua-arch") or ""),
                   "sec_ch_ua_bitness": str(hints.get("sec-ch-ua-bitness") or ""),
                   "sec_ch_ua_platform_version": str(hints.get("sec-ch-ua-platform-version") or "")}
        context = self.browser_context
        if context is not None:
            platform = str(getattr(context, "sec_ch_ua_platform", "") or "").strip('"')
            mapping = {"macOS": ("MacIntel", "macOS"), "Windows": ("Win32", "Windows"),
                       "Linux": ("Linux x86_64", "Linux")}
            if platform in mapping:
                profile["platform"], profile["platform_label"] = mapping[platform]
            width = int(getattr(context, "viewport_width", 0) or 0)
            height = int(getattr(context, "viewport_height", 0) or 0)
            if width >= 800:
                profile["screen_w"] = width
            if height >= 600:
                taskbar = _TASKBAR_PX.get(profile["platform_label"], 40)
                profile["screen_h"] = height
                profile["avail_h"] = max(1, height - taskbar)
        return profile

    def apply_browser_context(self, context: Any) -> None:
        """Apply a Route-C browser environment before create_checkout."""
        if context is None:
            return
        self.browser_context = context
        if getattr(context, "user_agent", ""):
            self.user_agent = str(context.user_agent)
        if getattr(context, "sec_ch_ua", ""):
            self._sec_ch_ua_override = str(context.sec_ch_ua)
        if getattr(context, "locale", ""):
            self.language = str(context.locale)
        if getattr(context, "timezone_id", ""):
            self.timezone = str(context.timezone_id)
        cookies = getattr(context, "cookies", None) or {}
        if isinstance(cookies, dict):
            from gss_core.browser_context import safe_browser_cookies
            self.extra_cookies.update(safe_browser_cookies(cookies))
        # Anonymous browser attestations are not credentials for this session.
        if self._http is not None:
            self._http.headers.update(self._identity_headers())
        if self._stripe_http is not None:
            self._stripe_http.headers.update(self._stripe_headers())

    def _document_nav_headers(self) -> Dict[str, str]:
        """顶层文档导航（`GET /`）的头集合：真 Chrome 的冷导航形状。

        与 `_identity_headers()`（API 请求）刻意分开：导航请求不携带 Authorization /
        Origin / oai-* / chatgpt-account-id，`sec-fetch-site: none` 时不带 Referer。
        把 API 头混进文档导航，等于在同一条会话上同时宣告「脚本 API 调用」与
        「用户在地址栏输入 URL」两种互斥的发起方式。
        """
        headers: Dict[str, str] = {
            "User-Agent": self.ua,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
            ),
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": _accept_language(self.language),
            "priority": "u=0, i",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            # 冷导航（sec-fetch-site: none）只发低熵 UA-CH：真 Chrome 的高熵 hints
            # 只在站点通过 Accept-CH 持久化策略之后才上送，首次顶层导航就带
            # full-version / arch / bitness / platform-version 是自相矛盾的。
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": self._effective_hints().get("sec-ch-ua-platform", '""'),
            "Cookie": self._cookie_header(),
        }
        if not headers.get("Cookie"):
            headers.pop("Cookie", None)
        if self.browser_context is not None:
            omit_lower = set(_DOC_NAV_OMIT_HEADERS) | set(_DOC_NAV_HIGH_ENTROPY_HINTS)
            context_headers = {
                k: v for k, v in (self.browser_context.headers() or {}).items()
                if k.lower() not in omit_lower and not k.lower().startswith("oai-")
            }
            _merge_headers_ci(headers, context_headers)
        # 防御：任何 oai-* / Referer / Authorization / Origin / Content-Type 键都不该出现在冷导航上。
        omit_final = {"referer", "authorization", "origin", "content-type", "chatgpt-account-id"}
        return {
            k: v for k, v in headers.items()
            if k.lower() not in omit_final and not k.lower().startswith("oai-")
        }

    def _get_headers(self) -> Dict[str, str]:
        """同源无 body 的 GET 头：去掉 `Origin` 与 `Content-Type`。

        真 Chrome 只在带 body 或跨源请求上发 `Origin` / `Content-Type`；
        同源 GET 同时带这两个头是「脚本直接构造请求」的痕迹（Fetch 规范里
        只有 CORS/POST 才会附加它们）。
        """
        headers = self._identity_headers()
        headers.pop("Origin", None)
        headers.pop("Content-Type", None)
        headers.pop("origin", None)
        headers.pop("content-type", None)
        return headers

    def _http_get(
        self,
        route: str,
        *,
        params: Optional[Dict[str, str]] = None,
        page_url: str = "",
        timeout: Optional[float] = None,
    ):
        url = route if route.startswith("http") else f"{APP_BASE}{route}"
        route_path = urlsplit(url).path or route
        route_headers = (
            self._route_headers(route_path, page_url=page_url)
            if page_url
            else self._route_headers(route_path)
        )
        if hasattr(self, "extra_cookies") and hasattr(self, "language"):
            headers = self._get_headers()
            headers.update(route_headers)
        else:
            headers = dict(route_headers)
        for key in ("Origin", "origin", "Content-Type", "content-type"):
            headers.pop(key, None)
        kwargs: Dict[str, Any] = {
            "headers": headers,
            "timeout": self.timeout if timeout is None else timeout,
        }
        if params is not None:
            kwargs["params"] = params
        http = self.http
        with _without_session_headers(http, {"origin", "content-type"}):
            return http.get(url, **kwargs)

    def _identity_headers(self) -> Dict[str, str]:
        build = self.chrome_build
        headers = {
            "User-Agent": self.ua,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": _accept_language(self.language),
            "Authorization": f"Bearer {self.access_token}",
            "Origin": APP_BASE,
            "Referer": f"{APP_BASE}/",
            "Content-Type": "application/json",
            "priority": "u=1, i",
            "oai-did": self.device_id,
            "oai-device-id": self.device_id,
            "oai-language": self.language,
            "oai-session-id": self.oai_session_id,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            # 高熵 Client Hints：真实 Chrome 在 chatgpt.com 的 API 请求上全部发送。
            "sec-ch-ua-full-version": f'"{build}"',
            # 与低熵 sec-ch-ua 共用同一个 GREASE 品牌（见 _grease_brand）
            "sec-ch-ua-full-version-list": self.sec_ch_ua_full_list,
            "sec-ch-ua-model": '""',
            **self._effective_hints(),
            "Cookie": self._cookie_header(),
        }
        # 只在 warmup 真从页面解析到构建号时才发，避免把过期版本写死在指纹里。
        if self.client_version:
            headers["oai-client-version"] = self.client_version
        if self.client_build_number:
            headers["oai-client-build-number"] = self.client_build_number
        if not self.access_token:
            # 仅凭 session cookie 换 token 时不发空的 Bearer。
            headers.pop("Authorization")
        if self.account_id:
            headers["chatgpt-account-id"] = self.account_id
        if self.browser_context is not None:
            _merge_headers_ci(headers, self.browser_context.headers() or {})
        return headers

    def _route_headers(self, route: str, *, page_url: str = "") -> Dict[str, str]:
        headers: Dict[str, str] = {"Referer": page_url or f"{APP_BASE}/"}
        # 真实 HAR 的 payments/* 请求不带 `x-openai-target-*`；
        # 其余非支付路由沿用旧契约（旧版本 SPA 确实发送）。
        if not route.startswith("/backend-api/payments/"):
            headers["x-openai-target-path"] = route
            headers["x-openai-target-route"] = route
        if route.startswith("/backend-api/payments/"):
            headers.update(_WEB_CHECKOUT_HEADERS)
        if route == "/backend-api/payments/checkout":
            headers["oai-telemetry"] = "[1,null]"
        if route.startswith("/backend-api/payments/") and self.attestation:
            headers["oai-web-deployment-attestation"] = self.attestation
        if route.startswith("/backend-api/payments/"):
            if self.sentinel_token:
                headers["openai-sentinel-token"] = self.sentinel_token
            if self.sentinel_so:
                headers["openai-sentinel-so-token"] = self.sentinel_so
        return headers

    def _app_cookie_names(self) -> set[str]:
        jar = getattr(getattr(self.http, "cookies", None), "jar", None)
        if not isinstance(jar, CookieJar):
            return set()
        return {
            cookie.name for cookie in jar
            if cookie.domain in {"", "chatgpt.com", ".chatgpt.com"} and cookie.path == "/"
        }

    def _clear_http_cookie_jar(self) -> None:
        """Keep curl_cffi's CookieJar empty after harvesting Set-Cookie into extra_cookies."""
        jar = getattr(self._http, "cookies", None)
        cookie_store = getattr(jar, "jar", None)
        if not isinstance(cookie_store, CookieJar):
            return
        try:
            clear = getattr(jar, "clear", None)
            if callable(clear):
                clear()
            else:
                cookie_store.clear()
        except Exception:
            pass

    def _merge_cookies(self, previous_names: set[str] | None = None) -> None:
        jar = getattr(self.http, "cookies", None)
        if jar is None:
            return
        cookie_store = getattr(jar, "jar", None)
        removed: set[str] = set()
        if isinstance(cookie_store, CookieJar):
            known_names = self._app_cookie_names() | (previous_names or set())
            cookie_store.clear_expired_cookies()
            removed = known_names - self._app_cookie_names()
            for name in removed:
                self.extra_cookies.pop(name, None)
            if "__Secure-next-auth.session-token" in removed:
                self.session_token = ""
        gathered: Dict[str, str] = {}
        try:
            getter = getattr(jar, "get_dict", None)
            if callable(getter):
                gathered.update(getter() or {})
                for domain in ("chatgpt.com", ".chatgpt.com"):
                    try:
                        gathered.update(getter(domain=domain) or {})
                    except TypeError:
                        break
                    except Exception:
                        continue
            elif hasattr(jar, "items"):
                gathered.update({str(k): str(v) for k, v in jar.items()})
        except Exception:
            gathered = {}
        for name, value in gathered.items():
            if name and name not in removed and value is not None:
                self.extra_cookies[str(name)] = str(value)
        self._clear_http_cookie_jar()
        self.http.headers.update(self._identity_headers())

    def _sync_rotated_session_token(self, response: Any, payload: Dict[str, Any]) -> bool:
        """Adopt a rotated sessionToken from the body or Set-Cookie, in memory only."""
        rotated = str(payload.get("sessionToken") or payload.get("session_token") or "").strip()
        if not rotated:
            rotated = _session_token_from_set_cookie(response)
        if not rotated or rotated == self.session_token:
            return False
        self.session_token = rotated
        self.extra_cookies["__Secure-next-auth.session-token"] = rotated
        jar = getattr(self.http, "cookies", None)
        setter = getattr(jar, "set", None) if jar is not None else None
        if callable(setter):
            try:
                setter("__Secure-next-auth.session-token", rotated, domain="chatgpt.com")
            except TypeError:
                try:
                    setter("__Secure-next-auth.session-token", rotated)
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass
        if self._http is not None:
            self._http.headers.update(self._identity_headers())
        logger.info("session_token_rotated")
        return True

    def refresh_session(self, *, trust_statsig: bool = True, reject_unauthorized: bool = False) -> bool:
        """用当前出口 IP 请求 /api/auth/session，换取与本批次出口一致的 accessToken 与 cookie。

        结账地区由出口代理决定（align_proxy_region），session 本身不绑定国家；
        刷新的目的是让本次请求携带的 accessToken / oai-sc 等 cookie 与出口 IP
        同批次，降低 Cloudflare 403 与 unusual activity 概率。

        软失败：session-token 缺失/失效、上游报错都返回 False，调用方沿用原
        session 继续。建单方显式要求在 HTTP 401 时以脱敏诊断立即终止。刷新结果只在内存，不落库。
        """
        self._last_refresh_transport_error = None
        if not self.session_token:
            return False
        # NextAuth /api/auth/session 仅凭 sessionToken Cookie 鉴权；
        # 绝不携带旧/可能已被吊销的 Authorization Bearer，也不在 GET 请求上发送
        # Origin / Content-Type / chatgpt-account-id / oai-* / x-openai-target-*。
        http_headers = getattr(self.http, "headers", None)
        prior_auth: list[tuple[str, Any]] = []
        auth_omit_lower = {
            "authorization", "origin", "content-type", "chatgpt-account-id",
            "oai-did", "oai-device-id", "oai-language", "oai-session-id",
            "oai-client-version", "oai-client-build-number",
        }
        if http_headers is not None and hasattr(http_headers, "pop"):
            for existing_key in list(http_headers.keys()):
                low_key = str(existing_key).lower()
                if low_key in auth_omit_lower or low_key.startswith("oai-"):
                    old_val = http_headers.pop(existing_key, None)
                    if old_val is not None:
                        prior_auth.append((existing_key, old_val))

        def _restore_auth() -> None:
            if http_headers is not None and prior_auth:
                for auth_key, old_val in prior_auth:
                    try:
                        http_headers[auth_key] = old_val
                    except Exception:  # noqa: BLE001
                        pass

        try:
            previous_names = self._app_cookie_names()
            response = self.http.get(
                f"{APP_BASE}/api/auth/session",
                headers={"Referer": f"{APP_BASE}/"},
                timeout=self.timeout,
            )
        except Exception as exc:
            _restore_auth()
            curl_code = getattr(exc, "code", None)
            safe_code = curl_code if isinstance(curl_code, int) else None
            self._last_refresh_transport_error = (type(exc).__name__, safe_code)
            # Exception strings can contain complete proxy URLs or request headers.
            logger.warning(
                "session_refresh_transport_error type=%s code=%s",
                type(exc).__name__,
                safe_code if safe_code is not None else "-",
            )
            return False
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 401:
            _restore_auth()
            if reject_unauthorized:
                self._reject_unauthorized(response)
            logger.warning("session 刷新失败（HTTP 401）")
            return False
        if status >= 400:
            _restore_auth()
            _log_upstream_failure("/api/auth/session", response)
            logger.warning("session 刷新失败（HTTP %s），继续用原 session", status)
            return False
        payload = _json(response)
        # The server may rotate the session cookie even when it does not
        # re-issue accessToken; persist it in memory before any early return.
        self._sync_rotated_session_token(response, payload)
        token = str(payload.get("accessToken") or "").strip()
        if not token:
            self._clear_http_cookie_jar()
            _restore_auth()
            # session-token 已失效时上游返回 200 + 空 accessToken。
            logger.warning("session 刷新未返回 accessToken（session-token 可能已失效），继续用原 session")
            return False
        self.access_token = token
        account = jwt_account_id(token)
        if account:
            self.account_id = account
        # /api/auth/session 会回带签发时的 statsigContext；普通刷新时同步真实
        # 设备身份。新出口重签发（trust_statsig=False 或 _strip_ambient=True）时
        # 只接受新 token/cookie，不允许旧 statsig 环境字段覆盖刚生成的干净设备。
        if trust_statsig and not self._strip_ambient:
            identity = parse_statsig_context(payload)
            if identity["device_id"]:
                self.device_id = identity["device_id"]
            if identity["user_agent"]:
                self.user_agent = identity["user_agent"]
            if identity["country"]:
                self.session_country = identity["country"]
        self._merge_cookies(previous_names)
        logger.info("session 刷新成功，已与本批次出口对齐")
        return True

    def refresh_oauth_token(self, *, client_id: str = "") -> bool:
        """Exchange an OAuth refresh token on this exit for a new access token.

        This path is independent of the web session cookie: the token is bound
        to the client that requested it, not to the browser's login country.
        The refresh token is rotated in memory when the server returns a new
        one; nothing is written to disk.
        """
        if not self.refresh_token:
            return False
        client_id = (client_id or os.environ.get("GSS_OAUTH_CLIENT_ID", OAUTH_CLIENT_ID)).strip()
        if not client_id:
            raise DomainError(ErrorCode.INVALID_SESSION, "缺少 OAuth client_id，无法刷新。",
                              detail={"code": "oauth_refresh_rejected"})
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": client_id,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": self.ua,
        }
        try:
            session = _new_session(proxy=self.proxy, timeout=self.timeout, impersonate=self.impersonate)
            try:
                response = session.post(OAUTH_TOKEN_URL, data=form, headers=headers)
            finally:
                close = getattr(session, "close", None)
                if callable(close):
                    close()
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                "OAuth 刷新请求未完成，未创建结账。",
                detail={"code": "oauth_refresh_transport", "reason": type(exc).__name__},
            ) from None
        status = int(getattr(response, "status_code", 0) or 0)
        payload = _json(response)
        if status >= 400:
            wire = str(payload.get("error") or "")[:40]
            raise DomainError(
                ErrorCode.INVALID_SESSION,
                "OAuth refresh_token 刷新被上游拒绝，请重新登录导出凭据。",
                detail={"code": "oauth_refresh_rejected", "upstream_status": status,
                        "upstream_code": wire},
            )
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise DomainError(
                ErrorCode.INVALID_SESSION,
                "OAuth 刷新未返回 access_token，请重新登录导出凭据。",
                detail={"code": "oauth_refresh_rejected", "upstream_status": status},
            )
        self.access_token = token
        rotated = str(payload.get("refresh_token") or "").strip()
        if rotated:
            self.refresh_token = rotated
        account = jwt_account_id(token)
        if account:
            self.account_id = account
        logger.info("oauth_refresh_ok rotated=%s", bool(rotated))
        return True

    def warmup(
        self,
        *,
        page_url: str = "",
        reject_unauthorized: bool = False,
        anonymous: bool = False,
        raise_on_transport_error: bool = False,
    ) -> None:
        url = page_url or f"{APP_BASE}/"
        headers = self._document_nav_headers()
        if anonymous:
            anon_cookie = self._cookie_header(include_session_token=False)
            if anon_cookie:
                headers["Cookie"] = anon_cookie
            else:
                headers.pop("Cookie", None)
        # curl_cffi merges per-request headers onto session.headers, so omit-only
        # keys must be temporarily popped from session.headers during document GET.
        http_headers = getattr(self.http, "headers", None)
        saved_headers: list[tuple[str, Any]] = []
        omit_lower = set(_DOC_NAV_OMIT_HEADERS) | set(_DOC_NAV_HIGH_ENTROPY_HINTS)
        if anonymous:
            omit_lower.add("cookie")
        if http_headers is not None and hasattr(http_headers, "pop"):
            for key in list(http_headers.keys()):
                low_key = str(key).lower()
                if low_key in omit_lower or low_key.startswith("oai-"):
                    old = http_headers.pop(key, None)
                    if old is not None:
                        saved_headers.append((key, old))
        try:
            home = self.http.get(url, headers=headers, timeout=self.timeout)
        except Exception as exc:
            curl_code = getattr(exc, "code", None)
            logger.warning(
                "warmup_transport_error type=%s code=%s",
                type(exc).__name__,
                curl_code if isinstance(curl_code, int) else "-",
            )
            if raise_on_transport_error:
                raise DomainError(
                    ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                    "重签发前出口代理连通失败（未发送会话凭证），请稍后重试。",
                    detail={
                        "code": "proxy_tunnel_failed",
                        "stage": "reissue",
                        "credentials_sent": False,
                    },
                ) from None
            return
        finally:
            if http_headers is not None and saved_headers:
                for key, old in saved_headers:
                    try:
                        http_headers[key] = old
                    except Exception:  # noqa: BLE001
                        pass
        if reject_unauthorized and getattr(home, "status_code", None) == 401:
            self._reject_unauthorized(home)
        html = str(getattr(home, "text", "") or "")
        version, build, session_id = extract_build(html)
        if version:
            self.client_version = version
        if build:
            self.client_build_number = build
        if session_id:
            self.oai_session_id = session_id
        attestation = extract_attestation(html)
        if attestation:
            self.attestation = attestation
        sentinel_version = extract_sentinel_version(html)
        if sentinel_version:
            self.sentinel_version = sentinel_version
        self._merge_cookies()

    def warm_account(self, *, full: bool = False) -> None:
        """Fetch the normal account bootstrap endpoint before checkout.

        Default is the single lightweight /backend-api/me call. ``full=True``
        adds the HTML shell and accounts/check for diagnostics, but costs extra
        latency and is not used in the order hot path.
        """
        if full:
            self.warmup(reject_unauthorized=True)
        endpoints = (("/backend-api/me", None),)
        if full:
            endpoints += ((ACCOUNTS_CHECK_PATH, {"timezone_offset_min": "0"}),)
        for path, params in endpoints:
            try:
                # Do not rely on the HTTP session's cached headers: after a
                # cross-country refresh they can still hold the previous
                # Bearer/Cookie. Build the account request from the live
                # client fields so /me always uses the just-issued credentials.
                response = self._http_get(path, params=params)
            except Exception as exc:  # noqa: BLE001
                logger.warning("warmup request failed path=%s error=%s", path, type(exc).__name__)
                continue
            status = int(getattr(response, "status_code", 0) or 0)
            if status == 401:
                self._reject_unauthorized(response)
            if status >= 400:
                _log_upstream_failure(path, response)
                continue
            self._merge_cookies()
            logger.info("warmup ok path=%s status=%s", path, status)
        self.http.headers.update(self._identity_headers())

    def warm_exit_session(self, *, reissue: bool = True, oauth: bool = False,
                          interval: tuple[float, float] = (1.5, 2.5)) -> Optional[Dict[str, Any]]:
        """Bring an imported session up to date on the exit it will run on.

        Order matters: the edge handshake creates cookie state for this exit,
        the credential is re-issued for this exit (OAuth refresh token when
        present, otherwise the web session), and the account probes prove the
        credentials are accepted from here. Nothing financial is called before
        all three succeed.
        """
        self.warmup(reject_unauthorized=True)
        self._warm_wait(interval)
        if oauth and self.refresh_token:
            self.refresh_oauth_token()
            self._warm_wait(interval)
        elif reissue and self.session_token:
            if not self.refresh_session(trust_statsig=False, reject_unauthorized=True):
                raise DomainError(
                    ErrorCode.INVALID_SESSION,
                    "目标出口下 Session 刷新未获确认，未创建结账。",
                    detail={"code": "warmup_rejected", "stage": "auth_session"},
                )
            self._warm_wait(interval)
        self._warm_probe("/backend-api/me", None, stage="me")
        self._warm_wait(interval)
        return self._warm_probe(ACCOUNTS_CHECK_PATH, {"timezone_offset_min": "0"},
                                stage="accounts_check")

    @staticmethod
    def _warm_wait(interval: tuple[float, float]) -> None:
        configured = os.environ.get("GSS_WARMUP_INTERVAL", "").strip()
        if configured:
            try:
                delay = max(0.0, float(configured))
            except ValueError:
                delay = random.uniform(*interval)
        else:
            delay = random.uniform(*interval)
        if delay:
            time.sleep(delay)

    def _warm_probe(self, path: str, params: Optional[Dict[str, str]], *,
                    stage: str) -> Dict[str, Any]:
        try:
            response = self._http_get(path, params=params)
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                "预热阶段无法连接 ChatGPT，未创建结账。",
                detail={"code": "warmup_transport_error", "stage": stage,
                        "reason": type(exc).__name__},
            ) from None
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 401:
            detail = self._checkout_response_detail(response, _json(response))
            detail.update({"code": "warmup_rejected", "stage": stage})
            message = ("预热阶段 Session 已被上游撤销（token_revoked），未创建结账。"
                       if detail.get("upstream_code") == "token_revoked"
                       else "预热阶段 Session 被上游拒绝，未创建结账。")
            raise DomainError(ErrorCode.INVALID_SESSION, message, detail=detail)
        if status >= 400:
            detail = self._checkout_response_detail(response, _json(response))
            detail.update({"code": "warmup_rejected", "stage": stage})
            _log_upstream_failure(path, response)
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                f"预热阶段上游拒绝（HTTP {status}），未创建结账。",
                detail=detail,
            )
        self._merge_cookies()
        return _json(response)

    def mint_sentinel(self, *, flow: str, page_url: str = "", ping: bool = True) -> None:
        token = mint(
            flow=flow,
            device_id=self.device_id,
            user_agent=self.ua,
            proxy=self.proxy or "",
            page_url=page_url or f"{APP_BASE}/",
            language=self.language,
            timezone=self.timezone,
            cookie_header=self._cookie_header(),
            timeout_s=max(5.0, min(self.timeout, float(os.environ.get("GSS_SENTINEL_TIMEOUT", "15") or 15), 90.0)),
            impersonate=self.impersonate,
            sec_ch_ua=self.sec_ch_ua,
            sec_ch_ua_platform=self._effective_hints().get("sec-ch-ua-platform", ""),
            version=self.sentinel_version,
            **self.sentinel_profile,
        )
        value = header_value(token)
        if not value:
            reason = token.get("_error") if isinstance(token, dict) else ""
            detail = f"：{redact_sensitive(str(reason))[:200]}" if reason else ""
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                f"无法签发 ChatGPT 风控令牌（Sentinel）{detail}。请确认 Node 可用、出口代理稳定。",
            )
        self.sentinel_token = value
        self.sentinel_so = so_header_value(token)
        # 真浏览器在 /sentinel/req 成功后由 iframe 立刻回一次 /sentinel/ping。
        # plus_har 自己按 HAR 设备档案 ping，传 ping=False 避免同一 req 回两次。
        if ping:
            self._sentinel_ping(page_url=page_url or f"{APP_BASE}/")

    def _sentinel_ping(self, *, page_url: str = "") -> None:
        """对齐真实浏览器：签发令牌后补一次 `/backend-api/sentinel/ping`。

        纯遥测补齐（受 GSS_HAR_TELEMETRY 控制），软失败且超时上限 5s，
        绝不阻断建单；错误正文不记录。
        """
        if os.environ.get("GSS_HAR_TELEMETRY", "full").strip().lower() == "off":
            return
        omit_keys = (
            "Authorization",
            "authorization",
            "Content-Type",
            "content-type",
            "chatgpt-account-id",
            "oai-did",
            "oai-device-id",
            "oai-language",
            "oai-session-id",
            "oai-client-version",
            "oai-client-build-number",
        )
        http_headers = None
        saved_headers: list[tuple[str, Any]] = []
        try:
            http = self.http
            headers = self._identity_headers()
            headers["Accept"] = "*/*"
            headers["Referer"] = page_url or f"{APP_BASE}/"
            headers["cache-control"] = "no-cache"
            headers["pragma"] = "no-cache"
            # iframe 内的 ping 不带 Bearer、JSON Content-Type 及 SPA 业务头；
            # curl_cffi 会将单次请求 headers 合并到 session.headers，因此必须临时弹出并恢复。
            for key in omit_keys:
                headers.pop(key, None)
            if type(http).__module__.startswith("curl_cffi"):
                # libcurl 对空 POST 默认补 application/x-www-form-urlencoded；传 None 彻底禁用。
                headers["Content-Type"] = None  # type: ignore[assignment]
            http_headers = getattr(http, "headers", None)
            if http_headers is not None and hasattr(http_headers, "pop"):
                for key in omit_keys:
                    old = http_headers.pop(key, None)
                    if old is not None:
                        saved_headers.append((key, old))
            timeout = max(1.0, min(float(self.timeout or 5.0), 5.0))
            http.post(
                f"{APP_BASE}/backend-api/sentinel/ping",
                data=b"",
                headers=headers,
                timeout=timeout,
            )
            logger.info("sentinel_ping_ok")
        except Exception:  # noqa: BLE001
            logger.info("sentinel_ping_soft_failed")
        finally:
            if http_headers is not None and saved_headers:
                for key, old in saved_headers:
                    try:
                        http_headers[key] = old
                    except Exception:  # noqa: BLE001
                        pass

    def prepare_risk(self, *, flow: str, page_url: str = "", country: str = "") -> None:
        # 语言/时区跟随 Session 签发地（与出口 IP 同区）；没有 statsig 国家才用账单国。
        effective_country = self.session_country or country
        if effective_country:
            self.language = locale_for(effective_country)
            self.timezone = timezone_for(effective_country)
        url = page_url or f"{APP_BASE}/"
        self.warmup(page_url=url, reject_unauthorized=flow == "chatgpt_checkout")
        self.mint_sentinel(flow=flow, page_url=url)

    def _check_credentials(self) -> None:
        if self.credentials_current is not None and not self.credentials_current():
            raise DomainError(ErrorCode.SESSION_EXPIRED, "付款凭证已过期，停止后续请求")

    @property
    def http(self):
        self._check_credentials()
        if self._http is None:
            self._http = _new_session(proxy=self.proxy, timeout=self.timeout, impersonate=self.impersonate)
            # Cookies are sent exclusively via _identity_headers()["Cookie"] (and
            # any Set-Cookie captured in the jar is merged + cleared in _merge_cookies)
            # so curl_cffi never emits duplicate Cookie headers on the wire.
            self._http.headers.update(self._identity_headers())
        return self._http

    def _stripe_headers(self) -> Dict[str, str]:
        stripe_headers = {
            "User-Agent": self.ua,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": _accept_language(self.language),
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "Content-Type": "application/x-www-form-urlencoded",
            "priority": "u=1, i",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "sec-fetch-storage-access": "active",
        }
        platform_hint = self._effective_hints().get("sec-ch-ua-platform")
        if platform_hint:
            stripe_headers["sec-ch-ua-platform"] = platform_hint
        return stripe_headers

    @property
    def stripe_http(self):
        self._check_credentials()
        if self._stripe_http is None:
            self._stripe_http = _new_session(proxy=self.proxy, timeout=self.timeout, impersonate=self.impersonate)
            self._stripe_http.headers.update(self._stripe_headers())
        return self._stripe_http

    def close(self) -> None:
        for handle in (self._http, self._stripe_http):
            if handle is not None:
                closer = getattr(handle, "close", None)
                if callable(closer):
                    closer()
        self._http = None
        self._stripe_http = None

    @staticmethod
    def _unauthorized_message(response: Any) -> str:
        body = str(getattr(response, "text", "") or "").lower()
        if "token_revoked" in body or "invalidated oauth token" in body:
            return "Session 已被上游撤销（token_revoked），请重新登录 ChatGPT 后导出新 session"
        return "Session 无效或已过期"

    @staticmethod
    def _upstream_error_code(response: Any) -> str:
        payload = _json(response)
        for block in (payload.get("error"), payload.get("detail"), payload):
            if isinstance(block, dict):
                value = block.get("code") or block.get("type")
                if isinstance(value, str) and value.strip():
                    return value.strip()[:80]
        return ""

    @staticmethod
    def _checkout_response_detail(response: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Capture safe values and whether recognized fields survived validation."""
        wire_code = None
        code_present = False
        for block in (payload.get("error"), payload.get("detail"), payload):
            if isinstance(block, dict) and "code" in block:
                code_present = True
                if isinstance(block["code"], str) and block["code"]:
                    wire_code = block["code"]
                    break
        headers = {
            str(key).lower(): value
            for key, value in (getattr(response, "headers", None) or {}).items()
            if str(key).lower() in {"cf-ray", "x-request-id", "request-id"}
        }
        detail = safe_checkout_diagnostics({
            "upstream_status": getattr(response, "status_code", None),
            "upstream_code": wire_code,
            "cf_ray": headers.get("cf-ray"),
            "request_id": headers.get("x-request-id") or headers.get("request-id"),
        })
        # Only these locally calculated states survive; do not retain rejected values.
        id_present = "x-request-id" in headers or "request-id" in headers
        for field, present in (("upstream_code", code_present), ("request_id", id_present)):
            detail[field + "_state"] = "accepted" if field in detail else ("filtered" if present else "not_found")
        return detail

    def _reject_unauthorized(
        self,
        response: Any,
        *,
        method: str = "GET",
        url: str = "",
        title: str = "账号鉴权校验",
        stage: str = "session_auth",
        request_json: Any = None,
    ) -> NoReturn:
        # Pre-checkout account/bootstrap requests can fail before the checkout
        # POST exists. Preserve only this response, never its headers or body.
        payload = _json(response)
        detail = self._checkout_response_detail(response, payload)
        message = self._unauthorized_message(response)
        if detail.get("upstream_code") == "token_revoked":
            message = "Session 已被上游撤销（token_revoked），请重新登录 ChatGPT 后导出新 session"
        exc = DomainError(ErrorCode.INVALID_SESSION, message, detail=detail)
        resolved_url = url or str(getattr(response, "url", "") or f"{APP_BASE}/backend-api/me")
        exc._http_trace = build_redacted_http_trace(
            title=title,
            method=method,
            url=resolved_url,
            http_status=detail.get("upstream_status") or 401,
            stage=stage,
            request_json=request_json,
            response_json=payload,
            response_text=getattr(response, "text", ""),
            cf_ray=detail.get("cf_ray"),
            request_id=detail.get("request_id"),
        )
        raise exc

    def _reject_checkout(
        self,
        response: Any,
        payload: Dict[str, Any],
        *,
        fallback: str = "",
        request_body: Any = None,
        url: str = "",
        title: str = "Checkout 建单",
    ) -> NoReturn:
        detail = self._checkout_response_detail(response, payload)
        status = detail.get("upstream_status")
        wire_code = detail.get("upstream_code")
        # Classification may consult text locally; neither logs nor exceptions carry that text.
        text = str(getattr(response, "text", "") or "")[:500].lower()
        code = ErrorCode.CHECKOUT_UPSTREAM_ERROR
        if status == 401 or wire_code in {"invalid_session", "token_revoked"}:
            code = ErrorCode.INVALID_SESSION
            message = ("Session 已被上游撤销（token_revoked）" if wire_code == "token_revoked"
                       else self._unauthorized_message(response))
        elif wire_code == "unusual_activity" or "unusual activity" in text:
            detail["unusual_activity_source"] = "structured_code" if wire_code == "unusual_activity" else "body_match"
            message = "ChatGPT 判定异常流量，已停止创建结账。"
        elif "billing country must match" in text:
            code, message = ErrorCode.BILLING_COUNTRY_MISMATCH, "上游拒绝结账：账单国家不匹配。"
        elif "already paid" in text:
            code, message = ErrorCode.ALREADY_SUBSCRIBED, "该账号已是付费订阅。"
        elif "only for internal checkout sessions" in text:
            message = "上游只接受内部结账会话。"
        elif fallback:
            message = fallback
        elif status == 403:
            message = "上游拒绝创建结账（HTTP 403）。"
        elif status is not None and status >= 500:
            message = f"创建结账失败：ChatGPT 暂时返回 {status}。"
        else:
            message = f"上游拒绝创建结账（HTTP {status}）。" if status is not None else "上游拒绝创建结账。"
        logger.warning("upstream_error where=/backend-api/payments/checkout status=%s", status)
        exc = DomainError(code, message, detail=detail)
        exc._http_trace = build_redacted_http_trace(
            title=title,
            method="POST",
            url=url or f"{APP_BASE}/backend-api/payments/checkout",
            http_status=status,
            stage="creating_checkout",
            request_json=request_body,
            response_json=payload,
            response_text=getattr(response, "text", ""),
            cf_ray=detail.get("cf_ray"),
            request_id=detail.get("request_id"),
        )
        raise exc

    def _raise(
        self,
        code: ErrorCode,
        response: Any,
        prefix: str,
        *,
        method: str = "POST",
        url: str = "",
        request_json: Any = None,
    ) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 401:
            self._reject_unauthorized(response, method=method, url=url, title=prefix, request_json=request_json)
        _log_upstream_failure(prefix, response)
        raw_text = str(getattr(response, "text", ""))
        body = redact_sensitive(raw_text[:500])
        headers = getattr(response, "headers", {}) or {}
        detail = {
            "upstream_status": status,
            "upstream_code": self._upstream_error_code(response),
            "cf_ray": str(headers.get("cf-ray") or headers.get("CF-Ray") or "")[:80],
            "request_id": str(headers.get("x-request-id") or headers.get("X-Request-Id") or "")[:120],
        }
        detail = {key: value for key, value in detail.items() if value not in (None, "", "-")}
        trace = build_redacted_http_trace(
            title=prefix,
            method=method,
            url=url or str(getattr(response, "url", "") or f"{APP_BASE}/backend-api/payments/checkout"),
            http_status=status or None,
            stage="upstream_request",
            request_json=request_json,
            response_json=_json(response),
            response_text=raw_text,
            cf_ray=detail.get("cf_ray"),
            request_id=detail.get("request_id"),
        )
        lowered = body.lower()
        if "unusual activity" in lowered:
            exc = DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                "ChatGPT 判定异常流量。请换一个同国家 SID 节点，"
                "或用与账单国家相同网络导出的新 session 再试。",
                detail=detail,
            )
            exc._http_trace = trace
            raise exc
        if "billing country must match" in lowered:
            exc = DomainError(
                ErrorCode.BILLING_COUNTRY_MISMATCH,
                "账单国家必须与出口代理国家一致，请用所选地区的 SID 重试。",
            )
            exc._http_trace = trace
            raise exc
        if "already paid" in lowered:
            exc = DomainError(ErrorCode.ALREADY_SUBSCRIBED, f"{prefix}: 该账号已是付费订阅", detail=detail)
            exc._http_trace = trace
            raise exc
        if "only for internal checkout sessions" in lowered:
            exc = DomainError(
                code,
                "ChatGPT 内部确认只接受 oaics_ 会话。当前是 Stripe cs_ 自定义结账，不能调用 /payments/checkout/confirm。",
            )
            exc._http_trace = trace
            raise exc
        if status == 403:
            exc = DomainError(code, f"{prefix}: 被拦截（HTTP 403），请检查出口代理是否可用", detail=detail)
            exc._http_trace = trace
            raise exc
        if status >= 500:
            exc = DomainError(code, f"{prefix}失败：ChatGPT 暂时返回 {status}，请换档位或稍后重试", detail=detail)
            exc._http_trace = trace
            raise exc
        exc = DomainError(code, f"{prefix}: HTTP {status} {body}", detail=detail)
        exc._http_trace = trace
        raise exc

    def hydrate(self) -> None:
        if not self.account_id:
            self.account_id = jwt_account_id(self.access_token)
        if self.account_id:
            return
        me = self._http_get("/backend-api/me")
        if int(getattr(me, "status_code", 0) or 0) == 401:
            self._reject_unauthorized(me)
        if int(getattr(me, "status_code", 0) or 0) >= 400:
            logger.warning("account lookup unsuccessful status=%s", int(me.status_code))
            check = self._http_get(ACCOUNTS_CHECK_PATH, params={"timezone_offset_min": "0"})
            if int(getattr(check, "status_code", 0) or 0) == 401:
                self._reject_unauthorized(check)
            if int(getattr(check, "status_code", 0) or 0) >= 400:
                self._raise(ErrorCode.INVALID_SESSION, check, "读取账号")
            payload = _json(check)
        else:
            payload = _json(me)
        self.account_id = _account_id(payload)
        if not self.account_id:
            raise DomainError(ErrorCode.INVALID_SESSION, "无法从 session 解析账号 id")
        self.http.headers.update(self._identity_headers())

    def promo_eligibility(self, *, timezone_offset_min: int = 0) -> Dict[str, Any]:
        """读取 v4 账号检查并返回可安全展示/复用的活动摘要。"""
        if not self.account_id:
            self.hydrate()
        response = self._http_get(
            ACCOUNTS_CHECK_PATH,
            params={"timezone_offset_min": str(int(timezone_offset_min))},
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 401:
            _log_upstream_failure(ACCOUNTS_CHECK_PATH, response)
            raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效或已过期")
        if status >= 400:
            self._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "查询优惠资格")
        return parse_account_check(_json(response), account_id=self.account_id)

    def create_checkout(
        self,
        *,
        plan: str,
        country: str,
        billing_country: str = "",
        promo: bool = False,
        promo_code: str = "",
        promo_campaign_id: str = "",
        workspace_name: str = "",
        seat_quantity: int = 5,
        price_interval: str = "month",
        verified_billing: Optional[Dict[str, str]] = None,
    ) -> CheckoutSession:
        country = country.upper()
        tax_country = (billing_country or country).upper()
        currency = currency_for(country)
        hosted = str(plan) == "team"
        billing = None if hosted else checkout_address(
            country, tax_country, verified_billing,
            profile=verified_billing if hasattr(verified_billing, "verification_id") else None,
        )
        campaign_id = str(promo_campaign_id or "").strip()[:120]
        # 真实 HAR：建单请求的 Referer 是定价弹窗页（?cta_tab=personal[&promo_campaign=…]），
        # 与 cancel_url 同源同参，不再是裸首页。
        page_query = "cta_tab=personal"
        if promo or campaign_id:
            page_query += "&promo_campaign=" + quote(campaign_id or "plus-1-month-free", safe="")
        page_url = f"{APP_BASE}/?{page_query}"
        # Billing country is fixed and may differ from the exit; an upstream echo
        # of the selected exit region is not a payer-address conflict.
        self._billing_exit_country = country
        self.prepare_risk(flow="chatgpt_checkout", page_url=page_url, country=country)
        if not self.account_id:
            self.hydrate()
        if self._http is not None:
            self._http.headers.update(self._identity_headers())
        body = _checkout_body(
            plan=plan,
            country=country,
            currency=currency,
            billing_country=tax_country,
            promo=promo,
            promo_code=promo_code,
            promo_campaign_id=campaign_id,
            workspace_name=workspace_name,
            seat_quantity=seat_quantity,
            price_interval=price_interval,
            cancel_url=page_url,
        )
        logger.info(
            "checkout_payload plan=%s exit_country=%s billing_country=%s currency=%s keys=%s",
            plan, country, tax_country, currency, sorted(body),
        )
        checkout_url = f"{APP_BASE}/backend-api/payments/checkout"
        response = self.http.post(
            checkout_url,
            json=body,
            headers=self._route_headers("/backend-api/payments/checkout", page_url=page_url),
            timeout=self.timeout,
        )
        # 单次风控令牌只服务这一次建单：后续 GET /checkout/... 与 /checkout/taxes
        # 必须换成各自的令牌（HAR 里 taxes 完全不带 sentinel 头），
        # 复用同一份 so/t 会被判成重放。
        self.sentinel_token = ""
        self.sentinel_so = ""
        response_status = int(getattr(response, "status_code", 0) or 0)
        if response_status == 401:
            _log_checkout_unauthorized(response, self.http.headers)
        payload = _json(response)
        if response_status >= 400:
            self._reject_checkout(response, payload, request_body=body, url=checkout_url)
        if billing is not None:
            try:
                check_observed_billing(payload, billing,
                                       alternate_countries=(getattr(self, "_billing_exit_country", ""),))
            except DomainError as exc:
                exc.detail = self._checkout_response_detail(response, payload)
                exc._http_trace = build_redacted_http_trace(
                    title="Checkout 建单",
                    method="POST",
                    url=checkout_url,
                    http_status=response_status,
                    stage="creating_checkout",
                    request_json=body,
                    response_json=payload,
                )
                raise
        raw_url = str(payload.get("url") or payload.get("checkout_url") or "").strip()
        checkout_id = public_checkout_id(
            payload.get("checkout_session_id") or payload.get("session_id") or payload.get("id") or ""
        )
        stripe_id = stripe_checkout_id(payload, raw_url, checkout_id)
        internal_id = openai_internal_checkout_id(payload, raw_url, checkout_id)
        if hosted:
            url = hosted_checkout_url(raw_url, stripe_id or checkout_id)
            if not url:
                self._reject_checkout(response, payload, fallback="未拿到 Team 托管长链", request_body=body, url=checkout_url)
            session_id = stripe_id or checkout_id
        else:
            # 官方晚间 SPA 常把 cs_live 放在顶层 id；内部 confirm 仍要 oaics_（若响应里有）。
            session_id = internal_id or checkout_id or stripe_id
            if not session_id:
                self._reject_checkout(response, payload, fallback="结账响应缺少 session id", request_body=body, url=checkout_url)
            processor = str(payload.get("processor_entity") or _processor(country))
            path_id = stripe_id if (session_id.startswith("oaics_") and stripe_id) else session_id
            url = raw_url if raw_url.startswith("http") else f"{APP_BASE}/checkout/{processor}/{path_id}"
        processor = str(payload.get("processor_entity") or _processor(country))
        logger.info("checkout_created hosted=%s", hosted)
        confirm_return = str(payload.get("confirm_return_url") or "").strip()
        if not hosted and not confirm_return:
            confirm_return = stripe_verify_url(session_id, processor=processor, plan=plan)
        pk = str(payload.get("publishable_key") or payload.get("publishableKey") or "").strip()
        if not pk.startswith(("pk_live_", "pk_test_")):
            try:
                pk = self._publishable_key()
            except DomainError:
                # 托管长链拿不到公钥时降级为纯链接模式（用户手动去 Stripe 页支付）；
                # custom 会话没有公钥就无法支付，必须抛错。
                if not hosted:
                    raise
                pk = ""
        page = url if str(url).startswith("http") else page_url
        amount, quote_currency = quote_from_payload(payload, currency)
        tax_delta: Optional[Decimal] = None
        if not hosted:
            refreshed, refreshed_currency = self._quote_from_context(
                processor=processor, checkout_id=session_id, page_url=page, currency=quote_currency or currency, billing=billing
            )
            if refreshed is not None:
                amount, quote_currency = refreshed, refreshed_currency
            before_tax = amount
            taxed, taxed_currency = self._quote_from_taxes(
                checkout_id=session_id,
                page_url=page,
                currency=quote_currency or currency,
                billing=billing,
                processor=processor,
            )
            if taxed is not None:
                amount, quote_currency = taxed, taxed_currency
                tax_delta = (before_tax - taxed) if before_tax is not None else None
                logger.info(
                    "checkout_tax_recalc exit_country=%s billing_country=%s "
                    "before=%s after=%s currency=%s tax_delta=%s",
                    country,
                    tax_country,
                    before_tax,
                    taxed,
                    quote_currency,
                    tax_delta,
                )
        customer_session = str(
            payload.get("customer_session_client_secret")
            or payload.get("customerSessionClientSecret")
            or ""
        ).strip()
        return CheckoutSession(
            checkout_session_id=session_id,
            publishable_key=pk,
            account_id=self.account_id,
            checkout_url=url,
            currency=quote_currency or currency,
            amount=amount,
            processor_entity=processor,
            attestation=self.attestation,
            client_build_number=self.client_build_number,
            client_version=self.client_version,
            oai_session_id=self.oai_session_id,
            customer_session_client_secret=customer_session,
            checkout_kind="hosted" if hosted else "custom",
            confirm_return_url="" if hosted else confirm_return,
            tax_delta=tax_delta,
        )

    def _publishable_key(self) -> str:
        response = self._http_get(
            "/backend-api/payments/stripe_client_bootstrap",
            params={"account_id": self.account_id},
        )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            self._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "读取 Stripe 公钥")
        payload = _json(response)
        key = str(payload.get("publishable_key") or payload.get("publishableKey") or "")
        if not key.startswith(("pk_live_", "pk_test_")):
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "未拿到 Stripe 公钥")
        return key

    def _quote_from_context(
        self, *, processor: str, checkout_id: str, page_url: str, currency: str, billing: dict
    ) -> tuple[Optional[Decimal], str]:
        entities: list[str] = []
        for entity in (processor, "openai_llc", "openai_ie"):
            if entity and entity not in entities:
                entities.append(entity)
        routes = [f"/backend-api/payments/checkout/{entity}/{checkout_id}" for entity in entities]
        routes.append(f"/backend-api/payments/checkout/{checkout_id}")
        for route in routes:
            try:
                response = self._http_get(route, page_url=page_url)
            except Exception:
                continue
            if int(getattr(response, "status_code", 0) or 0) >= 400:
                _log_upstream_failure(route, response, level=logging.INFO)
                continue
            payload = _json(response)
            check_observed_billing(payload, billing,
                                   alternate_countries=(getattr(self, "_billing_exit_country", ""),))
            amount, found = quote_from_payload(payload, currency)
            if amount is not None:
                return amount, found or currency
        return None, currency

    def _quote_from_taxes(
        self,
        *,
        checkout_id: str,
        page_url: str,
        currency: str,
        billing: Optional[Dict[str, str]] = None,
        processor: str = "",
    ) -> tuple[Optional[Decimal], str]:
        try:
            response = self.http.post(
                f"{APP_BASE}/backend-api/payments/checkout/taxes",
                json=_taxes_body(
                    checkout_id=checkout_id,
                    billing=billing,
                    processor=processor,
                    currency=currency,
                ),
                headers=self._route_headers("/backend-api/payments/checkout/taxes", page_url=page_url),
                timeout=self.timeout,
            )
        except Exception:
            return None, currency
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            _log_upstream_failure("/backend-api/payments/checkout/taxes", response, level=logging.INFO)
            return None, currency
        payload = _json(response)
        if billing is not None:
            check_observed_billing(payload, billing,
                                   alternate_countries=(getattr(self, "_billing_exit_country", ""),))
        return quote_from_payload(payload, currency)

    def prepare_setup_intent(self) -> tuple[str, str]:
        if not self.account_id:
            self.hydrate()
        response = self.http.post(
            f"{APP_BASE}/backend-api/payments/payment_method",
            json={"account_id": self.account_id},
            headers=self._route_headers("/backend-api/payments/payment_method"),
            timeout=self.timeout,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 401:
            _log_upstream_failure("/backend-api/payments/payment_method", response)
            raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效或已过期")
        if status >= 400:
            self._raise(ErrorCode.PAYMENT_UNKNOWN_STATE, response, "准备支付方式")
        return setup_intent_from_payload(_json(response))

    def confirm_setup_intent(
        self, *, setup_id: str, client_secret: str, payment_method: str, publishable_key: str
    ) -> Dict[str, Any]:
        body = {
            "key": publishable_key,
            "client_secret": client_secret,
            "payment_method": payment_method,
        }
        response = self.stripe_http.post(
            f"{STRIPE_BASE}/v1/setup_intents/{setup_id}/confirm",
            data=urlencode(body),
            timeout=self.timeout,
        )
        payload = _json(response)
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            _log_upstream_failure(f"/v1/setup_intents/{setup_id}/confirm", response)
            message = (
                (payload.get("error") or {}).get("message")
                if isinstance(payload.get("error"), dict)
                else payload.get("message")
            )
            text = str(message or f"Stripe confirm HTTP {response.status_code}")
            if "billing country must match" in text.lower():
                raise DomainError(
                    ErrorCode.BILLING_COUNTRY_MISMATCH,
                    "账单国家必须与出口代理国家一致，请用所选地区的 SID 重试。",
                )
            raise DomainError(ErrorCode.PAYMENT_DECLINED, redact_sensitive(text))
        return payload

    def create_confirmation_token(
        self,
        *,
        publishable_key: str,
        payment_method: str = "",
        setup_future_usage: Optional[str] = "off_session",
        card: Optional[Dict[str, Any]] = None,
        billing: Optional[Dict[str, Any]] = None,
        radar_token: str = "",
        time_on_page_ms: int = 0,
        client_session_id: str = "",
        customer_id: str = "",
        currency: str = "",
    ) -> str:
        """创建 confirmation token。

        ``setup_future_usage`` 必须与确认时的 ``save_payment_method_for_reuse``
        配对，否则上游返回 ``Card save choice does not match confirmation token``：

            off_session + true     表示保存卡片
            None        + 不带该字段  表示不保存
        """
        body: Dict[str, Any] = {
            "key": publishable_key,
            "_stripe_version": STRIPE_API_VERSION,
        }
        if setup_future_usage:
            body["setup_future_usage"] = setup_future_usage

        if card and card.get("number"):
            # 对齐真实浏览器（HAR）：卡号直接交给 confirmation_tokens，
            # 而不是先造 pm_ 再传引用。
            b = dict(billing or {})
            import uuid as _uuid
            _csid = client_session_id or str(_uuid.uuid4())
            body["payment_method_data[type]"] = "card"
            body["payment_method_data[card][number]"] = str(card.get("number") or "")
            body["payment_method_data[card][cvc]"] = str(card.get("cvc") or "")
            body["payment_method_data[card][exp_month]"] = str(card.get("exp_month") or "")
            body["payment_method_data[card][exp_year]"] = str(card.get("exp_year") or "")
            body["payment_method_data[allow_redisplay]"] = "always"
            body["payment_method_data[pasted_fields]"] = "number,exp"
            body["payment_method_data[billing_details][name]"] = str(b.get("name") or "")
            body["payment_method_data[billing_details][email]"] = str(b.get("email") or "")
            body["payment_method_data[billing_details][address][country]"] = str(b.get("country") or "US").upper()
            body["payment_method_data[billing_details][address][line1]"] = str(b.get("line1") or "")
            body["payment_method_data[billing_details][address][city]"] = str(b.get("city") or "")
            body["payment_method_data[billing_details][address][postal_code]"] = str(
                b.get("postal_code") or b.get("zip") or "")
            body["payment_method_data[billing_details][address][state]"] = str(b.get("state") or "")
            body["payment_method_data[payment_user_agent]"] = (
                "stripe.js/7e9062bdeb; stripe-js-v3/7e9062bdeb; payment-element; deferred")
            body["payment_method_data[referrer]"] = APP_BASE
            body["payment_method_data[time_on_page]"] = str(int(time_on_page_ms or 45000))
            body["payment_method_data[client_attribution_metadata][client_session_id]"] = _csid
            body["payment_method_data[client_attribution_metadata][merchant_integration_source]"] = "elements"
            body["payment_method_data[client_attribution_metadata][merchant_integration_subtype]"] = "payment-element"
            body["payment_method_data[client_attribution_metadata][merchant_integration_version]"] = "2021"
            body["payment_method_data[client_attribution_metadata][payment_intent_creation_flow]"] = "deferred"
            body["payment_method_data[client_attribution_metadata][payment_method_selection_flow]"] = "merchant_specified"
            body["payment_method_data[guid]"] = uuid.uuid4().hex
            body["payment_method_data[muid]"] = uuid.uuid4().hex
            body["payment_method_data[sid]"] = uuid.uuid4().hex
            if radar_token:
                body["payment_method_data[radar_options][hcaptcha_token]"] = radar_token
            body["client_attribution_metadata[client_session_id]"] = _csid
            body["client_attribution_metadata[merchant_integration_source]"] = "elements"
            body["client_attribution_metadata[merchant_integration_subtype]"] = "payment-element"
            body["client_attribution_metadata[merchant_integration_version]"] = "2021"
            body["client_context[mode]"] = "subscription"
            body["client_context[payment_method_types][0]"] = "card"
            if currency:
                body["client_context[currency]"] = currency.lower()
            if customer_id:
                body["client_context[customer]"] = customer_id
        elif payment_method:
            body["payment_method"] = payment_method
        response = self.stripe_http.post(
            f"{STRIPE_BASE}/v1/confirmation_tokens",
            data=urlencode(body),
            timeout=self.timeout,
        )
        payload = _json(response)
        token = str(payload.get("id") or "")
        if int(getattr(response, "status_code", 0) or 0) >= 400 or not token:
            _log_upstream_failure("/v1/confirmation_tokens", response)
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            message = str(error.get("message") or payload.get("message") or "创建 confirmation token 失败")
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, redact_sensitive(message))
        return token

    def _sync_billing_tax(
        self,
        *,
        session_id: str,
        page_url: str,
        billing: Optional[Dict[str, str]],
        processor: str,
        currency: str,
        strict: bool,
    ) -> Optional[int]:
        self.mint_sentinel(flow="checkout_session_approval", page_url=page_url)
        taxed: Dict[str, Any] = {}
        try:
            response = self.http.post(
                f"{APP_BASE}/backend-api/payments/checkout/taxes",
                json=_taxes_body(
                    checkout_id=session_id,
                    billing=billing,
                    processor=processor,
                    currency=currency,
                ),
                headers=self._route_headers("/backend-api/payments/checkout/taxes", page_url=page_url),
                timeout=self.timeout,
            )
            if int(getattr(response, "status_code", 0) or 0) >= 400:
                _log_upstream_failure("/backend-api/payments/checkout/taxes", response)
            taxed = _json(response)
            session = taxed.get("checkout_session") if isinstance(taxed.get("checkout_session"), dict) else {}
            logger.info(
                "checkout_taxes status=%s amount_total=%s billing_country=%s",
                getattr(response, "status_code", 0),
                session.get("amount_total") or (taxed.get("snapshot") or {}).get("amount_total"),
                (billing or {}).get("country", ""),
            )
        except Exception as exc:
            if strict:
                raise
            logger.warning("checkout_taxes 失败（继续确认）: %s", redact_sensitive(str(exc))[:200])
        try:
            self.http.post(
                f"{APP_BASE}/backend-api/payments/checkout/update_tax_id",
                json={"checkout_session_id": session_id, "tax_id_info": None},
                headers=self._route_headers(
                    "/backend-api/payments/checkout/update_tax_id", page_url=page_url
                ),
                timeout=self.timeout,
            )
        except Exception:
            pass
        return expected_amount_minor(taxed)

    def confirm_checkout(
        self,
        *,
        checkout_session_id: str,
        confirm_token: str,
        checkout_url: str = "",
        billing: Optional[Dict[str, str]] = None,
        processor_entity: str = "",
        currency: str = "",
        publishable_key: str = "",
        save_choice: Optional[bool] = None,
        expected_amount_minor: Optional[int] = None,
    ) -> Dict[str, Any]:
        session_id = public_checkout_id(checkout_session_id)
        page_url = checkout_url or f"{APP_BASE}/checkout/{session_id}"
        processor = processor_entity or _processor_from_checkout_url(checkout_url)
        # 文档 2.3：taxes snapshot.amount_total 才是 Confirm 的 expected_amount。
        tax_minor = self._sync_billing_tax(
            session_id=session_id,
            page_url=page_url,
            billing=billing,
            processor=processor,
            currency=currency,
            strict=not session_id.startswith("cs_"),
        )
        if expected_amount_minor is not None:
            if tax_minor is None:
                raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "未读到最终税费报价，尚未提交确认")
            if tax_minor != expected_amount_minor:
                raise CheckoutAmountChanged(tax_minor, currency)
        if session_id.startswith("cs_"):
            return self.confirm_stripe_custom_checkout(
                checkout_session_id=session_id,
                publishable_key=publishable_key,
                confirmation_token=confirm_token,
                return_url=stripe_verify_url(session_id, processor=processor),
                expected_amount=tax_minor,
                billing=billing,
                currency=currency,
                save_choice=save_choice,
                strict_expected_amount=expected_amount_minor is not None,
            )
        confirm_body: Dict[str, Any] = {
            "checkout_session_id": session_id,
            "confirmation_token": confirm_token,
            "confirm_token": confirm_token,
            "selected_payment_method_type": "card",
        }
        # 会话若声明了 saved_payment_link_initial_save_for_reuse，
        # 保存选择必须与 ctoken 的 setup_future_usage 一致，且上游按字符串解析。
        if save_choice is not None:
            confirm_body["save_payment_method_for_reuse"] = "true" if save_choice else "false"

        # A validation message is not evidence that the submitted request had no effect.
        response = self.http.post(
            f"{APP_BASE}/backend-api/payments/checkout/confirm",
            json=confirm_body,
            headers=self._route_headers("/backend-api/payments/checkout/confirm", page_url=page_url),
            timeout=self.timeout,
        )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            self._raise(ErrorCode.PAYMENT_UNKNOWN_STATE, response, "确认结账")
        payload = _json(response)
        logger.info(
            "checkout_confirm status=%s type=%s has_secret=%s",
            payload.get("status") or payload.get("result") or "",
            payload.get("type") or "",
            bool(payload.get("client_secret")),
        )
        return payload

    def confirm_team_hosted_checkout(
        self, *, checkout_session_id: str, publishable_key: str,
        payment_method: str, checkout_url: str = "",
        billing: Optional[Dict[str, str]] = None, currency: str = "",
    ) -> Dict[str, Any]:
        """Compatibility entry; Team API prepares context before creating PaymentMethod."""
        from .hosted import TeamHostedClient
        hosted = TeamHostedClient(self)
        context = hosted.prepare(session_id=public_checkout_id(checkout_session_id), key=publishable_key,
                                 billing=dict(billing or {}), currency=currency,
                                 browser_locale=self.language, browser_timezone=self.timezone)
        return hosted.confirm(context, payment_method)

    def confirm_stripe_custom_checkout(
        self,
        *,
        checkout_session_id: str,
        publishable_key: str,
        confirmation_token: str,
        return_url: str = "",
        expected_amount: Optional[int] = None,
        billing: Optional[Dict[str, str]] = None,
        currency: str = "",
        save_choice: Optional[bool] = None,
        strict_expected_amount: bool = False,
    ) -> Dict[str, Any]:
        """cs_live：文档 4.2 Hosted/Custom Checkout（init → tax_region → elements → confirm）。

        ``save_choice``：会话要求显式保存选择时传 True/False；
        None 表示不带该字段（默认行为）。
        """
        session_id = public_checkout_id(checkout_session_id)
        pk = str(publishable_key or "").strip()
        token = str(confirmation_token or "").strip()
        if not session_id.startswith("cs_"):
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "Stripe Custom Checkout 需要 cs_ 会话")
        if not pk.startswith(("pk_live_", "pk_test_")):
            raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "缺少 Stripe 公钥")
        if not token.startswith("ctoken_"):
            raise DomainError(ErrorCode.PAYMENT_DECLINED, "缺少 confirmation token")
        locale = str((self.language or "en-US").split("-")[0] or "en")
        stripe_js_id = str(uuid.uuid4())
        started = time.monotonic()
        init_body: Dict[str, str] = {
            "browser_locale": self.language or "en-US",
            "browser_timezone": self.timezone or "America/New_York",
            "key": pk,
            "_stripe_version": STRIPE_CHECKOUT_API_VERSION,
            **_elements_session_client_fields(stripe_js_id=stripe_js_id, locale=locale),
        }
        init_payload = self._post_stripe_form(
            f"{STRIPE_BASE}/v1/payment_pages/{session_id}/init",
            init_body,
        )
        tax_payload = self._update_stripe_tax_region(
            session_id=session_id,
            publishable_key=pk,
            billing=billing,
            stripe_js_id=stripe_js_id,
            locale=locale,
        )
        if strict_expected_amount:
            actual_minor = expected_amount_minor(tax_payload)
            _, actual_currency = quote_from_payload(tax_payload, currency)
            if actual_minor is None or (actual_currency and actual_currency.upper() != currency.upper()):
                raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "最终 Stripe 报价未核实，尚未提交确认")
            if actual_minor != expected_amount:
                raise CheckoutAmountChanged(actual_minor, currency)
        minor = expected_amount
        if minor is None:
            minor = expected_amount_minor(tax_payload) or expected_amount_minor(init_payload)
        if minor is None:
            raise DomainError(
                ErrorCode.PAYMENT_UNKNOWN_STATE,
                "Stripe 确认缺少应付金额（expected_amount）。请重新创建结账后再确认。",
            )
        elements = self._fetch_elements_session(
            session_id=session_id,
            publishable_key=pk,
            amount=minor,
            currency=currency or str(init_payload.get("currency") or ""),
            stripe_js_id=stripe_js_id,
        )
        elements_session_id = str(
            elements.get("session_id") or init_payload.get("elements_session_id") or ""
        )
        elements_config_id = str(elements.get("config_id") or init_payload.get("config_id") or "")
        self._stripe_js_id = stripe_js_id
        self._elements_session_id = elements_session_id
        confirm_body: Dict[str, str] = {
            "key": pk,
            "_stripe_version": STRIPE_CHECKOUT_API_VERSION,
            "confirmation_token": token,
            "expected_payment_method_type": "card",
            "expected_amount": str(minor),
            "return_url": return_url or stripe_verify_url(session_id),
            "consent[terms_of_service]": "accepted",
            "guid": _stripe_device_id(),
            "muid": _stripe_device_id(),
            "sid": _stripe_device_id(),
            "time_on_page": str(max(8000, int((time.monotonic() - started) * 1000))),
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": session_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            **_elements_session_client_fields(
                stripe_js_id=stripe_js_id,
                elements_session_id=elements_session_id,
                locale=locale,
            ),
            **(
                {"save_payment_method_for_reuse": "true" if save_choice else "false"}
                if save_choice is not None
                else {}
            ),
        }
        if elements_config_id:
            confirm_body["client_attribution_metadata[elements_session_config_id]"] = elements_config_id
            confirm_body["client_attribution_metadata[checkout_config_id]"] = elements_config_id
        if elements_session_id:
            confirm_body["client_attribution_metadata[elements_session_id]"] = elements_session_id
        for key in ("init_checksum", "js_checksum", "rv_timestamp"):
            value = str(init_payload.get(key) or tax_payload.get(key) or "")
            if value:
                confirm_body[key] = value
        payload = self._post_stripe_form(
            f"{STRIPE_BASE}/v1/payment_pages/{session_id}/confirm",
            confirm_body,
        )
        logger.info(
            "stripe_payment_page_confirm status=%s submission=%s has_pi=%s expected_amount=%s",
            payload.get("status") or "",
            submission_state(payload),
            bool(_intent_object(payload)),
            minor,
        )
        return payload

    def finalize_stripe_custom_checkout(
        self,
        *,
        checkout_session_id: str,
        publishable_key: str,
        checkout_url: str = "",
        processor_entity: str = "",
        confirmed: Optional[Dict[str, Any]] = None,
        attempts: int = 12,
        interval: float = 0.8,
    ) -> Dict[str, Any]:
        """cs_live confirm 之后：先 fall-through 轮询，仍 requires_approval 再 Sentinel approve。"""
        page = dict(confirmed or {})
        if not payment_page_settled(page):
            page = self.poll_payment_page(
                checkout_session_id=checkout_session_id,
                publishable_key=publishable_key,
                attempts=min(3, attempts),
                interval=min(0.6, interval) if interval else 0,
            ) or page
        if self.credentials_current is not None and not self.credentials_current():
            return page
        if checkout_requires_approval(page):
            approved = self.approve_checkout(
                checkout_session_id=checkout_session_id,
                processor_entity=processor_entity,
                checkout_url=checkout_url,
            )
            logger.info(
                "checkout_approve result=%s status=%s",
                approved.get("result") or "",
                approved.get("status") or "",
            )
            if checkout_confirm_blocked(approved):
                page["status"] = str(approved.get("status") or "blocked")
                page["result"] = str(approved.get("result") or "blocked")
                extra = checkout_confirm_message(approved)
                if extra:
                    page["message"] = extra
                return page
        if payment_page_settled(page) and not checkout_requires_approval(page):
            return page
        polled = self.poll_payment_page(
            checkout_session_id=checkout_session_id,
            publishable_key=publishable_key,
            attempts=attempts,
            interval=interval,
        )
        return polled or page

    def poll_payment_page(
        self,
        *,
        checkout_session_id: str,
        publishable_key: str,
        attempts: int = 12,
        interval: float = 0.8,
    ) -> Dict[str, Any]:
        session_id = public_checkout_id(checkout_session_id)
        pk = str(publishable_key or "").strip()
        if not session_id.startswith("cs_") or not pk:
            return {}
        last: Dict[str, Any] = {}
        for _ in range(max(1, attempts)):
            if self.credentials_current is not None and not self.credentials_current():
                break
            payload = self._poll_payment_page_once(session_id=session_id, publishable_key=pk)
            if payload:
                last = payload
                logger.info(
                    "stripe_payment_page_poll status=%s object=%s submission=%s has_pi=%s",
                    payload.get("status") or "",
                    payload.get("payment_object_status") or "",
                    submission_state(payload),
                    bool(_intent_object(payload)),
                )
                if submission_state(payload) in {"failed", "declined", "canceled", "cancelled"}:
                    attempt = payload.get("submission_attempt")
                    updates = attempt.get("manual_approval_updates") if isinstance(attempt, dict) else None
                    logger.warning(
                        "stripe_submission_failed detail=%s attempt_keys=%s pi_status=%s approval_updates=%s",
                        submission_failure_detail(payload) or "-",
                        sorted(attempt) if isinstance(attempt, dict) else [],
                        nested_intent_status(payload) or "-",
                        redact_sensitive(json.dumps(updates, ensure_ascii=False, default=str))[:400] if updates else "-",
                    )
                if payment_page_settled(payload):
                    return payload
            if interval > 0:
                time.sleep(interval)
        return last

    def _poll_payment_page_once(self, *, session_id: str, publishable_key: str) -> Dict[str, Any]:
        locale = str((self.language or "en-US").split("-")[0] or "en")
        body = {
            "key": publishable_key,
            "_stripe_version": STRIPE_CHECKOUT_API_VERSION,
            **_elements_session_client_fields(
                stripe_js_id=self._stripe_js_id or str(uuid.uuid4()),
                elements_session_id=self._elements_session_id,
                locale=locale,
            ),
        }
        posted = self._post_stripe_form_soft(
            f"{STRIPE_BASE}/v1/payment_pages/{session_id}/poll",
            body,
        )
        if posted:
            return posted
        getter = getattr(self.stripe_http, "get", None)
        if not callable(getter):
            return {}
        params = dict(body)
        for url in (
            f"{STRIPE_BASE}/v1/payment_pages/{session_id}/poll",
            f"{STRIPE_BASE}/v1/payment_pages/{session_id}",
        ):
            try:
                response = getter(url, params=params, timeout=self.timeout)
            except Exception:
                continue
            if int(getattr(response, "status_code", 0) or 0) < 400:
                payload = _json(response)
                if payload:
                    return payload
        return {}

    def _update_stripe_tax_region(
        self,
        *,
        session_id: str,
        publishable_key: str,
        billing: Optional[Dict[str, str]],
        stripe_js_id: str,
        locale: str,
    ) -> Dict[str, Any]:
        if not billing:
            return {}
        body: Dict[str, str] = {
            "key": publishable_key,
            "_stripe_version": STRIPE_CHECKOUT_API_VERSION,
            **_elements_session_client_fields(stripe_js_id=stripe_js_id, locale=locale),
        }
        for field, key in (
            ("country", "country"),
            ("line1", "line1"),
            ("city", "city"),
            ("postal_code", "postal_code"),
            ("state", "state"),
        ):
            value = str((billing or {}).get(key) or "").strip()
            if value:
                body[f"tax_region[{field}]"] = value
        return self._post_stripe_form_soft(
            f"{STRIPE_BASE}/v1/payment_pages/{session_id}",
            body,
        )

    def _stripe_get(
        self,
        url: str,
        *,
        params: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        http = self.stripe_http
        getter = getattr(http, "get", None)
        if not callable(getter):
            return None
        with _without_session_headers(http, {"content-type"}):
            return getter(
                url,
                params=params,
                timeout=self.timeout if timeout is None else timeout,
            )

    def _fetch_elements_session(
        self,
        *,
        session_id: str,
        publishable_key: str,
        amount: int,
        currency: str,
        stripe_js_id: str,
    ) -> Dict[str, Any]:
        if not callable(getattr(self.stripe_http, "get", None)):
            return {}
        params = {
            "type": "deferred_intent",
            "deferred_intent[mode]": "subscription",
            "deferred_intent[amount]": str(amount),
            "deferred_intent[currency]": str(currency or "usd").lower(),
            "deferred_intent[setup_future_usage]": "off_session",
            "deferred_intent[payment_method_types][0]": "card",
            "deferred_intent[payment_method_types][1]": "link",
            "checkout_session_id": session_id,
            "stripe_js_id": stripe_js_id,
            "key": publishable_key,
            "_stripe_version": STRIPE_CHECKOUT_API_VERSION,
            "elements_init_source": "custom_checkout",
            "referrer_host": "chatgpt.com",
            "locale": str((self.language or "en-US").split("-")[0] or "en"),
        }
        try:
            response = self._stripe_get(
                f"{STRIPE_BASE}/v1/elements/sessions",
                params=params,
            )
        except Exception:
            return {}
        if response is None or int(getattr(response, "status_code", 0) or 0) >= 400:
            return {}
        return _json(response)

    def approve_checkout(
        self,
        *,
        checkout_session_id: str,
        processor_entity: str = "",
        checkout_url: str = "",
    ) -> Dict[str, Any]:
        session_id = public_checkout_id(checkout_session_id)
        processor = processor_entity or _processor_from_checkout_url(checkout_url) or _processor("")
        page_url = checkout_url or f"{APP_BASE}/checkout/{processor}/{session_id}"
        self.mint_sentinel(flow="checkout_session_approval", page_url=page_url)
        headers = self._route_headers("/backend-api/payments/checkout/approve", page_url=page_url)
        headers["x-oai-is-client-observation"] = "true"
        response = self.http.post(
            f"{APP_BASE}/backend-api/payments/checkout/approve",
            json={"checkout_session_id": session_id, "processor_entity": processor},
            headers=headers,
            timeout=self.timeout,
        )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            self._raise(ErrorCode.PAYMENT_UNKNOWN_STATE, response, "批准结账")
        return _json(response)

    def _post_stripe_form(self, url: str, body: Dict[str, str]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        status = 0
        data = dict(body)
        for _ in range(8):
            response = self.stripe_http.post(
                url,
                data=urlencode(data),
                timeout=self.timeout,
            )
            payload = _json(response)
            status = int(getattr(response, "status_code", 0) or 0)
            if status != 400:
                break
            unknown = _stripe_unknown_param(payload)
            if not unknown:
                break
            removed = False
            for key in list(data):
                if key == unknown or key.startswith(unknown + "["):
                    data.pop(key, None)
                    removed = True
            if not removed:
                break
        if status >= 400:
            _log_upstream_failure(url, response)
            _raise_stripe(payload, status)
        return payload

    def _post_stripe_form_soft(self, url: str, body: Dict[str, str]) -> Dict[str, Any]:
        try:
            return self._post_stripe_form(url, body)
        except DomainError:
            return {}

    def retrieve_stripe_intent(
        self,
        *,
        intent_id: str,
        client_secret: str = "",
        publishable_key: str,
    ) -> Dict[str, Any]:
        if not callable(getattr(self.stripe_http, "get", None)) or not intent_id:
            return {}
        kind = "payment_intents" if intent_id.startswith("pi_") else "setup_intents"
        params = {
            "key": publishable_key,
            "_stripe_version": STRIPE_API_VERSION,
        }
        if client_secret:
            params["client_secret"] = client_secret
        try:
            response = self._stripe_get(
                f"{STRIPE_BASE}/v1/{kind}/{intent_id}",
                params=params,
            )
        except Exception:
            return {}
        if response is None or int(getattr(response, "status_code", 0) or 0) >= 400:
            if response is not None:
                _log_upstream_failure(f"/v1/{kind}/{intent_id}", response)
            return {}
        return _json(response)

    def confirm_stripe_intent(
        self,
        *,
        intent_id: str,
        client_secret: str,
        publishable_key: str,
        payment_method: str = "",
        confirmation_token: str = "",
        return_url: str = "",
        request_three_d_secure: str = "",
    ) -> Dict[str, Any]:
        intent_id = intent_id or intent_id_from_secret(client_secret)
        kind = "payment_intents" if intent_id.startswith("pi_") else "setup_intents"
        body: Dict[str, str] = {
            "key": publishable_key,
            "client_secret": client_secret,
            "_stripe_version": STRIPE_API_VERSION,
            "client_attribution_metadata[merchant_integration_source]": "l1",
        }
        if confirmation_token:
            body["confirmation_token"] = confirmation_token
        elif payment_method:
            body["payment_method"] = payment_method
        if return_url:
            body["return_url"] = return_url
        if request_three_d_secure:
            # automatic（默认）多数卡不会弹挑战；any 表示「只要卡支持就做 3DS」
            body["payment_method_options[card][request_three_d_secure]"] = request_three_d_secure
        response = self.stripe_http.post(
            f"{STRIPE_BASE}/v1/{kind}/{intent_id}/confirm",
            data=urlencode(body),
            timeout=self.timeout,
        )
        payload = _json(response)
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            _log_upstream_failure(f"/v1/{kind}/{intent_id}/confirm", response)
            _raise_stripe(payload, status)
        return payload

    def cancel_auto_renew(self, *, plan_type: str = "") -> Dict[str, Any]:
        """Cancel next renewal using the live order session.

        The real ChatGPT cancel endpoint is deployment-specific and must be
        verified from a browser capture. No endpoint is guessed here: if the
        env var is absent, callers must mark manual_required.
        """
        path = os.environ.get("GSS_CANCEL_AUTORENEW_PATH", "").strip()
        if not path:
            raise DomainError(
                ErrorCode.SUBSCRIPTION_NOT_ACTIVE,
                "自动关闭续费接口未配置，需人工处理",
            )
        method = (os.environ.get("GSS_CANCEL_AUTORENEW_METHOD", "POST").strip().upper()
                  or "POST")
        if method not in {"POST", "PATCH", "PUT"}:
            raise DomainError(ErrorCode.COMPLIANCE_BLOCKED, "自动关闭续费请求方法不支持")
        subscription = self.subscription_payload()
        subscription_id = _subscription_id(subscription)
        if not self.account_id:
            self.hydrate()
        template = os.environ.get("GSS_CANCEL_AUTORENEW_BODY", "").strip()
        if "{subscription_id}" in path or "{subscription_id}" in template:
            if not subscription_id:
                raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "无法从订阅响应提取 subscription_id")
        if "{account_id}" in path or "{account_id}" in template:
            if not self.account_id:
                raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "无法提取 account_id")
        body: Any = None
        if template:
            try:
                rendered = template
                for key, value in {
                    "plan_type": _PLAN_TYPE.get(str(plan_type), str(plan_type)),
                    "subscription_id": subscription_id,
                    "account_id": self.account_id,
                }.items():
                    rendered = rendered.replace("{" + key + "}", str(value))
                body = json.loads(rendered)
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                raise DomainError(
                    ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                    "取消续费请求模板无效，已停止",
                ) from exc
        rendered_path = path
        for key, value in {
            "plan_type": _PLAN_TYPE.get(str(plan_type), str(plan_type)),
            "subscription_id": subscription_id,
            "account_id": self.account_id,
        }.items():
            rendered_path = rendered_path.replace("{" + key + "}", str(value))
        url = f"{APP_BASE}{rendered_path}"
        headers = self._route_headers(rendered_path)
        logger.info("cancel_auto_renew_start path=%s method=%s", path, method)
        if method == "POST":
            response = self.http.post(url, json=body, headers=headers,
                                      timeout=self.timeout, allow_redirects=False)
        elif method == "PATCH":
            response = self.http.patch(url, json=body, headers=headers,
                                       timeout=self.timeout, allow_redirects=False)
        else:
            response = self.http.put(url, json=body, headers=headers,
                                     timeout=self.timeout, allow_redirects=False)
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 401:
            _log_upstream_failure(path, response)
            raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效，无法关闭自动续费")
        if 300 <= status < 400:
            headers = getattr(response, "headers", {}) or {}
            location = str(headers.get("location") or headers.get("Location") or "")
            if "billing.stripe.com" in location or status in {301, 302, 303, 307, 308}:
                raise DomainError(
                    ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                    "上游要求跳转 Stripe 账单门户，需人工处理",
                )
        if status >= 400:
            _log_upstream_failure(path, response)
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "关闭自动续费请求失败")
        verified = self.subscription_payload()
        will_renew = _will_renew(verified)
        if will_renew is True:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "上游未确认自动续费已关闭")
        logger.info("cancel_auto_renew_ok path=%s will_renew=%s", path, will_renew)
        return {"canceled": True, "will_renew": will_renew, "subscription_id": subscription_id}

    def subscription_payload(self) -> Dict[str, Any]:
        self._check_credentials()
        if not self.account_id:
            self.hydrate()
        self._check_credentials()  # /me can finish after the Vault lease expires.
        response = self._http_get(
            "/backend-api/subscriptions",
            # 上游现已要求显式 account_id，否则 400 "must specify either organization_id or account_id"。
            params={"account_id": self.account_id} if self.account_id else None,
        )
        if int(getattr(response, "status_code", 0) or 0) == 401:
            _log_upstream_failure("/backend-api/subscriptions", response)
            raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效或已过期")
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            self._raise(ErrorCode.SUBSCRIPTION_NOT_ACTIVE, response, "查询订阅")
        return _json(response)



from gss_core.chatgpt_parsers import (
    _CHECKOUT_AUTH_ERROR_CODES,
    _CONFIRM_BLOCKED,
    _DECLINE_ADVICE,
    _FAILURE_KEYS,
    _LOGGABLE_ENDPOINTS,
    _SETI_ID,
    _SETI_SECRET,
    _account_id,
    _as_minor_int,
    _expected_amount_from_object,
    _failure_parts,
    _intent_object,
    _json,
    _log_checkout_unauthorized,
    _log_upstream_failure,
    _nested_first,
    _processor,
    _processor_from_checkout_url,
    _raise_stripe,
    _session_token_from_set_cookie,
    _stripe_unknown_param,
    _subscription_id,
    _taxes_body,
    _walk,
    _will_renew,
    checkout_confirm_blocked,
    checkout_confirm_blocked_advice,
    checkout_confirm_message,
    checkout_requires_approval,
    decline_message,
    expected_amount_minor,
    intent_id_from_secret,
    nested_intent_status,
    payment_intent_from_confirm,
    payment_page_settled,
    setup_intent_from_payload,
    submission_failure_detail,
    submission_state,
)


__all__ = [
    "APP_BASE",
    "ChatGPTClient",
    "CheckoutSession",
    "hosted_checkout_url",
    "stripe_checkout_id",
    "openai_internal_checkout_id",
    "public_checkout_id",
    "clamp_team_seats",
    "checkout_confirm_blocked",
    "checkout_confirm_blocked_advice",
    "checkout_confirm_message",
    "checkout_requires_approval",
    "payment_page_settled",
    "submission_state",
    "nested_intent_status",
    "expected_amount_minor",
    "intent_id_from_secret",
    "jwt_account_id",
    "payment_intent_from_confirm",
    "setup_intent_from_payload",
    "stripe_verify_url",
]
