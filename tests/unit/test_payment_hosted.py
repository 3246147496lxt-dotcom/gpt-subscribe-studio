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
