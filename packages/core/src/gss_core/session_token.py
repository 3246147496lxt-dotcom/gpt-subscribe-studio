"""把用户粘贴的 session 解析成 access token、session cookie、设备 id。只在内存中使用。"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

_COUNTRY = re.compile(r"^[A-Z]{2}$")
SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"
# Real Chrome enforces RFC 6265's 4096-byte cookie limit; NextAuth splits
# oversized JWE session tokens at 3933 bytes into `.0`, `.1`, ... chunks.
SESSION_COOKIE_CHUNK_SIZE = 3933
_SESSION_CHUNK_RE = re.compile(r"^__Secure-next-auth\.session-token\.(\d+)$")


def is_session_cookie_name(name: str) -> bool:
    clean = str(name or "").strip()
    return clean == SESSION_COOKIE_NAME or bool(_SESSION_CHUNK_RE.match(clean))


def reassemble_session_token(cookies: Dict[str, str]) -> str:
    """Extract sessionToken from single or chunked (.0/.1/...) NextAuth cookies."""
    if not isinstance(cookies, dict):
        return ""
    direct = str(cookies.get(SESSION_COOKIE_NAME) or "").strip()
    if direct:
        return direct
    chunks: list[tuple[int, str]] = []
    for key, val in cookies.items():
        m = _SESSION_CHUNK_RE.match(str(key or "").strip())
        if m and val is not None:
            chunks.append((int(m.group(1)), str(val).strip()))
    if not chunks:
        return ""
    chunks.sort(key=lambda pair: pair[0])
    return "".join(part for _, part in chunks).strip()


def chunk_session_cookie(token: str, *, chunk_size: int = SESSION_COOKIE_CHUNK_SIZE) -> Dict[str, str]:
    """Format a NextAuth session token into browser-faithful cookie entry/entries."""
    clean = str(token or "").replace("\r", "").replace("\n", "").strip()
    if not clean:
        return {}
    if len(clean) <= chunk_size:
        return {SESSION_COOKIE_NAME: clean}
    return {
        f"{SESSION_COOKIE_NAME}.{idx // chunk_size}": clean[idx : idx + chunk_size]
        for idx in range(0, len(clean), chunk_size)
    }


@dataclass
class SessionBundle:
    access_token: str
    session_token: str = ""
    device_id: str = ""
    oai_session_id: str = ""
    cookies: Dict[str, str] = field(default_factory=dict)
    # statsigContext 是 Session 签发时浏览器上报的真实身份，优先级高于任何伪造档案。
    user_agent: str = ""
    country: str = ""
    # 从 UA 或导出 JSON 平台字段识别出的客户端系统（macos / windows / linux / ""）。
    os_hint: str = ""
    # OAuth refresh token (Codex/CLI exports). It is a credential, so it
    # survives strip_ambient even though every ambient field is dropped.
    refresh_token: str = ""


def parse_session(raw: str) -> str:
    return parse_session_bundle(raw).access_token


def parse_session_bundle(raw: str, *, strip_ambient: bool = False) -> SessionBundle:
    """Parse a pasted session. ``strip_ambient`` keeps only the credentials.

    A session exported in another country carries that browser's device id,
    statsigContext and cookies. Replaying them from a different exit is the
    cross-region mismatch the upstream rejects, so callers that will run on a
    new exit pass ``strip_ambient=True`` and let the new exit mint its own
    device identity.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("session 为空")
    if text.lower().startswith("bearer "):
        text = text.split(None, 1)[1].strip()
    
    # 处理 __Secure-next-auth.session-token=xxx 格式
    if text.startswith("__Secure-next-auth.session-token="):
        text = text.split("=", 1)[1].strip()
    
    if text.startswith("{") or text.startswith("["):
        try:
            data: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("session JSON 无法解析") from exc
        return _from_mapping(data, strip_ambient=strip_ambient)
    if ";" in text and "=" in text and not text.lstrip().startswith("eyJ"):
        cookies = _parse_cookie_header(text)
        if "__Secure-next-auth.session-token" in cookies or "oai-did" in cookies:
            raise ValueError("请粘贴包含 accessToken 的整段 session JSON，仅 Cookie 不够")
    
    # 识别 JWE（5段）vs JWS（3段）
    # JWE (session token): eyJ...dir...  (5段，用于 next-auth)
    # JWS (access token): eyJ...alg... (3段，用于 API 授权)
    if text.startswith("eyJ") and text.count(".") >= 2:
        parts = text.split(".")
        if len(parts) == 5:
            # 这是 JWE，当作 session_token 而非 access_token
            return SessionBundle(access_token="", session_token=text)
    
    return SessionBundle(access_token=text)


def _from_mapping(data: Any, *, strip_ambient: bool = False) -> SessionBundle:
    if isinstance(data, list) and data:
        return _from_mapping(data[0], strip_ambient=strip_ambient)
    if not isinstance(data, dict):
        return SessionBundle(access_token="")
    nested = data.get("session")
    if isinstance(nested, dict):
        inner = _from_mapping(nested, strip_ambient=strip_ambient)
    else:
        inner = SessionBundle(access_token="")
    token = _first_string(data, ("accessToken", "access_token", "token", "at")) or inner.access_token
    if not token and isinstance(data.get("user"), dict):
        token = _first_string(data["user"], ("accessToken", "access_token", "token"))
    cookies = dict(inner.cookies)
    cookies.update(_cookies_of(data))
    session_token = (
        _first_string(data, ("sessionToken", "session_token", SESSION_COOKIE_NAME))
        or reassemble_session_token(cookies)
        or inner.session_token
    )
    refresh_token = (
        _first_string(data, ("refresh_token", "refreshToken", "oauth_refresh_token"))
        or inner.refresh_token
    )
    # statsigContext 优先：它是 Session 签发时真实浏览器的 deviceId / UA / 国家，
    # 顶层字段和 cookie 可能是旧导出里残留的值。
    statsig = parse_statsig_context(data)
    if strip_ambient:
        # Only credentials cross the import boundary. The exit this order binds
        # to mints the device id, UA, session id and cookie jar itself.
        device_id = ""
        user_agent = ""
        country = ""
        os_hint = ""
        session_id = ""
        cookies = {}
    else:
        device_id = (
            statsig["device_id"]
            or _first_string(data, ("oai-did", "oai_did", "deviceId", "device_id"))
            or cookies.get("oai-did")
            or inner.device_id
        )
        user_agent = (
            statsig["user_agent"]
            or _clean_user_agent(_first_string(data, ("userAgent", "user_agent", "ua")))
            or inner.user_agent
        )
        country = statsig["country"] or inner.country
        os_hint = _detect_os_hint(data, user_agent) or inner.os_hint
        session_id = (
            _first_string(data, ("sessionId", "session_id", "oai-session-id", "oai_session_id"))
            or inner.oai_session_id
        )
        if not session_id:
            statsig_mapping = _statsig_mapping(data)
            session_id = _first_string(statsig_mapping, ("sessionId", "session_id"))
    if not token and isinstance(nested, str) and len(nested) > 20:
        token = nested.strip()
    if not token and not session_token and not refresh_token:
        raise ValueError("JSON 中没有 accessToken")
    if session_token:
        for ck_name, ck_val in chunk_session_cookie(session_token).items():
            cookies.setdefault(ck_name, ck_val)
    if device_id:
        # 强制覆盖：请求头 oai-device-id 与 Cookie oai-did 必须同为 statsig 的 deviceId。
        cookies["oai-did"] = device_id
    return SessionBundle(
        access_token=token,
        session_token=session_token,
        device_id=device_id,
        oai_session_id=session_id,
        cookies=cookies,
        user_agent=user_agent,
        country=country,
        os_hint=os_hint,
        refresh_token=refresh_token,
    )


def _first_string(data: Dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _statsig_mapping(data: Any) -> Dict[str, Any]:
    """取出 statsigContext；兼容 JSON 字符串和嵌套 session 两种导出格式。"""
    if not isinstance(data, dict):
        return {}
    for key in ("statsigContext", "statsig_context", "statsig"):
        raw = data.get(key)
        if isinstance(raw, dict) and raw:
            return raw
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed:
                return parsed
    nested = data.get("session")
    if isinstance(nested, dict):
        return _statsig_mapping(nested)
    return {}


def _clean_user_agent(value: Any) -> str:
    """只接受浏览器真实 UA，避免把空串/多行/伪造串写进请求头。"""
    text = str(value or "").strip()
    if not text or len(text) > 512 or "\r" in text or "\n" in text:
        return ""
    if not text.lower().startswith("mozilla/"):
        return ""
    return text


def _first_country(data: Dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            code = value.strip().upper()
            if _COUNTRY.match(code):
                return code
    return ""


def _detect_os_hint(data: Dict[str, Any], user_agent: str = "") -> str:
    """从 UA 或导出 JSON 里的 platform/os 字段识别导出端操作系统。"""
    ua_low = str(user_agent or "").lower()
    if "macintosh" in ua_low or "mac os x" in ua_low:
        return "macos"
    if "windows" in ua_low:
        return "windows"
    if "linux" in ua_low or "x11" in ua_low:
        return "linux"
    statsig = _statsig_mapping(data)
    candidates = (
        _first_string(data, ("sec_ch_ua_platform", "sec-ch-ua-platform", "platform", "os", "osName", "os_name")),
        _first_string(statsig, ("sec_ch_ua_platform", "sec-ch-ua-platform", "platform", "os", "osName", "os_name")),
    )
    for raw in candidates:
        low = raw.strip().strip('"').lower()
        if not low:
            continue
        if any(tok in low for tok in ("mac", "darwin", "osx")):
            return "macos"
        if any(tok in low for tok in ("win",)):
            return "windows"
        if "linux" in low:
            return "linux"
    return ""


def parse_statsig_context(data: Any) -> Dict[str, str]:
    """提取 Session 签发时的设备身份：deviceId、User-Agent、国家码。

    返回的键固定为 device_id / user_agent / country，缺省为空串。
    """
    statsig = _statsig_mapping(data)
    return {
        "device_id": _first_string(statsig, ("deviceId", "device_id", "deviceID", "did")),
        "user_agent": _clean_user_agent(
            _first_string(statsig, ("userAgent", "user_agent", "ua"))
        ),
        "country": _first_country(statsig, ("country", "countryCode", "country_code")),
    }


def _cookies_of(data: Dict[str, Any]) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    raw_cookies = data.get("cookies")
    if isinstance(raw_cookies, list):
        for item in raw_cookies:
            if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                cookies[str(item["name"])] = str(item["value"])
    elif isinstance(raw_cookies, dict):
        cookies.update({str(k): str(v) for k, v in raw_cookies.items() if v is not None})
    elif isinstance(raw_cookies, str):
        cookies.update(_parse_cookie_header(raw_cookies))
    for key in ("cookie", "cookie_header", "Cookie"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            cookies.update(_parse_cookie_header(value))
    return cookies


def _parse_cookie_header(text: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for part in str(text or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


__all__ = [
    "SessionBundle",
    "SESSION_COOKIE_NAME",
    "SESSION_COOKIE_CHUNK_SIZE",
    "is_session_cookie_name",
    "reassemble_session_token",
    "chunk_session_cookie",
    "parse_session",
    "parse_session_bundle",
    "parse_statsig_context",
    "identity_cookies_for_reissue",
    "jwt_claims",
    "access_token_exp_remaining",
    "is_access_token_expired",
]


def jwt_claims(token: str) -> Dict[str, Any]:
    """解析 3 段 JWS JWT 的 payload；非标准 JWT 或 5 段 JWE 返回空字典。"""
    parts = str(token or "").strip().split(".")
    if len(parts) != 3 or not parts[1]:
        return {}
    payload = parts[1].replace("-", "+").replace("_", "/")
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.b64decode(payload).decode("utf-8", "replace"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def access_token_exp_remaining(
    access_token: str, *, now: Optional[float] = None
) -> Optional[float]:
    """返回 JWT accessToken 距离过期的剩余秒数；非 JWT 或无 exp 时返回 None。"""
    claims = jwt_claims(access_token)
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    current = time.time() if now is None else float(now)
    return float(exp) - current


def is_access_token_expired(
    access_token: str, *, leeway_seconds: int = 0, now: Optional[float] = None
) -> bool:
    """判断 JWT accessToken 是否已过期（或在 leeway_seconds 内即将过期）。"""
    remaining = access_token_exp_remaining(access_token, now=now)
    if remaining is None:
        return False
    return remaining <= float(leeway_seconds)

def email_from_access_token(access_token: str) -> str:
    """从 ChatGPT accessToken（JWT）里取账户邮箱。

    载荷里邮箱出现的位置不止一处，按以下顺序尝试：
      · "email"
      · "https://api.openai.com/profile" → "email"   ← 实测位置
      · "https://api.openai.com/auth" → "email"
      · "user" → "email"
    解析失败一律返回空串，由调用方决定兜底。
    """
    import base64
    import json

    token = (access_token or "").strip()
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1].replace("-", "+").replace("_", "/")
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.b64decode(payload).decode("utf-8", "replace"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""

    def _ok(value: Any) -> str:
        text = str(value or "").strip()
        return text if "@" in text and "." in text.split("@")[-1] else ""

    direct = _ok(data.get("email"))
    if direct:
        return direct
    # 实测 ChatGPT accessToken 的邮箱在这里
    profile = data.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        found = _ok(profile.get("email"))
        if found:
            return found
    auth = data.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        found = _ok(auth.get("email"))
        if found:
            return found
    user = data.get("user")
    if isinstance(user, dict):
        found = _ok(user.get("email"))
        if found:
            return found
    return ""


# 跨区重签发只允许身份凭据进入新出口。旧环境 Cookie 带有签发地的
# Cloudflare / 设备指纹，必须丢弃，不能在目标国家复用。
REISSUE_COOKIE_ALLOWLIST = frozenset({"__Secure-next-auth.session-token"})


def identity_cookies_for_reissue(cookies: Dict[str, str]) -> Dict[str, str]:
    """Return only cookies that establish identity, never old geo/device state."""
    return {
        str(name): str(value)
        for name, value in (cookies or {}).items()
        if is_session_cookie_name(name) and value is not None
    }

