import pytest

from gss_api.schemas import QuickPayRequest
from gss_api.services.quick_pay_intent import quick_pay_intent_digest


def request(**data):
    return QuickPayRequest.model_validate({
        "session": "synthetic-private-session",
        "plan": "plus",
        "debug_checkout_only": True,
        **data,
    })


def digest(req, fingerprint="synthetic-account"):
    return quick_pay_intent_digest(req, fingerprint)


def test_intent_format_is_stable_across_restarts_and_default_spelling():
    # A silent format change would break persisted replays.
    assert digest(request()) == "v1:c5ce6bcf2237e9cca943e05a9d84b1692aec9d24d5f8d1aed773d987efbfd9e2"
    assert digest(request(plan="PRO5", billing_country=" us ", price_interval="YEAR")) == digest(
        request(plan="pro_lite", billing_country="US", price_interval="year", auto_cancel_renew=True)
    )
    assert digest(request(promo_code="promo ", workspace_name=" A ")) == digest(
        request(workspace_name="A", promo_code="promo")
    )


@pytest.mark.parametrize("changes", [
    {"plan": "pro_lite"},
    {"billing_country": "US"}, {"billing_country": ""}, {"exit_country": "US"},
    {"mode": {"account_source": "platform"}}, {"mode": {"card_source": "platform"}},
    {"mode": {"billing_mode": "promo"}}, {"mode": {"risk_acknowledged": True}},
    {"seat_quantity": 2}, {"price_interval": "year"}, {"workspace_name": "workspace"},
    {"promo_code": "a"}, {"promo_campaign_id": "b"}, {"auto_cancel_renew": False},
    {"debug_checkout_only": False, "card": {"number": "0" * 16, "cvc": "000", "exp_month": "1", "exp_year": "30"}},
])
def test_different_business_intent_conflicts(changes):
    assert digest(request()) != digest(request(**changes))


def test_exact_branch_account_and_case_sensitive_strings_are_not_collapsed():
    assert digest(request(plan="team")) != digest(request(plan="TEAM"))
    assert digest(request(promo_code="Promo")) != digest(request(promo_code="PROMO"))
    assert digest(request(mode={"account_source": "customer_session"})) != digest(
        request(mode={"account_source": "CUSTOMER_SESSION"})
    )
    assert digest(request(mode={"unknown": "not-retained"})) != digest(request())
    assert digest(request(mode={"unknown": "first"})) == digest(request(mode={"other": "second"}))
    assert digest(request(), "account-1") != digest(request(), "account-2")


def test_private_inputs_are_not_hashed_or_read():
    # Even reading these attributes and later redacting them would fail the test.
    class Guarded(QuickPayRequest):
        def __getattribute__(self, name):
            if name in {"card", "session", "billing", "model_dump", "model_dump_json"}:
                raise AssertionError("attempted to read private fields or serialize request")
            return super().__getattribute__(name)

    raw = dict(plan="plus", session="not-a-session", card={
        "number": "0" * 16, "cvc": "000", "exp_month": "1", "exp_year": "30",
    })
    normal = QuickPayRequest(**raw)
    guarded = Guarded(**{**raw, "session": "different-session", "card": {
        "number": "1" * 16, "cvc": "111", "exp_month": "2", "exp_year": "31",
    }, "billing": {"email": "test@example.invalid", "name": "example", "line1": "synthetic", "city": "unused",
                   "postal_code": "00000", "country": "US"}})
    assert digest(normal) == digest(guarded)
