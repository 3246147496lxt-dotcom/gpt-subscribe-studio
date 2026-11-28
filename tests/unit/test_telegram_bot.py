"""Tests for interactive Telegram Bot commands, inline buttons, daily digest,
consecutive-failure streak alert, and low-disk alert."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

from fastapi import HTTPException
import httpx
import pytest

from gss_api import locks, persistence
from gss_api.routers import orders as orders_router
from gss_api.services import reconciler, telegram_bot, webhooks
from gss_api.store import InMemoryStore, OrderView
from gss_contracts import Mode, Order, OrderStatus, Plan


@pytest.fixture()
def _isolated_bot(tmp_path, monkeypatch):
    db = persistence.Persistence(":memory:")
    store = InMemoryStore()
    store.attach_db(db)
    monkeypatch.setattr(persistence, "_db", db)
    monkeypatch.setattr(orders_router, "store", store)
    monkeypatch.setattr(locks, "store", store)
    monkeypatch.setenv("GSS_CONFIG_DIR", str(tmp_path / "cfg"))
    mgr = webhooks.WebhookManager(tmp_path / "cfg" / "webhooks.json")
    monkeypatch.setattr(webhooks, "manager", mgr)
    telegram_bot.reset_runtime_monitors()
    cfg = mgr.save(
        telegram_enabled=True,
        telegram_bot_token="123456789:AAExampleFakeTokenForBotUnitTest1234",
        telegram_chat_id="8918520934",
        telegram_bot_commands_enabled=True,
        telegram_digest_enabled=True,
        telegram_digest_hour=22,
        telegram_streak_threshold=3,
        telegram_disk_warn_mb=1024,
        events=list(webhooks.SUPPORTED_EVENTS),
    )
    yield db, store, mgr, cfg
    telegram_bot.reset_runtime_monitors()
    db.close()


def _seed_order(store: InMemoryStore, *, oid: str, status: OrderStatus, plan: Plan = Plan.PLUS,
                payment_status: str = "succeeded", delivery_status: str = "delivered",
                amount: str = "20.00", currency: str = "USD", email: str = "alice@example.com",
                failure_code: str | None = None, auto_cancel_renew: bool = True,
                auto_renew_status: str = "canceled") -> Order:
    order = Order(
        id=oid,
        idempotency_key=f"idem-{oid}",
        purchase_attempt_id=f"pa-{oid}",
        plan=plan,
        billing_country="US",
        mode=Mode(),
        status=status,
        amount=Decimal(amount),
    )
    view = OrderView(
        account_email=email,
        exit_country="PH",
        amount=amount,
        currency=currency,
        payment_status=payment_status,
        delivery_status=delivery_status,
        failure_code=failure_code,
        auto_cancel_renew=auto_cancel_renew,
        auto_renew_status=auto_renew_status,
    )
    store.orders[order.id] = order
    store.views[order.id] = view
    store.persist(order.id)
    return order


def test_bot_rejects_stranger_and_serves_owner_commands(monkeypatch, _isolated_bot):
    db, store, _mgr, cfg = _isolated_bot
    _seed_order(store, oid="d2ccdaba-8c57-49c4-8bc9-c3a64b52b971", status=OrderStatus.DELIVERED,
                plan=Plan.PRO_MAX, amount="200.00")
    _seed_order(store, oid="3b79c362-b60f-4537-ba4b-9831cd57b78d", status=OrderStatus.FAILED,
                payment_status="failed", delivery_status="pending",
                failure_code="sentinel_challenge_failed", auto_renew_status="disabled")

    sent: list[tuple[str, dict]] = []

    async def fake_tg_call(c, method, payload, *, timeout_s=12.0):
        sent.append((method, payload))
        return {"ok": True, "result": {}}

    monkeypatch.setattr(telegram_bot, "_tg_call", fake_tg_call)

    # 1) Unauthorized chat is silently ignored for both message and callback_query
    ok_stranger = asyncio.run(telegram_bot.handle_update(cfg, {
        "message": {"chat": {"id": 999999999}, "text": "/status"},
    }))
    assert ok_stranger is False
    assert sent == []
    ok_cb_stranger = asyncio.run(telegram_bot.handle_update(cfg, {
        "callback_query": {"id": "cb-x", "data": "st:", "message": {"chat": {"id": 999999999}}},
    }))
    assert ok_cb_stranger is False
    assert sent == []

    # 2) Owner /status, /orders, /order <prefix>, /locks, /disk, /digest
    for cmd in ("/status", "/orders 5", "/order 3b79c362", "/locks", "/disk", "/digest"):
        sent.clear()
        ok = asyncio.run(telegram_bot.handle_update(cfg, {
            "message": {"chat": {"id": 8918520934}, "text": cmd},
        }))
        assert ok is True
        assert sent and sent[-1][0] == "sendMessage"
        txt = sent[-1][1]["text"]
        assert "alice@example.com" not in txt  # raw email never leaked
        assert "AAExampleFakeToken" not in txt

    # Verify /order <prefix> shows Pro500 & masked email
    sent.clear()
    asyncio.run(telegram_bot.handle_update(cfg, {
        "message": {"chat": {"id": 8918520934}, "text": "/order d2ccdaba"},
    }))
    detail_txt = sent[-1][1]["text"]
    assert "Pro500" in detail_txt
    assert "al***@example.com" in detail_txt

    # Seed an active held lock and verify /locks displays it with action buttons
    db.acquire_lock(
        lock_id="lock-held-1234",
        lock_key="fp-held-1",
        lock_type="unknown_payment",
        reason="unknown_payment",
        order_id="3b79c362-b60f-4537-ba4b-9831cd57b78d",
    )
    sent.clear()
    asyncio.run(telegram_bot.handle_update(cfg, {
        "message": {"chat": {"id": 8918520934}, "text": "/locks"},
    }))
    assert "lock-held-12" in sent[-1][1]["text"]
    assert any(
        btn["callback_data"] == "rc:3b79c362-b60f-4537-ba4b-9831cd57b78d"
        for row in sent[-1][1]["reply_markup"]["inline_keyboard"]
        for btn in row
    )


def test_bot_inline_reconcile_and_auto_renew_callbacks(monkeypatch, _isolated_bot):
    _db, store, _mgr, cfg = _isolated_bot
    _seed_order(
        store,
        oid="ord-reconcile-001",
        status=OrderStatus.UNKNOWN,
        payment_status="processing",
        delivery_status="pending",
    )
    _seed_order(
        store,
        oid="ord-renew-002",
        status=OrderStatus.DELIVERED,
        payment_status="succeeded",
        delivery_status="delivered",
        auto_cancel_renew=True,
        auto_renew_status="failed",
    )

    calls: list[tuple[str, dict]] = []

    async def fake_tg_call(c, method, payload, *, timeout_s=12.0):
        calls.append((method, payload))
        return {"ok": True, "result": {}}

    async def fake_reconcile(oid, *, force=False):
        assert oid == "ord-reconcile-001"
        assert force is True
        return {
            "outcome": "resolved_delivered",
            "order_status": "delivered",
            "payment_status": "succeeded",
            "delivery_status": "delivered",
            "message": "只读对账核验通过，订阅已确认交付。",
        }

    async def fake_cancel_ar(oid):
        assert oid == "ord-renew-002"
        raise HTTPException(409, detail={"code": "vault_expired", "message": "原订单会话上下文已过期，请人工登录账号取消续费"})

    monkeypatch.setattr(telegram_bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(reconciler, "reconcile_order", fake_reconcile)
    monkeypatch.setattr(orders_router, "cancel_auto_renew", fake_cancel_ar)

    # Tap read-only reconcile button
    ok_rc = asyncio.run(telegram_bot.handle_update(cfg, {
        "callback_query": {
            "id": "cb-rc-1",
            "data": "rc:ord-reconcile-001",
            "message": {"chat": {"id": 8918520934}},
        },
    }))
    assert ok_rc is True
    assert calls[0][0] == "answerCallbackQuery"
    assert calls[1][0] == "sendMessage"
    assert "resolved_delivered" in calls[1][1]["text"]

    # Tap retry cancel auto-renew button (gracefully surfaces vault_expired)
    calls.clear()
    ok_ar = asyncio.run(telegram_bot.handle_update(cfg, {
        "callback_query": {
            "id": "cb-ar-1",
            "data": "ar:ord-renew-002",
            "message": {"chat": {"id": 8918520934}},
        },
    }))
    assert ok_ar is True
    assert calls[0][0] == "answerCallbackQuery"
    assert "原订单会话上下文已过期" in calls[1][1]["text"]


def test_outgoing_telegram_alerts_include_contextual_buttons_and_streak_digest_disk(monkeypatch, _isolated_bot):
    _db, store, mgr, cfg = _isolated_bot
    _seed_order(store, oid="ord-btn-1", status=OrderStatus.UNKNOWN, payment_status="processing", delivery_status="pending")

    posted: list[dict] = []

    class DummyClient:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, content=None, json=None, headers=None):
            body = json if json is not None else __import__("json").loads(content.decode("utf-8"))
            posted.append(body)
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {}})

    monkeypatch.setattr(httpx, "AsyncClient", DummyClient)

    # 1) Outgoing unknown alert attaches read-only reconcile button
    asyncio.run(mgr.deliver_telegram(cfg, "unknown", {"order_id": "ord-btn-1", "message": "unconfirmed"}, max_attempts=1))
    kb_unknown = posted[-1]["reply_markup"]["inline_keyboard"][0]
    assert any(btn["callback_data"] == "rc:ord-btn-1" for btn in kb_unknown)

    # 2) Outgoing succeeded alert with failed auto_renew attaches retry-cancel button
    posted.clear()
    asyncio.run(mgr.deliver_telegram(
        cfg, "succeeded", {"order_id": "ord-btn-2", "auto_renew_status": "failed"}, max_attempts=1,
    ))
    kb_ar = posted[-1]["reply_markup"]["inline_keyboard"][0]
    assert any(btn["callback_data"] == "ar:ord-btn-2" for btn in kb_ar)

    # 3) Consecutive failure streak triggers at threshold=3 and resets on succeeded
    async def run_streak():
        posted.clear()
        assert telegram_bot.on_order_event(cfg, "failed", {"order_id": "f-1", "failure_code": "card_declined"}) is False
        assert telegram_bot.on_order_event(cfg, "failed", {"order_id": "f-1", "failure_code": "card_declined"}) is False  # dedup
        assert telegram_bot.on_order_event(cfg, "unknown", {"order_id": "f-2", "failure_code": "unknown"}) is False
        assert telegram_bot.on_order_event(cfg, "failed", {"order_id": "f-3", "failure_code": "proxy_error"}) is True
        await mgr.drain()
        assert len(posted) == 1
        assert "连续失败预警" in posted[0]["text"]
        # Reset on success
        telegram_bot.on_order_event(cfg, "succeeded", {"order_id": "ok-1"})
        assert telegram_bot.on_order_event(cfg, "failed", {"order_id": "f-4", "failure_code": "err"}) is False

    asyncio.run(run_streak())

    # 4) Daily digest respects Beijing hour (22:00) and deduplicates same Beijing date
    posted.clear()
    before_22_bj = datetime(2026, 11, 28, 13, 30, tzinfo=timezone.utc)  # 21:30 BJ
    at_22_bj = datetime(2026, 11, 28, 14, 5, tzinfo=timezone.utc)      # 22:05 BJ
    assert asyncio.run(telegram_bot.check_daily_digest(cfg, now_utc=before_22_bj)) is False
    assert asyncio.run(telegram_bot.check_daily_digest(cfg, now_utc=at_22_bj)) is True
    assert len(posted) == 1 and "每日运行简报" in posted[0]["text"]
    assert asyncio.run(telegram_bot.check_daily_digest(cfg, now_utc=at_22_bj)) is False  # same day dedup

    # 5) Low disk warning triggers under 1024 MB and respects 6h cooldown
    posted.clear()
    monkeypatch.setattr(telegram_bot, "_data_disk_info", lambda: {
        "mount_path": "/data", "free_mb": 640, "total_mb": 20480, "used_pct": 96.8, "db_bytes": 100, "wal_bytes": 0,
    })
    assert asyncio.run(telegram_bot.check_disk_warning(cfg, now_mono=1000.0)) is True
    assert len(posted) == 1 and "磁盘余量预警" in posted[0]["text"]
    assert asyncio.run(telegram_bot.check_disk_warning(cfg, now_mono=2000.0)) is False  # cooldown
    assert asyncio.run(telegram_bot.check_disk_warning(cfg, now_mono=1000.0 + 7 * 3600)) is True  # after 6h
