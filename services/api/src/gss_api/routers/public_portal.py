"""Public customer-facing portal APIs (`/api/public/*`) and admin site-config endpoint.

Strict privacy boundary:
- Never exposes other users' orders/tasks (strict user_id or 7-day guest token check).
- Never exposes internal proxy IPs, proxy SIDs, billing files, locks, or raw upstream payloads.
- Never persists raw sessions, cards, cookies, or tokens.
"""
from __future__ import annotations

import asyncio
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ChatGPTClient
from gss_core.config import ORDER_HTTP_TIMEOUT_S, use_fake_adapters
from gss_core.proxies import align_proxy_region, configured_proxy
from gss_core.session_token import email_from_access_token, parse_session_bundle

from .. import locks
from ..account_detail import (
    CACHE_VERSION,
    fetch_basic_snapshot,
    fetch_billing_history,
    fetch_snapshot,
    fetch_stripe_portal,
    normalize,
    parse_credentials,
)
from ..auto_renew import cancel_account_auto_renew
from ..persistence import get_db_optional
from ..portal_auth import (
    GUEST_TTL_DAYS,
    clear_user_cookie,
    extract_guest_token,
    extract_user_id,
    guest_expiry_iso,
    hash_guest_token,
    hash_password,
    mint_guest_token,
    set_guest_cookie,
    set_user_cookie,
    validate_password,
    validate_username,
    verify_password,
)
from ..portal_config import load_portal_config, save_portal_config
from ..schemas import (
    AccountBillingHistoryOut,
    AccountBillingHistoryRequest,
    AccountSubscriptionOut,
    AccountSubscriptionRequest,
    CardIn,
    ModeIn,
    QuickPayRequest,
    StripePortalOut,
    StripePortalRequest,
)
from ..services import reference_pricing
from ..services.order_flow import get_job, start_quick_pay
from ..store import store
from .account import _http as _account_http
from .events import _quick_pay_stream, _sse
from .quick_pay import _out as _quick_pay_out

router = APIRouter(prefix="/api/public", tags=["public-portal"])
admin_router = APIRouter(prefix="/api/settings", tags=["settings"])

_SUPPORTED_PUBLIC_PLANS = frozenset({"go", "plus", "pro_lite", "pro", "pro_max"})


def _mask_email(email: str) -> str:
    text = str(email or "").strip()
    if "@" not in text:
        return ""
    local, domain = text.split("@", 1)
    if not local or not domain:
        return ""
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[:2]}***@{domain}"


def _extract_masked_email(session_raw: str) -> str:
    try:
        bundle = parse_session_bundle(session_raw)
        if bundle.access_token:
            found = email_from_access_token(bundle.access_token)
            if found:
                return _mask_email(found)
    except Exception:  # noqa: BLE001
        pass
    try:
        import json
        parsed = json.loads(session_raw)
        if isinstance(parsed, dict):
            user = parsed.get("user") if isinstance(parsed.get("user"), dict) else {}
            email = str(user.get("email") or parsed.get("email") or "").strip()
            if email:
                return _mask_email(email)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _sanitize_public_job(raw: Dict[str, Any], *, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Strip operator-only telemetry (proxy SID/exit IP/diagnostics internals)."""
    next_action = raw.get("next_action")
    safe_next: Optional[Dict[str, Any]] = None
    if isinstance(next_action, dict):
        safe_next = {
            k: v for k, v in next_action.items()
            if k in {"type", "redirect_url", "message"} and v is not None
        } or None
    return {
        "job_id": raw.get("job_id") or "",
        "order_id": raw.get("order_id"),
        "status": raw.get("status") or "queued",
        "stage": raw.get("stage") or "queued",
        "queue_position": raw.get("queue_position"),
        "amount": raw.get("amount"),
        "currency": raw.get("currency"),
        "failure_code": raw.get("failure_code"),
        "message": raw.get("message"),
        "auto_renew_status": raw.get("auto_renew_status") or "disabled",
        "auto_renew_canceled_at": raw.get("auto_renew_canceled_at"),
        "auto_renew_error": raw.get("auto_renew_error"),
        "checkout_outcome": raw.get("checkout_outcome"),
        "next_action": safe_next,
        "events": raw.get("events") or [],
        "plan": (meta or {}).get("plan"),
        "exit_country": (meta or {}).get("exit_country"),
        "masked_email": (meta or {}).get("masked_email"),
        "created_at": (meta or {}).get("created_at"),
    }


def _resolve_viewer(request: Request) -> tuple[Optional[dict[str, Any]], str]:
    db = get_db_optional()
    user_id = extract_user_id(request)
    user = db.get_portal_user_by_id(user_id) if (db is not None and user_id) else None
    guest_token = extract_guest_token(request)
    guest_hash = hash_guest_token(guest_token) if guest_token else ""
    return user, guest_hash


# ── schemas ──────────────────────────────────────────────────────────────
class AuthCredentialsIn(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128, repr=False)


class PublicQuickPayIn(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    plan: str
    exit_country: Optional[str] = None
    session: str = Field(min_length=10, repr=False)
    card: CardIn = Field(repr=False)
    auto_cancel_renew: bool = True


class CancelAutoRenewIn(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    session: str = Field(min_length=10, repr=False)
    plan: str = ""
    account_id: str = Field(default="", alias="accountId")


class PortalSiteConfigUpdateIn(BaseModel):
    brand_name: Optional[str] = None
    brand_name_en: Optional[str] = None
    footer_slogan: Optional[str] = None
    footer_slogan_en: Optional[str] = None
    hero_title: Optional[str] = None
    hero_title_en: Optional[str] = None
    hero_subtitle_1: Optional[str] = None
    hero_subtitle_1_en: Optional[str] = None
    hero_subtitle_2: Optional[str] = None
    hero_subtitle_2_en: Optional[str] = None
    card_shop_url: Optional[str] = None
    card_shop_label: Optional[str] = None
    card_shop_label_en: Optional[str] = None
    support_url: Optional[str] = None
    support_label: Optional[str] = None
    support_label_en: Optional[str] = None
    support_note: Optional[str] = None
    support_note_en: Optional[str] = None
    usd_cny_rate: Optional[float] = None
    price_updated_at: Optional[str] = None
    price_note: Optional[str] = None
    price_note_en: Optional[str] = None
    announcements: Optional[List[Any]] = None


class LocalPriceOverrideIn(BaseModel):
    plan: str = Field(min_length=1, max_length=32)
    country: str = Field(min_length=2, max_length=2)
    currency: str = Field(min_length=3, max_length=6)
    amount: float = Field(gt=0)


# ── site config ──────────────────────────────────────────────────────────
@router.get("/site-config")
async def get_public_site_config() -> Dict[str, Any]:
    cfg = reference_pricing.apply_to_site_config(load_portal_config())
    return {
        **cfg,
        "connected": True,
        "guest_ttl_days": GUEST_TTL_DAYS,
    }


@admin_router.get("/portal-site")
async def get_admin_portal_site_config() -> Dict[str, Any]:
    return reference_pricing.apply_to_site_config(load_portal_config())


@admin_router.put("/portal-site")
async def update_admin_portal_site_config(payload: PortalSiteConfigUpdateIn) -> Dict[str, Any]:
    patch = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
    return reference_pricing.apply_to_site_config(save_portal_config(patch))


@admin_router.post("/reference-prices/refresh")
async def refresh_admin_reference_prices() -> Dict[str, Any]:
    status_out = await asyncio.to_thread(reference_pricing.refresh_fx_sync)
    cfg = reference_pricing.apply_to_site_config(load_portal_config())
    return {
        "status": status_out,
        "reference_prices": cfg.get("reference_prices") or {},
        "reference_price_status": cfg.get("reference_price_status") or {},
    }


@admin_router.put("/reference-prices/local")
async def update_admin_local_price(payload: LocalPriceOverrideIn) -> Dict[str, Any]:
    ok = reference_pricing.record_observed_price(
        plan=payload.plan,
        country=payload.country,
        currency=payload.currency,
        amount=payload.amount,
        source="manual",
    )
    if not ok:
        raise HTTPException(400, detail={"code": "invalid_local_price", "message": "套餐或地区价格参数无效。"})
    cfg = reference_pricing.apply_to_site_config(load_portal_config())
    return {
        "reference_prices": cfg.get("reference_prices") or {},
        "reference_price_status": cfg.get("reference_price_status") or {},
    }


# ── customer auth (cross-device sync + guest task claiming) ──────────────
@router.get("/auth/me")
async def auth_me(request: Request) -> Dict[str, Any]:
    user, guest_hash = _resolve_viewer(request)
    return {
        "authenticated": user is not None,
        "username": user["username"] if user else None,
        "user_id": user["id"] if user else None,
        "has_guest_token": bool(guest_hash),
        "guest_ttl_days": GUEST_TTL_DAYS,
    }


@router.post("/auth/register")
async def auth_register(payload: AuthCredentialsIn, request: Request, response: Response) -> Dict[str, Any]:
    db = get_db_optional()
    if db is None:
        raise HTTPException(503, detail={"code": "db_unavailable", "message": "本地存储暂不可用，请稍后重试。"})
    try:
        username = validate_username(payload.username)
        password = validate_password(payload.password)
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid_credentials", "message": str(exc)}) from None
    user_id = f"usr_{uuid.uuid4().hex[:16]}"
    created = db.create_portal_user(
        user_id=user_id,
        username=username,
        password_hash=hash_password(password),
    )
    if created is None:
        raise HTTPException(409, detail={"code": "username_exists", "message": "该用户名已被注册，请直接登录或更换用户名。"})
    guest_token = extract_guest_token(request)
    guest_hash = hash_guest_token(guest_token) if guest_token else ""
    claimed = db.claim_guest_tasks(guest_token_hash=guest_hash, user_id=user_id) if guest_hash else 0
    set_user_cookie(response, request, user_id)
    return {
        "authenticated": True,
        "username": username,
        "user_id": user_id,
        "claimed_tasks": claimed,
        "guest_ttl_days": GUEST_TTL_DAYS,
    }


@router.post("/auth/login")
async def auth_login(payload: AuthCredentialsIn, request: Request, response: Response) -> Dict[str, Any]:
    db = get_db_optional()
    if db is None:
        raise HTTPException(503, detail={"code": "db_unavailable", "message": "本地存储暂不可用，请稍后重试。"})
    try:
        username = validate_username(payload.username)
    except ValueError:
        raise HTTPException(401, detail={"code": "invalid_credentials", "message": "用户名或密码不正确。"}) from None
    user = db.get_portal_user_by_username(username)
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(401, detail={"code": "invalid_credentials", "message": "用户名或密码不正确。"})
    db.touch_portal_user_login(user["id"])
    guest_token = extract_guest_token(request)
    guest_hash = hash_guest_token(guest_token) if guest_token else ""
    claimed = db.claim_guest_tasks(guest_token_hash=guest_hash, user_id=user["id"]) if guest_hash else 0
    set_user_cookie(response, request, user["id"])
    return {
        "authenticated": True,
        "username": user["username"],
        "user_id": user["id"],
        "claimed_tasks": claimed,
        "guest_ttl_days": GUEST_TTL_DAYS,
    }


@router.post("/auth/logout")
async def auth_logout(request: Request, response: Response) -> Dict[str, Any]:
    clear_user_cookie(response, request)
    return {"authenticated": False}


# ── public quick-pay & scoped task history ───────────────────────────────
@router.post("/quick-pay", status_code=202)
async def public_quick_pay(
    payload: PublicQuickPayIn,
    request: Request,
    response: Response,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> Dict[str, Any]:
    plan_norm = str(payload.plan or "").strip().lower()
    if plan_norm == "codex":
        raise HTTPException(
            400,
            detail={"code": "plan_coming_soon", "message": "Codex 点数充值即将开放，暂不支持提交。"},
        )
    if plan_norm not in _SUPPORTED_PUBLIC_PLANS:
        raise HTTPException(
            400,
            detail={"code": "unsupported_plan", "message": "暂不支持该订阅套餐。"},
        )
    key = (idempotency_key or "").strip()
    if not key or len(key) > 255:
        raise HTTPException(
            400,
            detail={"code": "idempotency_conflict", "message": "缺少有效的 Idempotency-Key。"},
        )
    user, existing_guest_hash = _resolve_viewer(request)
    guest_token = extract_guest_token(request)
    minted_token: Optional[str] = None
    if not guest_token:
        guest_token = mint_guest_token()
        minted_token = guest_token
    guest_hash = hash_guest_token(guest_token)
    set_guest_cookie(response, request, guest_token)

    quick_req = QuickPayRequest(
        plan=plan_norm,
        exit_country=(payload.exit_country or "").strip().upper() or None,
        session=payload.session,
        card=payload.card,
        mode=ModeIn(
            account_source="customer_session",
            card_source="customer",
            billing_mode="standard",
            risk_acknowledged=True,
        ),
        auto_cancel_renew=bool(payload.auto_cancel_renew),
        debug_checkout_only=False,
    )
    fingerprint = locks.account_fingerprint(payload.session)
    job = start_quick_pay(quick_req, key, fingerprint)
    masked_email = _extract_masked_email(payload.session)

    db = get_db_optional()
    created_at = datetime.now(timezone.utc).isoformat()
    if db is not None:
        db.bind_portal_task(
            job_id=job.id,
            owner_user_id=user["id"] if user else None,
            guest_token_hash=guest_hash or existing_guest_hash or None,
            guest_expires_at=guest_expiry_iso(),
            masked_email=masked_email,
            plan=plan_norm,
            exit_country=quick_req.exit_country or "",
        )

    raw_out = _quick_pay_out(job, admitted=True).model_dump()
    result = _sanitize_public_job(
        raw_out,
        meta={
            "plan": plan_norm,
            "exit_country": quick_req.exit_country or "",
            "masked_email": masked_email,
            "created_at": created_at,
        },
    )
    if minted_token:
        result["guest_token"] = minted_token
    return result


@router.get("/tasks")
async def list_public_tasks(
    request: Request,
    q: str = Query(default="", max_length=100),
    page: int = Query(default=1, ge=1, le=500),
    page_size: int = Query(default=8, ge=1, le=50),
) -> Dict[str, Any]:
    db = get_db_optional()
    user, guest_hash = _resolve_viewer(request)
    if db is None or (user is None and not guest_hash):
        return {
            "items": [],
            "total": 0,
            "page": 1,
            "page_size": page_size,
            "total_pages": 1,
            "authenticated": user is not None,
            "username": user["username"] if user else None,
        }
    offset = (page - 1) * page_size
    rows, total = db.list_portal_tasks(
        owner_user_id=user["id"] if user else None,
        guest_token_hash=guest_hash or None,
        search=q,
        limit=page_size,
        offset=offset,
    )
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    items = [
        {
            "job_id": row["job_id"],
            "order_id": row.get("order_id"),
            "masked_email": row.get("masked_email") or "",
            "plan": row.get("plan") or "",
            "exit_country": row.get("exit_country") or "",
            "status": row.get("status") or "queued",
            "stage": row.get("stage") or "queued",
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
        "authenticated": user is not None,
        "username": user["username"] if user else None,
    }


@router.get("/tasks/{job_id}")
async def get_public_task(job_id: str, request: Request) -> Dict[str, Any]:
    db = get_db_optional()
    if db is None:
        raise HTTPException(503, detail={"code": "db_unavailable", "message": "暂时无法读取任务状态。"})
    user, guest_hash = _resolve_viewer(request)
    ownership = db.get_portal_task_ownership(
        job_id,
        owner_user_id=user["id"] if user else None,
        guest_token_hash=guest_hash or None,
    )
    if ownership is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "任务不存在或无权访问。"})
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "任务不存在。"})
    raw_out = _quick_pay_out(job).model_dump()
    return _sanitize_public_job(raw_out, meta=ownership)


@router.get("/tasks/{job_id}/stream")
async def stream_public_task(job_id: str, request: Request) -> StreamingResponse:
    db = get_db_optional()
    user, guest_hash = _resolve_viewer(request)
    ownership = (
        db.get_portal_task_ownership(
            job_id,
            owner_user_id=user["id"] if user else None,
            guest_token_hash=guest_hash or None,
        )
        if db is not None
        else None
    )
    job = get_job(job_id) if ownership is not None else None
    if ownership is None or job is None:
        async def not_found():
            yield _sse({"type": "error", "code": "not_found", "message": "任务不存在或无权访问"})
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
            transform=lambda snap: _sanitize_public_job(snap, meta=ownership),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ── public account subscription detail, Stripe portal, & cancel auto-renew ─
@router.post("/account/subscription-detail", response_model=AccountSubscriptionOut)
async def public_subscription_detail(payload: AccountSubscriptionRequest) -> AccountSubscriptionOut:
    try:
        credentials = parse_credentials(payload.session)
    except DomainError as exc:
        raise _account_http(exc) from None
    email = credentials["email"]
    account_id = payload.account_id.strip() or credentials["account_id"]
    key = account_id or email.lower()
    db = get_db_optional()
    existing_cached = db.get_account_snapshot(key) if (db is not None and key) else None
    if existing_cached and existing_cached.get("cacheVersion") == CACHE_VERSION and not payload.force_refresh:
        cached_out = {**existing_cached, "cached": True, "stale": False}
        if not cached_out.get("invoices") and cached_out.get("billingSource") in {"loading", "stripe_portal_timeout"}:
            cached_out["billingSource"] = "loading"
            cached_out["billingNote"] = None
        return AccountSubscriptionOut(**cached_out)
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
        if existing_cached and existing_cached.get("cacheVersion") == CACHE_VERSION and exc.code is not ErrorCode.INVALID_SESSION:
            return AccountSubscriptionOut(**{
                **existing_cached, "cached": True, "stale": True,
                "warning": "上游暂时不可用，显示上次查询结果。",
            })
        raise _account_http(exc) from None
    detail = normalize(merged, email=email, account_id=account_id)
    # Persist updated basic snapshot while never wiping previously verified invoices on a deferred/timeout run.
    if db is not None and key:
        to_save = dict(detail)
        if (
            not to_save["invoices"]
            and to_save.get("billingSource") in {"loading", "stripe_portal_timeout"}
            and existing_cached
            and existing_cached.get("invoices")
        ):
            to_save["invoices"] = existing_cached["invoices"]
            to_save["billingSource"] = existing_cached.get("billingSource") or "stripe_portal"
            to_save["billingNote"] = None
            if payload.include_invoices:
                detail = dict(to_save)
        db.save_account_snapshot(
            key, email=email, account_id=account_id, detail=to_save, queried_at=to_save["queriedAt"],
        )
    return AccountSubscriptionOut(**detail)


@router.post("/account/billing-history", response_model=AccountBillingHistoryOut)
async def public_billing_history(payload: AccountBillingHistoryRequest) -> AccountBillingHistoryOut:
    try:
        credentials = parse_credentials(payload.session)
    except DomainError as exc:
        raise _account_http(exc) from None
    email = credentials["email"]
    account_id = payload.account_id.strip() or credentials["account_id"]
    key = account_id or email.lower()
    db = get_db_optional()
    try:
        result = await fetch_billing_history(
            payload.session,
            credentials["access_token"],
            account_id=account_id,
            country=credentials["session_country"] or "US",
            default_plan=payload.plan_name,
            fallback_currency=payload.currency,
        )
    except DomainError as exc:
        raise _account_http(exc) from None

    # Persist verified results into the account snapshot so subsequent opens are instant.
    if db is not None and key and result.get("verified"):
        cached = db.get_account_snapshot(key)
        if cached:
            updated = {
                **cached,
                "invoices": result["invoices"],
                "billingSource": result["billingSource"],
                "billingNote": result["billingNote"],
                "currency": result["currency"] if result["currency"] != "-" else cached.get("currency", "-"),
                "cacheVersion": CACHE_VERSION,
            }
            db.save_account_snapshot(
                key,
                email=updated.get("email") or email,
                account_id=updated.get("accountId") or account_id,
                detail=updated,
                queried_at=updated.get("queriedAt") or "",
            )
    return AccountBillingHistoryOut(
        invoices=result["invoices"],
        billingSource=result["billingSource"],
        billingNote=result["billingNote"],
        currency=result["currency"],
        retryable=bool(result.get("retryable")),
    )


@router.post("/account/stripe-portal", response_model=StripePortalOut)
async def public_stripe_portal(payload: StripePortalRequest) -> StripePortalOut:
    try:
        credentials = parse_credentials(payload.session)
    except DomainError as exc:
        raise _account_http(exc) from None
    account_id = payload.account_id.strip() or credentials["account_id"]
    try:
        result = await asyncio.to_thread(
            fetch_stripe_portal,
            payload.session,
            credentials["access_token"],
            account_id=account_id,
            country=credentials["session_country"] or "US",
        )
    except DomainError as exc:
        raise _account_http(exc) from None
    return StripePortalOut(**result)


def _run_cancel_auto_renew(session: str, *, plan: str = "", account_id: str = "") -> Dict[str, Any]:
    return cancel_account_auto_renew(
        session,
        plan=plan,
        account_id=account_id,
    )


@router.post("/account/cancel-auto-renew")
async def public_cancel_auto_renew(payload: CancelAutoRenewIn) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(
            _run_cancel_auto_renew,
            payload.session,
            plan=payload.plan,
            account_id=payload.account_id,
        )
    except DomainError as exc:
        raise _account_http(exc) from None


@router.post("/tasks/{job_id}/cancel-auto-renew")
async def public_retry_task_cancel_auto_renew(
    job_id: str,
    payload: CancelAutoRenewIn,
    request: Request,
) -> Dict[str, Any]:
    db = get_db_optional()
    if db is None:
        raise HTTPException(503, detail={"code": "db_unavailable", "message": "本地存储暂不可用。"})
    user, guest_hash = _resolve_viewer(request)
    ownership = db.get_portal_task_ownership(
        job_id,
        owner_user_id=user["id"] if user else None,
        guest_token_hash=guest_hash or None,
    )
    if ownership is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "任务不存在或无权访问。"})
    try:
        result = await asyncio.to_thread(
            _run_cancel_auto_renew,
            payload.session,
            plan=payload.plan or str(ownership.get("plan") or ""),
            account_id=payload.account_id,
        )
    except DomainError as exc:
        raise _account_http(exc) from None
    if result.get("canceled"):
        job = get_job(job_id)
        if job is not None and job.order_id:
            order_pair = store.get(job.order_id)
            if order_pair is not None:
                order, view = order_pair
                view.auto_renew_status = "canceled"
                view.auto_renew_canceled_at = datetime.now(timezone.utc).isoformat()
                view.auto_renew_error = None
                store.persist(order.id)
    return result
