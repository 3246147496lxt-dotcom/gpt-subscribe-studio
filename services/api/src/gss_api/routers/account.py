"""Account subscription detail: cached read path plus an explicit refresh."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from gss_contracts.errors import DomainError, ErrorCode

from ..account_detail import (CACHE_VERSION, fetch_snapshot, fetch_stripe_portal,
                              normalize, parse_credentials)
from ..persistence import get_db_optional
from ..schemas import (AccountSubscriptionOut, AccountSubscriptionRequest,
                       StripePortalOut, StripePortalRequest)

router = APIRouter(prefix="/api", tags=["account"])


def _http(exc: DomainError) -> HTTPException:
    code = str(exc.code)
    detail = dict(exc.detail or {})
    wire = detail.get("code")
    if exc.code is ErrorCode.INVALID_SESSION:
        status = 401
    elif wire in {"cloudflare_challenge", "upstream_forbidden"}:
        status = 403
    else:
        status = 502
    return HTTPException(status, detail={
        "code": wire or code,
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

    # Always fetch fresh live data on manual account queries; never serve cached or stale snapshots.
    try:
        merged = await fetch_snapshot(
            payload.session,
            credentials["access_token"],
            account_id=account_id,
            country=credentials["session_country"] or "US",
        )
    except DomainError as exc:
        raise _http(exc) from None

    detail = normalize(merged, email=email, account_id=account_id)
    if db is not None and key:
        db.save_account_snapshot(key, email=email, account_id=account_id,
                                 detail=detail, queried_at=detail["queriedAt"])
    return AccountSubscriptionOut(**detail)


@router.post("/account/stripe-portal", response_model=StripePortalOut)
async def stripe_portal(payload: StripePortalRequest) -> StripePortalOut:
    try:
        credentials = parse_credentials(payload.session)
    except DomainError as exc:
        raise _http(exc) from None
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
        raise _http(exc) from None
    return StripePortalOut(**result)
