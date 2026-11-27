"""Admin settings endpoints for the fixed US tax-free billing address."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from gss_core import fixed_billing

from ..schemas import FixedBillingAddressIn, FixedBillingSettingsOut

router = APIRouter(prefix="/api/settings/fixed-billing", tags=["settings"])


@router.get("", response_model=FixedBillingSettingsOut)
async def get_fixed_billing() -> FixedBillingSettingsOut:
    return FixedBillingSettingsOut(**fixed_billing.fixed_billing_snapshot())


@router.put("", response_model=FixedBillingSettingsOut)
async def save_fixed_billing(payload: FixedBillingAddressIn) -> FixedBillingSettingsOut:
    try:
        fixed_billing.save_fixed_billing_file(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            400, detail={"code": "invalid_fixed_billing", "message": str(exc)},
        ) from None
    except OSError:
        raise HTTPException(
            500,
            detail={
                "code": "fixed_billing_save_failed",
                "message": "固定免税账单保存失败，请检查配置目录或目标文件权限。",
            },
        ) from None
    return FixedBillingSettingsOut(**fixed_billing.fixed_billing_snapshot())


@router.delete("", response_model=FixedBillingSettingsOut)
async def delete_fixed_billing() -> FixedBillingSettingsOut:
    try:
        fixed_billing.delete_fixed_billing_file()
    except ValueError as exc:
        raise HTTPException(
            400, detail={"code": "invalid_fixed_billing", "message": str(exc)},
        ) from None
    except OSError:
        raise HTTPException(
            500,
            detail={
                "code": "fixed_billing_delete_failed",
                "message": "清除固定免税账单失败，请检查配置目录或目标文件权限。",
            },
        ) from None
    return FixedBillingSettingsOut(**fixed_billing.fixed_billing_snapshot())
