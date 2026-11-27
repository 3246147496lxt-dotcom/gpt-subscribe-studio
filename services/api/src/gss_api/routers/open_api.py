"""Developer Open API (`/api/v1/*`) and Admin API Key management (`/api/developer/*`).

Authentication & tenant boundary:
- External callers authenticate via ``Authorization: Bearer gss_live_...`` or ``X-API-Key``.
- Only the SHA-256 hash of each API key is persisted in SQLite; plaintext secrets are
  returned exactly once at creation or rotation time.
- Each API key sees strictly its own tasks (bound in ``api_tasks``); cross-key access
  returns 404. Internal proxy SIDs, exit IPs, raw upstream traces, and vault credentials
  are never exposed on ``/api/v1/*``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from gss_contracts.errors import DomainError
from gss_core.proxies import align_proxy_region, configured_proxy

from .. import locks
from ..account_detail import (
    fetch_basic_snapshot,
    fetch_snapshot,
    normalize as normalize_account_detail,
    parse_credentials,
)
from ..auto_renew import cancel_account_auto_renew
from ..order_vault import vault_retention_days
from ..persistence import get_db_optional
from ..portal_config import load_portal_config
from ..schemas import (
    AccountSubscriptionOut,
    AccountSubscriptionRequest,
    ApiKeyCreateRequest,
    ApiKeyOut,
    ApiKeySecretOut,
    ApiKeyUpdateRequest,
    BillingIn,
    CardIn,
    ModeIn,
    QuickPayRequest,
)
from ..services import order_flow, reference_pricing
from ..services.order_flow import get_job, queue_stats, start_quick_pay
from .account import _http as _account_http
from .events import _quick_pay_stream, _sse
from .public_portal import _extract_masked_email, _sanitize_public_job
from .quick_pay import _out as _quick_pay_out

router = APIRouter(prefix="/api/v1", tags=["developer-api"])
admin_router = APIRouter(prefix="/api/developer", tags=["developer-admin"])

_SUPPORTED_API_PLANS = ("go", "plus", "pro_lite", "pro", "pro_max")
_PLAN_ALIASES: Dict[str, str] = {
    "go": "go",
    "plus": "plus",
    "pro_lite": "pro_lite",
    "pro100": "pro_lite",
    "pro_100": "pro_lite",
    "pro": "pro",
    "pro200": "pro",
    "pro_200": "pro",
    "pro_max": "pro_max",
    "pro500": "pro_max",
    "pro_500": "pro_max",
}
_PLAN_DISPLAY_NAMES: Dict[str, str] = {
    "go": "Go",
    "plus": "Plus",
    "pro_lite": "Pro100",
    "pro": "Pro200",
    "pro_max": "Pro500",
}

_KEY_PREFIX = "gss_live_"
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: Dict[str, Deque[float]] = {}


def _hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _mint_api_key() -> tuple[str, str, str]:
    token = secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]
    raw = f"{_KEY_PREFIX}{token}"
    prefix = raw[:16]
    return raw, prefix, _hash_api_key(raw)


def _normalize_plan(raw: str) -> Optional[str]:
    norm = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return _PLAN_ALIASES.get(norm)


def _normalize_allowed_plans(plans: Optional[List[str]]) -> List[str]:
    if not plans:
        return list(_SUPPORTED_API_PLANS)
    out: List[str] = []
    for item in plans:
        canonical = _normalize_plan(item)
        if canonical and canonical not in out:
            out.append(canonical)
    if not out:
        raise HTTPException(
            400,
            detail={"code": "invalid_plans", "message": "allowed_plans 至少需包含一个受支持的套餐（go/plus/pro_lite/pro/pro_max）。"},
        )
    return out


def _parse_allowed_plans(raw_json: Any) -> List[str]:
    if isinstance(raw_json, list):
        return [str(x) for x in raw_json if str(x) in _SUPPORTED_API_PLANS]
    try:
        parsed = json.loads(str(raw_json or "[]"))
        if isinstance(parsed, list):
            return [str(x) for x in parsed if str(x) in _SUPPORTED_API_PLANS]
    except Exception:  # noqa: BLE001
        pass
    return list(_SUPPORTED_API_PLANS)


def _serialize_api_key(row: Dict[str, Any]) -> ApiKeyOut:
    return ApiKeyOut(
        id=str(row["id"]),
        name=str(row["name"]),
        key_prefix=str(row["key_prefix"]),
        status=str(row.get("status") or "active"),
        rate_limit_rpm=int(row.get("rate_limit_rpm") or 30),
        allowed_plans=_parse_allowed_plans(row.get("allowed_plans_json")),
        note=row.get("note") or None,
        total_requests=int(row.get("total_requests") or 0),
        total_orders=int(row.get("total_orders") or 0),
        succeeded_orders=int(row.get("succeeded_orders") or 0),
        failed_orders=int(row.get("failed_orders") or 0),
        pending_orders=int(row.get("pending_orders") or 0),
        last_used_at=row.get("last_used_at") or None,
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
        revoked_at=row.get("revoked_at") or None,
    )


def _extract_raw_api_key(request: Request) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def _check_rate_limit(key_id: str, limit_rpm: int) -> None:
    now = time.monotonic()
    cutoff = now - 60.0
    cap = max(1, int(limit_rpm or 30))
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.setdefault(key_id, deque())
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= cap:
            retry_after = max(1, int(math.ceil(60.0 - (now - bucket[0]))))
            raise HTTPException(
                429,
                detail={
                    "code": "rate_limit_exceeded",
                    "message": f"请求频率超过当前 API Key 限制（{cap} 次/分钟），请在 {retry_after} 秒后重试。",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


def require_api_key(request: Request) -> Dict[str, Any]:
    db = get_db_optional()
    if db is None:
        raise HTTPException(503, detail={"code": "db_unavailable", "message": "服务存储暂不可用，请稍后重试。"})
    raw_key = _extract_raw_api_key(request)
    if not raw_key or not raw_key.startswith(_KEY_PREFIX) or len(raw_key) < 24:
        raise HTTPException(
            401,
            detail={
                "code": "invalid_api_key",
                "message": "缺少或无效的 API Key，请通过 Authorization: Bearer <key> 或 X-API-Key 请求头传入。",
            },
        )
    row = db.get_api_key_by_hash(_hash_api_key(raw_key))
    if row is None:
        raise HTTPException(
            401,
            detail={"code": "invalid_api_key", "message": "API Key 不存在或校验失败。"},
        )
    status_val = str(row.get("status") or "")
    if status_val == "revoked":
        raise HTTPException(
            401,
            detail={"code": "api_key_revoked", "message": "该 API Key 已被吊销，请更换有效密钥。"},
        )
    if status_val != "active":
        raise HTTPException(
            403,
            detail={"code": "api_key_disabled", "message": "该 API Key 已被管理员停用。"},
        )
    _check_rate_limit(str(row["id"]), int(row.get("rate_limit_rpm") or 30))
    db.touch_api_key_usage(str(row["id"]))
    row["allowed_plans"] = _parse_allowed_plans(row.get("allowed_plans_json"))
    return row


def _scoped_idempotency_key(key_id: str, raw_key: str) -> str:
    combined = f"api:{key_id}:{raw_key}"
    if len(combined) <= 255:
        return combined
    digest = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    return f"api:{key_id}:sha256:{digest}"


def _sanitize_developer_job(raw: Dict[str, Any], *, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = _sanitize_public_job(raw, meta=meta)
    plan_code = base.get("plan") or ""
    return {
        **base,
        "plan_display_name": _PLAN_DISPLAY_NAMES.get(plan_code, plan_code),
        "external_order_id": (meta or {}).get("external_order_id"),
    }


# ── Developer Open API Schemas ───────────────────────────────────────────
class OpenApiRechargeIn(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    plan: str = Field(..., min_length=2, max_length=32)
    exit_country: Optional[str] = Field(default=None, max_length=8)
    session: str = Field(..., min_length=10, repr=False)
    card: CardIn = Field(..., repr=False)
    billing: Optional[BillingIn] = Field(default=None, repr=False)
    auto_cancel_renew: bool = True
    external_order_id: Optional[str] = Field(default=None, max_length=128)


class OpenApiCancelRenewIn(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, populate_by_name=True)

    session: str = Field(..., min_length=10, repr=False)
    plan: str = ""
    account_id: str = Field(default="", alias="accountId")


# ── Developer Open API Endpoints (`/api/v1/*`) ───────────────────────────
@router.get("/ping")
async def developer_ping(api_key: Dict[str, Any] = Depends(require_api_key)) -> Dict[str, Any]:
    from ..main import __version__

    return {
        "ok": True,
        "version": __version__,
        "key_id": api_key["id"],
        "key_name": api_key["name"],
        "key_prefix": api_key["key_prefix"],
        "allowed_plans": api_key["allowed_plans"],
        "rate_limit_rpm": int(api_key.get("rate_limit_rpm") or 30),
        "queue": queue_stats(),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/plans")
async def developer_plans(api_key: Dict[str, Any] = Depends(require_api_key)) -> Dict[str, Any]:
    cfg = reference_pricing.apply_to_site_config(load_portal_config())
    ref_prices = cfg.get("reference_prices") or {}
    allowed = set(api_key["allowed_plans"])
    plans_out: List[Dict[str, Any]] = []
    for item in cfg.get("plans") or []:
        pid = str(item.get("id") or "")
        if pid not in _SUPPORTED_API_PLANS:
            continue
        plans_out.append({
            "id": pid,
            "display_name": _PLAN_DISPLAY_NAMES.get(pid, pid),
            "aliases": [alias for alias, target in _PLAN_ALIASES.items() if target == pid and alias != pid],
            "default_region": item.get("default_region"),
            "usd_price": item.get("usd_price"),
            "cny_price": item.get("cny_price"),
            "available": bool(item.get("available", True)),
            "allowed_for_key": pid in allowed,
            "prices_by_country": ref_prices.get(pid) or {},
        })
    return {
        "plans": plans_out,
        "regions": cfg.get("regions") or [],
        "reference_price_status": cfg.get("reference_price_status") or {},
    }


@router.post("/recharges", status_code=202)
async def create_developer_recharge(
    payload: OpenApiRechargeIn,
    response: Response,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    api_key: Dict[str, Any] = Depends(require_api_key),
) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    plan_norm = _normalize_plan(payload.plan)
    if not plan_norm or plan_norm not in _SUPPORTED_API_PLANS:
        raise HTTPException(
            400,
            detail={
                "code": "unsupported_plan",
                "message": "不支持的套餐代码，可选：go / plus / pro_lite (pro100) / pro (pro200) / pro_max (pro500)。",
            },
        )
    if plan_norm not in set(api_key["allowed_plans"]):
        raise HTTPException(
            403,
            detail={"code": "plan_not_allowed", "message": f"当前 API Key 未开放套餐 {plan_norm} 的调用权限。"},
        )
    raw_idem = (idempotency_key or "").strip()
    if not raw_idem or len(raw_idem) > 200:
        raise HTTPException(
            400,
            detail={
                "code": "idempotency_key_required",
                "message": "POST /api/v1/recharges 必须在请求头提供 1~200 字符的 Idempotency-Key。",
            },
        )
    exit_country = (payload.exit_country or "").strip().upper() or None
    ext_id = (payload.external_order_id or "").strip() or None
    quick_kwargs: Dict[str, Any] = {
        "plan": plan_norm,
        "exit_country": exit_country,
        "session": payload.session,
        "card": payload.card,
        "mode": ModeIn(
            account_source="customer_session",
            card_source="customer",
            billing_mode="standard",
            risk_acknowledged=True,
        ),
        "auto_cancel_renew": bool(payload.auto_cancel_renew),
        "debug_checkout_only": False,
    }
    if payload.billing is not None:
        quick_kwargs["billing"] = payload.billing
    quick_req = QuickPayRequest(**quick_kwargs)
    fingerprint = locks.account_fingerprint(payload.session)
    scoped_key = _scoped_idempotency_key(str(api_key["id"]), raw_idem)
    job = start_quick_pay(quick_req, scoped_key, fingerprint)
    masked_email = _extract_masked_email(payload.session)

    db = get_db_optional()
    created_at = datetime.now(timezone.utc).isoformat()
    if db is not None:
        db.bind_api_task(
            job_id=job.id,
            api_key_id=str(api_key["id"]),
            external_order_id=ext_id or "",
            masked_email=masked_email,
            plan=plan_norm,
            exit_country=exit_country or "",
        )
    raw_out = _quick_pay_out(job, admitted=True).model_dump()
    return _sanitize_developer_job(
        raw_out,
        meta={
            "plan": plan_norm,
            "exit_country": exit_country or "",
            "masked_email": masked_email,
            "external_order_id": ext_id,
            "created_at": created_at,
        },
    )


@router.get("/recharges")
async def list_developer_recharges(
    status_filter: str = Query(default="all", alias="status", max_length=32),
    external_order_id: str = Query(default="", max_length=128),
    q: str = Query(default="", max_length=100),
    page: int = Query(default=1, ge=1, le=500),
    page_size: int = Query(default=20, ge=1, le=100),
    api_key: Dict[str, Any] = Depends(require_api_key),
) -> Dict[str, Any]:
    db = get_db_optional()
    if db is None:
        raise HTTPException(503, detail={"code": "db_unavailable", "message": "服务存储暂不可用。"})
    offset = (page - 1) * page_size
    rows, total = db.list_api_tasks(
        api_key_id=str(api_key["id"]),
        status=status_filter,
        external_order_id=external_order_id,
        search=q,
        limit=page_size,
        offset=offset,
    )
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    items = [
        {
            "job_id": row["job_id"],
            "order_id": row.get("order_id"),
            "external_order_id": row.get("external_order_id"),
            "masked_email": row.get("masked_email") or "",
            "plan": row.get("plan") or "",
            "plan_display_name": _PLAN_DISPLAY_NAMES.get(str(row.get("plan") or ""), str(row.get("plan") or "")),
            "exit_country": row.get("exit_country") or "",
            "status": row.get("status") or "queued",
            "stage": row.get("stage") or "queued",
            "payment_status": row.get("payment_status") or "unpaid",
            "delivery_status": row.get("delivery_status") or "pending",
            "amount": row.get("amount"),
            "currency": row.get("currency"),
            "failure_code": row.get("failure_code"),
            "message": row.get("message"),
            "auto_renew_status": row.get("auto_renew_status") or "disabled",
            "auto_renew_canceled_at": row.get("auto_renew_canceled_at"),
            "created_at": row.get("created_at"),
            "finished_at": row.get("finished_at"),
        }
        for row in rows
    ]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.get("/recharges/{job_id}")
async def get_developer_recharge(
    job_id: str,
    api_key: Dict[str, Any] = Depends(require_api_key),
) -> Dict[str, Any]:
    db = get_db_optional()
    if db is None:
        raise HTTPException(503, detail={"code": "db_unavailable", "message": "暂时无法读取任务状态。"})
    ownership = db.get_api_task_ownership(job_id, api_key_id=str(api_key["id"]))
    if ownership is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "充值任务不存在或不属于当前 API Key。"})
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "充值任务不存在。"})
    raw_out = _quick_pay_out(job).model_dump()
    return _sanitize_developer_job(raw_out, meta=ownership)


@router.get("/recharges/{job_id}/stream")
async def stream_developer_recharge(
    job_id: str,
    api_key: Dict[str, Any] = Depends(require_api_key),
) -> StreamingResponse:
    db = get_db_optional()
    ownership = db.get_api_task_ownership(job_id, api_key_id=str(api_key["id"])) if db is not None else None
    job = get_job(job_id) if ownership is not None else None
    if ownership is None or job is None:
        async def not_found():
            yield _sse({"type": "error", "code": "not_found", "message": "充值任务不存在或不属于当前 API Key"})

        return StreamingResponse(
            not_found(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    initial = _quick_pay_out(job, admitted=True).model_dump()
    return StreamingResponse(
        _quick_pay_stream(
            job_id,
            initial,
            transform=lambda snap: _sanitize_developer_job(snap, meta=ownership),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/account/subscription", response_model=AccountSubscriptionOut)
async def developer_account_subscription(
    payload: AccountSubscriptionRequest,
    api_key: Dict[str, Any] = Depends(require_api_key),
) -> AccountSubscriptionOut:
    del api_key
    try:
        credentials = parse_credentials(payload.session)
    except DomainError as exc:
        raise _account_http(exc) from None
    email = credentials["email"]
    account_id = payload.account_id.strip() or credentials["account_id"]
    try:
        if payload.include_invoices:
            merged = await fetch_snapshot(
                payload.session,
                credentials["access_token"],
                account_id=account_id,
                country=credentials["session_country"] or "US",
            )
        else:
            merged = await fetch_basic_snapshot(
                payload.session,
                credentials["access_token"],
                account_id=account_id,
                country=credentials["session_country"] or "US",
            )
    except DomainError as exc:
        raise _account_http(exc) from None
    detail = normalize_account_detail(
        merged["entitlements"],
        merged["subscriptions"],
        email=email,
        account_id=account_id,
        warning=merged["warning"],
        invoices=merged.get("invoices"),
    )
    return AccountSubscriptionOut(**detail)


@router.post("/account/cancel-auto-renew")
async def developer_cancel_auto_renew(
    payload: OpenApiCancelRenewIn,
    api_key: Dict[str, Any] = Depends(require_api_key),
) -> Dict[str, Any]:
    del api_key
    try:
        credentials = parse_credentials(payload.session)
    except DomainError as exc:
        raise _account_http(exc) from None
    raw_proxy = configured_proxy("account") or configured_proxy("checkout") or ""
    proxy = align_proxy_region(raw_proxy, credentials["session_country"] or "US") if raw_proxy else ""
    result = await asyncio.to_thread(
        cancel_account_auto_renew,
        payload.session,
        plan=_normalize_plan(payload.plan) or payload.plan,
        proxy=proxy or None,
        account_id=payload.account_id.strip() or credentials["account_id"],
    )
    return {
        "canceled": bool(result.get("canceled")),
        "already_canceled": bool(result.get("already_canceled")),
        "stage": result.get("stage") or "",
        "message": result.get("message") or "",
    }


# ── Admin API Key Management Endpoints (`/api/developer/*`) ──────────────
@admin_router.get("/keys")
async def admin_list_api_keys() -> Dict[str, Any]:
    db = get_db_optional()
    if db is None:
        return {"items": [], "vault_retention_days": vault_retention_days()}
    rows = db.list_api_keys()
    return {
        "items": [_serialize_api_key(row).model_dump() for row in rows],
        "vault_retention_days": vault_retention_days(),
    }


@admin_router.post("/keys", response_model=ApiKeySecretOut, status_code=201)
async def admin_create_api_key(payload: ApiKeyCreateRequest, response: Response) -> ApiKeySecretOut:
    response.headers["Cache-Control"] = "no-store"
    db = get_db_optional()
    if db is None:
        raise HTTPException(503, detail={"code": "db_unavailable", "message": "本地存储暂不可用。"})
    plans = _normalize_allowed_plans(payload.allowed_plans)
    raw_key, prefix, key_hash = _mint_api_key()
    key_id = f"key_{uuid.uuid4().hex[:12]}"
    created = db.create_api_key(
        key_id=key_id,
        name=payload.name.strip(),
        key_prefix=prefix,
        key_hash=key_hash,
        rate_limit_rpm=payload.rate_limit_rpm,
        allowed_plans=plans,
        note=payload.note.strip(),
    )
    return ApiKeySecretOut(key=_serialize_api_key(created), api_key=raw_key)


@admin_router.patch("/keys/{key_id}", response_model=ApiKeyOut)
async def admin_update_api_key(key_id: str, payload: ApiKeyUpdateRequest) -> ApiKeyOut:
    db = get_db_optional()
    if db is None:
        raise HTTPException(503, detail={"code": "db_unavailable", "message": "本地存储暂不可用。"})
    existing = db.get_api_key_by_id(key_id)
    if existing is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "API Key 不存在。"})
    if existing.get("status") == "revoked" and payload.status is not None:
        raise HTTPException(409, detail={"code": "key_revoked", "message": "已吊销的 API Key 不能重新启用，请新建或轮换密钥。"})
    plans = _normalize_allowed_plans(payload.allowed_plans) if payload.allowed_plans is not None else None
    updated = db.update_api_key(
        key_id,
        name=payload.name.strip() if payload.name is not None else None,
        status=payload.status,
        rate_limit_rpm=payload.rate_limit_rpm,
        allowed_plans=plans,
        note=payload.note.strip() if payload.note is not None else None,
    )
    if updated is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "API Key 不存在。"})
    return _serialize_api_key(updated)


@admin_router.post("/keys/{key_id}/rotate", response_model=ApiKeySecretOut)
async def admin_rotate_api_key(key_id: str, response: Response) -> ApiKeySecretOut:
    response.headers["Cache-Control"] = "no-store"
    db = get_db_optional()
    if db is None:
        raise HTTPException(503, detail={"code": "db_unavailable", "message": "本地存储暂不可用。"})
    raw_key, prefix, key_hash = _mint_api_key()
    updated = db.rotate_api_key(key_id, key_prefix=prefix, key_hash=key_hash)
    if updated is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "API Key 不存在。"})
    with _RATE_LOCK:
        _RATE_BUCKETS.pop(key_id, None)
    return ApiKeySecretOut(key=_serialize_api_key(updated), api_key=raw_key)


@admin_router.delete("/keys/{key_id}", response_model=ApiKeyOut)
async def admin_revoke_api_key(key_id: str) -> ApiKeyOut:
    db = get_db_optional()
    if db is None:
        raise HTTPException(503, detail={"code": "db_unavailable", "message": "本地存储暂不可用。"})
    updated = db.revoke_api_key(key_id)
    if updated is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "API Key 不存在。"})
    with _RATE_LOCK:
        _RATE_BUCKETS.pop(key_id, None)
    return _serialize_api_key(updated)


__all__ = ["router", "admin_router", "require_api_key"]
