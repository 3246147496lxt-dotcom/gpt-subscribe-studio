"""Request-for-request check of the HAR-faithful Plus flow (offline)."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from types import SimpleNamespace

from gss_core.har_device import DeviceProfile
from gss_core.plus_har_protocol import PlusHarProtocol, run_har_context
from gss_core.stripe_protocol import PaymentOutcome

CHECKOUT_ID = "oaics_fixturecheckout"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROCESSOR = "openai_llc"


class FakeResponse:
    def __init__(self, payload, status: int = 200):
        self.status_code = status
        self._payload = payload
        self.content = b"{}" if payload is not None else b""
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


class StubClient:
    access_token = "token_fixture"
    account_id = "account_fixture"
    device_id = "did_fixture"
    sentinel_token = "sentinel_fixture"
    sentinel_so = "so_fixture"
    language = "en-US"
    timezone = "Asia/Tokyo"
    proxy = None
    extra_cookies: dict = {}
    session_token = ""

    def refresh_session(self):
        return False

    def hydrate(self):
        return None

    def subscription_payload(self):
        return {"plan_type": "plus", "is_active": True,
                "entitlement": {"has_active_subscription": True}}

    def retrieve_stripe_intent(self, **kwargs):
        return {"id": "pi_fixture", "status": "succeeded"}

    def close(self):
        pass


class FakeApp:
    def __init__(self):
        self.calls = []
        self.page_status = "open"

    def request(self, method, url, **kwargs):
        route = url.split("chatgpt.com", 1)[-1]
        self.calls.append((method, route, kwargs.get("json")))
        if method == "GET" and route.startswith("/backend-api/payments/checkout/"):
            return FakeResponse({
                "checkout_session_id": CHECKOUT_ID,
                "publishable_key": "pk_live_fixture",
                "processor_entity": PROCESSOR,
                "customer_session_client_secret": "cuss_fixture",
                "confirm_return_url": ("https://chatgpt.com/checkout/verify?"
                                       f"stripe_session_id={CHECKOUT_ID}&processor_entity={PROCESSOR}&plan_type=plus"),
                "status": self.page_status,
                "payment_status": "paid" if self.page_status == "complete" else "unpaid",
                **({"amount_paid": 1363, "payment_intent": "pi_fixture"} if self.page_status == "complete" else {}),
                "checkout_state": {"currency": "jpy", "total": {"total": {
                    "minorUnitsAmount": 1363 if self.page_status == "complete" else 1499}}},
            })
        if route == "/backend-api/payments/checkout/taxes":
            return FakeResponse({"checkout_session": {
                "checkout_session_id": CHECKOUT_ID,
                "publishable_key": "pk_live_fixture",
                "checkout_state": {"currency": "jpy", "total": {"total": {"minorUnitsAmount": 1363}}},
            }})
        if route == "/backend-api/payments/checkout/confirm":
            self.page_status = "complete"
            return FakeResponse({
                "status": "success",
                "type": "payment_intent",
                "payment_intent": {"id": "pi_fixture", "status": "requires_confirmation"},
                "client_secret": "pi_fixture_secret_fixture",
                "confirm_return_url": ("https://chatgpt.com/checkout/verify?"
                                       f"stripe_session_id={CHECKOUT_ID}&processor_entity={PROCESSOR}&plan_type=plus"),
            })
        if route == "/backend-api/payments/checkout_session":
            return FakeResponse({"success": True, "created_new_subscription": False})
        return FakeResponse({})


class FakeStripe:
    def __init__(self):
        self.calls = []
        self.forms = {}

    def get(self, url, **kwargs):
        route = url.split("stripe.com", 1)[-1]
        self.calls.append(("GET", route))
        if route.startswith("/v1/elements/sessions"):
            return FakeResponse({
                "session_id": "elements_session_fixture",
                "customer": {"id": "cus_fixture"},
                "passive_captcha": {"site_key": "sitekey_fixture", "rqdata": "rqdata_fixture"},
            })
        return FakeResponse({})

    def post(self, url, **kwargs):
        route = url.split("stripe.com", 1)[-1]
        self.calls.append(("POST", route))
        body = kwargs.get("content") or ""
        self.forms[(route, len(self.calls))] = parse_qs(body if isinstance(body, str) else body.decode())
        if route == "/v1/confirmation_tokens":
            return FakeResponse({"id": "ctoken_fixture", "payment_method_preview": {"id": "pm_fixture"}})
        if "/confirm" in route:
            return FakeResponse({"id": "pi_fixture", "status": "succeeded"})
        return FakeResponse({})


class FakeTelemetry:
    def __init__(self, ctx):
        self.ctx = ctx
        self.calls = []

    def warm_assets(self):
        self.calls.append(("warm_assets",))

    def register(self):
        self.calls.append(("register",))

    def emit_through(self, phase):
        self.calls.append(("emit", phase))

    def beat(self, times=2):
        self.calls.append(("beat", times))

    def ces_register(self):
        self.calls.append(("ces",))


class Harness(PlusHarProtocol):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.app = FakeApp()
        self.stripe = FakeStripe()
        self.sentinel_flows = []
        self.order = []

    def _prepare(self):
        self.client = StubClient()
        self.telemetry = FakeTelemetry(SimpleNamespace(amount=0, customer_id="", hcaptcha_token=""))
        self.order.append(("prepare",))

    def _prelude(self):
        self.telemetry.ces_register()
        self.order.append(("ces",))

    def _sentinel(self, flow):
        self.sentinel_flows.append(flow)
        self.order.append(("sentinel", flow))

    def _radar_token(self):
        return "P1_fixture_token"

    def _app(self, method, route, *, json_body=None, params=None, telemetry=None,
             referer="https://chatgpt.com/"):
        self.order.append(("app", method, route))
        return self.app.request(method, "https://chatgpt.com" + route, json=json_body, params=params)

    def _stripe_session(self):
        return self.stripe

    def _flush(self, phase):
        self.telemetry.emit_through(phase)
        self.order.append(("emit", phase))


def build_harness(**overrides):
    params = dict(
        session="raw-session-fixture",
        checkout_url=f"https://chatgpt.com/checkout/{PROCESSOR}/{CHECKOUT_ID}",
        publishable_key="pk_live_fixture",
        card={"number": "4242424242424242", "cvc": "123", "exp_month": "12", "exp_year": "2040"},  # release-check: ignore
        billing={"name": "Fixture Holder", "email": "fixture@example.test", "line1": "1 Fixture St",
                 "city": "Portland", "state": "OR", "postal_code": "97211", "country": "US"},
        currency="JPY",
        country="US",
        device=DeviceProfile.random(country="US", seed="flow"),
        telemetry="full",
    )
    params.update(overrides)
    return Harness(**params)


def test_flow_order_matches_capture():
    engine = build_harness()
    result = engine.pay()

    assert result.state == "paid"
    assert result.intent_status == "succeeded"

    steps = [(item[0], item[1] if len(item) > 1 else "") for item in engine.order]
    expected = [
        ("prepare", ""),
        ("ces", ""),
        ("sentinel", "chatgpt_checkout"),
        ("app", "GET"),  # read checkout before Elements
    ]
    assert steps[: len(expected)] == expected

    routes = [item[2] for item in engine.order if item[0] == "app"]
    assert routes[0].startswith("/backend-api/payments/checkout/")
    assert "/backend-api/payments/checkout/taxes" in routes
    assert "/backend-api/payments/checkout/update_tax_id" in routes
    assert routes.index("/backend-api/payments/checkout/taxes") < routes.index(
        "/backend-api/payments/checkout/confirm")
    assert "/backend-api/payments/checkout_session" in routes

    assert engine.sentinel_flows == [
        "chatgpt_checkout",          # page boot
        "chatgpt_checkout",          # right after reading the checkout
        "checkout_session_approval",  # before the app confirm
        "chatgpt_checkout",
        "checkout_session_approval",  # before the intent confirm
    ]

    stripe_calls = engine.stripe.calls
    assert stripe_calls[0][0] == "GET" and stripe_calls[0][1].startswith("/v1/elements/sessions")
    lookups = [c for c in stripe_calls if c[1] == "/v1/consumers/sessions/lookup"]
    assert len(lookups) == 3
    assert ("GET", "/edge-internal/card-metadata") in stripe_calls
    assert ("POST", "/v1/confirmation_tokens") in stripe_calls
    assert ("POST", "/v1/payment_intents/pi_fixture/confirm") in stripe_calls

    phases = [item[1] for item in engine.order if item[0] == "emit"]
    assert phases == [98, 160, 215, 266, 293, 329, 343]


def test_confirmation_token_body_matches_capture():
    engine = build_harness()
    engine.pay()

    form = None
    for (route, _), values in engine.stripe.forms.items():
        if route == "/v1/confirmation_tokens":
            form = values
    assert form is not None

    device = engine.device
    assert form["payment_method_data[type]"] == ["card"]
    assert form["payment_method_data[card][number]"] == ["4242424242424242"]  # release-check: ignore
    assert form["payment_method_data[allow_redisplay]"] == ["always"]
    assert form["payment_method_data[guid]"] == [device.guid]
    assert form["payment_method_data[muid]"] == [device.muid]
    assert form["payment_method_data[sid]"] == [device.sid]
    assert form["payment_method_data[radar_options][hcaptcha_token]"] == ["P1_fixture_token"]
    assert form["payment_method_data[pasted_fields]"] == ["number"]
    assert form["setup_future_usage"] == ["off_session"]
    assert form["set_as_default_payment_method"] == ["false"]
    assert form["client_context[customer]"] == ["cus_fixture"]
    assert form["client_attribution_metadata[merchant_integration_source]"] == ["elements"]
    assert form["client_attribution_metadata[payment_intent_creation_flow]"] == ["deferred"]
    assert form["client_attribution_metadata[elements_session_id]"] == ["elements_session_fixture"]
    assert form["_stripe_version"] == ["2025-03-31.basil"]


def test_confirm_and_intent_bodies_match_capture():
    engine = build_harness()
    engine.pay()

    confirm = next(body for method, route, body in engine.app.calls
                   if route == "/backend-api/payments/checkout/confirm")
    assert confirm == {
        "checkout_session_id": CHECKOUT_ID,
        "save_payment_method_for_reuse": True,
        "confirm_token": "ctoken_fixture",
        "selected_payment_method_type": "card",
    }

    intent_form = None
    for (route, _), values in engine.stripe.forms.items():
        if route == "/v1/payment_intents/pi_fixture/confirm":
            intent_form = values
    assert intent_form is not None
    assert intent_form["confirmation_token"] == ["ctoken_fixture"]
    assert intent_form["client_secret"] == ["pi_fixture_secret_fixture"]
    assert intent_form["client_attribution_metadata[merchant_integration_source]"] == ["l1"]
    assert intent_form["client_attribution_metadata[client_session_id]"] == [engine.device.stripe_js_id]
    return_url = intent_form["return_url"][0]
    assert return_url.startswith("https://chatgpt.com/checkout/verify?")
    assert f"stripe_session_id={CHECKOUT_ID}" in return_url
    assert "source=chatgpt_web_merged" in return_url


def test_declined_intent_maps_to_card_declined():
    engine = build_harness()

    original_post = engine.stripe.post

    def decline(url, **kwargs):
        route = url.split("stripe.com", 1)[-1]
        if not route.endswith("/confirm"):
            return original_post(url, **kwargs)
        engine.stripe.calls.append(("POST", route))
        return FakeResponse({"error": {"code": "card_declined", "decline_code": "generic_decline",
                                       "message": "Your card was declined."}}, status=402)

    engine.stripe.post = decline  # type: ignore[assignment]
    result = engine.pay()
    assert result.state == "card_declined"
    assert result.decline_code == "generic_decline"


def test_read_only_never_posts_payment(monkeypatch):
    engine = build_harness(read_only=True)
    result = engine.pay()
    assert result.state == "pending"
    assert not engine.stripe.calls, "status query must not touch Stripe"
    posts = [c for c in engine.app.calls if c[0] == "POST"]
    assert posts == []


def test_run_har_context_persists_device_and_ids(monkeypatch):
    calls = {}

    def fake_pay(self):
        calls["device"] = self.device.to_dict()
        self.ctoken = "ctoken_ctx"
        self.intent_id = "pi_ctx"
        self.intent_secret = "pi_ctx_secret"
        self.amount_minor = 1363
        self.confirmed = {"status": "success"}
        self.stage = "payment"
        return PaymentOutcome(state="paid", intent_status="succeeded")

    monkeypatch.setattr(PlusHarProtocol, "pay", fake_pay)
    context = {}
    spec = {
        "checkout_url": f"https://chatgpt.com/checkout/{PROCESSOR}/{CHECKOUT_ID}",
        "checkout_id": CHECKOUT_ID,
        "publishable_key": "pk_live_fixture",
        "currency": "JPY",
        "country": "US",
        "processor": PROCESSOR,
        "customer_session_client_secret": "cuss_fixture",
    }
    payload = run_har_context(StubClient(), context, spec, card={"number": "4242"},
                              billing={"email": "fixture@example.test", "country": "US"})
    assert payload["state"] == "paid"
    assert payload["stage"] == "payment"
    assert context["har_device"] == calls["device"]
    assert context["confirmation_token"] == "ctoken_ctx"
    assert context["intent_id"] == "pi_ctx"
    assert context["amount_minor"] == 1363
    assert context["app_confirmation_started"] is True


def test_elements_session_parses_live_body():
    """The live elements/sessions body (captured 2026-10-30) nests the cus_ id."""
    engine = build_harness()
    payload = json.loads((FIXTURES / "elements_session_live.json").read_text())

    class LiveStripe:
        def get(self, url, **kwargs):
            return FakeResponse(payload)

    engine._stripe_session = lambda: LiveStripe()  # type: ignore[assignment]
    engine._elements_session()

    assert engine.elements_session_id == payload["session_id"]
    assert engine.device.elements_session_id == payload["session_id"]
    assert engine.customer_id == "cus_fixture"
    assert engine.device.elements_session_config_id == payload["config_id"]
    assert engine.passive_captcha["site_key"] == payload["passive_captcha"]["site_key"]
    assert engine.passive_captcha["rqdata"] == ""


def test_ctoken_card_decline_maps_to_card_declined():
    engine = build_harness()
    engine._prepare()
    engine._radar_token = lambda: ""  # type: ignore[assignment]

    class DecliningStripe:
        def post(self, url, **kwargs):
            return FakeResponse({"error": {
                "code": "card_declined", "decline_code": "live_mode_test_card",
                "message": "Your card was declined. Your request was in live mode, but used a known test card.",
            }}, status=400)

    engine._stripe_session = lambda: DecliningStripe()  # type: ignore[assignment]
    from gss_contracts.errors import DomainError
    with pytest.raises(DomainError) as excinfo:
        engine._confirmation_token()
    assert str(excinfo.value.code) == "payment_declined"


def test_settle_reads_live_decline_body():
    """Live 402 body (2026-10-30) carries the PaymentIntent inside error."""
    engine = build_harness()
    engine.amount_minor = 2918
    payload = json.loads((FIXTURES / "intent_confirm_declined.json").read_text())
    engine.raw["intent"] = payload

    result = engine._settle()
    assert result.state == "card_declined"
    assert result.code == "card_declined"
    assert result.decline_code == "generic_decline"
    assert result.intent_status == "requires_payment_method"
    assert result.charge_id == "ch_fixture"
    assert (result.payment_method_id or "").startswith("pm_")


@pytest.mark.parametrize("field", ["intent.error", "last_error"])
def test_generic_processor_error_is_not_a_confirmed_decline(field):
    engine = build_harness()
    error = {"type": "api_error", "code": "processing_error", "message": "未扣款"}
    if field == "intent.error":
        engine.raw["intent"] = {"error": error}
    else:
        engine.last_error = error
    result = engine._settle()
    assert result.state == "unknown"
    assert result.intent_status is None


@pytest.mark.parametrize(("intent", "state"), [
    ({"id": "pi_fixture", "status": "succeeded", "amount_received": 1363, "currency": "jpy"}, "pending"),
    ({"id": "pi_fixture", "status": "succeeded"}, "unknown"),
    ({"id": "pi_fixture", "status": "requires_payment_method"}, "unknown"),
    ({"id": "pi_fixture", "status": "canceled"}, "unknown"),
    ({"id": "pi_fixture", "status": "requires_payment_method",
      "last_payment_error": {"code": "card_declined", "decline_code": "generic_decline"}}, "card_declined"),
    ({"id": "pi_unrelated", "status": "succeeded"}, "unknown"),
    ({}, "unknown"),
])
def test_read_only_known_payment_intent_evidence_keeps_payment_stage(monkeypatch, intent, state):
    engine = build_harness(read_only=True)
    engine.client = StubClient()
    engine.intent_id, engine.intent_secret = "pi_fixture", "fixture-secret"
    engine.receipt_expected_minor = 1363
    monkeypatch.setattr(engine.client, "retrieve_stripe_intent", lambda **_: intent)
    result = engine._read_only_settle()
    assert (engine.stage, result.state) == ("payment", state)
    observed_status = intent.get("status")
    has_reportable_status = (state != "unknown" or observed_status in {"requires_payment_method", "canceled"})
    expected_status = observed_status if has_reportable_status and intent.get("id") == "pi_fixture" else None
    assert result.intent_status == expected_status
    if expected_status in {"requires_payment_method", "canceled"}:
        assert result.code == ("card_declined" if state == "card_declined" else "intent_status_unconfirmed")
    assert not engine.app.calls and not engine.stripe.calls


def test_settle_flags_amount_mismatch():
    engine = build_harness()
    engine.amount_minor = 1363
    payload = json.loads((FIXTURES / "intent_confirm_declined.json").read_text())
    engine.raw["intent"] = payload

    result = engine._settle()
    assert result.state == "unknown"
    assert "不一致" in (result.message or "")


def test_quote_guard_pauses_when_tax_raises_total():
    h = build_harness()
    h.expected_amount_minor = 2178
    h.amount_minor = 2300
    out = h._quote_guard()
    assert out is not None
    assert out.state == "pending"
    assert h.stage == "checkout"
    assert "税费" in (out.message or "")


def test_quote_guard_allows_equal_or_lower_total():
    h = build_harness()
    h.expected_amount_minor = 3000
    h.amount_minor = 2891
    assert h._quote_guard() is None
    h.amount_minor = 3000
    assert h._quote_guard() is None


def test_quote_guard_skipped_without_expected():
    h = build_harness()
    h.expected_amount_minor = 0
    h.amount_minor = 9999
    assert h._quote_guard() is None


def test_outcome_from_payment_carries_intent_id():
    from gss_core.plus_har_protocol import outcome_from_payment

    payload = outcome_from_payment(
        PaymentOutcome(state="paid", intent_status="succeeded", intent_id="pi_test_123")
    )
    assert payload["payment_intent_id"] == "pi_test_123"
    assert payload["intent_status"] == "succeeded"


@pytest.mark.parametrize("boundary", ["create", "read", "taxes"])
def test_existing_response_billing_conflict_stops_before_confirmation(monkeypatch, boundary):
    from gss_contracts.errors import DomainError, ErrorCode

    engine = build_harness()
    if boundary == "create":
        engine.checkout_id = ""
    original = engine._app

    def app(method, route, **kwargs):
        reply = original(method, route, **kwargs)
        at = ("create" if method == "POST" and route.endswith("/checkout") else
              "taxes" if route.endswith("/taxes") else
              "read" if method == "GET" and "/checkout/" in route else None)
        if at == boundary:
            reply = FakeResponse({**reply.json(), "billing_details": {"country": "FR"}})
        return reply

    monkeypatch.setattr(engine, "_app", app)
    with pytest.raises(DomainError) as exc:
        engine.pay()
    assert exc.value.code is ErrorCode.BILLING_COUNTRY_MISMATCH
    assert not engine.confirmation_started
    assert not any("/confirm" in route for _method, route, _body in engine.app.calls)


def test_reset_web_session_preserves_checkout_cookies():
    from types import SimpleNamespace

    from gss_core.plus_har_protocol import PlusHarProtocol

    client = SimpleNamespace(extra_cookies={"oai-did": "DID"}, session_token="OLD")
    engine = PlusHarProtocol(session="", client=client)
    engine._web = SimpleNamespace(cookies={"__cf_bm": "cf", "oai-sc": "sc", "__Secure-next-auth.session-token": "NEW"})
    engine._snapshot_web_cookies()
    assert client.extra_cookies["__cf_bm"] == "cf"
    assert client.extra_cookies["oai-sc"] == "sc"
    assert client.session_token == "NEW"


def test_sentinel_failure_is_fail_closed():
    from gss_contracts.errors import DomainError, ErrorCode
    for err in (
        RuntimeError("reqfail"),
        DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "无法签发 ChatGPT 风控令牌（Sentinel）：proxy fetch: HTTP 403 forbidden"),
        DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "无法签发 ChatGPT 风控令牌（Sentinel）：proxy fetch: HTTP 400 bad request"),
    ):
        calls = 0

        def _fail(**kwargs):
            nonlocal calls
            calls += 1
            raise err

        engine = PlusHarProtocol(session="", client=SimpleNamespace(mint_sentinel=_fail))
        engine.credentials_current = lambda: True
        with pytest.raises(DomainError) as caught:
            engine._sentinel("checkout_session_approval")
        assert caught.value.code is ErrorCode.SENTINEL_CHALLENGE_FAILED
        assert calls == 1  # Non-transient / 4xx failure must not retry.


def test_sentinel_retries_once_on_transient_timeout_and_succeeds(monkeypatch):
    from gss_contracts.errors import DomainError, ErrorCode
    calls = 0

    def _flaky(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "无法签发 ChatGPT 风控令牌（Sentinel）：sentinel bridge timed out。")

    engine = PlusHarProtocol(session="", client=SimpleNamespace(mint_sentinel=_flaky))
    engine.credentials_current = lambda: True
    monkeypatch.setattr("gss_core.plus_har_protocol.sentinel_ping", lambda *a, **k: None)
    engine._sentinel("chatgpt_checkout")
    assert calls == 2


def test_sentinel_transient_timeout_fails_closed_after_single_retry():
    from gss_contracts.errors import DomainError, ErrorCode
    calls = 0

    def _always_timeout(**kwargs):
        nonlocal calls
        calls += 1
        raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "sentinel bridge timed out")

    engine = PlusHarProtocol(session="", client=SimpleNamespace(mint_sentinel=_always_timeout))
    engine.credentials_current = lambda: True
    with pytest.raises(DomainError) as caught:
        engine._sentinel("chatgpt_checkout")
    assert caught.value.code is ErrorCode.SENTINEL_CHALLENGE_FAILED
    assert calls == 2


def test_sentinel_default_timeout_is_35s(monkeypatch):
    from gss_core.chatgpt import ChatGPTClient
    monkeypatch.delenv("GSS_SENTINEL_TIMEOUT", raising=False)
    captured = {}

    def _fake_mint(**kwargs):
        captured.update(kwargs)
        return {"p": "p-ok", "c": "c-ok"}

    monkeypatch.setattr("gss_core.chatgpt.mint", _fake_mint)
    client = ChatGPTClient(access_token="at-test")
    assert client.timeout == 15.0
    client.mint_sentinel(flow="chatgpt_checkout", ping=False)
    assert captured["timeout_s"] == 35.0

    monkeypatch.setenv("GSS_SENTINEL_TIMEOUT", "50")
    captured.clear()
    client.mint_sentinel(flow="chatgpt_checkout", ping=False)
    assert captured["timeout_s"] == 50.0


def test_subscription_limit_confirm_is_terminal_rejection():
    from gss_contracts.errors import DomainError, ErrorCode
    engine = PlusHarProtocol(session="", checkout_id=CHECKOUT_ID)
    engine.ctoken = "ctoken"
    engine._reset_web_session = lambda: None
    engine._app = lambda *a, **k: FakeResponse({"status": "subscription_limit"})
    with pytest.raises(DomainError) as caught:
        engine._app_confirm()
    assert caught.value.code is ErrorCode.SUBSCRIPTION_LIMIT


def test_settle_marks_subscription_limit_rejected():
    engine = PlusHarProtocol(session="", checkout_id=CHECKOUT_ID)
    engine.raw = {"confirm": {"status": "subscription_limit"}, "intent": {}}
    out = engine._settle()
    assert out.state == "rejected"
    assert out.code == "subscription_limit"


def test_string_payment_intent_is_retrieved_and_confirmed():
    engine = build_harness()
    engine.client = StubClient()
    original = engine.app.request

    def string_confirm(method, url, **kwargs):
        if url.endswith("/backend-api/payments/checkout/confirm"):
            return FakeResponse({
                "status": "success",
                "type": "payment_intent",
                "payment_intent": "pi_fixture",
                "client_secret": "pi_fixture_secret_fixture",
            })
        return original(method, url, **kwargs)

    engine.app.request = string_confirm
    engine._app_confirm()
    assert engine.intent_id == "pi_fixture"
    assert engine.intent_secret == "pi_fixture_secret_fixture"
    assert engine.raw["intent"]["status"] == "succeeded"
    assert engine.confirmed["payment_intent"]["id"] == "pi_fixture"


# ── telemetry gate before any Stripe write ─────────────────────────
class _OfflineBeacon:
    """m.stripe.com is unreachable; no server-issued device ids exist."""

    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(url)
        raise OSError("offline")


class _BeaconServer:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(url)
        return FakeResponse(self.payload)


def _telemetry(engine, http, **overrides):
    from gss_core.har_telemetry import StripeTelemetry, TelemetryContext

    ctx = TelemetryContext(
        device=engine.device, key="pk_live_fixture", currency="jpy", amount=100,
        account_id="account_fixture", email="fixture@example.test", country="US",
    )
    telemetry = StripeTelemetry(http, ctx)
    for name, value in overrides.items():
        setattr(telemetry, name, value)
    return telemetry


@pytest.mark.parametrize(("registered", "adopted"), [(False, False), (True, False)])
def test_confirmation_fails_closed_without_server_device_ids(registered, adopted):
    from gss_contracts.errors import DomainError

    engine = build_harness()
    engine._prepare()
    http = _OfflineBeacon()
    engine.telemetry = _telemetry(engine, http, registered=registered, server_ids_adopted=adopted)

    with pytest.raises(DomainError) as caught:
        engine._confirmation_token()

    assert caught.value.detail["code"] == "telemetry_unavailable"
    assert caught.value.detail["stage"] == "confirmation_token"
    assert engine.stripe.calls == []
    assert engine.ctoken == ""
    assert not engine.confirmation_started


def test_confirmation_stops_after_failed_register_without_local_ids():
    from gss_contracts.errors import DomainError

    engine = build_harness()
    engine._prepare()
    telemetry = _telemetry(engine, _OfflineBeacon())
    telemetry.register()

    assert telemetry.registered is True
    assert telemetry.server_ids_adopted is False
    engine.telemetry = telemetry

    with pytest.raises(DomainError) as caught:
        engine._confirmation_token()
    assert caught.value.detail["code"] == "telemetry_unavailable"
    assert engine.stripe.calls == []


def test_confirmation_proceeds_with_server_issued_device_ids():
    engine = build_harness()
    engine._prepare()
    http = _BeaconServer({"guid": "guid_server", "muid": "muid_server", "sid": "sid_server"})
    telemetry = _telemetry(engine, http)
    telemetry.register()
    engine.telemetry = telemetry

    assert telemetry.server_ids_adopted is True
    assert engine._confirmation_token() == "ctoken_fixture"
    assert engine.device.guid == "guid_server"
    assert [call[0] for call in engine.stripe.calls] == ["POST"]


def test_telemetry_gate_is_skipped_only_when_the_mode_is_off():
    engine = build_harness(telemetry="off")
    engine._prepare()
    engine.telemetry = _telemetry(engine, _OfflineBeacon())

    assert engine._confirmation_token() == "ctoken_fixture"


def test_checkout_confirm_sends_dual_sentinel_and_server_confirm_headers():
    """2026-11-26 Sample 5 [1151]: checkout/confirm carries approval token + checkout SO token."""
    engine = PlusHarProtocol(
        session="",
        checkout_id=CHECKOUT_ID,
        publishable_key="pk_live_fixture",
        currency="PHP",
        country="US",
        device=DeviceProfile.random(country="PH", seed="confirm-hdrs"),
        client=StubClient(),
    )
    engine.client.sentinel_token = '{"flow":"checkout_session_approval","p":"p1","c":"c1"}'
    engine.client.sentinel_so = '{"flow":"chatgpt_checkout","so":"so1","c":"c2"}'

    captured = {}

    class CaptureWeb:
        def request(self, method, url, **kwargs):
            captured["headers"] = dict(kwargs.get("headers") or {})
            return FakeResponse({
                "status": "success",
                "type": "payment_intent",
                "client_secret": "pi_live_fixture_secret_abc",
                "confirm_return_url": f"https://chatgpt.com/checkout/verify?stripe_session_id={CHECKOUT_ID}",
            })

        def close(self):
            pass

    engine._web_session = lambda: CaptureWeb()  # type: ignore[assignment]
    engine._reset_web_session = lambda: None  # type: ignore[assignment]
    engine._hydrate_confirmed_intent = lambda: None  # type: ignore[assignment]
    engine.ctoken = "ctoken_fixture"
    engine._app_confirm()

    hdrs = {k.lower(): v for k, v in captured["headers"].items()}
    assert hdrs["openai-sentinel-token"] == '{"flow":"checkout_session_approval","p":"p1","c":"c1"}'
    assert hdrs["openai-sentinel-so-token"] == '{"flow":"chatgpt_checkout","so":"so1","c":"c2"}'
    assert hdrs["x-oaics-server-confirm-card"] == "true"
    assert hdrs["x-openai-checkout-method-retry"] == "1"
    # Cleared immediately after confirm so stale tokens cannot leak.
    assert engine.client.sentinel_token == ""
    assert engine.client.sentinel_so == ""


def test_publishable_key_payment_intent_amount_settles_receipt_without_amount_received():
    """2026-11-26 Sample 5 [1155]: publishable-key PI confirm returns `amount`, not `amount_received`."""
    from decimal import Decimal

    engine = build_harness(currency="PHP")
    engine.client = StubClient()
    engine.intent_id = "pi_3UgEw6Fixture"
    engine.receipt_expected_minor = 98214
    engine.raw["intent"] = {
        "id": "pi_3UgEw6Fixture",
        "object": "payment_intent",
        "amount": 98214,
        "currency": "php",
        "status": "succeeded",
    }
    engine._read_checkout = lambda: {  # type: ignore[assignment]
        "checkout_session_id": CHECKOUT_ID,
        "status": "complete",
        "payment_status": "paid",
        "client_secret": "pi_3UgEw6Fixture_secret_xyz",
    }
    engine._ack_checkout = lambda: True  # type: ignore[assignment]

    out = engine._settle()
    assert out.state == "paid"
    assert engine.receipt_evidence is not None
    assert engine.receipt_evidence.payment_id == "pi_3UgEw6Fixture"
    assert engine.receipt_evidence.amount == Decimal("982.14")
    assert engine.receipt_evidence.currency == "PHP"

    # Explicit zero amount_received must still be rejected.
    assert engine._bound_intent({
        "id": "pi_3UgEw6Fixture",
        "object": "payment_intent",
        "amount": 98214,
        "amount_received": 0,
        "currency": "php",
        "status": "succeeded",
    }) is False


def test_run_har_context_pre_confirm_transport_error_fails_without_unknown(monkeypatch):
    def fail_before_confirm(self):
        self.stage = "setup"
        self.confirmation_started = False
        raise TimeoutError("stripe confirmation_tokens timed out")

    monkeypatch.setattr(PlusHarProtocol, "pay", fail_before_confirm)
    context = {}
    spec = {
        "checkout_url": f"https://chatgpt.com/checkout/{PROCESSOR}/{CHECKOUT_ID}",
        "checkout_id": CHECKOUT_ID,
        "publishable_key": "pk_live_fixture",
        "currency": "PHP",
        "country": "US",
        "processor": PROCESSOR,
    }
    payload = run_har_context(StubClient(), context, spec, card={"number": "4242"},
                              billing={"email": "fixture@example.test", "country": "US"})
    assert payload["state"] == "failed"
    assert payload["code"] == "pre_confirm_transport_error"
    assert not context.get("app_confirmation_started")


def test_restore_account_skips_refresh_in_direct_session_mode(monkeypatch):
    from gss_core.saved_cards import restore_account

    class TrackingClient(StubClient):
        def __init__(self):
            self.refreshed = 0

        def refresh_session(self, **kwargs):
            self.refreshed += 1
            return True

    monkeypatch.setenv("GSS_DIRECT_SESSION_CHECKOUT", "1")
    client = TrackingClient()
    account_id = restore_account(client, expected="account_fixture")
    assert account_id == "account_fixture"
    assert client.refreshed == 0


@pytest.mark.parametrize(
    ("session_meta", "expected_field"),
    [
        (
            {
                "disable_saved_payment_methods": False,
                "experiments": {
                    "saved_payment_link_arm": "save_default_link",
                    "saved_payment_link_initial_save_for_reuse": "true",
                },
            },
            True,
        ),
        (
            {
                "disable_saved_payment_methods": False,
                "experiments": {
                    "saved_payment_link_arm": "save_default_link",
                    "saved_payment_link_initial_save_for_reuse": "false",
                },
            },
            False,
        ),
        (
            {"disable_saved_payment_methods": False, "experiments": {"saved_payment_link_arm": "control"}},
            None,
        ),
        (
            {"disable_saved_payment_methods": False, "experiments": {}},
            None,
        ),
        (
            {
                "disable_saved_payment_methods": True,
                "experiments": {"saved_payment_link_arm": "save_default_link"},
            },
            None,
        ),
    ],
)
def test_checkout_confirm_save_choice_matches_session_experiments(session_meta, expected_field):
    engine = build_harness()
    engine.client = StubClient()
    engine._sync_save_choice(session_meta)
    engine.ctoken = "ctoken_fixture"
    engine._reset_web_session = lambda: None  # type: ignore[assignment]
    engine._hydrate_confirmed_intent = lambda: None  # type: ignore[assignment]
    engine._app_confirm()

    confirm = next(body for method, route, body in engine.app.calls
                   if route == "/backend-api/payments/checkout/confirm")
    if expected_field is None:
        assert "save_payment_method_for_reuse" not in confirm
    else:
        assert confirm["save_payment_method_for_reuse"] is expected_field


def test_subscription_active_force_refreshes_session_after_upgrade_401():
    """2026-11-26 Sample 5 [1175]->[1179]: upgrading free->paid invalidates old AT (401)."""
    from gss_contracts.errors import DomainError, ErrorCode

    class Upgrade401Client(StubClient):
        def __init__(self):
            self.session_token = "st-live"
            self.calls = []
            self._sub_calls = 0

        def subscription_payload(self):
            self._sub_calls += 1
            if self._sub_calls == 1:
                raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效或已过期")
            return {
                "plan_type": "plus",
                "is_active": True,
                "entitlement": {"has_active_subscription": True, "subscription_plan": "chatgptplusplan"},
            }

        def refresh_session(self, **kwargs):
            self.calls.append(kwargs)
            return True

    engine = build_harness()
    client = Upgrade401Client()
    engine.client = client
    assert engine._subscription_active() is True
    assert client.calls == [{"trust_statsig": False, "reject_unauthorized": False, "force_refresh": True}]


def test_active_plus_entitlement_accepts_live_prolite_and_promax():
    from gss_core.plus_har_protocol import active_plus_entitlement

    assert active_plus_entitlement(
        {
            "plan_type": "prolite",
            "is_active": True,
            "entitlement": {
                "has_active_subscription": True,
                "subscription_plan": "chatgptprolite",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        },
        account_id="acct-1",
        plan="pro_lite",
    ) is True
    assert active_plus_entitlement(
        {
            "plan_type": "promax",
            "is_active": True,
            "entitlement": {
                "has_active_subscription": True,
                "subscription_plan": "chatgptpromax",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        },
        account_id="acct-1",
        plan="pro_max",
    ) is True


def test_refresh_session_sends_failed_iat_and_replaces_chunked_session_cookie():
    import base64
    import json
    from gss_core.chatgpt import ChatGPTClient
    from gss_core.session_token import chunk_session_cookie

    old_st = "A" * 4400
    new_st = "B" * 4400
    claims = {"iat": 1795746401, "exp": 1995746401}
    old_at = "h." + base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode() + ".s"
    new_at = "h." + base64.urlsafe_b64encode(json.dumps({"iat": 1795746999, "exp": 1995746999}).encode()).rstrip(b"=").decode() + ".s"

    captured = {}

    class FakeResp:
        status_code = 200
        headers = {}

        def json(self):
            return {"accessToken": new_at, "sessionToken": new_st}

    class FakeHttp:
        def __init__(self):
            self.headers = {}
            self.cookies = None

        def get(self, url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            return FakeResp()

    client = ChatGPTClient(
        access_token=old_at,
        session_token=old_st,
        extra_cookies=chunk_session_cookie(old_st),
    )
    client._http = FakeHttp()
    assert client.refresh_session(trust_statsig=False, reject_unauthorized=False, force_refresh=True) is True
    assert captured["headers"].get("X-OpenAI-Failed-Access-Token-Iat") == "1795746401"
    assert captured["headers"].get("X-OpenAI-Target-Path") == "/api/auth/session"
    assert client.access_token == new_at
    assert client.session_token == new_st
    cookie_header = client._cookie_header()
    assert "A" * 50 not in cookie_header
    assert "__Secure-next-auth.session-token.0=" + ("B" * 3933) in cookie_header
    assert "__Secure-next-auth.session-token.1=" + ("B" * (4400 - 3933)) in cookie_header
