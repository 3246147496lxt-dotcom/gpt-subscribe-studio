"""Read and manually release unknown-payment locks."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import locks
from ..persistence import get_db_optional

router = APIRouter(prefix="/api/payments/locks", tags=["payment-locks"])


class ReleaseRequest(BaseModel):
    confirm_no_charge: bool = False
    note: str = Field(default="", max_length=500)
    operator: str = Field(default="operator", max_length=80)


@router.get("/{lock_id}")
async def get_lock(lock_id: str) -> dict:
    db = get_db_optional()
    row = db.get_lock(lock_id) if db else None
    if row is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "锁不存在"})
    return row


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
