"""Read, reconcile, and manually release unknown-payment locks."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import locks
from ..persistence import get_db_optional
from ..schemas import PaymentLocksOut, PaymentLockSummaryOut, ReconcileResultOut

router = APIRouter(prefix="/api/payments/locks", tags=["payment-locks"])


class ReleaseRequest(BaseModel):
    confirm_no_charge: bool = False
    note: str = Field(default="", max_length=500)
    operator: str = Field(default="operator", max_length=80)


def _mask_lock_key(raw: str) -> str:
    text = str(raw or "")
    if text.startswith("account:"):
        fp = text.split(":", 1)[1]
        return f"account:{fp[:12]}..." if len(fp) > 12 else text
    return text[:16] + "..." if len(text) > 16 else text


@router.get("", response_model=PaymentLocksOut)
async def list_locks(
    state: str = Query(default="held", max_length=20),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaymentLocksOut:
    db = get_db_optional()
    if db is None:
        return PaymentLocksOut(counts={"held": 0, "released": 0, "resolved": 0}, total=0, limit=limit, offset=offset, items=[])
    rows, total, counts = db.list_locks(state=state, limit=limit, offset=offset)
    items = [
        PaymentLockSummaryOut(
            id=row["id"],
            lock_key_prefix=_mask_lock_key(row.get("lock_key") or ""),
            lock_type=row.get("lock_type") or "account",
            order_id=row.get("order_id"),
            job_id=row.get("job_id"),
            payment_intent_id=row.get("payment_intent_id"),
            state=row["state"],
            reason=row.get("reason") or "",
            created_at=row["created_at"],
            released_at=row.get("released_at"),
            released_by=row.get("released_by"),
            release_note=row.get("release_note"),
            order_plan=row.get("order_plan"),
            order_status=row.get("order_status"),
            order_payment_status=row.get("order_payment_status"),
            order_delivery_status=row.get("order_delivery_status"),
            order_amount=row.get("order_amount"),
            order_currency=row.get("order_currency"),
            order_billing_country=row.get("order_billing_country"),
        )
        for row in rows
    ]
    return PaymentLocksOut(counts=counts, total=total, limit=limit, offset=offset, items=items)


@router.get("/{lock_id}")
async def get_lock(lock_id: str) -> dict:
    db = get_db_optional()
    row = db.get_lock(lock_id) if db else None
    if row is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "锁不存在"})
    return row


@router.post("/{lock_id}/reconcile", response_model=ReconcileResultOut)
async def reconcile_lock(lock_id: str) -> ReconcileResultOut:
    from ..services import reconciler
    result = await reconciler.reconcile_lock(lock_id, force=True)
    if result.get("outcome") == "lock_not_found":
        raise HTTPException(404, detail={"code": "not_found", "message": "锁不存在"})
    return ReconcileResultOut(**result)


@router.post("/{lock_id}/release")
async def release_lock(lock_id: str, payload: ReleaseRequest) -> dict:
    ok, message, row = locks.release(
        lock_id, confirm_no_charge=payload.confirm_no_charge,
        note=payload.note, operator=payload.operator,
    )
    if not ok:
        raise HTTPException(409, detail={"code": "lock_release_refused", "message": message,
                                         "lock": row})
    return {"ok": True, "message": message, "lock": row}
