"""Webhook & Telegram alert configuration and test delivery endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

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
    fields = payload.model_fields_set
    try:
        saved = manager.save(
            enabled=payload.enabled if "enabled" in fields else None,
            url=payload.url if "url" in fields else None,
            secret=payload.secret if "secret" in fields else None,
            clear_secret=payload.clear_secret,
            events=payload.events if "events" in fields else None,
            telegram_enabled=payload.telegram_enabled if "telegram_enabled" in fields else None,
            telegram_bot_token=payload.telegram_bot_token if "telegram_bot_token" in fields else None,
            clear_telegram_token=payload.clear_telegram_token,
            telegram_chat_id=payload.telegram_chat_id if "telegram_chat_id" in fields else None,
            telegram_thread_id=payload.telegram_thread_id if "telegram_thread_id" in fields else None,
            clear_telegram_thread_id=payload.clear_telegram_thread_id,
            telegram_api_base=payload.telegram_api_base if "telegram_api_base" in fields else None,
            telegram_silent=payload.telegram_silent if "telegram_silent" in fields else None,
        )
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid_webhook_config", "message": str(exc)}) from None
    except OSError:
        raise HTTPException(500, detail={"code": "webhook_save_failed", "message": "告警通知配置保存失败，请检查配置目录权限"}) from None
    return WebhookConfigOut(**saved.public())


@router.post("/test", response_model=WebhookTestOut)
async def test_webhook(
    channel: str = Query(default="all", pattern="^(all|webhook|telegram)$"),
) -> WebhookTestOut:
    result = await manager.send_test(channel=channel)
    delivery = WebhookDeliveryOut(**result["delivery"]) if result.get("delivery") else None
    deliveries = [WebhookDeliveryOut(**item) for item in (result.get("deliveries") or [])]
    return WebhookTestOut(
        ok=bool(result["ok"]),
        status_code=result.get("status_code"),
        elapsed_ms=int(result.get("elapsed_ms") or 0),
        error=result.get("error"),
        delivery=delivery,
        deliveries=deliveries,
    )
