"""Phase 2 tests: read-only reconciler, server-side order ledger, and locks console."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from gss_api import locks, main, persistence
from gss_api.routers import orders as orders_router
from gss_api.services import order_flow, reconciler
from gss_api.store import InMemoryStore, OrderView
from gss_contracts import Mode, Order, OrderStatus, Plan
from gss_core.vault import OrderSecrets, vault


@pytest.fixture(autouse=True)
def _isolated_phase2(monkeypatch):
    db = persistence.Persistence(":memory:")
    store = InMemoryStore()
    store.attach_db(db)
    monkeypatch.setattr(persistence, "_db", db)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(orders_router, "store", store)
    monkeypatch.setattr(locks, "store", store)
    monkeypatch.setattr(order_flow, "_jobs", {})
    monkeypatch.setattr(order_flow, "_tasks", set())
    monkeypatch.setenv("GSS_RECONCILER_ENABLED", "0")
    order_flow.reset_shutdown()
    yield db, store
    order_flow.reset_shutdown()
    db.close()


def _seed_order(
    store: InMemoryStore,
    *,
    order_id: str,
    status: OrderStatus,
    payment_status: str,
    delivery_status: str = "pending",
    plan: Plan = Plan.PLUS,
    fingerprint: str = "fp-phase2",
    auto_cancel_renew: bool = False,
):
    order = Order(
        id=order_id,
        idempotency_key=f"idem-{order_id}",
        purchase_attempt_id=f"pa-{order_id}",
        plan=plan,
        billing_country="US",
        mode=Mode(),
        status=status,
        amount=Decimal("20.00"),
    )
    view = OrderView(
        purchase_attempt_id=order.purchase_attempt_id,
        account_fingerprint=fingerprint,
        exit_country="US",
        currency="USD",
        amount="20.00",
        payment_status=payment_status,
        delivery_status=delivery_status,
        payment_intent_id=f"pi_{order_id}",
        auto_cancel_renew=auto_cancel_renew,
        auto_renew_status="pending" if auto_cancel_renew else "disabled",
    )
    store.orders[order.id] = order
    store.views[order.id] = view
    store.persist(order.id)
    return order, view


def test_reconciler_converges_paid_pending_delivery_and_cancels_auto_renew(monkeypatch, _isolated_phase2):
    db, store = _isolated_phase2
    order, view = _seed_order(
        store,
        order_id="ord-paid-delay",
        status=OrderStatus.VERIFYING,
        payment_status="succeeded",
        delivery_status="verify_delayed",
        auto_cancel_renew=True,
    )
    job = order_flow.QuickPayJob(
        id="job-paid-delay",
        idempotency_key="idem-ord-paid-delay",
        account_fingerprint="fp-phase2",
        status="paid_pending_delivery",
        stage="paid_pending_delivery",
        order_id=order.id,
        amount="20.00",
        currency="USD",
    )
    db.save_job(job)
    order_flow._jobs[job.id] = job
    vault.put(order.id, OrderSecrets(session="synthetic-session"))

    async def fake_verify(ord_obj, ord_view):
        ord_obj.status = OrderStatus.DELIVERED
        ord_view.delivery_status = "delivered"
        store.persist(ord_obj.id)
        return True

    cancel_mock = AsyncMock()
    monkeypatch.setattr(orders_router, "_verify_after_payment", fake_verify)
    monkeypatch.setattr(order_flow, "_cancel_auto_renew", cancel_mock)

    try:
        res = asyncio.run(reconciler.reconcile_job(job.id, force=True))
    finally:
        vault.drop(order.id)

    assert res["outcome"] == "resolved_delivered"
    assert res["changed"] is True
    assert res["job_status"] == "succeeded"
    assert res["order_status"] == "delivered"
    assert res["reconcile_attempts"] == 1
    cancel_mock.assert_awaited_once()
    persisted = db.get_quick_pay_by_id(job.id)
    assert persisted["status"] == "succeeded"
    assert persisted["reconcile_attempts"] == 1
    assert persisted["last_reconciled_at"] is not None


def test_reconciler_keeps_held_lock_when_unknown_vault_expired(_isolated_phase2):
    db, store = _isolated_phase2
    order, _view = _seed_order(
        store,
        order_id="ord-unk-expired",
        status=OrderStatus.UNKNOWN,
        payment_status="processing",
        fingerprint="fp-unk-1",
    )
    lock = locks.hold_order_unknown(order.id, job_id="job-unk-expired")
    job = order_flow.QuickPayJob(
        id="job-unk-expired",
        idempotency_key="idem-ord-unk-expired",
        account_fingerprint="fp-unk-1",
        status="unknown",
        stage="unknown",
        order_id=order.id,
        lock_id=lock["id"],
    )
    db.save_job(job)
    order_flow._jobs[job.id] = job
    vault.drop(order.id)

    res = asyncio.run(reconciler.reconcile_lock(lock["id"], force=True))
    assert res["outcome"] == "manual_review_required"
    assert res["changed"] is False
    assert res["lock_state"] == "held"
    assert db.get_lock(lock["id"])["state"] == "held"


def test_reconciler_resolves_unknown_lock_only_on_authoritative_paid_delivery(monkeypatch, _isolated_phase2):
    db, store = _isolated_phase2
    order, view = _seed_order(
        store,
        order_id="ord-unk-paid",
        status=OrderStatus.UNKNOWN,
        payment_status="processing",
        fingerprint="fp-unk-paid",
    )
    lock = locks.hold_order_unknown(order.id, job_id="job-unk-paid")
    job = order_flow.QuickPayJob(
        id="job-unk-paid",
        idempotency_key="idem-ord-unk-paid",
        account_fingerprint="fp-unk-paid",
        status="unknown",
        stage="unknown",
        order_id=order.id,
        lock_id=lock["id"],
    )
    db.save_job(job)
    order_flow._jobs[job.id] = job
    vault.put(order.id, OrderSecrets(session="synthetic-session"))

    async def fake_probe(oid):
        order.status = OrderStatus.DELIVERED
        view.payment_status = "succeeded"
        view.delivery_status = "delivered"
        store.persist(oid)
        return {"state": "paid"}

    monkeypatch.setattr(reconciler.protocol_cards, "get_bind_protocol_status", fake_probe)

    try:
        res = asyncio.run(reconciler.reconcile_lock(lock["id"], force=True))
    finally:
        vault.drop(order.id)

    assert res["outcome"] == "resolved_delivered"
    assert res["changed"] is True
    assert res["job_status"] == "succeeded"
    assert res["lock_state"] == "resolved"
    assert db.get_lock(lock["id"])["state"] == "resolved"
    assert locks.find_active_lock("fp-unk-paid") is None


def test_reconciler_keeps_held_lock_even_when_probe_reports_failed(monkeypatch, _isolated_phase2):
    db, store = _isolated_phase2
    order, view = _seed_order(
        store,
        order_id="ord-unk-fail",
        status=OrderStatus.UNKNOWN,
        payment_status="processing",
        fingerprint="fp-unk-fail",
    )
    lock = locks.hold_order_unknown(order.id, job_id="job-unk-fail")
    job = order_flow.QuickPayJob(
        id="job-unk-fail",
        idempotency_key="idem-ord-unk-fail",
        account_fingerprint="fp-unk-fail",
        status="unknown",
        stage="unknown",
        order_id=order.id,
        lock_id=lock["id"],
    )
    db.save_job(job)
    order_flow._jobs[job.id] = job
    vault.put(order.id, OrderSecrets(session="synthetic-session"))

    async def fake_probe_failed(oid):
        view.payment_status = "failed"
        store.persist(oid)
        return {"state": "card_declined"}

    monkeypatch.setattr(reconciler.protocol_cards, "get_bind_protocol_status", fake_probe_failed)

    try:
        res = asyncio.run(reconciler.reconcile_lock(lock["id"], force=True))
    finally:
        vault.drop(order.id)

    assert res["outcome"] == "upstream_reports_unpaid_manual_unlock"
    assert res["lock_state"] == "held"
    assert db.get_lock(lock["id"])["state"] == "held"


def test_reconciler_backoff_and_max_attempts(monkeypatch, _isolated_phase2):
    db, store = _isolated_phase2
    monkeypatch.setenv("GSS_RECONCILER_MAX_ATTEMPTS", "2")
    order, _view = _seed_order(
        store,
        order_id="ord-backoff",
        status=OrderStatus.UNKNOWN,
        payment_status="processing",
        fingerprint="fp-backoff",
    )
    job = order_flow.QuickPayJob(
        id="job-backoff",
        idempotency_key="idem-ord-backoff",
        account_fingerprint="fp-backoff",
        status="unknown",
        stage="unknown",
        order_id=order.id,
    )
    db.save_job(job)
    order_flow._jobs[job.id] = job

    first = asyncio.run(reconciler.run_once())
    assert len(first) == 1 and first[0]["outcome"] == "manual_review_required"

    # Immediate second run is skipped by backoff window.
    second = asyncio.run(reconciler.run_once())
    assert len(second) == 1 and second[0]["outcome"] == "backoff_waiting"

    # Exhaust attempts -> max_attempts_reached.
    job.reconcile_attempts = 2
    job.last_reconciled_at = "2020-01-01T00:00:00+00:00"
    db.save_job(job)
    third = asyncio.run(reconciler.run_once())
    assert len(third) == 1 and third[0]["outcome"] == "max_attempts_reached"


def test_ledger_locks_and_reconciler_endpoints(_isolated_phase2):
    db, store = _isolated_phase2
    _seed_order(store, order_id="ord-delivered-1", status=OrderStatus.DELIVERED, payment_status="succeeded", delivery_status="delivered")
    _seed_order(store, order_id="ord-failed-1", status=OrderStatus.FAILED, payment_status="failed", plan=Plan.TEAM)
    _seed_order(store, order_id="ord-unknown-1", status=OrderStatus.UNKNOWN, payment_status="processing", fingerprint="fp-api-lock")
    lock = locks.hold_order_unknown("ord-unknown-1", reason="unknown_payment")
    db.add_event(stage="creating_order", event="running", order_id="ord-delivered-1", message="建单完成")

    with TestClient(main.create_app()) as client:
        all_resp = client.get("/api/orders?limit=10&offset=0")
        assert all_resp.status_code == 200
        body = all_resp.json()
        assert body["total"] == 3
        assert body["stats"]["delivered"] == 1
        assert body["stats"]["failed"] == 1
        assert body["stats"]["unknown"] == 1
        assert body["stats"]["active_locks"] == 1

        filtered = client.get("/api/orders?status=succeeded")
        assert filtered.status_code == 200
        assert [item["id"] for item in filtered.json()["items"]] == ["ord-delivered-1"]

        searched = client.get("/api/orders?search=ord-failed")
        assert searched.status_code == 200
        assert [item["id"] for item in searched.json()["items"]] == ["ord-failed-1"]

        events_resp = client.get("/api/orders/ord-delivered-1/events")
        assert events_resp.status_code == 200
        assert len(events_resp.json()) == 1
        assert events_resp.json()[0]["stage"] == "creating_order"

        locks_resp = client.get("/api/payments/locks?state=held")
        assert locks_resp.status_code == 200
        locks_body = locks_resp.json()
        assert locks_body["total"] == 1
        assert locks_body["counts"]["held"] == 1
        assert locks_body["items"][0]["id"] == lock["id"]
        assert locks_body["items"][0]["order_id"] == "ord-unknown-1"
        assert locks_body["items"][0]["lock_key_prefix"].startswith("account:")

        rec_resp = client.post(f"/api/payments/locks/{lock['id']}/reconcile")
        assert rec_resp.status_code == 200
        assert rec_resp.json()["outcome"] == "manual_review_required"
        assert rec_resp.json()["lock_state"] == "held"

        status_resp = client.get("/api/payments/reconciler")
        assert status_resp.status_code == 200
        assert "max_attempts" in status_resp.json()

        health_resp = client.get("/api/health")
        assert health_resp.status_code == 200
        assert "reconciler" in health_resp.json()


def test_ledger_enriched_fields_source_filter_search_and_csv_redaction(_isolated_phase2):
    db, store = _isolated_phase2

    # 1) Portal order (joined with quick_pay_jobs + portal_tasks + portal_users)
    portal_fp = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    o_portal, v_portal = _seed_order(
        store,
        order_id="ord-portal-1",
        status=OrderStatus.DELIVERED,
        payment_status="succeeded",
        delivery_status="delivered",
        fingerprint=portal_fp,
        auto_cancel_renew=True,
    )
    v_portal.exit_country = "JP"
    v_portal.account_email = "alice@example.com"
    v_portal.card_brand = "VISA"
    v_portal.checkout_session_id = "cs_portal_abc123"
    v_portal.checkout_kind = "custom"
    v_portal.route_profile = "A"
    v_portal.billing_verified = True
    v_portal.session_reissued_for_country = True
    v_portal.tax_delta = "0.00"
    v_portal.auto_renew_status = "canceled"
    v_portal.auto_renew_attempts = 1
    v_portal.auto_renew_http_status = 200
    v_portal.proxy_rotations = 2
    v_portal.proxy_last_sid = "supersecretproxysid999"
    v_portal.proxy_last_exit_ip = "203.0.113.42"
    store.persist(o_portal.id)

    db.create_portal_user(user_id="pu-1", username="alice_ops", password_hash="scrypt$fake")
    job_portal = order_flow.QuickPayJob(
        id="job-portal-1",
        idempotency_key="idem-ord-portal-1",
        account_fingerprint=portal_fp,
        status="succeeded",
        stage="succeeded",
        order_id=o_portal.id,
        amount="20.00",
        currency="USD",
        timings={"preflight_ms": 210, "payment_ms": 3400, "total_ms": 5120},
    )
    db.save_job(job_portal)
    db.bind_portal_task(
        job_id=job_portal.id,
        owner_user_id="pu-1",
        guest_token_hash=None,
        guest_expires_at=None,
        masked_email="al***@example.com",
        plan="plus",
        exit_country="JP",
    )

    # 2) Admin QuickPay order (job without portal_tasks) with upstream diagnostics
    o_qp, v_qp = _seed_order(
        store,
        order_id="ord-qp-fail",
        status=OrderStatus.REJECTED,
        payment_status="unpaid",
        delivery_status="pending",
        plan=Plan.PRO,
    )
    v_qp.failure_code = "unusual_activity"
    v_qp.last_error = "上游风控拒绝"
    store.persist(o_qp.id)
    job_qp = order_flow.QuickPayJob(
        id="job-qp-fail",
        idempotency_key="idem-ord-qp-fail",
        account_fingerprint="fp-phase2",
        status="failed",
        stage="failed",
        order_id=o_qp.id,
        diagnostics={
            "stage": "creating_checkout",
            "upstream_status": 403,
            "upstream_code": "unusual_activity",
            "cf_ray": "abcdef0123456789-lax",
            "secret_leak": "must-not-appear",
        },
        timings={"create_checkout_ms": 820, "total_ms": 1450},
    )
    db.save_job(job_qp)

    # 3) Step-by-step order (no quick_pay_jobs)
    _seed_order(
        store,
        order_id="ord-step-1",
        status=OrderStatus.AWAITING_PAYMENT,
        payment_status="unpaid",
        delivery_status="pending",
    )

    with TestClient(main.create_app()) as client:
        # Source filtering
        assert [i["id"] for i in client.get("/api/orders?source=portal").json()["items"]] == ["ord-portal-1"]
        assert [i["id"] for i in client.get("/api/orders?source=quick_pay").json()["items"]] == ["ord-qp-fail"]
        assert [i["id"] for i in client.get("/api/orders?source=step").json()["items"]] == ["ord-step-1"]

        # Enriched fields & strict redaction on portal item
        portal_item = client.get("/api/orders?source=portal").json()["items"][0]
        assert portal_item["source"] == "portal"
        assert portal_item["account_email"] == "alice@example.com"
        assert portal_item["card_brand"] == "VISA"
        assert portal_item["portal_username"] == "alice_ops"
        assert portal_item["masked_email"] == "al***@example.com"
        assert portal_item["account_tag"] == "a1b2c3d4"
        assert portal_item["checkout_session_id"] == "cs_portal_abc123"
        assert portal_item["checkout_kind"] == "custom"
        assert portal_item["route_profile"] == "A"
        assert portal_item["billing_verified"] is True
        assert portal_item["session_reissued"] is True
        assert portal_item["tax_delta"] == "0.00"
        assert portal_item["auto_renew_attempts"] == 1
        assert portal_item["auto_renew_http_status"] == 200
        assert portal_item["proxy_rotations"] == 2
        assert portal_item["proxy_last_exit_ip"] == "203.0.113.42"
        assert portal_item["proxy_last_sid"] is not None
        assert "supersecretproxysid999" not in portal_item["proxy_last_sid"]
        assert portal_item["total_ms"] == 5120
        assert portal_item["timings_ms"]["payment_ms"] == 3400

        # Enriched diagnostics with allowlist enforcement
        qp_item = client.get("/api/orders?source=quick_pay").json()["items"][0]
        assert qp_item["diagnostics"]["upstream_status"] == 403
        assert qp_item["diagnostics"]["upstream_code"] == "unusual_activity"
        assert qp_item["diagnostics"]["cf_ray"] == "abcdef0123456789-lax"
        assert "secret_leak" not in qp_item["diagnostics"]

        # Expanded search: email, card brand, username, checkout session id, job id, failure code
        assert [i["id"] for i in client.get("/api/orders?search=alice@example.com").json()["items"]] == ["ord-portal-1"]
        assert [i["id"] for i in client.get("/api/orders?search=VISA").json()["items"]] == ["ord-portal-1"]
        assert [i["id"] for i in client.get("/api/orders?search=alice_ops").json()["items"]] == ["ord-portal-1"]
        assert [i["id"] for i in client.get("/api/orders?search=al***@example.com").json()["items"]] == ["ord-portal-1"]
        assert [i["id"] for i in client.get("/api/orders?search=cs_portal_abc123").json()["items"]] == ["ord-portal-1"]
        assert [i["id"] for i in client.get("/api/orders?search=job-qp-fail").json()["items"]] == ["ord-qp-fail"]
        assert [i["id"] for i in client.get("/api/orders?search=unusual_activity").json()["items"]] == ["ord-qp-fail"]

        # CSV export includes enriched fields, supports source filter, and never leaks raw SID or password hash
        csv_resp = client.get("/api/orders/export.csv?source=portal")
        assert csv_resp.status_code == 200
        csv_text = csv_resp.content.decode("utf-8-sig")
        assert "source,account_email,portal_username,masked_email,account_tag,plan,card_brand" in csv_text
        assert "alice@example.com" in csv_text
        assert "VISA" in csv_text
        assert "alice_ops" in csv_text
        assert "al***@example.com" in csv_text
        assert "ord-portal-1" in csv_text
        assert "ord-qp-fail" not in csv_text
        assert "supersecretproxysid999" not in csv_text
        assert "scrypt$fake" not in csv_text


def test_detect_and_normalize_card_brand():
    from gss_api.public_payment import detect_card_brand, normalize_card_brand

    assert detect_card_brand("4111-1111") == "VISA"
    assert detect_card_brand("5200 1234") == "Mastercard"
    assert detect_card_brand("2221 0000") == "Mastercard"
    assert detect_card_brand("3782 8224") == "Amex"
    assert detect_card_brand("6222 0200") == "UnionPay"
    assert detect_card_brand("6011 1111") == "Discover"
    assert detect_card_brand("3528 0000") == "JCB"
    assert detect_card_brand("123") is None
    assert normalize_card_brand("visa") == "VISA"
    assert normalize_card_brand("mastercard") == "Mastercard"
    assert normalize_card_brand("american express") == "Amex"


def test_failure_detail_endpoint_redacts_captured_trace_and_supports_legacy_orders(_isolated_phase2):
    from gss_core.failure_trace import build_redacted_http_trace

    db, store = _isolated_phase2

    # 1) New failed order with captured redacted HTTP trace (like Checkout 确认 -> subscription_limit)
    o_cap, v_cap = _seed_order(
        store,
        order_id="ord-confirm-limit",
        status=OrderStatus.REJECTED,
        payment_status="unpaid",
        delivery_status="pending",
    )
    v_cap.failure_code = "subscription_limit"
    v_cap.last_error = "该账号已达到订阅数量上限"
    v_cap.failure_trace = build_redacted_http_trace(
        title="Checkout 确认",
        method="POST",
        url="https://chatgpt.com/backend-api/payments/checkout/confirm?secret=dropme",
        http_status=200,
        stage="payment_confirm",
        request_json={
            "checkout_session_id": "oaics_secret_1234567890",
            "confirm_token": "ctoken_secret_abcdef1234",
            "selected_payment_method_type": "card",
            "client_secret": "pi_123_secret_xyz999",
        },
        response_json={"status": "subscription_limit"},
    )
    store.persist(o_cap.id)

    # 2) Historical failed order without failure_trace_json
    o_leg, v_leg = _seed_order(
        store,
        order_id="ord-legacy-fail",
        status=OrderStatus.REJECTED,
        payment_status="unpaid",
        delivery_status="pending",
    )
    v_leg.failure_code = "unusual_activity"
    v_leg.last_error = "上游报告异常流量，已停止建单。"
    store.persist(o_leg.id)
    job_leg = order_flow.QuickPayJob(
        id="job-legacy-fail",
        idempotency_key="idem-ord-legacy-fail",
        account_fingerprint="fp-phase2",
        status="failed",
        stage="failed",
        order_id=o_leg.id,
        diagnostics={
            "stage": "creating_checkout",
            "upstream_status": 400,
            "cf_ray": "abcdef0123456789-atl",
        },
    )
    db.save_job(job_leg)

    with TestClient(main.create_app()) as client:
        r1 = client.get("/api/orders/ord-confirm-limit/failure-detail")
        assert r1.status_code == 200
        body1 = r1.json()
        assert body1["title"] == "开通失败详情"
        assert body1["error_code"] == "SUBSCRIPTION_LIMIT"
        assert body1["stage"] == "payment_confirm"
        ex1 = body1["exchange"]
        assert ex1["captured"] is True
        assert ex1["title"] == "Checkout 确认"
        assert ex1["method"] == "POST"
        assert ex1["url"] == "https://chatgpt.com/backend-api/payments/checkout/confirm"
        assert ex1["http_status"] == 200
        assert ex1["request"]["json"]["checkout_session_id"] == "[REDACTED]"
        assert ex1["request"]["json"]["confirm_token"] == "[REDACTED]"
        assert ex1["request"]["json"]["client_secret"] == "[REDACTED]"
        assert ex1["request"]["json"]["selected_payment_method_type"] == "card"
        assert ex1["response"] == {"status": "subscription_limit"}
        raw1 = r1.text
        assert "oaics_secret_1234567890" not in raw1
        assert "ctoken_secret_abcdef1234" not in raw1
        assert "pi_123_secret_xyz999" not in raw1
        assert "dropme" not in raw1

        r2 = client.get("/api/orders/ord-legacy-fail/failure-detail")
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["error_code"] == "UNUSUAL_ACTIVITY"
        assert body2["stage"] == "creating_checkout"
        assert body2["cf_ray"] == "abcdef0123456789-atl"
        assert body2["exchange"]["captured"] is False
        assert body2["exchange"]["title"] == "Checkout 建单"
        assert body2["exchange"]["http_status"] == 400
        assert body2["exchange"]["url"] == "https://chatgpt.com/backend-api/payments/checkout"
