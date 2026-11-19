"""Team 托管（hosted）cs_ 会话的站内支付路径。

Team 长链也是 Stripe cs_ 会话：站内支付走同一套 payment_pages 确认，
但审批不经 chatgpt.com 后端 —— approve 被拒时必须降级轮询而不是报错。
"""

from __future__ import annotations

import asyncio

import pytest

from gss_contracts import PaymentRequest, PaymentStatus
from gss_contracts.errors import DomainError, ErrorCode
from gss_payment.adapter import UpstreamPaymentAdapter

_REQUIRES_APPROVAL = {
    "id": "cs_live_1",
    "status": "open",
    "submission_attempt": {"state": "requires_approval"},
}

_SETTLED = {
    "id": "cs_live_1",
    "status": "complete",
    "payment_object_status": "succeeded",
    "submission_attempt": {"state": "succeeded"},
    "payment_intent": {"id": "pi_1", "client_secret": "pi_1_secret_x", "status": "succeeded"},
}


class _StubChatGPTClient:
    language = "en-US"
    timezone = "America/New_York"
    timeout = 5.0

    def __init__(self, poll_payload: dict | None = None) -> None:
        self.calls: list[str] = []
        self.poll_payload = poll_payload

    def refresh_session(self) -> bool:
        self.calls.append("refresh")
        return True

    def warmup(self, *, page_url: str = "") -> None:
        self.calls.append("warmup")

    def confirm_checkout(self, **kwargs):
        self.calls.append("confirm")
        return dict(_REQUIRES_APPROVAL)

    def confirm_team_hosted_checkout(self, **kwargs):
        self.calls.append("team_confirm")
        self.team_confirm_args = kwargs
        return dict(_REQUIRES_APPROVAL)

    def finalize_stripe_custom_checkout(self, **kwargs):
        self.calls.append("finalize")
        raise DomainError(ErrorCode.PAYMENT_UNKNOWN_STATE, "批准结账失败: 403")

    def poll_payment_page(self, **kwargs):
        self.calls.append("poll")
        return dict(self.poll_payload) if self.poll_payload else dict(_REQUIRES_APPROVAL)


def _stub_factory(monkeypatch: pytest.MonkeyPatch, client: _StubChatGPTClient) -> None:
    class _Factory:
        @staticmethod
        def from_raw_session(session: str, proxy: str | None = None) -> _StubChatGPTClient:
            return client

    monkeypatch.setattr("gss_payment.adapter.ChatGPTClient", _Factory)


def _pay(plan: str = "team"):
    return asyncio.run(
        UpstreamPaymentAdapter().pay(
            PaymentRequest(
                checkout_url="https://pay.openai.com/c/pay/cs_live_1",
                country="US",
                payment_method_token="ctoken_1",
                session="sess",
                checkout_session_id="cs_live_1",
                publishable_key="pk_live_test",
                plan=plan,
            )
        )
    )


def test_custom_cs_approve_rejected_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """非托管（plus custom）会话的 approve 失败必须原样抛错，不能吞掉。"""

    client = _StubChatGPTClient()
    _stub_factory(monkeypatch, client)

    with pytest.raises(DomainError):
        _pay(plan="plus")


# Dedicated Hosted pipeline and missing context tests are in test_hosted_protocol.py.


def test_application_block_does_not_prove_the_card_was_declined(monkeypatch):
    client = _StubChatGPTClient()
    _stub_factory(monkeypatch, client)
    blocked = {"status": "blocked", "message": "本次扣款尚未确认"}
    monkeypatch.setattr(client, "confirm_checkout", lambda **_: blocked)
    monkeypatch.setattr(client, "finalize_stripe_custom_checkout", lambda **_: blocked)
    assert _pay(plan="plus").status is PaymentStatus.ERROR


@pytest.mark.parametrize("error, expected", [
    ({"code": "api_error", "type": "api_error", "message": "未扣款"}, PaymentStatus.ERROR),
    ({"code": "card_declined", "decline_code": "insufficient_funds"}, PaymentStatus.DECLINED),
])
def test_only_financial_error_evidence_can_decline_the_intent(error, expected):
    from gss_payment.adapter import hosted_payment_result

    payload = {"payment_intent": {"id": "pi_fixture", "last_payment_error": error}}
    assert hosted_payment_result(payload, "cs_fixture").status is expected


@pytest.mark.parametrize("field", ["state", "status", "payment_object_status"])
def test_expired_checkout_without_payment_evidence_is_inconclusive(field):
    from gss_payment.adapter import hosted_payment_result

    assert hosted_payment_result({field: "expired"}, "cs_fixture").status is PaymentStatus.ERROR



def test_internal_approval_error_does_not_repeat_confirmation(monkeypatch) -> None:
    from unittest.mock import Mock

    client = _StubChatGPTClient()
    _stub_factory(monkeypatch, client)
    payload = {"submission_attempt": {"state": "failed", "error": {"code": "checkout_approval_internal_error"}}}
    confirm = Mock(return_value=payload)
    finalize = Mock(return_value=payload)
    monkeypatch.setattr(client, "confirm_checkout", confirm)
    monkeypatch.setattr(client, "finalize_stripe_custom_checkout", finalize)
    try:
        with pytest.raises(DomainError) as error:
            _pay(plan="plus")
    finally:
        # A missing intent in a truncated response is not proof that no money moved.
        confirm.assert_called_once()
    assert error.value.code is ErrorCode.PAYMENT_UNKNOWN_STATE
    finalize.assert_not_called()


@pytest.mark.parametrize("payload,expected", [
    ({"mode": "setup", "state": "succeeded", "payment_object_status": "succeeded",
      "setup_intent": {"id": "seti_fixture", "status": "succeeded"}}, PaymentStatus.PROCESSING),
    ({"setup_intent": {"id": "seti_fixture", "status": "succeeded"}}, PaymentStatus.PROCESSING),
    ({"status": "complete", "payment_status": "unpaid"}, PaymentStatus.PROCESSING),
    ({"payment_status": "paid", "mode": "subscription"}, PaymentStatus.SUCCEEDED),
    ({"payment_intent": {"id": "pi_fixture", "status": "succeeded"}}, PaymentStatus.SUCCEEDED),
])
def test_hosted_success_requires_payment_evidence(payload, expected) -> None:
    from gss_payment.adapter import hosted_payment_result

    assert hosted_payment_result(payload, "cs_test_fixture").status is expected


@pytest.mark.parametrize("payload", [
    {"status": "complete", "submission_attempt": {"state": "succeeded"}},
    {"status": "success", "payment_intent": {"id": "pi_1", "client_secret": "pi_1_secret_x"}},
])
def test_custom_confirmation_without_payment_evidence_is_not_resubmitted(monkeypatch, payload) -> None:
    from unittest.mock import Mock

    client = _StubChatGPTClient()
    _stub_factory(monkeypatch, client)
    monkeypatch.setattr(client, "confirm_checkout", lambda **_: payload)
    monkeypatch.setattr(client, "finalize_stripe_custom_checkout", lambda **_: payload)
    repeat = Mock(return_value={"status": "succeeded"})
    monkeypatch.setattr(client, "confirm_stripe_intent", repeat, raising=False)
    result = _pay(plan="plus")
    repeat.assert_not_called()
    assert result.status in {PaymentStatus.PROCESSING, PaymentStatus.ERROR}


@pytest.mark.parametrize("change, expected", [
    ({}, True), ({"session_id": None}, False), ({"id": "cs_test_other"}, False),
    ({"payment_status": "paid"}, False), ({"payment_intent": "pi_started"}, False),
    ({"submission_attempt": {"state": "processing"}}, False),
    ({"payment_object_status": "processing"}, False),
])
def test_only_explicit_unattempted_bound_checkout_is_open(change, expected):
    from gss_payment.adapter import hosted_payment_result
    data = {"session_id": "cs_fixture", "status": "open", "payment_status": "unpaid", **change}
    assert hosted_payment_result(data, "cs_fixture").hosted_open_unpaid is expected


@pytest.mark.parametrize("change, has_evidence", [
    ({}, True), ({"session_id": "cs_foreign"}, False), ({"currency": "eur"}, False),
])
def test_custom_plus_preserves_only_observed_settlement(monkeypatch, change, has_evidence):
    from gss_contracts.adapter import receipt_matches_order
    client = _StubChatGPTClient()
    _stub_factory(monkeypatch, client)
    raw = {"session_id": "cs_live_1", "payment_status": "paid", "currency": "usd",
           "payment_intent": {"id": "pi_test", "object": "payment_intent", "status": "succeeded",
                              "currency": "usd", "amount_received": 2000}, **change}
    monkeypatch.setattr(client, "confirm_checkout", lambda **_: raw)
    monkeypatch.setattr(client, "finalize_stripe_custom_checkout", lambda **_: raw)
    record = _pay(plan="plus").receipt_evidence
    assert receipt_matches_order(record, checkout_id="cs_live_1", payment_id="pi_test",
                                 amount="20", currency="USD") is has_evidence
    assert not receipt_matches_order(record, checkout_id="cs_live_1", payment_id="pi_other",
                                     amount="20", currency="USD")


@pytest.mark.parametrize("plan", ["team", "plus"])
@pytest.mark.parametrize("settled", [False, True])
def test_expired_lease_keeps_confirm_reply_without_new_poll_or_finalize(monkeypatch, plan, settled):
    from types import SimpleNamespace
    current = True
    reads = []
    raw = ({"session_id": "cs_live_1", "payment_status": "paid",
            "payment_intent": {"object": "payment_intent", "id": "pi_fixture", "status": "succeeded",
                               "amount_received": 2000, "currency": "usd"}} if settled else _REQUIRES_APPROVAL)

    def confirm(*args, **kwargs):
        nonlocal current
        current = False  # Simulate vault expiry during the original outbound confirmation.
        return raw
    def read(*args, **kwargs):
        reads.append("unexpected")
        return {}

    client = _StubChatGPTClient()
    client.close = lambda: None
    _stub_factory(monkeypatch, client)
    monkeypatch.setattr(client, "confirm_checkout", confirm)
    monkeypatch.setattr(client, "finalize_stripe_custom_checkout", read)
    monkeypatch.setattr("gss_payment.adapter.TeamHostedClient", lambda _: SimpleNamespace(confirm=confirm, poll=read))
    result = asyncio.run(UpstreamPaymentAdapter().pay(PaymentRequest(
        checkout_url="https://example.invalid/fixture", country="US", plan=plan, session="fixture",
        checkout_session_id="cs_live_1", publishable_key="pk_fixture", payment_method_token="ctoken_fixture",
        team_context={"session_id": "cs_live_1", "publishable_key": "pk_fixture"},
        credentials_current=lambda: current,
    )))
    assert reads == []
    if settled:
        assert result.status is PaymentStatus.SUCCEEDED and result.receipt_evidence.amount == 20
    else:
        assert result.status is not PaymentStatus.SUCCEEDED and result.receipt_evidence is None


@pytest.mark.parametrize("outer, inner", [
    ({"payment_intent_id": "pi_other"}, {}), ({}, {"checkout_session_id": "cs_other"}),
    ({"checkout_id": "cs_other"}, {}), ({"intent_id": "pi_other"}, {}),
])
def test_contradictory_cross_references_are_never_payment_evidence(outer, inner):
    from gss_payment.adapter import hosted_payment_result
    payload = {"session_id": "cs_this", "payment_status": "paid", **outer,
               "payment_intent": {"id": "pi_this", "status": "succeeded", "amount_received": 2000,
                                  "currency": "usd", **inner}}
    assert hosted_payment_result(payload, "cs_this").receipt_evidence is None
