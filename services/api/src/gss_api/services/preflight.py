"""Read-only pre-flight checks before creating an order or charging a card.

Never persists or echoes raw session tokens, cookies, billing addresses,
proxy credentials, or card fields.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Optional

from gss_contracts import Plan
from gss_core.billing_profiles import load_billing_profiles
from gss_core.config import use_fake_adapters
from gss_core.countries import currency_for, is_supported, normalize_country
from gss_core.proxies import GatewayStore, align_proxy_region, configured_exit_country, configured_proxy
from gss_core.session_token import parse_session_bundle
from gss_core.fixed_billing import BILLING_COUNTRY
from gss_core.verified_payer import MissingProfile, exit_billing_must_match

from .. import account_detail, locks
from ..persistence import get_db_optional
from ..routers.orders import normalize_plan_name
from ..routers.proxies import probe_gateway
from ..schemas import PreflightCheckOut, PreflightOut, PreflightRequest, QueueStatsOut

_VALID_PLANS = {member.value for member in Plan}
_EXPIRY_WARN_SECONDS = 300


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        padded = parts[1] + ("=" * (-len(parts[1]) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_account_id(token: str) -> str:
    claims = _jwt_claims(token)
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        account_id = str(auth.get("chatgpt_account_id") or "").strip()
        if account_id:
            return account_id
    return str(claims.get("sub") or "").strip()


def _resolve_preflight_exit(exit_country_raw: Optional[str], billing_country_raw: Optional[str]) -> str:
    explicit = normalize_country(exit_country_raw or "")
    if explicit:
        return explicit
    if not use_fake_adapters():
        configured = normalize_country(configured_exit_country() or "")
        if configured:
            return configured
    return normalize_country(billing_country_raw or "US")


def _check_plan_region(
    plan_raw: str,
    exit_country_raw: Optional[str],
    billing_country_raw: Optional[str],
) -> tuple[PreflightCheckOut, str, str]:
    normalized_plan = normalize_plan_name(plan_raw)
    if normalized_plan not in _VALID_PLANS:
        return (
            PreflightCheckOut(
                key="plan_region",
                label="套餐与结算地区",
                status="fail",
                code="invalid_request",
                message="不支持该订阅套餐代码，请重新选择。",
            ),
            normalized_plan,
            "USD",
        )
    target_exit = _resolve_preflight_exit(exit_country_raw, billing_country_raw)
    if not target_exit or not is_supported(target_exit):
        return (
            PreflightCheckOut(
                key="plan_region",
                label="套餐与结算地区",
                status="fail",
                code="unsupported_country",
                message=f"暂不支持出口/结算地区 {exit_country_raw or billing_country_raw or '-'}。",
            ),
            normalized_plan,
            "USD",
        )
    if billing_country_raw:
        target_billing = normalize_country(billing_country_raw)
        if not target_billing or not is_supported(target_billing):
            return (
                PreflightCheckOut(
                    key="plan_region",
                    label="套餐与结算地区",
                    status="fail",
                    code="unsupported_country",
                    message=f"暂不支持账单国家 {billing_country_raw}。",
                ),
                normalized_plan,
                "USD",
            )
    currency = currency_for(target_exit)
    return (
        PreflightCheckOut(
            key="plan_region",
            label="套餐与结算地区",
            status="pass",
            code="ok",
            message=f"套餐 {normalized_plan} · 出口 {target_exit} · 预计结算币种 {currency}。",
        ),
        normalized_plan,
        currency,
    )


def _check_session(session_raw: str, *, allow_synthetic_token: bool = False) -> tuple[PreflightCheckOut, str, bool]:
    text = str(session_raw or "").strip()
    if not text:
        return (
            PreflightCheckOut(
                key="session",
                label="Session 凭证",
                status="fail",
                code="invalid_session",
                message="请先粘贴完整的 ChatGPT Session JSON。",
            ),
            "",
            False,
        )
    try:
        bundle = parse_session_bundle(text)
    except ValueError:
        return (
            PreflightCheckOut(
                key="session",
                label="Session 凭证",
                status="fail",
                code="invalid_session",
                message="Session 格式无法解析，请粘贴包含 accessToken 的完整 JSON。",
            ),
            "",
            False,
        )

    has_refresh = bool(bundle.session_token or bundle.refresh_token)
    token = bundle.access_token or ""
    if not token:
        if has_refresh:
            return (
                PreflightCheckOut(
                    key="session",
                    label="Session 凭证",
                    status="warn",
                    code="session_refresh_needed",
                    message="未包含 accessToken，将尝试通过 sessionToken/refreshToken 刷新。",
                ),
                "",
                True,
            )
        return (
            PreflightCheckOut(
                key="session",
                label="Session 凭证",
                status="fail",
                code="invalid_session",
                message="Session 缺少 accessToken，请重新登录后导出。",
            ),
            "",
            False,
        )

    claims = _jwt_claims(token)
    account_id = _extract_account_id(token)
    exp = claims.get("exp")
    now = datetime.now(timezone.utc).timestamp()
    if isinstance(exp, (int, float)):
        remaining = exp - now
        if remaining <= 0:
            if has_refresh:
                return (
                    PreflightCheckOut(
                        key="session",
                        label="Session 凭证",
                        status="warn",
                        code="session_expiring",
                        message="accessToken 已过期，检测到可刷新凭证，建单时将尝试自动刷新。",
                    ),
                    account_id,
                    True,
                )
            return (
                PreflightCheckOut(
                    key="session",
                    label="Session 凭证",
                    status="fail",
                    code="session_expired",
                    message="accessToken 已过期且缺少可刷新凭证，请重新导出 Session。",
                ),
                account_id,
                False,
            )
        if remaining < _EXPIRY_WARN_SECONDS:
            return (
                PreflightCheckOut(
                    key="session",
                    label="Session 凭证",
                    status="warn",
                    code="session_expiring",
                    message="accessToken 即将过期（不足 5 分钟），建议重新导出或在刷新前尽快操作。",
                ),
                account_id,
                True,
            )
        suffix = "（含刷新凭证）" if has_refresh else "（不含刷新凭证，跨区重签发可能受限）"
        return (
            PreflightCheckOut(
                key="session",
                label="Session 凭证",
                status="pass",
                code="ok",
                message=f"accessToken 格式有效且未过期{suffix}。",
            ),
            account_id,
            True,
        )

    if allow_synthetic_token:
        return (
            PreflightCheckOut(
                key="session",
                label="Session 凭证",
                status="pass",
                code="ok",
                message="已接收会话凭证。",
            ),
            account_id,
            True,
        )
    return (
        PreflightCheckOut(
            key="session",
            label="Session 凭证",
            status="warn",
            code="non_jwt_token",
            message="未从 accessToken 中解析到 JWT 过期时间，请确认粘贴的是完整 Session JSON。",
        ),
        account_id,
        True,
    )


def _check_account_lock(fingerprint: str) -> PreflightCheckOut:
    if not fingerprint:
        return PreflightCheckOut(
            key="account_lock",
            label="账号防重付资金锁",
            status="pass",
            code="ok",
            message="未命中账号资金锁。",
        )
    held = locks.find_active_lock(fingerprint)
    if held is not None:
        return PreflightCheckOut(
            key="account_lock",
            label="账号防重付资金锁",
            status="fail",
            code="payment_unknown_lock",
            message="该账号存在未核销的 unknown 订单，已锁定禁止再次扣款，请先核对账单后手动解锁。",
        )
    return PreflightCheckOut(
        key="account_lock",
        label="账号防重付资金锁",
        status="pass",
        code="ok",
        message="账号未锁定，可正常排队执行。",
    )


def _check_verified_billing(
    plan: str,
    currency: str,
    *,
    exit_country: str = "",
    billing_country: str = "",
) -> PreflightCheckOut:
    if plan == "team":
        return PreflightCheckOut(
            key="verified_billing",
            label="固定核验账单",
            status="pass",
            code="ok",
            message="Team 档位沿用独立账单规则。",
        )
    if use_fake_adapters():
        return PreflightCheckOut(
            key="verified_billing",
            label="固定核验账单",
            status="pass",
            code="fake_mode",
            message="当前为离线模拟模式（GSS_USE_FAKE=true），跳过固定账单文件检查。",
        )
    try:
        profile = load_billing_profiles().resolve_for_country(BILLING_COUNTRY, currency)
    except MissingProfile:
        return PreflightCheckOut(
            key="verified_billing",
            label="固定核验账单",
            status="fail",
            code="BILLING_PROFILE_UNAVAILABLE",
            message="未配置可用的固定核验账单：可在后台「系统 → 固定账单」直接配置，或设置 GSS_FIXED_BILLING_FILE（权限须为 0400/0600）。",
        )
    payer_country = str(profile.get("country") or "").upper()
    if exit_billing_must_match() and exit_country and exit_country.upper() != payer_country:
        return PreflightCheckOut(
            key="verified_billing",
            label="固定核验账单",
            status="fail",
            code="billing_country_mismatch",
            message=f"严格模式下出口地区（{exit_country.upper()}）必须与固定账单国家（{payer_country}）一致。",
        )
    exit_note = f" · 出口 {exit_country.upper()}（可自由更换）" if exit_country else ""
    ignored = ""
    if billing_country and billing_country.upper() != payer_country:
        ignored = f" · 提交账单国 {billing_country.upper()} 已忽略"
    return PreflightCheckOut(
        key="verified_billing",
        label="固定核验账单",
        status="pass",
        code="ok",
        message=f"已就绪（固定账单国家 {payer_country} · 币种 {currency}{exit_note}{ignored}）。",
    )


def _check_cached_subscription(plan: str, account_id: str) -> PreflightCheckOut:
    if plan == "team":
        return PreflightCheckOut(
            key="subscription",
            label="订阅状态预检",
            status="pass",
            code="ok",
            message="Team 档位不拦截个人订阅冲突。",
        )
    db = get_db_optional()
    if db is None or not account_id:
        return PreflightCheckOut(
            key="subscription",
            label="订阅状态预检",
            status="pass",
            code="unchecked",
            message="本地无近期订阅缓存；勾选“含上游实测”可实时核查是否已订阅。",
        )
    snap = db.get_account_snapshot(account_id)
    if not snap or snap.get("cacheVersion") != account_detail.CACHE_VERSION:
        return PreflightCheckOut(
            key="subscription",
            label="订阅状态预检",
            status="pass",
            code="unchecked",
            message="本地无近期订阅缓存；建单前将由服务端再次核验。",
        )
    status = str(snap.get("status") or "").lower()
    plan_name = str(snap.get("planName") or "")
    if status in {"active", "trialing"} and "免费" not in plan_name and "free" not in plan_name.lower():
        return PreflightCheckOut(
            key="subscription",
            label="订阅状态预检",
            status="warn",
            code="already_subscribed_cached",
            message=f"本地缓存显示该账号近期已有有效订阅（{plan_name or status}），建议勾选“含上游实测”确认最新状态，避免重复建单。",
        )
    return PreflightCheckOut(
        key="subscription",
        label="订阅状态预检",
        status="pass",
        code="ok",
        message="本地缓存未见有效付费订阅冲突。",
    )


async def _check_upstream_subscription(plan: str, session_raw: str) -> PreflightCheckOut:
    if plan == "team":
        return PreflightCheckOut(
            key="subscription",
            label="订阅状态预检",
            status="pass",
            code="ok",
            message="Team 档位不拦截个人订阅冲突。",
        )
    try:
        credentials = account_detail.parse_credentials(session_raw)
        merged = await asyncio.wait_for(
            account_detail.fetch_snapshot(
                session_raw,
                credentials["access_token"],
                account_id=credentials["account_id"],
                country=credentials["session_country"] or "US",
            ),
            timeout=20.0,
        )
        detail = account_detail.normalize(
            merged,
            email=credentials["email"],
            account_id=credentials["account_id"],
        )
        db = get_db_optional()
        cache_key = credentials["account_id"] or credentials["email"].lower()
        if db is not None and cache_key:
            try:
                db.save_account_snapshot(
                    cache_key,
                    email=credentials["email"],
                    account_id=credentials["account_id"],
                    detail=detail,
                    queried_at=detail["queriedAt"],
                )
            except Exception:  # noqa: BLE001
                pass
    except account_detail.CredentialError as exc:
        code = str((exc.detail or {}).get("code") or "invalid_session")
        return PreflightCheckOut(
            key="subscription",
            label="订阅状态预检",
            status="fail",
            code=code,
            message=exc.message,
        )
    except asyncio.TimeoutError:
        return PreflightCheckOut(
            key="subscription",
            label="订阅状态预检",
            status="warn",
            code="upstream_timeout",
            message="上游订阅状态核验超时，未确认最新状态，请稍后重试。",
        )
    except Exception:  # noqa: BLE001
        return PreflightCheckOut(
            key="subscription",
            label="订阅状态预检",
            status="warn",
            code="account_check_unavailable",
            message="实时查询账号订阅状态失败，请检查代理连通性或 Session 有效性。",
        )

    status = str(detail.get("status") or "").lower()
    plan_name = str(detail.get("planName") or "")
    if status in {"active", "trialing"} and "免费" not in plan_name and "free" not in plan_name.lower():
        return PreflightCheckOut(
            key="subscription",
            label="订阅状态预检",
            status="fail",
            code="already_subscribed",
            message=f"实时核验确认该账号已有有效订阅（{plan_name or status}），已拦截避免重复开通。",
        )
    return PreflightCheckOut(
        key="subscription",
        label="订阅状态预检",
        status="pass",
        code="ok",
        message=f"实时核验通过（当前档位：{plan_name or '免费版'}）。",
    )


async def _check_proxy(
    *,
    exit_country: str,
    gateway_store: Optional[GatewayStore],
    test_connectivity: bool,
) -> PreflightCheckOut:
    if use_fake_adapters():
        return PreflightCheckOut(
            key="proxy",
            label="出口代理网关",
            status="pass",
            code="fake_mode",
            message="当前为离线模拟模式，无需出口代理。",
        )
    proxy_url = None
    if gateway_store is not None:
        try:
            proxy_url = gateway_store.get("checkout").proxy_url()
        except Exception:  # noqa: BLE001
            proxy_url = None
    if not proxy_url:
        proxy_url = configured_proxy("checkout")
    if not proxy_url:
        return PreflightCheckOut(
            key="proxy",
            label="出口代理网关",
            status="warn",
            code="proxy_not_configured",
            message="尚未启用出口代理；直连请求可能受限于网络出口，可在「代理设置」配置。",
        )
    if not test_connectivity:
        return PreflightCheckOut(
            key="proxy",
            label="出口代理网关",
            status="pass",
            code="ok",
            message="出口代理已配置并启用（勾选“含代理实测”可实时测试连通与延迟）。",
        )
    aligned = align_proxy_region(proxy_url, exit_country) if exit_country else proxy_url
    try:
        res = await asyncio.wait_for(probe_gateway(aligned), timeout=15.0)
        return PreflightCheckOut(
            key="proxy",
            label="出口代理网关",
            status="pass",
            code="ok",
            message=f"代理连通正常 · 出口 IP {res['ip']} · 延迟 {res['elapsed_ms']} ms。",
        )
    except Exception:  # noqa: BLE001
        return PreflightCheckOut(
            key="proxy",
            label="出口代理网关",
            status="fail",
            code="proxy_tunnel_failed",
            message="出口代理实测连接失败，请检查代理配置、账号密码或供应商白名单。",
        )


def _check_runtime(stats: QueueStatsOut) -> PreflightCheckOut:
    if stats.draining:
        return PreflightCheckOut(
            key="runtime",
            label="本地运行与队列状态",
            status="fail",
            code="service_draining",
            message="服务正在优雅停机排空，暂不接收新任务，请稍后重试。",
        )
    node_bin = os.environ.get("SENTINEL_NODE") or shutil.which("node")
    if not node_bin and not use_fake_adapters():
        return PreflightCheckOut(
            key="runtime",
            label="本地运行与队列状态",
            status="warn",
            code="node_missing",
            message="未检测到 Node.js，真实建单的 Sentinel 令牌签发可能失败。",
        )
    return PreflightCheckOut(
        key="runtime",
        label="本地运行与队列状态",
        status="pass",
        code="ok",
        message=f"队列健康（运行中 {stats.running}/{stats.max_concurrency} · 排队 {stats.queued}）。",
    )


async def run_preflight(
    payload: PreflightRequest,
    *,
    queue_stats: QueueStatsOut,
    gateway_store: Optional[GatewayStore] = None,
) -> PreflightOut:
    """Execute full read-only pre-flight checks and return structured report."""
    started = time.perf_counter()
    checks: list[PreflightCheckOut] = []

    runtime_check = _check_runtime(queue_stats)
    checks.append(runtime_check)

    plan_check, normalized_plan, currency = _check_plan_region(
        payload.plan, payload.exit_country, payload.billing_country,
    )
    checks.append(plan_check)

    session_check, account_id, session_usable = _check_session(payload.session, allow_synthetic_token=False)
    checks.append(session_check)

    exit_code = _resolve_preflight_exit(payload.exit_country, payload.billing_country)
    billing_code = normalize_country(payload.billing_country or "")
    fingerprint = locks.account_fingerprint(payload.session) if payload.session.strip() else ""
    checks.append(_check_account_lock(fingerprint))
    checks.append(_check_verified_billing(
        normalized_plan,
        currency,
        exit_country=exit_code,
        billing_country=billing_code,
    ))

    if payload.check_account_upstream and session_usable:
        checks.append(await _check_upstream_subscription(normalized_plan, payload.session))
    else:
        checks.append(_check_cached_subscription(normalized_plan, account_id))

    checks.append(await _check_proxy(
        exit_country=exit_code,
        gateway_store=gateway_store,
        test_connectivity=payload.check_proxy_connectivity,
    ))

    failed = [c for c in checks if c.status == "fail"]
    warns = [c for c in checks if c.status == "warn"]
    elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
    if failed:
        summary = f"发现 {len(failed)} 项阻断问题，建议修复后再提交：{failed[0].message}"
        return PreflightOut(
            ok=False,
            blocking_code=failed[0].code,
            summary=summary,
            checks=checks,
            queue=queue_stats,
            elapsed_ms=elapsed_ms,
        )
    if warns:
        summary = f"必检项全部通过（另有 {len(warns)} 项提示），可继续提交。"
    else:
        summary = "所有前置体检项全部通过，可放心提交任务。"
    return PreflightOut(
        ok=True,
        blocking_code=None,
        summary=summary,
        checks=checks,
        queue=queue_stats,
        elapsed_ms=elapsed_ms,
    )


def run_quick_pay_preflight(
    *,
    plan: str,
    exit_country: Optional[str],
    billing_country: Optional[str],
    session: str,
    account_fingerprint: str = "",
) -> Optional[tuple[str, str]]:
    """Fast deterministic local gate run inside quick-pay before create_order.

    Returns ``(failure_code, safe_message)`` if blocked, or ``None`` if passed.
    """
    lock_check = _check_account_lock(account_fingerprint)
    if lock_check.status == "fail":
        return lock_check.code, lock_check.message

    plan_check, normalized_plan, currency = _check_plan_region(plan, exit_country, billing_country)
    if plan_check.status == "fail":
        return plan_check.code, plan_check.message

    session_check, _account_id, _usable = _check_session(session, allow_synthetic_token=True)
    if session_check.status == "fail":
        return session_check.code, session_check.message

    exit_code = _resolve_preflight_exit(exit_country, billing_country)
    billing_code = normalize_country(billing_country or "")
    from gss_core import runtime_flags

    strict = runtime_flags.get_bool("preflight_strict")
    billing_check = _check_verified_billing(
        normalized_plan,
        currency,
        exit_country=exit_code,
        billing_country=billing_code,
    )
    if billing_check.status == "fail":
        # Quick-pay admission stays permissive here; the create/pay boundary
        # remains the fail-closed check for a missing billing profile.
        if strict or billing_check.code == "billing_country_mismatch":
            return billing_check.code, billing_check.message

    return None


__all__ = ["run_preflight", "run_quick_pay_preflight"]
