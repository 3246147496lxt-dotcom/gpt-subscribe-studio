"""Runtime & protocol feature switches management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from gss_core import runtime_flags

from ..schemas import (
    RuntimeFlagsOut,
    RuntimeFlagsResetIn,
    RuntimeFlagsUpdateIn,
)
from ..services import reconciler

router = APIRouter(prefix="/api/settings/flags", tags=["settings"])


async def _sync_live_services() -> None:
    if reconciler.reconciler_enabled():
        reconciler.start()
    else:
        await reconciler.stop()


@router.get("", response_model=RuntimeFlagsOut)
async def get_runtime_flags() -> RuntimeFlagsOut:
    return RuntimeFlagsOut(**runtime_flags.snapshot())


@router.put("", response_model=RuntimeFlagsOut)
async def update_runtime_flags(payload: RuntimeFlagsUpdateIn) -> RuntimeFlagsOut:
    try:
        snap = runtime_flags.update_flags(payload.updates)
    except ValueError as exc:
        raise HTTPException(
            400, detail={"code": "invalid_runtime_flag", "message": str(exc)},
        ) from None
    except OSError:
        raise HTTPException(
            500, detail={"code": "runtime_flag_save_failed", "message": "策略开关保存失败，请检查配置目录权限"},
        ) from None
    await _sync_live_services()
    return RuntimeFlagsOut(**snap)


@router.post("/reset", response_model=RuntimeFlagsOut)
async def reset_runtime_flags(payload: RuntimeFlagsResetIn | None = None) -> RuntimeFlagsOut:
    keys = payload.keys if payload is not None else None
    try:
        snap = runtime_flags.reset_flags(keys)
    except ValueError as exc:
        raise HTTPException(
            400, detail={"code": "invalid_runtime_flag", "message": str(exc)},
        ) from None
    except OSError:
        raise HTTPException(
            500, detail={"code": "runtime_flag_save_failed", "message": "重置策略开关失败，请检查配置目录权限"},
        ) from None
    await _sync_live_services()
    return RuntimeFlagsOut(**snap)
