"""Encrypted 30-day after-sales vault for per-order Session & virtual card credentials.

Security properties:
- Application-layer AES-256-GCM AEAD encryption bound to ``(order_id, field_name)`` via AAD.
- Master key is stored outside SQLite (``GSS_VAULT_MASTER_KEY`` env var or mode-0600
  ``$GSS_CONFIG_DIR/vault-master.key``), never inside the database or logs.
- Plaintext session / card data never enters ``orders``, CSV exports, regular list/detail
  endpoints, webhooks, or error logs; decryption only happens via explicit admin reveal
  with immutable audit logging in ``order_events``.
- Automatic TTL expiration (default 30 days via ``GSS_VAULT_RETENTION_DAYS``) wipes
  ciphertexts while preserving non-sensitive card analytics metadata (BIN, last4, brand,
  irreversible HMAC fingerprint).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .persistence import get_db_optional
from .public_payment import detect_card_brand

logger = logging.getLogger("gss.order_vault")

DEFAULT_RETENTION_DAYS = 30
_BLOB_PREFIX = b"v1:"
_NONCE_LEN = 12
_KEY_FILENAME = "vault-master.key"
_SALT = b"gss-order-vault-hkdf-salt-v1"
_ENC_INFO = b"gss-order-vault-aes256gcm-v1"
_FP_INFO = b"gss-order-vault-card-fp-v1"

_KEY_CACHE: Dict[tuple[str, str], tuple[bytes, bytes]] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def vault_retention_days() -> int:
    raw = os.environ.get("GSS_VAULT_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)).strip()
    try:
        return max(1, min(365, int(raw)))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def _key_file_path() -> Path:
    explicit = os.environ.get("GSS_VAULT_KEY_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    config_dir = Path(os.environ.get("GSS_CONFIG_DIR", "config")).expanduser()
    return config_dir / _KEY_FILENAME


def _decode_raw_key(raw: str) -> bytes:
    text = raw.strip()
    if not text:
        raise ValueError("empty vault master key")
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return bytes.fromhex(text)
    padded = text + "=" * (-len(text) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(decoded) >= 32:
            return decoded
    except Exception:  # noqa: BLE001
        pass
    return text.encode("utf-8")


def _load_or_create_key_file(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        content = path.read_text(encoding="utf-8").strip()
        if content:
            return _decode_raw_key(content)
    generated = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(generated).decode("ascii")
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, (encoded + "\n").encode("ascii"))
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return generated
    except FileExistsError:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            raise RuntimeError("vault master key file is empty")
        return _decode_raw_key(content)


def _derive_subkeys(master_ikm: bytes) -> tuple[bytes, bytes]:
    enc_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        info=_ENC_INFO,
    ).derive(master_ikm)
    fp_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        info=_FP_INFO,
    ).derive(master_ikm)
    return enc_key, fp_key


def _active_keys() -> tuple[bytes, bytes]:
    env_key = os.environ.get("GSS_VAULT_MASTER_KEY", "").strip()
    key_path = _key_file_path()
    cache_key = (env_key, str(key_path.resolve()) if not env_key else "")
    cached = _KEY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    ikm = _decode_raw_key(env_key) if env_key else _load_or_create_key_file(key_path)
    subkeys = _derive_subkeys(ikm)
    if len(_KEY_CACHE) > 32:
        _KEY_CACHE.clear()
    _KEY_CACHE[cache_key] = subkeys
    return subkeys


def encrypt_field(order_id: str, field_name: str, plaintext: str) -> bytes:
    enc_key, _ = _active_keys()
    nonce = secrets.token_bytes(_NONCE_LEN)
    aad = f"{order_id}:{field_name}:v1".encode("utf-8")
    ct = AESGCM(enc_key).encrypt(nonce, plaintext.encode("utf-8"), aad)
    return _BLOB_PREFIX + nonce + ct


def decrypt_field(order_id: str, field_name: str, blob: bytes) -> str:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError("invalid vault ciphertext type")
    raw = bytes(blob)
    if not raw.startswith(_BLOB_PREFIX) or len(raw) <= len(_BLOB_PREFIX) + _NONCE_LEN + 16:
        raise ValueError("corrupted vault ciphertext envelope")
    nonce_start = len(_BLOB_PREFIX)
    nonce_end = nonce_start + _NONCE_LEN
    nonce = raw[nonce_start:nonce_end]
    ct = raw[nonce_end:]
    enc_key, _ = _active_keys()
    aad = f"{order_id}:{field_name}:v1".encode("utf-8")
    plain = AESGCM(enc_key).decrypt(nonce, ct, aad)
    return plain.decode("utf-8")


def _model_to_dict(obj: Any) -> Optional[Dict[str, Any]]:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "model_dump"):
        return dict(obj.model_dump())
    return None


def _normalize_card_dict(card: Any, billing: Any = None) -> Optional[Dict[str, Any]]:
    raw = _model_to_dict(card)
    if not raw:
        return None
    number_digits = re.sub(r"\D+", "", str(raw.get("number") or ""))
    if not number_digits:
        return None
    exp_month_raw = str(raw.get("exp_month") or "").strip()
    exp_year_raw = str(raw.get("exp_year") or "").strip()
    cvc_raw = str(raw.get("cvc") or "").strip()
    out: Dict[str, Any] = {
        "number": number_digits,
        "exp_month": exp_month_raw.zfill(2) if exp_month_raw.isdigit() else exp_month_raw,
        "exp_year": (f"20{exp_year_raw}" if len(exp_year_raw) == 2 and exp_year_raw.isdigit() else exp_year_raw),
        "cvc": cvc_raw,
    }
    billing_dict = _model_to_dict(billing)
    if billing_dict:
        clean_billing = {str(k): str(v) for k, v in billing_dict.items() if v is not None and str(v).strip() != ""}
        if clean_billing:
            out["billing"] = clean_billing
    return out


def extract_card_metadata(card: Any) -> Dict[str, Optional[str]]:
    norm = _normalize_card_dict(card)
    if not norm:
        return {
            "card_brand": None,
            "card_bin": None,
            "card_last4": None,
            "card_exp": None,
            "card_fingerprint": None,
        }
    digits = norm["number"]
    brand = detect_card_brand(digits)
    card_bin = digits[:6] if len(digits) >= 6 else None
    card_last4 = digits[-4:] if len(digits) >= 4 else None
    mm = norm.get("exp_month") or ""
    yy = norm.get("exp_year") or ""
    card_exp = f"{mm}/{yy}" if mm and yy else None
    card_fp: Optional[str] = None
    if len(digits) >= 8:
        _, fp_key = _active_keys()
        card_fp = hmac.new(fp_key, digits.encode("ascii"), hashlib.sha256).hexdigest()[:24]
    return {
        "card_brand": brand,
        "card_bin": card_bin,
        "card_last4": card_last4,
        "card_exp": card_exp,
        "card_fingerprint": card_fp,
    }


def remaining_days_from_iso(expires_at: Optional[str], *, now: Optional[datetime] = None) -> Optional[int]:
    if not expires_at:
        return None
    try:
        dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ref = now or _utcnow()
    delta_s = (dt - ref).total_seconds()
    if delta_s <= 0:
        return 0
    return max(1, int((delta_s + 86399) // 86400))


def is_expired_iso(expires_at: Optional[str], *, now: Optional[datetime] = None) -> bool:
    if not expires_at:
        return False
    try:
        dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= (now or _utcnow())


def capture_order_credentials(
    order_id: str,
    *,
    session: Optional[str] = None,
    card: Any = None,
    billing: Any = None,
    db: Any = None,
    retention_days: Optional[int] = None,
) -> bool:
    """Encrypt and persist complete session and/or virtual card data for after-sales."""
    if not order_id:
        return False
    target_db = db or get_db_optional()
    if target_db is None:
        return False
    session_text = str(session or "").strip()
    card_dict = _normalize_card_dict(card, billing=billing)
    if not session_text and not card_dict:
        return False
    try:
        session_ct = encrypt_field(order_id, "session", session_text) if session_text else None
        card_ct = (
            encrypt_field(order_id, "card", json.dumps(card_dict, ensure_ascii=False, separators=(",", ":")))
            if card_dict
            else None
        )
        meta = extract_card_metadata(card_dict) if card_dict else {}
        days = retention_days if retention_days is not None else vault_retention_days()
        expires_at = (_utcnow() + timedelta(days=max(1, int(days)))).isoformat()
        target_db.upsert_order_vault(
            order_id=order_id,
            session_ciphertext=session_ct,
            card_ciphertext=card_ct,
            card_brand=meta.get("card_brand"),
            card_bin=meta.get("card_bin"),
            card_last4=meta.get("card_last4"),
            card_exp=meta.get("card_exp"),
            card_fingerprint=meta.get("card_fingerprint"),
            expires_at=expires_at,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("order_vault_capture_failed order=%s type=%s", order_id, type(exc).__name__)
        return False


def format_vault_meta(order_id: str, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if row is None:
        return {
            "order_id": order_id,
            "available": False,
            "has_session": False,
            "has_card": False,
            "card_brand": None,
            "card_bin": None,
            "card_last4": None,
            "card_exp": None,
            "card_fingerprint": None,
            "created_at": None,
            "updated_at": None,
            "expires_at": None,
            "remaining_days": None,
            "expired": False,
            "purged_at": None,
            "purge_reason": None,
            "retention_days": vault_retention_days(),
        }
    purged_at = row.get("purged_at")
    expires_at = row.get("expires_at")
    expired = bool(is_expired_iso(expires_at)) or row.get("purge_reason") == "ttl_expired"
    has_session = bool(row.get("has_session")) and not purged_at and not expired
    has_card = bool(row.get("has_card")) and not purged_at and not expired
    return {
        "order_id": order_id,
        "available": has_session or has_card,
        "has_session": has_session,
        "has_card": has_card,
        "card_brand": row.get("card_brand"),
        "card_bin": row.get("card_bin"),
        "card_last4": row.get("card_last4"),
        "card_exp": row.get("card_exp"),
        "card_fingerprint": row.get("card_fingerprint"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "expires_at": expires_at,
        "remaining_days": 0 if (purged_at or expired) else remaining_days_from_iso(expires_at),
        "expired": expired,
        "purged_at": purged_at,
        "purge_reason": row.get("purge_reason"),
        "retention_days": vault_retention_days(),
    }


def reveal_order_credentials(order_id: str, *, db: Any = None) -> Dict[str, Any]:
    """Decrypt stored session & card credentials after enforcing TTL expiration."""
    target_db = db or get_db_optional()
    if target_db is None:
        raise RuntimeError("vault storage unavailable")
    row = target_db.get_order_vault_row(order_id)
    if row is None:
        return {"meta": format_vault_meta(order_id, None), "session": None, "card": None}
    if not row.get("purged_at") and is_expired_iso(row.get("expires_at")):
        target_db.purge_order_vault(order_id, reason="ttl_expired")
        row = target_db.get_order_vault_row(order_id)
    meta = format_vault_meta(order_id, row)
    if not meta["available"]:
        return {"meta": meta, "session": None, "card": None}
    session_plain: Optional[str] = None
    card_plain: Optional[Dict[str, Any]] = None
    if row and row.get("session_ciphertext"):
        session_plain = decrypt_field(order_id, "session", row["session_ciphertext"])
    if row and row.get("card_ciphertext"):
        raw_card = decrypt_field(order_id, "card", row["card_ciphertext"])
        parsed = json.loads(raw_card)
        if isinstance(parsed, dict):
            card_plain = parsed
    return {
        "meta": meta,
        "session": session_plain,
        "card": card_plain,
    }


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "capture_order_credentials",
    "decrypt_field",
    "encrypt_field",
    "extract_card_metadata",
    "format_vault_meta",
    "is_expired_iso",
    "remaining_days_from_iso",
    "reveal_order_credentials",
    "vault_retention_days",
]
