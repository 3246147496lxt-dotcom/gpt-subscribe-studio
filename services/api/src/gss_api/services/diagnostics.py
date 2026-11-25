"""Read-only local system diagnostics and safe SQLite maintenance."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import subprocess
from typing import Any, Optional

from gss_core.billing_profiles import load_billing_profiles
from gss_core.config import use_fake_adapters
from gss_core.proxies import GatewayStore, configured_proxy
from gss_core.verified_payer import MissingProfile

from ..persistence import get_db_optional
from ..schemas import SystemCheckOut, SystemDiagnosticsOut
from ..store import store
from . import order_flow, reconciler, webhooks


def _detect_node_version() -> Optional[str]:
    node_bin = os.environ.get("SENTINEL_NODE") or shutil.which("node")
    if not node_bin:
        return None
    try:
        out = subprocess.run(
            [node_bin, "-v"], capture_output=True, text=True, timeout=3.0, check=True,
        ).stdout.strip()
        return out or "available"
    except Exception:  # noqa: BLE001
        return None


def _disk_free_mb() -> int:
    db = get_db_optional()
    target = Path(db.path).parent if (db is not None and db.path != ":memory:") else Path.cwd()
    try:
        usage = shutil.disk_usage(target)
        return int(usage.free // (1024 * 1024))
    except OSError:
        return 0


def collect_diagnostics(*, gateway_store: Optional[GatewayStore] = None) -> SystemDiagnosticsOut:
    checks: list[SystemCheckOut] = []
    py_ver = platform.python_version()
    node_ver = _detect_node_version()

    if node_ver:
        checks.append(SystemCheckOut(
            key="runtime",
            label="Python / Node 运行时",
            status="pass",
            code="ok",
            message=f"Python {py_ver} · Node {node_ver}。",
        ))
    else:
        checks.append(SystemCheckOut(
            key="runtime",
            label="Python / Node 运行时",
            status="warn",
            code="node_missing",
            message=f"Python {py_ver} 就绪；未检测到 Node.js。",
        ))

    db = get_db_optional()
    db_stats: dict[str, Any] = {"db_bytes": 0, "wal_bytes": 0, "table_counts": {}}
    if db is None:
        checks.append(SystemCheckOut(
            key="database",
            label="SQLite 持久化",
            status="fail",
            code="db_unavailable",
            message="SQLite 持久化未初始化。",
        ))
    else:
        db_stats = db.maintenance_stats()
        wal_mb = db_stats["wal_bytes"] / (1024 * 1024)
        db_kb = db_stats["db_bytes"] / 1024
        if wal_mb >= 32:
            checks.append(SystemCheckOut(
                key="database",
                label="SQLite 持久化",
                status="warn",
                code="wal_large",
                message=f"主库 {db_kb:.1f} KB · WAL {wal_mb:.1f} MB 偏大，建议执行一键维护（WAL 截断）。",
            ))
        else:
            checks.append(SystemCheckOut(
                key="database",
                label="SQLite 持久化",
                status="pass",
                code="ok",
                message=f"主库 {db_kb:.1f} KB · WAL {db_stats['wal_bytes'] / 1024:.1f} KB · 内存缓存 {len(store.orders)} 单。",
            ))

    if use_fake_adapters():
        checks.append(SystemCheckOut(
            key="verified_billing",
            label="固定账单资料",
            status="pass",
            code="fake_mode",
            message="当前处于离线模拟模式（GSS_USE_FAKE=true）。",
        ))
    else:
        try:
            profiles = load_billing_profiles()
            sample = profiles.fallback or next(iter(profiles.by_currency.values()))
            country = str(sample.get("country") or "US").upper()
            checks.append(SystemCheckOut(
                key="verified_billing",
                label="固定账单资料",
                status="pass",
                code="ok",
                message=f"已加载固定账单（国家 {country} · 权限校验通过；出口可自由更换）。",
            ))
        except (MissingProfile, StopIteration):
            checks.append(SystemCheckOut(
                key="verified_billing",
                label="固定账单资料",
                status="fail",
                code="BILLING_PROFILE_UNAVAILABLE",
                message="未配置可用的固定账单文件（GSS_FIXED_BILLING_FILE / GSS_VERIFIED_BILLING_FILE / GSS_BILLING_PROFILES_FILE）或权限不满足 0400/0600。",
            ))

    proxy_url = None
    if gateway_store is not None:
        try:
            proxy_url = gateway_store.get("checkout").proxy_url()
        except Exception:  # noqa: BLE001
            proxy_url = None
    proxy_url = proxy_url or configured_proxy("checkout")
    if proxy_url:
        checks.append(SystemCheckOut(
            key="proxy",
            label="出口代理网关",
            status="pass",
            code="ok",
            message="已启用出口代理网关配置。",
        ))
    else:
        checks.append(SystemCheckOut(
            key="proxy",
            label="出口代理网关",
            status="warn",
            code="proxy_not_configured",
            message="尚未启用出口代理（直连模式）。",
        ))

    free_mb = _disk_free_mb()
    checks.append(SystemCheckOut(
        key="disk",
        label="本地磁盘余量",
        status="warn" if 0 < free_mb < 512 else "pass",
        code="disk_low" if 0 < free_mb < 512 else "ok",
        message=f"数据卷剩余空间约 {free_mb} MB。" if free_mb > 0 else "已跳过磁盘检测。",
    ))

    q_stats = order_flow.queue_stats()
    rec_stats = reconciler.status()
    wh_stats = webhooks.manager.health_summary()
    checks.append(SystemCheckOut(
        key="background_services",
        label="后台调度 / 对账 / 告警",
        status="warn" if q_stats["draining"] else "pass",
        code="draining" if q_stats["draining"] else "ok",
        message=(
            f"并发 {q_stats['running']}/{q_stats['max_concurrency']}（排队 {q_stats['queued']}）· "
            f"对账器{'运行中' if rec_stats['running'] else '未运行'} · "
            f"Webhook{'已启用' if wh_stats['enabled'] else '未启用'}。"
        ),
    ))

    ok = all(item.status != "fail" for item in checks)
    return SystemDiagnosticsOut(
        ok=ok,
        python_version=py_ver,
        node_version=node_ver,
        db_bytes=int(db_stats.get("db_bytes") or 0),
        wal_bytes=int(db_stats.get("wal_bytes") or 0),
        table_counts=dict(db_stats.get("table_counts") or {}),
        cached_orders=len(store.orders),
        disk_free_mb=free_mb,
        checks=checks,
    )


__all__ = ["collect_diagnostics"]
