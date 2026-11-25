"""Read-only system self-diagnostics and safe database maintenance endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..persistence import get_db_optional
from ..schemas import MaintenanceOut, SystemDiagnosticsOut
from ..services.diagnostics import collect_diagnostics

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/diagnostics", response_model=SystemDiagnosticsOut)
async def system_diagnostics(request: Request) -> SystemDiagnosticsOut:
    gateway_store = getattr(request.app.state, "gateway_store", None)
    return collect_diagnostics(gateway_store=gateway_store)


@router.post("/maintenance", response_model=MaintenanceOut)
async def system_maintenance(retention_days: int = Query(default=90, ge=7, le=3650)) -> MaintenanceOut:
    db = get_db_optional()
    if db is None:
        raise HTTPException(503, detail={"code": "db_unavailable", "message": "持久化数据库不可用"})
    result = db.run_maintenance(retention_days=retention_days)
    return MaintenanceOut(
        retention_days=result["retention_days"],
        pruned_events=result["pruned_events"],
        db_bytes=result["db_bytes"],
        wal_bytes=result["wal_bytes"],
        wal_checkpoint=result.get("wal_checkpoint"),
        table_counts=result["table_counts"],
    )
