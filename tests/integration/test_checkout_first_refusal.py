"""Explicit refusals are terminal at the router, above the checkout adapter."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from gss_api.persistence import Persistence
from gss_api.routers import orders
from gss_api.schemas import OrderCreateRequest
from gss_api.store import InMemoryStore
from gss_contracts.errors import DomainError, ErrorCode


@pytest.fixture
def isolated(monkeypatch, verified_us_payer):
    db = Persistence(":memory:")
    store = InMemoryStore()
    store.attach_db(db)
    monkeypatch.setattr(orders, "store", store)
    monkeypatch.setattr(orders, "vault", Mock())
    monkeypatch.setattr(orders.locks, "find_active_lock", lambda *a: None)
    monkeypatch.setattr(orders.locks, "hold_order_unknown", lambda *a, **kw: pytest.fail("unexpected funds lock"))
    monkeypatch.setattr(orders, "checkout_proxy_for", lambda *a, **kw: "http://unused.invalid")
    rotate = Mock(return_value="http://unused.invalid")
    monkeypatch.setattr(orders, "rotate_proxy_sid", rotate)
    monkeypatch.setenv("GSS_ROUTE_PROFILE", "A")
    monkeypatch.delenv("GSS_BILLING_PROFILES_FILE", raising=False)
    checkout = AsyncMock()
    monkeypatch.setattr(orders, "build_pipeline", lambda: SimpleNamespace(create_checkout=checkout))
    try:
        yield db, checkout, rotate
    finally:
        db.close()


def run_refusal(checkout, errors):
    checkout.side_effect = errors
    with pytest.raises(HTTPException) as caught:
        asyncio.run(orders._create_order(OrderCreateRequest(
            plan="plus", billing_country="US", session=json.dumps({"accessToken": "offline-fixture"}),
        ), None))
    return caught.value


@pytest.mark.parametrize("rotation", ["0", "1"])
@pytest.mark.parametrize("business,prose,diag,expected", [
    (ErrorCode.CHECKOUT_UPSTREAM_ERROR, "proxy tunnel failed: unusual activity",
     {"upstream_status": 403, "upstream_code": "unusual_activity"}, "unusual_activity"),
    (ErrorCode.CHECKOUT_UPSTREAM_ERROR, "proxy tunnel failed",
     {"upstream_status": 401, "upstream_code": "token_revoked"}, "invalid_session"),
    (ErrorCode.CHECKOUT_UPSTREAM_ERROR, "proxy tunnel failed",
     {"request_id": "req_abcdef0123456789abcdef0123456789"}, "unknown_upstream"),
    (ErrorCode.INVALID_SESSION, "proxy tunnel failed", {}, "invalid_session"),
    (ErrorCode.CHECKOUT_UPSTREAM_ERROR, "proxy tunnel failed: 异常流量", {}, "unusual_activity"),
    (ErrorCode.CHECKOUT_UPSTREAM_ERROR, "proxy tunnel failed", {"upstream_code": "already_subscribed"}, "already_subscribed"),
])
def test_refusal_is_terminal_and_persisted(isolated, monkeypatch, rotation, business, prose, diag, expected):
    db, checkout, rotate = isolated
    monkeypatch.setenv("GSS_CHECKOUT_ROTATE", rotation)
    err = run_refusal(checkout, [DomainError(business, prose, diag), AssertionError("unexpected resend")])
    assert checkout.await_count == 1
    rotate.assert_not_called()
    assert (err.detail.get("detail") or {}) == diag
    assert "_checkout_order_id" not in err.detail
    [(order, view, _)] = db.load_orders()
    assert order.id == err._checkout_order_id
    assert str(order.status) == "rejected" and view.failure_code == expected
    assert view.last_error == err.detail["message"]


def test_connection_only_then_first_upstream_refusal(isolated, monkeypatch):
    db, checkout, rotate = isolated
    monkeypatch.setenv("GSS_CHECKOUT_ROTATE", "1")
    diag = {"upstream_status": 403, "upstream_code": "unusual_activity"}
    err = run_refusal(checkout, [
        DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "proxy tunnel failed"),
        DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "upstream refusal", diag),
    ])
    assert checkout.await_count == 2 and rotate.call_count == 1
    assert err.detail["detail"] == diag
    assert db.load_orders()[0][1].failure_code == "unusual_activity"
