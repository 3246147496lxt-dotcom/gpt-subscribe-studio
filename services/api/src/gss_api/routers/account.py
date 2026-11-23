"""Account subscription detail: cached read path plus an explicit refresh."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from gss_contracts.errors import DomainError, ErrorCode

from ..account_detail import fetch_snapshot, normalize, parse_credentials
from ..persistence import get_db_optional
from ..schemas import AccountSubscriptionOut, AccountSubscriptionRequest

router = APIRouter(prefix="/api", tags=["account"])


def _http(exc: DomainError) -> HTTPException:
    code = str(exc.code)
    status = 401 if exc.code is ErrorCode.INVALID_SESSION else 502
    detail = dict(exc.detail or {})
    return HTTPException(status, detail={
        "code": detail.get("code") or code,
        "message": exc.message,
        "detail": detail or None,
    })


@router.post("/account/subscription-detail", response_model=AccountSubscriptionOut)
async def subscription_detail(payload: AccountSubscriptionRequest) -> AccountSubscriptionOut:
    try:
        credentials = parse_credentials(payload.session)
    except DomainError as exc:
        raise _http(exc) from None

    email = credentials["email"]
    account_id = payload.account_id.strip() or credentials["account_id"]
    key = account_id or email.lower()
    db = get_db_optional()

    # Cached first: a read must not spend an upstream request unless asked.
    if db is not None and key and not payload.force_refresh:
        cached = db.get_account_snapshot(key)
        if cached:
            return AccountSubscriptionOut(**{**cached, "cached": True, "stale": False})

    try:
        merged = await fetch_snapshot(
            payload.session,
            credentials["access_token"],
            account_id=account_id,
            country=credentials["session_country"] or "US",
        )
    except DomainError as exc:
        if db is not None and key:
            cached = db.get_account_snapshot(key)
            if cached and exc.code is not ErrorCode.INVALID_SESSION:
                return AccountSubscriptionOut(**{
                    **cached, "cached": True, "stale": True,
                    "warning": "上游暂时不可用，显示上次查询结果。",
                })
        raise _http(exc) from None

    detail = normalize(merged, email=email, account_id=account_id)
    if db is not None and key:
        db.save_account_snapshot(key, email=email, account_id=account_id,
                                 detail=detail, queried_at=detail["queriedAt"])
    return AccountSubscriptionOut(**detail)
