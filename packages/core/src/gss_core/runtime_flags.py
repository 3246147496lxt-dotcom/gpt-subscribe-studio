"""Persistent, non-secret runtime & protocol switch manager.

Precedence (highest to lowest):
  1. Explicit operator overrides persisted in ``$GSS_CONFIG_DIR/runtime-flags.json`` (mode 0600).
  2. Environment variable (``GSS_*``).
  3. Built-in safe default.

Security guarantees:
- Strict allowlist of non-secret operational flags (booleans, fixed enums, bounded ints).
- Never stores tokens, cookies, proxy credentials, billing addresses, or card data.
- Atomic writes via temporary file + ``os.replace`` with ``0600`` permissions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable, Dict, Iterable, Literal, Optional, Tuple

from .config import use_fake_adapters

FlagKind = Literal["bool", "enum", "int"]
FlagCategory = Literal["session", "protocol", "runtime"]

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class FlagSpec:
    key: str
    category: FlagCategory
    label: str
    description: str
    kind: FlagKind
    env_var: str
    default: Any = None
    default_fn: Optional[Callable[[], Any]] = None
    options: Tuple[Tuple[str, str], ...] = ()
    min_value: Optional[int] = None
    max_value: Optional[int] = None

    def resolved_default(self) -> Any:
        if self.default_fn is not None:
            return self.default_fn()
        return self.default


def _default_reissue_on_exit() -> bool:
    return not use_fake_adapters()


FLAG_SPECS: Tuple[FlagSpec, ...] = (
    # ── Session 与出口重颁策略 ──────────────────────────────────────
    FlagSpec(
        key="reissue_session_on_exit",
        category="session",
        label="建单前始终用 ST 在当前出口重颁新 AT",
        description=(
            "真实上游模式下，只要 Session 包含 sessionToken（ST）或 OAuth refresh_token，"
            "建单前一律先丢弃旧 AT 与旧浏览器 Cookie，在本次充值代理出口重新签发新 AT，"
            "降低因 IP/UA/环境漂移触发 401 token_revoked 的概率。"
        ),
        kind="bool",
        env_var="GSS_REISSUE_SESSION_ON_EXIT",
        default_fn=_default_reissue_on_exit,
    ),
    FlagSpec(
        key="reissue_keep_ua",
        category="session",
        label="重颁 AT 时沿用原 Session 的 User-Agent",
        description=(
            "关闭时使用与 curl_cffi TLS 指纹一致的干净出口 Chrome UA；"
            "开启后在请求 /api/auth/session 时优先沿用导出 Session 中自带的原 userAgent。"
        ),
        kind="bool",
        env_var="GSS_REISSUE_KEEP_UA",
        default=False,
    ),
    FlagSpec(
        key="warm_account",
        category="session",
        label="跨区重颁后预热账号接口 (/backend-api/me)",
        description="跨区重颁新 AT 后额外等待 1–1.5 秒并探测一次 /backend-api/me；默认关闭以保持主链路精简。",
        kind="bool",
        env_var="GSS_WARM_ACCOUNT",
        default=False,
    ),
    FlagSpec(
        key="proxy_preflight",
        category="session",
        label="刷新 Session 前校验代理出口与国家一致性",
        description="发送 sessionToken 前先通过无凭证探针核验代理 IP 与归属国，异常时直接中止且不外发凭证。",
        kind="bool",
        env_var="GSS_PROXY_PREFLIGHT",
        default=False,
    ),
    # ── 协议引擎与风控遥测 ──────────────────────────────────────────
    FlagSpec(
        key="plus_engine",
        category="protocol",
        label="Plus 支付协议引擎",
        description="选择 Plus / Go / Pro 家族在 oaics_ 链路下的协议实现：默认 HAR 逐请求复刻，可按需切回 legacy。",
        kind="enum",
        env_var="GSS_PLUS_ENGINE",
        default="har",
        options=(
            ("har", "HAR 逐请求复刻（推荐）"),
            ("legacy", "Legacy 旧版直付契约"),
        ),
    ),
    FlagSpec(
        key="har_telemetry",
        category="protocol",
        label="HAR 浏览器遥测与指纹回放",
        description="控制是否完整回放 m.stripe.com/6、r.stripe.com/b、CES 及 /sentinel/ping；关闭可缩短耗时但可能提高风控概率。",
        kind="enum",
        env_var="GSS_HAR_TELEMETRY",
        default="full",
        options=(
            ("full", "全量回放（推荐）"),
            ("off", "关闭遥测（极速）"),
        ),
    ),
    FlagSpec(
        key="checkout_rotate",
        category="protocol",
        label="建单代理隧道握手失败时自动轮换 SID 重试",
        description="仅在连接建立前出现代理隧道/TLS 握手失败且未收到任何上游响应时轮换一次 SID 重试，绝不重放已提交请求。",
        kind="bool",
        env_var="GSS_CHECKOUT_ROTATE",
        default=False,
    ),
    FlagSpec(
        key="require_exit_billing_match",
        category="protocol",
        label="严格模式：强制出口地区与固定账单国家一致",
        description="默认关闭（固定美国免税账单 + 自由切换任意出口地区）；开启后所选出口必须与固定账单国家一致。",
        kind="bool",
        env_var="GSS_REQUIRE_EXIT_BILLING_MATCH",
        default=False,
    ),
    # ── 队列、预检与自动对账 ────────────────────────────────────────
    FlagSpec(
        key="quick_pay_concurrency",
        category="runtime",
        label="一键充值最大并发任务数",
        description="全局同时执行的 quick-pay 槽位数（1–16）；同一账号始终串行，不受此数值影响。",
        kind="int",
        env_var="GSS_QUICK_PAY_CONCURRENCY",
        default=3,
        min_value=1,
        max_value=16,
    ),
    FlagSpec(
        key="preflight_strict",
        category="runtime",
        label="一键充值入队严格拦截（缺账单直接拒绝）",
        description="开启后若缺少固定核验账单文件，任务在前置体检阶段即直接拦截失败，不再进入建单阶段。",
        kind="bool",
        env_var="GSS_PREFLIGHT_STRICT",
        default=False,
    ),
    FlagSpec(
        key="reconciler_enabled",
        category="runtime",
        label="启用后台只读自动对账补偿器",
        description="后台定期对 paid_pending_delivery 与 unknown 订单执行纯只读状态核验与自动核销，绝不发起扣款。",
        kind="bool",
        env_var="GSS_RECONCILER_ENABLED",
        default=True,
    ),
)

FLAG_BY_KEY: Dict[str, FlagSpec] = {spec.key: spec for spec in FLAG_SPECS}

_LOCK = threading.RLock()
_CACHE_KEY: Optional[Tuple[str, int, int]] = None
_CACHE_DATA: Tuple[Dict[str, Any], Optional[str]] = ({}, None)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def flags_file_path() -> Path:
    explicit = os.environ.get("GSS_RUNTIME_FLAGS_FILE", "").strip()
    if explicit:
        return Path(explicit)
    config_dir = Path(os.environ.get("GSS_CONFIG_DIR", "config"))
    return config_dir / "runtime-flags.json"


def _coerce_value(spec: FlagSpec, raw: Any) -> Any:
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str):
            low = raw.strip().lower()
            if low in _TRUE_VALUES:
                return True
            if low in _FALSE_VALUES:
                return False
        raise ValueError(f"开关 {spec.key} 需要布尔值")
    if spec.kind == "enum":
        if not isinstance(raw, str):
            raise ValueError(f"开关 {spec.key} 需要字符串选项")
        val = raw.strip().lower()
        allowed = {opt_val for opt_val, _ in spec.options}
        if val not in allowed:
            raise ValueError(f"开关 {spec.key} 仅支持：{', '.join(sorted(allowed))}")
        return val
    if spec.kind == "int":
        if isinstance(raw, bool):
            raise ValueError(f"开关 {spec.key} 需要整数")
        try:
            num = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"开关 {spec.key} 需要整数") from exc
        if spec.min_value is not None and num < spec.min_value:
            raise ValueError(f"开关 {spec.key} 不能小于 {spec.min_value}")
        if spec.max_value is not None and num > spec.max_value:
            raise ValueError(f"开关 {spec.key} 不能大于 {spec.max_value}")
        return num
    raise ValueError(f"不支持的开关类型: {spec.kind}")


def _parse_env(spec: FlagSpec) -> Tuple[bool, Any]:
    raw = os.environ.get(spec.env_var)
    if raw is None or not str(raw).strip():
        return False, None
    try:
        return True, _coerce_value(spec, raw)
    except ValueError:
        return False, None


def _read_overrides_locked() -> Tuple[Dict[str, Any], Optional[str]]:
    global _CACHE_KEY, _CACHE_DATA
    path = flags_file_path()
    try:
        st = path.stat()
    except OSError:
        _CACHE_KEY = None
        _CACHE_DATA = ({}, None)
        return {}, None
    cache_key = (str(path), st.st_mtime_ns, st.st_size)
    if _CACHE_KEY == cache_key:
        return dict(_CACHE_DATA[0]), _CACHE_DATA[1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _CACHE_KEY = cache_key
        _CACHE_DATA = ({}, None)
        return {}, None
    raw_overrides = payload.get("overrides") if isinstance(payload, dict) else None
    clean: Dict[str, Any] = {}
    if isinstance(raw_overrides, dict):
        for key, value in raw_overrides.items():
            spec = FLAG_BY_KEY.get(str(key))
            if spec is None or value is None:
                continue
            try:
                clean[spec.key] = _coerce_value(spec, value)
            except ValueError:
                continue
    updated_at = str(payload.get("updated_at") or "") or None if isinstance(payload, dict) else None
    _CACHE_KEY = cache_key
    _CACHE_DATA = (clean, updated_at)
    return dict(clean), updated_at


def _write_overrides_locked(overrides: Dict[str, Any]) -> Optional[str]:
    global _CACHE_KEY, _CACHE_DATA
    path = flags_file_path()
    updated_at = _utcnow_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {"version": 1, "updated_at": updated_at, "overrides": overrides},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    try:
        st = path.stat()
        _CACHE_KEY = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        _CACHE_KEY = None
    _CACHE_DATA = (dict(overrides), updated_at)
    return updated_at


def resolve_flag(key: str) -> Tuple[Any, Literal["override", "env", "default"]]:
    spec = FLAG_BY_KEY[key]
    with _LOCK:
        overrides, _ = _read_overrides_locked()
    if key in overrides:
        return overrides[key], "override"
    has_env, env_val = _parse_env(spec)
    if has_env:
        return env_val, "env"
    return spec.resolved_default(), "default"


def get_flag(key: str) -> Any:
    value, _ = resolve_flag(key)
    return value


def get_bool(key: str) -> bool:
    return bool(get_flag(key))


def get_int(key: str) -> int:
    return int(get_flag(key))


def get_str(key: str) -> str:
    return str(get_flag(key))


def snapshot() -> Dict[str, Any]:
    with _LOCK:
        overrides, updated_at = _read_overrides_locked()
    items = []
    for spec in FLAG_SPECS:
        default_val = spec.resolved_default()
        has_env, env_val = _parse_env(spec)
        if spec.key in overrides:
            value = overrides[spec.key]
            source = "override"
        elif has_env:
            value = env_val
            source = "env"
        else:
            value = default_val
            source = "default"
        items.append({
            "key": spec.key,
            "category": spec.category,
            "label": spec.label,
            "description": spec.description,
            "kind": spec.kind,
            "env_var": spec.env_var,
            "value": value,
            "default": default_val,
            "env_value": env_val if has_env else None,
            "source": source,
            "options": [{"value": v, "label": lbl} for v, lbl in spec.options],
            "min_value": spec.min_value,
            "max_value": spec.max_value,
        })
    return {
        "items": items,
        "override_count": len(overrides),
        "updated_at": updated_at,
        "fake_mode": use_fake_adapters(),
    }


def update_flags(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and persist a partial set of flag updates (``None`` clears override)."""
    if not isinstance(updates, dict):
        raise ValueError("updates 必须是对象")
    validated: Dict[str, Any] = {}
    for raw_key, raw_val in updates.items():
        key = str(raw_key)
        spec = FLAG_BY_KEY.get(key)
        if spec is None:
            raise ValueError(f"未知的开关项：{key}")
        if raw_val is None:
            validated[key] = None
        else:
            validated[key] = _coerce_value(spec, raw_val)
    with _LOCK:
        overrides, _ = _read_overrides_locked()
        for key, value in validated.items():
            if value is None:
                overrides.pop(key, None)
            else:
                overrides[key] = value
        _write_overrides_locked(overrides)
    return snapshot()


def reset_flags(keys: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Clear overrides for the given keys (or all flags when ``keys`` is None)."""
    target_keys = None
    if keys is not None:
        target_keys = []
        for raw_key in keys:
            key = str(raw_key)
            if key not in FLAG_BY_KEY:
                raise ValueError(f"未知的开关项：{key}")
            target_keys.append(key)
    with _LOCK:
        overrides, _ = _read_overrides_locked()
        if target_keys is None:
            overrides.clear()
        else:
            for key in target_keys:
                overrides.pop(key, None)
        _write_overrides_locked(overrides)
    return snapshot()


def invalidate_cache() -> None:
    global _CACHE_KEY, _CACHE_DATA
    with _LOCK:
        _CACHE_KEY = None
        _CACHE_DATA = ({}, None)


__all__ = [
    "FLAG_BY_KEY",
    "FLAG_SPECS",
    "FlagSpec",
    "flags_file_path",
    "get_bool",
    "get_flag",
    "get_int",
    "get_str",
    "invalidate_cache",
    "reset_flags",
    "resolve_flag",
    "snapshot",
    "update_flags",
]
