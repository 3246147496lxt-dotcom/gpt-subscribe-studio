"""Customer portal user authentication & 7-day scoped guest task credentials.

Passwords use PBKDF2-HMAC-SHA256 with per-user random salts. Session tokens
are HMAC-SHA256 signed cookies. Guest task ownership uses random opaque tokens
whose SHA-256 hash is stored in SQLite with a 7-day TTL.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Request, Response

USER_COOKIE = "gss_portal_user"
GUEST_COOKIE = "gss_portal_guest"
GUEST_HEADER = "x-portal-guest-token"
USER_SESSION_SECONDS = 30 * 24 * 60 * 60
GUEST_TTL_DAYS = 7
GUEST_TTL_SECONDS = GUEST_TTL_DAYS * 24 * 60 * 60
_PBKDF2_ITERATIONS = 260_000
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{2,31}$")

_cached_secret: Optional[bytes] = None


def _signing_secret() -> bytes:
    global _cached_secret
    env = os.environ.get("GSS_PORTAL_AUTH_SECRET", "").strip()
    if env:
        return hashlib.sha256(env.encode("utf-8")).digest()
    if _cached_secret is not None:
        return _cached_secret
    data_dir = Path(os.environ.get("GSS_DATA_DIR", "").strip() or (Path.cwd() / "data"))
    secret_file = data_dir / ".portal_auth_secret"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        if secret_file.is_file():
            raw = secret_file.read_text(encoding="utf-8").strip()
            if len(raw) >= 32:
                _cached_secret = hashlib.sha256(raw.encode("utf-8")).digest()
                return _cached_secret
        generated = secrets.token_hex(32)
        fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(generated)
        except Exception:
            os.close(fd)
            raise
        _cached_secret = hashlib.sha256(generated.encode("utf-8")).digest()
        return _cached_secret
    except OSError:
        _cached_secret = secrets.token_bytes(32)
        return _cached_secret


def normalize_username(raw: str) -> str:
    return str(raw or "").strip().lower()


def validate_username(raw: str) -> str:
    value = normalize_username(raw)
    if not _USERNAME_RE.fullmatch(value):
        raise ValueError("用户名需为 3–32 位字母、数字、下划线或点号。")
    return value


def validate_password(raw: str) -> str:
    text = str(raw or "")
    if len(text) < 6 or len(text) > 128:
        raise ValueError("密码长度需为 6–128 位。")
    return text


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters_str, salt_hex, hash_hex = str(stored or "").split("$")
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iters_str)
        if iterations < 10_000 or iterations > 2_000_000:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def issue_user_cookie_value(user_id: str) -> str:
    expiry = int(time.time()) + USER_SESSION_SECONDS
    nonce = secrets.token_urlsafe(12)
    payload = f"{user_id}.{expiry}.{nonce}"
    sig = hmac.new(_signing_secret(), payload.encode("utf-8"), "sha256").hexdigest()
    return f"{payload}.{sig}"


def verify_user_cookie_value(token: str) -> Optional[str]:
    if not token or len(token) > 256:
        return None
    try:
        user_id, expiry_s, nonce, sig = token.split(".")
        if int(expiry_s) <= int(time.time()):
            return None
        payload = f"{user_id}.{expiry_s}.{nonce}"
        expected = hmac.new(_signing_secret(), payload.encode("utf-8"), "sha256").hexdigest()
        if not hmac.compare_digest(sig.encode("ascii"), expected.encode("ascii")):
            return None
        return user_id
    except (ValueError, UnicodeError):
        return None


def mint_guest_token() -> str:
    return "gst_" + secrets.token_urlsafe(28)


def hash_guest_token(token: str) -> str:
    text = str(token or "").strip()
    if len(text) < 16 or len(text) > 128:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def guest_expiry_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=GUEST_TTL_DAYS)).isoformat()


def _is_https(request: Request) -> bool:
    proto = str(request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.url.scheme == "https"


def set_user_cookie(response: Response, request: Request, user_id: str) -> None:
    response.set_cookie(
        USER_COOKIE,
        issue_user_cookie_value(user_id),
        max_age=USER_SESSION_SECONDS,
        httponly=True,
        secure=_is_https(request),
        samesite="lax",
        path="/",
    )


def clear_user_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        USER_COOKIE,
        httponly=True,
        secure=_is_https(request),
        samesite="lax",
        path="/",
    )


def set_guest_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        GUEST_COOKIE,
        token,
        max_age=GUEST_TTL_SECONDS,
        httponly=True,
        secure=_is_https(request),
        samesite="lax",
        path="/",
    )


def extract_guest_token(request: Request) -> str:
    header = str(request.headers.get(GUEST_HEADER) or "").strip()
    if len(header) >= 16:
        return header
    cookie = str(request.cookies.get(GUEST_COOKIE) or "").strip()
    if len(cookie) >= 16:
        return cookie
    return ""


def extract_user_id(request: Request) -> Optional[str]:
    return verify_user_cookie_value(str(request.cookies.get(USER_COOKIE) or ""))


__all__ = [
    "GUEST_COOKIE",
    "GUEST_HEADER",
    "GUEST_TTL_DAYS",
    "USER_COOKIE",
    "clear_user_cookie",
    "extract_guest_token",
    "extract_user_id",
    "guest_expiry_iso",
    "hash_guest_token",
    "hash_password",
    "issue_user_cookie_value",
    "mint_guest_token",
    "normalize_username",
    "set_guest_cookie",
    "set_user_cookie",
    "validate_password",
    "validate_username",
    "verify_password",
    "verify_user_cookie_value",
]
