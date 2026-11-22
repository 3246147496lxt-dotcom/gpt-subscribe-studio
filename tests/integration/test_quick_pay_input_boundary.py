"""Separate cardless checkout-only inputs from payment inputs before any job exists."""
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gss_api import main, persistence
from gss_api.routers import quick_pay
from gss_api.schemas import QuickPayRequest
from gss_api.services.order_flow import QuickPayJob
from gss_api.store import InMemoryStore


# Completely inert test input. No handler below contacts a payment provider.
PAN = "4242" * 4
SESSION = "private-synthetic-session-canary"
CVC = "987"
CARD = dict(number=PAN, cvc=CVC, exp_month="10", exp_year="2028")
PATH = "/api/payments/quick-pay"


@pytest.fixture
def api(monkeypatch):
    db = persistence.Persistence(":memory:")
    monkeypatch.setattr(persistence, "_db", db)
    monkeypatch.setattr(main, "store", InMemoryStore())
    start = Mock(return_value=QuickPayJob(id="inert-fixture", idempotency_key="input-boundary"))
    monkeypatch.setattr(quick_pay, "start_quick_pay", start)
    try:
        with TestClient(main.create_app()) as client:
            yield client, start, db
    finally:
        db.close()


@pytest.mark.parametrize("fields", [
    {"debug_checkout_only": True, "card": CARD},
    {"debug_checkout_only": True, "card": None},
    {"debug_checkout_only": True, "card": {"unknown": SESSION}},
    {}, {"debug_checkout_only": False}, {"card": None}, {"card": {"number": PAN}},
    {"debug_checkout_only": "true", "card": CARD},
])
def test_ineligible_input_fails_closed_without_disclosing_values(fields, api, caplog):
    client, start, db = api
    response = client.post(PATH, json={"plan": "plus", "session": SESSION, **fields},
                           headers={"Idempotency-Key": "input-boundary"})
    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "INVALID_ORDER_INPUT", "message": "订单请求字段无效，未提交。"}
    start.assert_not_called()
    assert not db.load_jobs()
    for private in (PAN, SESSION, CVC):
        assert private not in response.text and private not in caplog.text


@pytest.mark.parametrize(("fields", "card_present"), [
    ({"debug_checkout_only": True}, False),
    ({"card": CARD}, True),
    ({"debug_checkout_only": False, "card": CARD}, True),
])
def test_valid_inputs_reach_only_stubbed_job_start(fields, card_present, api):
    client, start, _db = api
    response = client.post(PATH, json={"plan": "plus", "session": SESSION, **fields},
                           headers={"Idempotency-Key": "input-boundary"})
    assert response.status_code == 202
    payload = start.call_args.args[0]
    assert ("card" in payload.model_fields_set) == card_present
    assert (payload.card is not None) == card_present


def test_team_order_validation_does_not_echo_input(api):
    client, _, _ = api
    response = client.post("/api/orders", json={"plan": "team", "session": {"secret": SESSION}})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_ORDER_INPUT"
    assert SESSION not in response.text


def test_conditional_schema_and_validation_errors_do_not_print_secrets():
    schema = QuickPayRequest.model_json_schema()
    assert schema["then"]["not"]["required"] == ["card"]
    assert schema["else"]["required"] == ["card"]
    with pytest.raises(ValidationError) as caught:
        QuickPayRequest(plan="plus", session=SESSION, debug_checkout_only=True, card=CARD)
    assert all(v not in str(caught.value) for v in (PAN, SESSION, CVC))
