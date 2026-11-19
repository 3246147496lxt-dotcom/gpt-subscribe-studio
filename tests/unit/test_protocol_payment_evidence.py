"""A checkout or transport acknowledgment is not evidence of a charge."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ChatGPTClient, _raise_stripe
from gss_core.plus_protocol import PlusProtocol
from gss_core.stripe_protocol import PaymentOutcome, StripeProtocol


@pytest.mark.parametrize("payload,poll,state", [
    ({"payment_intent": {"status": "requires_capture"}}, None, "pending"),
    ({"setup_intent": {"status": "succeeded"}}, None, "unknown"),
    ({"status": "succeeded"}, {"state": "succeeded"}, "unknown"),
    ({}, {"state": "failed"}, "unknown"),
    ({"error": {"code": "api_error"}}, None, "unknown"),
    ({"error": {"code": "card_declined"}}, None, "card_declined"),
    ({"payment_intent": {"status": "succeeded"}}, None, "paid"),
    ({}, {"mode": "subscription", "payment_status": "paid"}, "paid"),
])
def test_classify_only_reports_observed_payment(payload, poll, state):
    assert StripeProtocol.classify({"status": 200, "body": payload}, poll).state == state


@pytest.mark.parametrize("engine,args", [(PlusProtocol, ()), (StripeProtocol, ("cs_test_fixture",))])
def test_transport_failure_never_resubmits_payment(monkeypatch, engine, args):
    monkeypatch.setattr("time.sleep", lambda _: None)
    protocol = object.__new__(engine)
    protocol.pin_proxy = False
    protocol.rotate_proxy_sid = Mock()
    protocol.pay = Mock(side_effect=OSError("TLS EOF"))
    with pytest.raises(OSError):
        protocol.pay_from_url(*args, retries=3)
    protocol.pay.assert_called_once()
    protocol.rotate_proxy_sid.assert_not_called()


def test_unknown_hosted_result_never_starts_another_order(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    protocol = object.__new__(StripeProtocol)
    protocol.order_factory = Mock(return_value="")
    protocol.pay = Mock(return_value=PaymentOutcome(state="unknown"))
    assert protocol.pay_from_url("cs_test_fixture", retries=2).state == "unknown"
    protocol.pay.assert_called_once()
    protocol.order_factory.assert_not_called()


def test_save_choice_error_text_cannot_trigger_another_confirmation():
    protocol = object.__new__(PlusProtocol)
    protocol.credentials_current = lambda: True
    protocol.client = Mock()
    protocol.client.create_confirmation_token.return_value = "ctoken_fixture"
    protocol.publishable_key, protocol.currency, protocol._t0 = "pk_test_fixture", "USD", 0
    protocol._confirm = Mock(side_effect=DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "save choice"))
    with pytest.raises(DomainError):
        protocol._confirm_with_ctoken("pm_fixture")
    protocol._confirm.assert_called_once()
    protocol.client.create_confirmation_token.assert_called_once()

    http = Mock()
    http.post.return_value = SimpleNamespace(status_code=400, text="save choice")
    client = Mock(http=http, timeout=10)
    client._raise.side_effect = DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "save choice")
    with pytest.raises(DomainError):
        ChatGPTClient.confirm_checkout(client, checkout_session_id="oaics_fixture", confirm_token="fixture")
    http.post.assert_called_once()


def test_http_402_without_card_evidence_is_not_a_decline():
    with pytest.raises(DomainError) as exc:
        _raise_stripe({"error": {"code": "api_error"}}, 402)
    assert exc.value.code == ErrorCode.PAYMENT_UNKNOWN_STATE


@pytest.mark.parametrize("payload,expected", [
    ({"status": "complete"}, "unknown"),
    ({"submission_attempt": {"state": "failed"}}, "unknown"),
    ({"payment_intent": {"id": "pi_fixture", "client_secret": "pi_fixture_secret_test"}}, "unknown"),
    ({"setup_intent": {"id": "seti_fixture", "status": "succeeded"}}, "unknown"),
    ({"payment_intent": {"id": "pi_fixture", "status": "succeeded"}}, "paid"),
])
def test_legacy_plus_does_not_confirm_again_from_an_ambiguous_envelope(payload, expected):
    protocol = object.__new__(PlusProtocol)
    protocol.credentials_current = lambda: True
    protocol.checkout_id, protocol.checkout_url = "oaics_fixture", ""
    protocol.publishable_key = "pk_test_fixture"
    protocol.is_stripe_custom = False
    protocol.external_pm = "pm_fixture"
    protocol.client = Mock()
    protocol._warmup = Mock()
    protocol._confirm_with_ctoken = Mock(return_value=(payload, "ctoken_fixture"))
    protocol.confirm_intent_raw = Mock()
    assert protocol.pay().state == expected
    protocol._confirm_with_ctoken.assert_called_once()
    protocol.confirm_intent_raw.assert_not_called()


def test_standalone_checkout_returns_original_challenge_without_another_submission(monkeypatch):
    protocol = StripeProtocol()
    protocol.step_fingerprint = protocol.step_elements = protocol.step_dwell = protocol.step_tax_region = False
    protocol.external_pm = "pm_fixture"
    confirm = Mock(return_value={"status": 200, "body": {"payment_intent": {
        "id": "pi_fixture", "object": "payment_intent", "status": "requires_action"}}})
    monkeypatch.setattr(protocol, "_sleep", lambda seconds: None)
    monkeypatch.setattr(protocol, "init_checkout", lambda oid: (None, object()))
    monkeypatch.setattr(protocol, "confirm", confirm)
    monkeypatch.setattr(protocol, "poll", Mock(side_effect=AssertionError("must not continue")))
    try:
        assert protocol.pay("cs_test_fixture").state == "challenge_required"
        confirm.assert_called_once()
    finally:
        protocol.close()
