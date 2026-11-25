"""Webhook alert configuration and test delivery endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..schemas import (
    WebhookConfigIn,
    WebhookConfigOut,
    WebhookDeliveryOut,
    WebhookSettingsOut,
    WebhookTestOut,
)
from ..services.webhooks import manager

router = APIRouter(prefix="/api/settings/webhooks", tags=["settings"])


@router.get("", response_model=WebhookSettingsOut)
async def get_webhooks() -> WebhookSettingsOut:
    cfg = manager.load()
    deliveries = [WebhookDeliveryOut(**item) for item in manager.list_deliveries()]
    return WebhookSettingsOut(config=WebhookConfigOut(**cfg.public()), deliveries=deliveries)


@router.put("", response_model=WebhookConfigOut)
async def save_webhooks(payload: WebhookConfigIn) -> WebhookConfigOut:
    try:
        saved = manager.save(
            enabled=payload.enabled,
            url=payload.url if "url" in payload.model_fields_set else None,
            secret=payload.secret,
            clear_secret=payload.clear_secret,
            events=payload.events,
        )
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid_webhook_config", "message": str(exc)}) from None
    except OSError:
        raise HTTPException(500, detail={"code": "webhook_save_failed", "message": "Webhook 配置保存失败，请检查配置目录权限"}) from None
    return WebhookConfigOut(**saved.public())


@router.post("/test", response_model=WebhookTestOut)
async def test_webhook() -> WebhookTestOut:
    result = await manager.send_test()
    delivery = WebhookDeliveryOut(**result["delivery"]) if result.get("delivery") else None
    return WebhookTestOut(
        ok=bool(result["ok"]),
        status_code=result.get("status_code"),
        elapsed_ms=int(result.get("elapsed_ms") or 0),
        error=result.get("error"),
        delivery=delivery,
    )
