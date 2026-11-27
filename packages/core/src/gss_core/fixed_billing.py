"""单一固定账单地址：美国免税州。

出口代理可以按订单自由变化，账单地址永远只取这一份，禁止按国家/币种随机生成。
优先读取管理员配置的 ``GSS_FIXED_BILLING_FILE``（私有 JSON，0600/0400、绝对路径、
非符号链接）；没有配置时使用仓库内置的固定样例地址。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict

BILLING_COUNTRY = "US"

#: 没有州级销售税的五个州（AK / DE / MT / NH / OR）。
TAX_FREE_STATES = frozenset({"AK", "DE", "MT", "NH", "OR"})

TAX_FREE_STATE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("OR", "Oregon (OR · 俄勒冈州)"),
    ("DE", "Delaware (DE · 特拉华州)"),
    ("MT", "Montana (MT · 蒙大拿州)"),
    ("NH", "New Hampshire (NH · 新罕布什尔州)"),
    ("AK", "Alaska (AK · 阿拉斯加州)"),
)

FIELDS = ("name", "email", "line1", "city", "state", "postal_code", "country")
_LIMITS = {"name": 100, "email": 254, "line1": 200, "city": 100, "state": 100,
           "postal_code": 20, "country": 2}
_LIMIT = 8192

#: Built-in fixture. It is deterministic on purpose: the same address for every
#: order, so a checkout can never drift to another street/ZIP between attempts.
DEFAULT_ADDRESS: Dict[str, str] = {
    "name": "Daniel Mercer",
    "email": "billing@example.com",
    "line1": "1425 SW Morrison Street",
    "city": "Portland",
    "state": "OR",
    "postal_code": "97205",
    "country": BILLING_COUNTRY,
}


def is_tax_free_state(state: str) -> bool:
    return str(state or "").strip().upper() in TAX_FREE_STATES


def _clean(raw, limit: int, *, allow_empty: bool = False, field_label: str = "") -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{field_label or '字段'}必须为字符串")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError(f"{field_label or '字段'}不能包含控制字符")
    value = raw.strip()
    if not value and not allow_empty:
        raise ValueError(f"{field_label or '字段'}不能为空")
    if len(value) > limit:
        raise ValueError(f"{field_label or '字段'}长度不能超过 {limit} 个字符")
    return value


_FIELD_LABELS = {
    "name": "付款方姓名",
    "email": "账单邮箱",
    "line1": "街道地址",
    "city": "城市",
    "state": "免税州",
    "postal_code": "邮编",
    "country": "账单国家",
}


def validate_address(raw) -> Dict[str, str]:
    """只接受完整的七个字段；美国账单必须落在免税州。"""
    if not isinstance(raw, dict) or set(raw) != set(FIELDS):
        raise ValueError("固定账单必须完整包含 name / email / line1 / city / state / postal_code / country 七个字段")
    address = {
        key: _clean(raw[key], _LIMITS[key], allow_empty=key == "state", field_label=_FIELD_LABELS[key])
        for key in FIELDS
    }
    address["country"] = address["country"].upper()
    address["state"] = address["state"].upper()
    if address["country"] != BILLING_COUNTRY:
        raise ValueError("固定免税账单国家必须为 US")
    if not is_tax_free_state(address["state"]):
        raise ValueError(f"美国免税州仅支持 {', '.join(sorted(TAX_FREE_STATES))}")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address["email"]):
        raise ValueError("账单邮箱格式不正确")
    return address


def default_managed_fixed_billing_path() -> Path:
    """管理后台托管的固定美国免税账单路径（位于 GSS_CONFIG_DIR 下）。"""
    config_dir = Path(os.environ.get("GSS_CONFIG_DIR", "config")).expanduser().resolve()
    return config_dir / "fixed-billing.json"


def configured_fixed_billing_path() -> Path | None:
    """返回当前生效的固定账单文件路径；环境变量优先，其次为后台托管文件。"""
    explicit = os.environ.get("GSS_FIXED_BILLING_FILE", "").strip()
    if explicit:
        return Path(explicit)
    managed = default_managed_fixed_billing_path()
    if managed.is_symlink() or managed.exists():
        return managed
    return None


def writable_fixed_billing_path() -> Path:
    """后台保存/清除操作写入的目标路径。"""
    explicit = os.environ.get("GSS_FIXED_BILLING_FILE", "").strip()
    if explicit:
        target = Path(explicit)
        if not target.is_absolute():
            raise ValueError("GSS_FIXED_BILLING_FILE 必须为绝对路径")
        return target
    return default_managed_fixed_billing_path()


def has_fixed_billing_source() -> bool:
    return configured_fixed_billing_path() is not None


def _read_fixed_file(target: Path) -> Dict[str, str]:
    if not target.is_absolute():
        raise ValueError("固定账单文件路径必须为绝对路径")
    fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) not in {0o600, 0o400}
                or info.st_uid not in {0, os.geteuid()}
                or info.st_size > _LIMIT):
            raise ValueError("固定账单文件权限须为 0600/0400 且为当前用户普通文件")
        with os.fdopen(fd, "rb", closefd=False) as source:
            blob = source.read(_LIMIT + 1)
    finally:
        os.close(fd)
    if len(blob) > _LIMIT:
        raise ValueError("固定账单文件体积超过限制")
    record = json.loads(blob.decode("utf-8"))
    if isinstance(record, dict) and "billing" in record:
        record = record["billing"]
    return validate_address(record)


def load_fixed_billing_file(path: Path | str | None = None) -> Dict[str, str] | None:
    """Read the operator's fixed address. ``None`` when no file is configured.

    Invalid content raises ``ValueError``: a broken billing config must stop new
    checkouts instead of silently falling back to a different address.
    """
    if path is not None:
        text = str(path).strip()
        if not text:
            return None
        return _read_fixed_file(Path(text))
    target = configured_fixed_billing_path()
    if target is None:
        return None
    return _read_fixed_file(target)


def save_fixed_billing_file(raw: Dict[str, Any], path: Path | str | None = None) -> tuple[Dict[str, str], Path]:
    """Validate and atomically persist the fixed US tax-free billing address (mode 0600)."""
    payload = dict(raw or {})
    if "country" not in payload or not str(payload.get("country") or "").strip():
        payload["country"] = BILLING_COUNTRY
    address = validate_address(payload)
    target = Path(path) if path is not None else writable_fixed_billing_path()
    if not target.is_absolute():
        raise ValueError("固定账单文件路径必须为绝对路径")
    if target.is_symlink():
        raise ValueError("固定账单文件路径不能是符号链接")
    if target.exists() and target.is_dir():
        raise ValueError("固定账单文件路径不能是目录")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(address, indent=2, ensure_ascii=False) + "\n"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    os.chmod(target, 0o600)
    return address, target


def delete_fixed_billing_file(path: Path | str | None = None) -> bool:
    """Remove the saved fixed US tax-free billing file if present."""
    target = Path(path) if path is not None else writable_fixed_billing_path()
    if not target.is_absolute():
        raise ValueError("固定账单文件路径必须为绝对路径")
    if target.is_symlink():
        raise ValueError("固定账单文件路径不能是符号链接")
    if target.exists():
        if target.is_dir():
            raise ValueError("固定账单文件路径不能是目录")
        target.unlink()
        return True
    return False


def fixed_billing_snapshot() -> Dict[str, Any]:
    """Return structured status + current address for the admin settings UI."""
    from . import runtime_flags

    explicit = os.environ.get("GSS_FIXED_BILLING_FILE", "").strip()
    managed = default_managed_fixed_billing_path()
    strict_match = runtime_flags.get_bool("require_exit_billing_match")
    states = [{"value": code, "label": label} for code, label in TAX_FREE_STATE_OPTIONS]

    base: Dict[str, Any] = {
        "configured": False,
        "valid": False,
        "source": "none",
        "storage_label": "GSS_FIXED_BILLING_FILE" if explicit else "config/fixed-billing.json",
        "can_delete": False,
        "error": None,
        "address": None,
        "verification_id": None,
        "updated_at": None,
        "strict_exit_match": strict_match,
        "tax_free_states": states,
        "default_sample": dict(DEFAULT_ADDRESS),
    }

    target: Path | None = None
    if explicit:
        base["source"] = "env_fixed"
        target = Path(explicit)
    elif managed.is_symlink() or managed.exists():
        base["source"] = "managed"
        target = managed

    if target is not None:
        base["can_delete"] = target.exists() and not target.is_symlink() and not target.is_dir()
        try:
            st = target.stat()
            base["updated_at"] = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            pass
        try:
            addr = _read_fixed_file(target)
            base["configured"] = True
            base["valid"] = True
            base["address"] = addr
            base["verification_id"] = fixed_verification_id(addr)[:16]
            return base
        except (OSError, ValueError) as exc:
            base["configured"] = True
            base["valid"] = False
            base["error"] = str(exc) or "固定账单文件不可读或权限不符合 0600/0400"
            return base

    # Fallback check: legacy GSS_BILLING_PROFILES_FILE / GSS_VERIFIED_BILLING_FILE
    if (os.environ.get("GSS_BILLING_PROFILES_FILE", "").strip()
            or os.environ.get("GSS_VERIFIED_BILLING_FILE", "").strip()):
        from .billing_profiles import load_billing_profiles
        from .verified_payer import MissingProfile

        base["source"] = "legacy_env"
        try:
            profiles = load_billing_profiles()
            sample = profiles.resolve_for_country(BILLING_COUNTRY, "USD")
            base["configured"] = True
            base["valid"] = True
            base["address"] = dict(sample)
            base["verification_id"] = sample.verification_id[:16]
            return base
        except (MissingProfile, ValueError):
            base["configured"] = True
            base["valid"] = False
            base["error"] = "旧版核验账单文件不可用或校验未通过，建议在此直接保存美国固定免税账单。"
            return base

    return base


def fixed_billing_address() -> Dict[str, str]:
    """The one billing address used by every non-Team order and every fallback."""
    try:
        configured = load_fixed_billing_file()
    except (OSError, ValueError, AttributeError, RecursionError):
        configured = None
    return dict(configured or DEFAULT_ADDRESS)


def fixed_verification_id(address: Dict[str, str]) -> str:
    """Stable id for the fixed address. Same value for create and later payment."""
    material = json.dumps(address, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(material).hexdigest()


def billing_country() -> str:
    return BILLING_COUNTRY


__all__ = [
    "BILLING_COUNTRY",
    "DEFAULT_ADDRESS",
    "TAX_FREE_STATES",
    "TAX_FREE_STATE_OPTIONS",
    "billing_country",
    "configured_fixed_billing_path",
    "default_managed_fixed_billing_path",
    "delete_fixed_billing_file",
    "fixed_billing_address",
    "fixed_billing_snapshot",
    "fixed_verification_id",
    "has_fixed_billing_source",
    "is_tax_free_state",
    "load_fixed_billing_file",
    "save_fixed_billing_file",
    "validate_address",
    "writable_fixed_billing_path",
]
