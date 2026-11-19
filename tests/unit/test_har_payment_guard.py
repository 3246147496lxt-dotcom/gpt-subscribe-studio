"""Real default dispatcher with a nonfinancial, in-memory provider."""
from unittest.mock import Mock

import pytest

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.checkout_protocol import run_checkout_protocol
from gss_core import plus_har_protocol as har
from test_plus_har_protocol import CHECKOUT_ID, PROCESSOR, Harness, StubClient


@pytest.mark.parametrize("first", ["requires_action", "response_lost"])
def test_default_har_does_not_submit_again(monkeypatch, first):
    monkeypatch.delenv("GSS_PLUS_ENGINE", raising=False)
    context, writes, fences = {}, [], []

    class InMemoryProvider(Harness):
        def _app_confirm(self):
            writes.append("checkout")
            fences.append(context.get("app_confirmation_started"))
            if first == "response_lost":
                raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "response lost")
            self.confirmed = {"status": "success", "payment_intent": {"id": "pi_fixture", "status": "requires_confirmation"}}
            self.intent_id, self.intent_secret = "pi_fixture", "pi_fixture_secret_fixture"

        def _intent_confirm(self):
            writes.append("intent")
            self.raw["intent"] = {"id": "pi_fixture", "status": "requires_action"}

    monkeypatch.setattr(har, "PlusHarProtocol", InMemoryProvider)
    spec = {"checkout_url": f"https://chatgpt.com/checkout/{PROCESSOR}/{CHECKOUT_ID}",
            "checkout_id": CHECKOUT_ID, "publishable_key": "pk_fixture",
            "currency": "JPY", "country": "JP", "processor": PROCESSOR}
    for _ in range(2):
        run_checkout_protocol(StubClient(), context, spec, card={"number": "fixture"}, billing={})
    assert writes.count("checkout") == 1
    assert writes.count("intent") == (1 if first == "requires_action" else 0)
    assert fences == [True]


def test_standalone_har_does_not_replay_on_transport_loss(monkeypatch):
    engine = Harness(session="", checkout_id=CHECKOUT_ID, card={}, billing={})
    pay = Mock(side_effect=ConnectionError("TLS EOF"))
    monkeypatch.setattr(engine, "pay", pay)
    monkeypatch.setattr(engine, "rotate_proxy_sid", lambda: None)
    with pytest.raises(ConnectionError):
        engine.pay_from_url()
    pay.assert_called_once()

@pytest.mark.parametrize("overrides", [{"checkout_session_id": "different"}, {"amount_received": 1},
                                        {"currency": "usd"}, {"payment_status": "no_payment_required"}])
def test_foreign_or_conflicting_completed_checkout_is_not_payment(overrides):
    from test_plus_har_protocol import build_harness
    h = build_harness(read_only=True)
    h.receipt_expected_minor = 1300
    h.raw["checkout"] = {"checkout_session_id": h.checkout_id, "status": "complete", "payment_status": "paid",
                         "checkout_state": {"currency": "jpy", "total": {"total": {"minorUnitsAmount": 1300}}},
                         **overrides}
    h._subscription_active = Mock(return_value=True)
    outcome = h._read_only_settle()
    assert outcome.state == "unknown" and outcome.intent_status is None
    h._subscription_active.assert_not_called()


def test_expiry_after_paid_receipt_skips_entitlement_query():
    from test_plus_har_protocol import build_harness
    h = build_harness(read_only=True)
    h.client = Mock()
    h.receipt_expected_minor = 1300
    h.raw["checkout"] = {"checkout_session_id": h.checkout_id, "status": "complete", "payment_status": "paid",
                         "amount_paid": 1300, "payment_intent": "pi_fixture",
                         "checkout_state": {"currency": "jpy", "total": {"total": {"minorUnitsAmount": 1300}}}}
    h.credentials_current = lambda: False
    receipt = h._read_only_settle()
    assert receipt.state == "pending" and receipt.intent_status == "succeeded"
    h.client.subscription_payload.assert_not_called()

@pytest.mark.parametrize("already", ["succeeded", "processing", "requires_action", "unrecognized", None])
def test_har_does_not_submit_intent_from_ambiguous_or_existing_application_payment(already):
    from test_plus_har_protocol import build_harness
    h = build_harness()
    h.confirmed = {"status": "success", "payment_intent": {"id": "pi_fixture", "status": already}}
    h.intent_id, h.intent_secret = "pi_fixture", "pi_fixture_secret_fixture"
    h._app_confirm = Mock()
    h._intent_confirm = Mock()
    h.pay()
    h._app_confirm.assert_called_once()
    h._intent_confirm.assert_not_called()
