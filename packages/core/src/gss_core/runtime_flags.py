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
        label="建单前强制重颁新 AT（ST → AT）",
        description=(
            "真实上游模式下，建单前先丢弃旧 AT 与旧 Cookie，使用 sessionToken（ST）或 OAuth refresh_token "
            "在当前代理出口重新签发新 AT。若已开启「直通模式」且 AT 未过期，则自动跳过重颁。"
        ),
        kind="bool",
        env_var="GSS_REISSUE_SESSION_ON_EXIT",
        default_fn=_default_reissue_on_exit,
    ),
    FlagSpec(
        key="reissue_keep_ua",
        category="session",
        label="原样保留原 Session 的 User-Agent",
        description=(
            "关闭（默认）时自动匹配原 Session 同系统（Mac/Win）的干净 Chrome 构建号；"
            "开启后完整保留原 Session 里的 UA 字符串，并自动展开缩减版 .0.0.0 版本号。"
        ),
        kind="bool",
        env_var="GSS_REISSUE_KEEP_UA",
        default=False,
    ),
    FlagSpec(
        key="client_os_profile",
        category="session",
        label="客户端系统画像（macOS / Windows / Linux）",
        description=(
            "控制请求头、Client Hints 与 Sentinel 沙箱的系统标识：「自动跟随」优先按 session.json 导出端自动匹配 "
            "（Mac 导出发 macOS、Win 导出发 Windows），无 UA 的裸 ST 跟随宿主机系统；Linux 服务器专跑 Mac 裸 ST 可选「强制 macOS」。"
        ),
        kind="enum",
        env_var="GSS_CLIENT_OS_PROFILE",
        default="auto",
        options=(
            ("auto", "自动跟随（Session → 本机）"),
            ("macos", "强制 macOS"),
            ("windows", "强制 Windows"),
            ("linux", "强制 Linux"),
        ),
    ),
    FlagSpec(
        key="direct_session_checkout",
        category="session",
        label="直通建单模式（免重颁、保留原 oai-did）",
        description=(
            "当 session.json 已含未过期 accessToken 时，保留原 oai-did 与 accessToken，不冷调 /api/auth/session，"
            "直接在目标出口完成首页、账号与定价预热后发起建单（对齐真机空白环境实测）。"
        ),
        kind="bool",
        env_var="GSS_DIRECT_SESSION_CHECKOUT",
        default=False,
    ),
    FlagSpec(
        key="warm_account",
        category="session",
        label="重颁后额外预热账号接口 (/backend-api/me)",
        description="仅在走重颁模式时生效：重颁新 AT 后额外停顿 1–1.5 秒并探测一次 /backend-api/me。",
        kind="bool",
        env_var="GSS_WARM_ACCOUNT",
        default=False,
    ),
    FlagSpec(
        key="proxy_preflight",
        category="session",
        label="重颁前校验代理出口国家一致性",
        description="外发 sessionToken 前先用无凭证探针核对代理出口 IP 归属国，不匹配立即中止且不外发凭证。",
        kind="bool",
        env_var="GSS_PROXY_PREFLIGHT",
        default=False,
    ),
    # ── 协议引擎与风控遥测 ──────────────────────────────────────────
    FlagSpec(
        key="plus_engine",
        category="protocol",
        label="Plus / Pro 支付协议引擎",
        description="选择 oaics_ 结账链路的支付协议实现：默认 HAR 逐请求复刻，可按需切回 legacy 旧版直付。",
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
        label="Stripe 遥测与指纹回放",
        description="控制是否完整回放 m.stripe.com/6、r.stripe.com/b、CES 及 /sentinel/ping；关闭可提速但可能抬高风控。",
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
        label="建单代理超时自动换节点重试",
        description="连接建立前遇到代理隧道超时 / TLS 握手失败（未提交业务请求）时自动切换新 SID 重试一次，避免单节点卡死。",
        kind="bool",
        env_var="GSS_CHECKOUT_ROTATE",
        default=False,
    ),
    FlagSpec(
        key="require_exit_billing_match",
        category="protocol",
        label="严格模式：出口国必须与账单国一致",
        description="关闭（默认）时支持固定美国免税账单 + 任意低价区出口；开启后强制出口国家必须与账单国相同。",
        kind="bool",
        env_var="GSS_REQUIRE_EXIT_BILLING_MATCH",
        default=False,
    ),
    # ── 队列、预检与自动对账 ────────────────────────────────────────
    FlagSpec(
        key="quick_pay_concurrency",
        category="runtime",
        label="一键充值全局最大并发数",
        description="同时执行的一键充值任务槽位上限（1–16）；同一账号始终串行，不受此数值影响。",
        kind="int",
        env_var="GSS_QUICK_PAY_CONCURRENCY",
        default=3,
        min_value=1,
        max_value=16,
    ),
    FlagSpec(
        key="preflight_strict",
        category="runtime",
        label="入队前置严格拦截（缺账单直接拒绝）",
        description="开启后若未配置固定免税账单，任务在前置体检阶段即直接拦截，不进入建单阶段。",
        kind="bool",
        env_var="GSS_PREFLIGHT_STRICT",
        default=False,
    ),
    FlagSpec(
        key="reconciler_enabled",
        category="runtime",
        label="后台只读自动对账补偿",
        description="定期对「待核验」与「未知状态」订单做纯只读订阅核对与自动交付，绝不重复扣款。",
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
