import base64
import json
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from gss_contracts.errors import DomainError, ErrorCode
from gss_core.billing import address_for
from gss_core.money import quote_from_payload
from gss_core.chatgpt import (
    ChatGPTClient,
    ACCOUNTS_CHECK_PATH,
    checkout_confirm_blocked,
    checkout_confirm_blocked_advice,
    checkout_confirm_message,
    checkout_requires_approval,
    clamp_team_seats,
    expected_amount_minor,
    hosted_checkout_url,
    intent_id_from_secret,
    jwt_account_id,
    openai_internal_checkout_id,
    parse_account_check,
    payment_intent_from_confirm,
    setup_intent_from_payload,
    stripe_checkout_id,
    stripe_verify_url,
)
from gss_core.countries import country_codes, currency_for, is_supported
from gss_core.proxies import (
    align_proxy_region,
    exit_country_for,
    new_proxy_sid,
    proxy_exit_region,
    rewrite_proxy_exit,
    rotate_proxy_sid,
)
from gss_core.sentinel import extract_attestation, extract_build, header_value, mint
from gss_core.session_token import parse_session_bundle


def _jwt(account_id: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}).encode()
    ).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.x"


def _checkout_post(http) -> dict:
    """Sentinel ping / taxes 与建单共用同一 transport：取建单那条 POST。"""
    for post in http.posts:
        if post["url"].endswith("/backend-api/payments/checkout"):
            return post
    raise AssertionError("未记录到 /backend-api/payments/checkout 请求")


def _posts_to(http, suffix: str) -> list:
    return [post for post in http.posts if post["url"].endswith(suffix)]


def test_jwt_account_id() -> None:
    assert jwt_account_id(_jwt("acct-123")) == "acct-123"
    assert jwt_account_id("not-a-jwt") == ""


def test_from_raw_session_uses_full_bundle() -> None:
    raw = json.dumps(
        {
            "accessToken": _jwt("acct-bundle"),
            "sessionToken": "sess-keep",
            "sessionId": "sid-keep",
            "cookies": {"oai-did": "did-keep", "cf_clearance": "cf-keep"},
        }
    )
    client = ChatGPTClient.from_raw_session(raw)
    assert client.account_id == "acct-bundle"
    assert client.session_token == "sess-keep"
    assert client.device_id == "did-keep"
    assert client.oai_session_id == "sid-keep"
    header = client._cookie_header()
    assert "sess-keep" in header
    assert "did-keep" in header
    assert "cf-keep" in header


def test_client_uses_jwt_account_without_hydrate() -> None:
    token = _jwt("acct-xyz")
    client = ChatGPTClient.from_raw_session(token)
    assert client.account_id == "acct-xyz"
    assert client.device_id
    assert client.oai_session_id


def test_unusual_activity_is_mapped() -> None:
    client = ChatGPTClient(access_token="tok")
    response = SimpleNamespace(
        status_code=400,
        text='{"detail":"Our systems have detected unusual activity. Please try again later."}',
    )
    with pytest.raises(DomainError) as exc:
        client._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "创建结账")
    assert exc.value.code is ErrorCode.CHECKOUT_UPSTREAM_ERROR
    assert "异常流量" in str(exc.value)


def test_session_bundle_reads_statsig_device() -> None:
    raw = json.dumps(
        {
            "accessToken": "tok_json",
            "sessionToken": "sess_json",
            "statsigContext": {"deviceId": "did-from-statsig", "country": "JP"},
        }
    )
    bundle = parse_session_bundle(raw)
    assert bundle.device_id == "did-from-statsig"


def test_session_bundle_reads_cookies() -> None:
    raw = json.dumps(
        {
            "accessToken": "tok_json",
            "sessionToken": "sess_json",
            "cookies": {"oai-did": "did-1"},
        }
    )
    bundle = parse_session_bundle(raw)
    assert bundle.access_token == "tok_json"
    assert bundle.session_token == "sess_json"
    assert bundle.device_id == "did-1"


def test_rotate_proxy_sid_changes_only_sid() -> None:
    original = "http://user-region-US-sid-AAAA1111:secret@us.example.com:3010"
    rotated = rotate_proxy_sid(original)
    assert rotated != original
    assert "us.example.com:3010" in rotated
    assert "region-US" in rotated
    assert "sid-AAAA1111" not in rotated


def test_rewrite_proxy_exit_changes_region_and_sid() -> None:
    original = "http://user-region-US-sid-AAAA1111:secret@us.example.com:3010"
    first = rewrite_proxy_exit(original, region="PH", sid=new_proxy_sid())
    second = rewrite_proxy_exit(original, region="CL", sid=new_proxy_sid())
    assert "region-PH" in first
    assert "region-CL" in second
    assert "region-US" not in first
    assert first != second
    assert "sid-AAAA1111" not in first
    assert "sid-AAAA1111" not in second


def test_supported_regions_include_requested_countries() -> None:
    for code, currency in (("US", "USD"), ("PH", "PHP"), ("CL", "CLP"), ("EG", "EGP")):
        assert is_supported(code)
        assert currency_for(code) == currency


def test_quote_from_payload_reads_major_and_minor_units() -> None:
    dollars, usd = quote_from_payload({"amount": "20.00", "currency": "USD"})
    assert dollars == Decimal("20.00") and usd == "USD"
    cents, usd_cents = quote_from_payload({"amount_total": 2000, "currency": "usd"})
    assert cents == Decimal("20.00") and usd_cents == "USD"
    yen, jpy = quote_from_payload({"invoice": {"amount_due": 2980, "currency": "jpy"}})
    assert yen == Decimal("2980") and jpy == "JPY"


def test_quote_from_chatgpt_checkout_state_skips_tax_line() -> None:
    amount, currency = quote_from_payload(
        {
            "currency": "JPY",
            "amount_total": None,
            "checkout_state": {
                "lineItems": [{"subtotal": {"minorUnitsAmount": 15273, "currency": "JPY"}}],
                "taxAmounts": [{"subtotal": {"minorUnitsAmount": 1527, "currency": "JPY"}}],
            },
        },
        "JPY",
    )
    assert amount == Decimal("15273") and currency == "JPY"
    taxed, taxed_currency = quote_from_payload({"amount_total": 15273, "currency": "jpy"})
    assert taxed == Decimal("15273") and taxed_currency == "JPY"


def test_checkout_confirm_blocked_is_detected() -> None:
    assert checkout_confirm_blocked({"status": "blocked"})
    assert checkout_confirm_blocked({"result": "blocked"})
    assert not checkout_confirm_blocked({"status": "success"})
    assert checkout_confirm_message({"status": "blocked", "error": {"code": "risk_blocked"}}) == "risk_blocked"
    expected = "ChatGPT 未批准本次结账（blocked），上游未提供明确原因。尚未确认扣款，请先核对账单。"
    for country in ("US", "JP", "PH", "EG", ""):
        assert checkout_confirm_blocked_advice(country) == expected


def test_blocked_message_preserves_explicit_upstream_reason() -> None:
    message = checkout_confirm_blocked_advice("PH", upstream_reason="risk_blocked")
    assert "上游信息：risk_blocked" in message
    assert "上游未提供明确原因" not in message
    assert "session" not in message and "国家" not in message
    assert "（rejected）" in checkout_confirm_blocked_advice(upstream_status="rejected")


def test_payment_intent_from_chatgpt_confirm_success() -> None:
    intent_id, secret, return_url = payment_intent_from_confirm(
        {
            "status": "success",
            "type": "payment_intent",
            "client_secret": "pi_abc_secret_xyz",
            "confirm_return_url": "https://chatgpt.com/checkout/verify?stripe_session_id=oaics_1",
        }
    )
    assert intent_id == "pi_abc"
    assert secret == "pi_abc_secret_xyz"
    assert "checkout/verify" in return_url
    assert intent_id_from_secret("seti_only_secret_yyyy") == "seti_only"


def test_quote_from_payload_prefers_chatgpt_minor_fields() -> None:
    amount, currency = quote_from_payload(
        {
            "amount": "99.00",
            "checkout_amount_minor": 2000,
            "currency": "USD",
        }
    )
    assert amount == Decimal("20.00") and currency == "USD"
    php, php_code = quote_from_payload(
        {"total": {"taxInclusive": 114900, "currency": "php"}, "amount": "20.00"}
    )
    assert php == Decimal("1149.00") and php_code == "PHP"
    zero, usd = quote_from_payload({"checkout_amount_minor": 0, "currency": "USD"})
    assert zero == Decimal("0.00") and usd == "USD"


def test_setup_intent_from_payload_reads_nested_and_missing() -> None:
    setup_id, secret = setup_intent_from_payload(
        {
            "setup_intent": {
                "id": "seti_abc123",
                "client_secret": "seti_abc123_secret_zzzz",
            }
        }
    )
    assert setup_id == "seti_abc123"
    assert secret == "seti_abc123_secret_zzzz"
    from_secret = setup_intent_from_payload({"client_secret": "seti_only_secret_yyyy"})
    assert from_secret[0] == "seti_only"
    assert from_secret[1] == "seti_only_secret_yyyy"
    assert setup_intent_from_payload({"id": "pm_card"}) == ("", "")


def test_align_proxy_region_matches_billing_country() -> None:
    original = "http://user-region-US-sid-AAAA1111:secret@us.example.com:3010"
    aligned = align_proxy_region(original, "PH")
    assert aligned is not None
    assert proxy_exit_region(aligned) == "PH"
    assert "sid-AAAA1111" in aligned
    assert align_proxy_region(aligned, "PH") == aligned


def test_billing_address_matches_country() -> None:
    for code in country_codes():
        address = address_for(code)
        assert address["country"] == code
        assert address["name"]
        assert address["line1"]
        assert address["city"]
        assert address["postal_code"]
    us = address_for("US")
    assert us["state"] in {"AK", "DE", "MT", "NH", "OR"}
    assert us == address_for("US")  # 固定地址，不再随机
    assert us["postal_code"].isdigit() and len(us["postal_code"]) == 5


def test_extract_attestation_and_build_from_html() -> None:
    html = (
        '<html data-build="ignored" data-seq="9758774">'
        '"sessionId":"11111111-2222-3333-4444-55555555aaaa"'
        "prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
        '"webDeploymentAttestation":"abc.def"'
        "</html>"
    )
    assert extract_attestation(html) == "abc.def"
    version, build, session_id = extract_build(html)
    assert version == "prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert build == "9758774"
    assert session_id == "11111111-2222-3333-4444-55555555aaaa"


def test_header_value_strips_private_keys() -> None:
    raw = {"p": "pow", "c": "chal", "t": "turn", "id": "did", "flow": "chatgpt_checkout", "_so": {"so": "x"}}
    value = header_value(raw)
    assert '"p":"pow"' in value
    assert "_so" not in value


def test_mint_without_node_reports_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_NODE", "/nonexistent-gss-node")
    token = mint(flow="chatgpt_checkout", device_id="did", user_agent="ua")
    assert token.get("_error")
    assert "node executable not found" in token["_error"]


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", payload: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, html: str, payload: dict | None = None) -> None:
        self.headers: dict = {}
        self.cookies: dict = {}
        self.posts: list = []
        self._html = html
        self.payload = payload or {
            "checkout_session_id": "cs_test_1",
            "processor_entity": "openai_llc",
            "publishable_key": "pk_test_liveish",
            "customer_session_client_secret": "cuss_test",
        }

    def get(self, url: str, **kwargs):
        if "stripe_client_bootstrap" in url:
            return _FakeResponse(payload={"publishable_key": "pk_test_liveish"})
        return _FakeResponse(text=self._html)

    def post(self, url: str, **kwargs):
        self.posts.append({"url": url, "headers": kwargs.get("headers") or {}, "json": kwargs.get("json")})
        return _FakeResponse(payload=self.payload)


class _PromoHttp(_FakeHttp):
    def __init__(self, payload: dict) -> None:
        super().__init__("")
        self.gets: list = []
        self.promo_payload = payload

    def get(self, url: str, **kwargs):
        self.gets.append({"url": url, "params": kwargs.get("params"), "headers": kwargs.get("headers") or {}})
        if ACCOUNTS_CHECK_PATH in url:
            return _FakeResponse(payload=self.promo_payload)
        return super().get(url, **kwargs)


@pytest.mark.parametrize(
    ("content_type", "payload", "expected_kind", "expected_code"),
    [
        ("application/json; charset=utf-8", {"error": {"code": "invalid_token"}}, "json", "invalid_token"),
        ("application/json", {"detail": {"code": "private-diagnostic-marker"}}, "json", "unknown"),
        ("text/html", {}, "html", "unknown"),
    ],
)
def test_checkout_401_records_safe_diagnostics(monkeypatch, caplog, content_type, payload, expected_kind, expected_code, verified_payer):
    verified_payer("PH")
    http = _FakeHttp("")
    response = _FakeResponse(status_code=401, text="private-diagnostic-marker", payload=payload)
    response.headers = {"Content-Type": content_type}
    response.content = response.text.encode()
    response.url = "https://chatgpt.com/checkout/private-diagnostic-marker?secret=private-diagnostic-marker"
    response.request = SimpleNamespace(headers={"Oai-Device-Id": "private-diagnostic-marker"})
    monkeypatch.setattr(http, "post", lambda *args, **kwargs: response)
    client = ChatGPTClient("offline-token", account_id="offline-account", _http=http)
    monkeypatch.setattr(client, "prepare_risk", lambda **kwargs: None)

    with pytest.raises(DomainError) as exc:
        client.create_checkout(plan="plus", country="PH")

    assert exc.value.code is ErrorCode.INVALID_SESSION
    assert "checkout_unauthorized status_code=401 header_source=" in caplog.text
    assert "header_source=request has_oai_device_id=True has_user_agent=False" in caplog.text
    assert f"body_kind={expected_kind} body_bytes={len(response.content)}" in caplog.text
    assert f"upstream_error_code={expected_code}" in caplog.text
    # Checkout error logs contain safe metadata only, even for unknown body shapes.
    assert "upstream_error where=/backend-api/payments/checkout status=401" in caplog.text
    assert "body=" not in caplog.text
    assert "private-diagnostic-marker" not in caplog.text
    assert "secret=private-diagnostic-marker" not in caplog.text


def test_parse_account_check_preserves_50_percent_campaign() -> None:
    result = parse_account_check(
        {
            "accounts": {
                "acct-50": {
                    "account": {"account_id": "acct-50", "plan_type": "free"},
                    "entitlement": {"subscription_plan": "chatgptfreeplan"},
                    "eligible_promo_campaigns": {
                        "plus": {
                            "id": "plus-3-months-50-pct-off",
                            "metadata": {
                                "discount": {"percentage": 50},
                                "duration": {"num_periods": 3, "period": "month"},
                                "title": "Get a 50% discount on Plus for 3 months",
                            },
                        },
                        "go": {"id": "go-50-pct-off", "metadata": {}},
                    },
                }
            }
        },
        account_id="acct-50",
    )
    assert result["plus_eligible"] is True
    assert result["campaigns"][0]["id"] == "plus-3-months-50-pct-off"
    assert result["campaigns"][0]["discount_percentage"] == 50
    assert result["campaigns"][0]["duration_num_periods"] == 3


def test_promo_eligibility_uses_versioned_endpoint() -> None:
    payload = {
        "accounts": {
            "acct-check": {
                "account": {"account_id": "acct-check", "plan_type": "free"},
                "entitlement": {"subscription_plan": "chatgptfreeplan"},
                "eligible_promo_campaigns": {},
            }
        }
    }
    client = ChatGPTClient(access_token="tok", account_id="acct-check")
    client._http = _PromoHttp(payload)
    result = client.promo_eligibility(timezone_offset_min=0)
    assert result["plus_eligible"] is False
    request = client._http.gets[0]
    assert request["url"].endswith(ACCOUNTS_CHECK_PATH)
    assert request["params"] == {"timezone_offset_min": "0"}
    assert request["headers"]["x-openai-target-path"] == ACCOUNTS_CHECK_PATH


@pytest.mark.usefixtures("verified_us_payer")
def test_create_checkout_sends_selected_personal_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    _mint_stub(monkeypatch)
    client = ChatGPTClient.from_raw_session(_jwt("acct-campaign"))
    client._http = _FakeHttp("")
    client.create_checkout(
        plan="plus",
        country="US",
        promo_campaign_id="plus-3-months-50-pct-off",
    )
    body = _checkout_post(client._http)["json"]
    assert body["promo_campaign"]["promo_campaign_id"] == "plus-3-months-50-pct-off"
    assert body["promo_campaign"]["is_coupon_from_query_param"] is False


class _StripeHttp:
    def __init__(self, poll_payload: dict | None = None) -> None:
        self.headers: dict = {}
        self.posts: list = []
        self.gets: list = []
        self.poll_count = 0
        self.poll_payload = poll_payload or {
            "id": "cs_live_1",
            "status": "complete",
            "payment_object_status": "succeeded",
            "submission_attempt": {"state": "succeeded"},
            "payment_intent": {
                "id": "pi_1",
                "client_secret": "pi_1_secret_x",
                "status": "succeeded",
            },
        }
        self.open_payload = {
            "id": "cs_live_1",
            "status": "open",
            "submission_attempt": {"state": "requires_approval"},
        }

    def _next_poll(self) -> dict:
        self.poll_count += 1
        if self.poll_count <= 2:
            return self.open_payload
        return self.poll_payload

    def get(self, url: str, **kwargs):
        self.gets.append({"url": url, "params": kwargs.get("params")})
        if "elements/sessions" in url:
            return _FakeResponse(payload={"session_id": "elements_session_test", "config_id": "cfg_test"})
        if url.endswith("/poll") or "/payment_pages/" in url:
            return _FakeResponse(payload=self._next_poll())
        return _FakeResponse(payload=self.poll_payload)

    def post(self, url: str, **kwargs):
        self.posts.append({"url": url, "data": kwargs.get("data")})
        if url.endswith("/init"):
            return _FakeResponse(
                payload={
                    "init_checksum": "chk_1",
                    "js_checksum": "js_1",
                    "rv_timestamp": "1",
                    "total_summary": {"due": 2000, "subtotal": 1800},
                }
            )
        if url.endswith("/confirm"):
            return _FakeResponse(
                payload={
                    "id": "cs_live_1",
                    "status": "open",
                    "submission_attempt": {"state": "requires_approval"},
                }
            )
        if url.endswith("/poll"):
            return _FakeResponse(payload=self._next_poll())
        return _FakeResponse(payload={})


@pytest.mark.usefixtures("verified_us_payer")
def test_create_checkout_sends_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    flows: list[str] = []

    def fake_mint(**kwargs):
        flows.append(str(kwargs.get("flow") or ""))
        return {"p": "pow", "c": "chal", "t": "turn", "id": kwargs.get("device_id"), "flow": kwargs.get("flow")}

    monkeypatch.setattr("gss_core.chatgpt.mint", fake_mint)
    html = (
        'prod-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb '
        'data-seq="111" "webDeploymentAttestation":"sig.payload"'
    )
    client = ChatGPTClient.from_raw_session(_jwt("acct-sentinel"))
    client._http = _FakeHttp(html)
    created = client.create_checkout(plan="plus", country="US")
    assert flows == ["chatgpt_checkout"]
    assert created.checkout_session_id == "cs_test_1"
    assert created.customer_session_client_secret == "cuss_test"
    assert created.attestation == "sig.payload"
    headers = _checkout_post(client._http)["headers"]
    assert "openai-sentinel-token" in headers
    assert "oai-web-deployment-attestation" in headers
    assert '"flow":"chatgpt_checkout"' in headers["openai-sentinel-token"]
    # 真浏览器在 /sentinel/req 后由 iframe 补一次 /sentinel/ping。
    assert _posts_to(client._http, "/backend-api/sentinel/ping"), "缺少 sentinel ping"
    # 单次令牌立即消费：后续 GET /checkout/… 与 /taxes 不能再带同一份 so/t。
    assert client.sentinel_token == ""
    assert client.sentinel_so == ""
    body = _checkout_post(client._http)["json"]
    assert body["plan_name"] == "chatgptplusplan"
    assert body["checkout_source"] == "chatgpt_web_merged"
    assert body["purchase_attempt_id"]
    assert body["cancel_url"] == "https://chatgpt.com/?cta_tab=personal"
    assert created.processor_entity == "openai_llc"
    assert created.checkout_url == "https://chatgpt.com/checkout/openai_llc/cs_test_1"
    assert "stripe_session_id=cs_test_1" in created.confirm_return_url
    assert "processor_entity=openai_llc" in created.confirm_return_url
    assert "plan_type=plus" in created.confirm_return_url


@pytest.mark.usefixtures("verified_us_payer")
def test_create_checkout_maps_pro_max_to_chatgptpromax(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gss_core.chatgpt.mint",
        lambda **kwargs: {
            "p": "pow",
            "c": "chal",
            "t": "turn",
            "id": kwargs.get("device_id"),
            "flow": kwargs.get("flow"),
        },
    )
    client = ChatGPTClient.from_raw_session(_jwt("acct-sentinel"))
    client._http = _FakeHttp("")
    created = client.create_checkout(plan="pro_max", country="US")
    assert created.checkout_session_id == "cs_test_1"
    assert _checkout_post(client._http)["json"]["plan_name"] == "chatgptpromax"
    assert _checkout_post(client._http)["json"]["billing_details"]["country"] == "US"  # 出口 US
    assert "plan_type=promax" in created.confirm_return_url


def test_create_checkout_jp_uses_openai_llc(monkeypatch: pytest.MonkeyPatch, verified_payer) -> None:
    verified_payer("JP")
    monkeypatch.setattr(
        "gss_core.chatgpt.mint",
        lambda **kwargs: {
            "p": "pow",
            "c": "chal",
            "t": "turn",
            "id": kwargs.get("device_id"),
            "flow": kwargs.get("flow"),
        },
    )
    client = ChatGPTClient.from_raw_session(_jwt("acct-sentinel"))
    client._http = _FakeHttp("")
    created = client.create_checkout(plan="pro_lite", country="JP")
    assert created.processor_entity == "openai_llc"
    assert created.checkout_url == "https://chatgpt.com/checkout/openai_llc/cs_test_1"
    assert _checkout_post(client._http)["json"]["plan_name"] == "chatgptprolite"
    assert "plan_type=prolite" in created.confirm_return_url


def test_stripe_automatic_payment_methods_conflict_is_mapped() -> None:
    payload = {
        "error": {
            "message": (
                "Payment details were collected through Stripe Elements using automatic payment methods "
                "and cannot be confirmed through the API configured with payment_method_types or "
                "allowed_payment_method_types."
            )
        }
    }
    with pytest.raises(DomainError) as exc:
        from gss_core.chatgpt import _raise_stripe

        _raise_stripe(payload, 400)
    assert "采集模式和确认接口不一致" in str(exc.value)
    assert "automatic payment methods" not in str(exc.value)


def test_stripe_missing_expected_amount_is_mapped() -> None:
    from gss_core.chatgpt import _raise_stripe

    with pytest.raises(DomainError) as exc:
        _raise_stripe(
            {"error": {"message": "The expected amount of the confirmation has to be provided."}},
            400,
        )
    assert "应付金额" in str(exc.value)
    assert "has to be provided" not in str(exc.value)


def test_chatgpt_http_500_is_mapped() -> None:
    client = ChatGPTClient(access_token="tok")
    response = SimpleNamespace(status_code=500, text='{"detail":"Internal Server Error"}')
    with pytest.raises(DomainError) as exc:
        client._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "创建结账")
    assert "暂时返回 500" in str(exc.value)
    assert "Internal Server Error" not in str(exc.value)


def test_team_seats_and_hosted_url() -> None:
    assert clamp_team_seats(8) == 8
    with pytest.raises(ValueError, match="最少 2"):
        clamp_team_seats(1)
    assert stripe_checkout_id("https://checkout.stripe.com/c/pay/cs_live_abc123") == "cs_live_abc123"
    url = hosted_checkout_url("", "cs_live_abc123")
    assert url.startswith("https://pay.openai.com/c/pay/cs_live_abc123#")


def test_create_checkout_maps_team_seats_and_promo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gss_core.chatgpt.mint",
        lambda **kwargs: {
            "p": "pow",
            "c": "chal",
            "t": "turn",
            "id": kwargs.get("device_id"),
            "flow": kwargs.get("flow"),
        },
    )
    client = ChatGPTClient.from_raw_session(_jwt("acct-sentinel"))
    client._http = _FakeHttp("")
    created = client.create_checkout(
        plan="team",
        country="JP",
        promo_code="TEAM50",
        workspace_name="Desk",
        seat_quantity=8,
        price_interval="month",
    )
    body = _checkout_post(client._http)["json"]
    assert body["plan_name"] == "chatgptteamplan"
    assert body["checkout_ui_mode"] == "redirect"
    assert body["team_plan_data"]["seat_quantity"] == 8
    assert body["team_plan_data"]["workspace_name"] == "Desk"
    assert body["promo_code"] == "TEAM50"
    assert body["billing_details"]["currency"] == "JPY"
    assert created.checkout_kind == "hosted"
    assert created.publishable_key == "pk_test_liveish"
    assert "pay.openai.com/c/pay/cs_test_1" in created.checkout_url


class _NoKeyBootstrapHttp(_FakeHttp):
    """托管长链兜底：bootstrap 拿不到公钥时，Team 仍返回长链，custom 必须报错。"""

    def get(self, url: str, **kwargs):
        if "stripe_client_bootstrap" in url:
            return _FakeResponse(status_code=500, text='{"detail":"boom"}')
        return super().get(url, **kwargs)


def _mint_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gss_core.chatgpt.mint",
        lambda **kwargs: {
            "p": "pow",
            "c": "chal",
            "t": "turn",
            "id": kwargs.get("device_id"),
            "flow": kwargs.get("flow"),
        },
    )


def test_create_checkout_team_survives_missing_publishable_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _mint_stub(monkeypatch)
    client = ChatGPTClient.from_raw_session(_jwt("acct-nopk"))
    client._http = _NoKeyBootstrapHttp("", payload={"checkout_session_id": "cs_test_1"})
    created = client.create_checkout(plan="team", country="US")
    assert created.checkout_kind == "hosted"
    assert created.publishable_key == ""
    assert "pay.openai.com/c/pay/" in created.checkout_url


@pytest.mark.usefixtures("verified_us_payer")
def test_create_checkout_custom_without_publishable_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mint_stub(monkeypatch)
    client = ChatGPTClient.from_raw_session(_jwt("acct-nopk"))
    client._http = _NoKeyBootstrapHttp("", payload={"checkout_session_id": "cs_test_1"})
    with pytest.raises(DomainError, match="Stripe 公钥"):
        client.create_checkout(plan="plus", country="US")


def test_stripe_verify_url_matches_har_return_url() -> None:
    url = stripe_verify_url("oaics_1", processor="openai_llc", plan="pro_lite")
    assert url.startswith("https://chatgpt.com/checkout/verify?")
    assert "stripe_session_id=oaics_1" in url
    assert "processor_entity=openai_llc" in url
    assert "source=chatgpt_web_merged" in url
    assert "plan_type=prolite" in url


@pytest.mark.usefixtures("verified_us_payer")
def test_create_checkout_maps_go_to_chatgptgoplan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gss_core.chatgpt.mint",
        lambda **kwargs: {
            "p": "pow",
            "c": "chal",
            "t": "turn",
            "id": kwargs.get("device_id"),
            "flow": kwargs.get("flow"),
        },
    )
    client = ChatGPTClient.from_raw_session(_jwt("acct-sentinel"))
    client._http = _FakeHttp("")
    created = client.create_checkout(plan="go", country="US")
    assert created.checkout_session_id == "cs_test_1"
    assert _checkout_post(client._http)["json"]["plan_name"] == "chatgptgoplan"
    assert "plan_type=go" in created.confirm_return_url


def test_create_checkout_maps_go_lite_plan(monkeypatch: pytest.MonkeyPatch, verified_payer) -> None:
    verified_payer("JP")
    monkeypatch.setattr(
        "gss_core.chatgpt.mint",
        lambda **kwargs: {
            "p": "pow",
            "c": "chal",
            "t": "turn",
            "id": kwargs.get("device_id"),
            "flow": kwargs.get("flow"),
        },
    )
    client = ChatGPTClient.from_raw_session(_jwt("acct-sentinel"))
    client._http = _FakeHttp("")
    created = client.create_checkout(plan="pro_lite", country="JP")
    assert created.checkout_session_id == "cs_test_1"
    assert _checkout_post(client._http)["json"]["plan_name"] == "chatgptprolite"
    assert _checkout_post(client._http)["json"]["billing_details"]["currency"] == "JPY"


@pytest.mark.usefixtures("verified_us_payer")
def test_create_checkout_fails_closed_without_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gss_core.chatgpt.mint", lambda **kwargs: {})
    client = ChatGPTClient.from_raw_session(_jwt("acct-sentinel"))
    client._http = _FakeHttp("")
    with pytest.raises(DomainError) as exc:
        client.create_checkout(plan="plus", country="US")
    assert "风控令牌" in str(exc.value)


def _allow_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gss_core.chatgpt.mint",
        lambda **kwargs: {
            "p": "pow",
            "c": "chal",
            "t": "turn",
            "id": kwargs.get("device_id"),
            "flow": kwargs.get("flow"),
        },
    )


@pytest.mark.usefixtures("verified_us_payer")
def test_create_checkout_prefers_oaics_when_cs_live_is_also_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_sentinel(monkeypatch)
    client = ChatGPTClient.from_raw_session(_jwt("acct-sentinel"))
    client._http = _FakeHttp(
        "",
        payload={
            "id": "cs_live_spa",
            "checkout_session_id": "cs_live_spa",
            "confirm_return_url": "https://chatgpt.com/checkout/verify?stripe_session_id=oaics_internal",
            "processor_entity": "openai_llc",
            "publishable_key": "pk_test_liveish",
        },
    )
    created = client.create_checkout(plan="plus", country="US")
    assert created.checkout_session_id == "oaics_internal"
    assert created.checkout_url == "https://chatgpt.com/checkout/openai_llc/cs_live_spa"
    assert openai_internal_checkout_id(created.confirm_return_url) == "oaics_internal"


def test_internal_checkout_error_is_mapped() -> None:
    client = ChatGPTClient(access_token="tok")
    response = SimpleNamespace(
        status_code=400,
        text='{"detail":"This endpoint is only for internal checkout sessions"}',
    )
    with pytest.raises(DomainError) as exc:
        client._raise(ErrorCode.PAYMENT_UNKNOWN_STATE, response, "确认结账")
    assert "oaics_" in str(exc.value)
    assert "cs_" in str(exc.value)
    assert "internal checkout sessions" not in str(exc.value)


def test_confirm_checkout_oaics_posts_chatgpt_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_sentinel(monkeypatch)
    client = ChatGPTClient.from_raw_session(_jwt("acct-sentinel"))
    client._http = _FakeHttp("")
    client.confirm_checkout(checkout_session_id="oaics_abc", confirm_token="ctoken_1")
    urls = [item["url"] for item in client._http.posts]
    assert any(url.endswith("/backend-api/payments/checkout/confirm") for url in urls)
    assert all("/payment_pages/" not in url for url in urls)
    confirm = next(item for item in client._http.posts if item["url"].endswith("/confirm"))
    assert confirm["json"]["checkout_session_id"] == "oaics_abc"
    assert confirm["json"]["confirmation_token"] == "ctoken_1"
    assert confirm["json"]["confirm_token"] == "ctoken_1"


def test_confirm_checkout_cs_live_uses_payment_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_sentinel(monkeypatch)
    client = ChatGPTClient.from_raw_session(_jwt("acct-sentinel"))
    client._http = _FakeHttp("")
    client._stripe_http = _StripeHttp()
    payload = client.confirm_checkout(
        checkout_session_id="cs_live_abc123",
        confirm_token="ctoken_1",
        publishable_key="pk_test_liveish",
    )
    chatgpt_urls = [item["url"] for item in client._http.posts]
    assert not any("/payments/checkout/confirm" in url for url in chatgpt_urls)
    stripe_urls = [item["url"] for item in client._stripe_http.posts]
    assert any(url.endswith("/payment_pages/cs_live_abc123/init") for url in stripe_urls)
    assert any(url.endswith("/payment_pages/cs_live_abc123/confirm") for url in stripe_urls)
    assert payload.get("payment_intent") is None
    confirm = next(item for item in client._stripe_http.posts if item["url"].endswith("/confirm"))
    fields = parse_qs(str(confirm["data"] or ""))
    assert fields["expected_amount"] == ["2000"]
    assert fields["elements_session_client[elements_init_source]"] == ["custom_checkout"]
    assert fields["init_checksum"] == ["chk_1"]
    assert checkout_requires_approval(payload) is True
    assert checkout_requires_approval({"status": "open"}) is False


def test_expected_amount_minor_reads_stripe_init_shapes() -> None:
    assert expected_amount_minor({"total_summary": {"due": 22000}}) == 22000
    assert expected_amount_minor({"invoice": {"amount_due": 16800}}) == 16800
    assert expected_amount_minor({"session": {"amount_total": 3000}}) == 3000
    assert expected_amount_minor({"snapshot": {"amount_total": 11000, "currency": "usd"}}) == 11000
    assert expected_amount_minor({"init_checksum": "chk"}) is None


def test_confirm_checkout_cs_live_requires_expected_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_sentinel(monkeypatch)

    class _NoAmountHttp:
        headers: dict = {}
        posts: list = []

        def post(self, url: str, **kwargs):
            self.posts.append({"url": url, "data": kwargs.get("data")})
            if url.endswith("/init"):
                return _FakeResponse(payload={"init_checksum": "chk_1"})
            if url.endswith("/confirm"):
                raise AssertionError("confirm must not run without expected_amount")
            return _FakeResponse(payload={})

    client = ChatGPTClient.from_raw_session(_jwt("acct-sentinel"))
    client._http = _FakeHttp("")
    client._stripe_http = _NoAmountHttp()
    with pytest.raises(DomainError) as exc:
        client.confirm_checkout(
            checkout_session_id="cs_live_abc123",
            confirm_token="ctoken_1",
            publishable_key="pk_test_liveish",
        )
    assert "expected_amount" in str(exc.value)
    assert not any(item["url"].endswith("/confirm") for item in client._stripe_http.posts)


def test_finalize_stripe_custom_checkout_approves_and_polls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_sentinel(monkeypatch)
    client = ChatGPTClient.from_raw_session(_jwt("acct-sentinel"))
    client._http = _FakeHttp("")
    client._stripe_http = _StripeHttp()
    confirmed = client.confirm_checkout(
        checkout_session_id="cs_live_abc123",
        confirm_token="ctoken_1",
        publishable_key="pk_test_liveish",
    )
    assert checkout_requires_approval(confirmed) is True
    final = client.finalize_stripe_custom_checkout(
        checkout_session_id="cs_live_abc123",
        publishable_key="pk_test_liveish",
        confirmed=confirmed,
        attempts=2,
        interval=0,
    )
    assert any("/payments/checkout/approve" in item["url"] for item in client._http.posts)
    assert client._stripe_http.gets
    assert final["status"] == "complete"
    intent_id, secret, _return_url = payment_intent_from_confirm(final)
    assert intent_id == "pi_1"
    assert secret == "pi_1_secret_x"


def test_payment_intent_from_confirm_accepts_string_id() -> None:
    intent_id, secret, _return_url = payment_intent_from_confirm({"payment_intent": "pi_only"})
    assert intent_id == "pi_only"
    assert secret == ""


def test_payment_intent_from_payment_page_ignores_cs_id() -> None:
    intent_id, secret, return_url = payment_intent_from_confirm(
        {
            "id": "cs_live_1",
            "status": "complete",
            "return_url": "https://chatgpt.com/checkout/verify?stripe_session_id=cs_live_1",
            "payment_intent": {
                "id": "pi_nested",
                "client_secret": "pi_nested_secret_zz",
                "status": "succeeded",
            },
        }
    )
    assert intent_id == "pi_nested"
    assert secret == "pi_nested_secret_zz"
    assert "checkout/verify" in return_url


def test_payment_checkout_body_matches_har_and_headers_stay_web_identity() -> None:
    from gss_core.chatgpt import ChatGPTClient, _checkout_body

    body = _checkout_body(plan="pro_lite", country="US", currency="USD")
    assert body["checkout_ui_mode"] == "custom"
    assert body["checkout_source"] == "chatgpt_web_merged"

    client = ChatGPTClient(access_token="t")
    headers = client._route_headers("/backend-api/payments/checkout")
    # 本客户端伪装的是 Windows Chrome 的 ChatGPT Web：不能混发 macOS Codex WebView
    # 的客户端身份头（参考 HAR 出自 Codex Browser，混发会让同一会话出现两种客户端）。
    assert "originator" not in headers
    assert "x-openai-web-frontend" not in headers
    assert "x-openai-codex-window-type" not in headers
    # 与客户端身份无关、Web 侧同样存在的两项保留。
    assert headers["x-openai-checkout-source"] == "chatgpt_web_merged"
    assert headers["x-openai-rate-limit-join-workspace"] == "1"
    # HAR 的 payments/* 上没有这两个头，多发即是多余指纹。
    assert "x-oai-mcp-form-version" not in headers
    assert "x-openai-target-path" not in headers
    assert "x-openai-target-route" not in headers
    assert "originator" not in client._route_headers("/backend-api/sentinel/req")


def test_stripe_custom_confirm_syncs_billing_tax_first(monkeypatch) -> None:
    from gss_core.chatgpt import ChatGPTClient

    calls: list[tuple[str, dict]] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"checkout_session": {"amount_total": 15273}}

    class _Http:
        def post(self, url, json=None, headers=None, timeout=None):
            calls.append((url.rsplit("/", 1)[-1], json or {}))
            return _Resp()

    client = ChatGPTClient(access_token="t")
    client._http = _Http()
    monkeypatch.setattr(client, "mint_sentinel", lambda **kw: None)
    monkeypatch.setattr(
        client,
        "confirm_stripe_custom_checkout",
        lambda **kw: calls.append(("stripe_confirm", kw)) or {},
    )
    billing = {"name": "Jane Roe", "line1": "1 Main St", "city": "Austin", "state": "TX",
               "postal_code": "73301", "country": "US"}

    client.confirm_checkout(
        checkout_session_id="cs_live_abc",
        confirm_token="ctoken_x",
        billing=billing,
        currency="JPY",
        publishable_key="pk_live_x",
    )

    assert [name for name, _ in calls] == ["taxes", "update_tax_id", "stripe_confirm"]
    assert calls[0][1]["billing_address"]["postal_code"] == "73301"
    assert calls[0][1]["billing_country"] == "US"
    assert calls[2][1]["expected_amount"] == 15273


def test_decline_message_keeps_code_and_adds_advice() -> None:
    from gss_core.chatgpt import decline_message

    known = decline_message("do_not_honor")
    assert known.startswith("发卡行拒绝: do_not_honor。")
    assert "联系银行" in known
    assert decline_message("some_new_code") == "发卡行拒绝: some_new_code"


# Team Hosted currency/address regressions live in test_hosted_protocol.py.


SESSION_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.7390.55 Safari/537.36"
)


def test_client_uses_statsig_user_agent_for_http_and_sentinel(monkeypatch) -> None:
    raw = json.dumps(
        {
            "accessToken": "tok_ua",
            "statsigContext": {
                "deviceId": "did-ua",
                "userAgent": SESSION_UA,
                "country": "JP",
            },
        }
    )
    client = ChatGPTClient.from_raw_session(raw)
    try:
        assert client.ua == SESSION_UA
        assert client.device_id == "did-ua"
        assert client.session_country == "JP"
        headers = client._identity_headers()
        assert headers["User-Agent"] == SESSION_UA
        assert headers["oai-device-id"] == "did-ua"
        assert headers["oai-did"] == "did-ua"
        assert "oai-did=did-ua" in headers["Cookie"]
        assert 'v="141"' in headers["sec-ch-ua"]
        assert headers["sec-ch-ua-platform"] == '"macOS"'
        # curl_cffi 的 Stripe 会话也必须使用同一个 UA。
        assert client.stripe_http.headers["User-Agent"] == SESSION_UA
    finally:
        client.close()

    captured = {}

    def fake_mint(**kwargs):
        captured.update(kwargs)
        return {"p": "proof", "c": "challenge"}

    monkeypatch.setattr("gss_core.chatgpt.mint", fake_mint)
    client.mint_sentinel(flow="chatgpt_checkout")
    assert captured["user_agent"] == SESSION_UA
    assert captured["device_id"] == "did-ua"
    assert captured["platform"] == "MacIntel"
    assert captured["platform_label"] == "macOS"


def test_prepare_risk_uses_session_country_over_billing(monkeypatch) -> None:
    from gss_core.countries import timezone_for

    client = ChatGPTClient(access_token="tok", session_country="JP")
    monkeypatch.setattr(client, "warmup", lambda **kwargs: None)
    monkeypatch.setattr(client, "mint_sentinel", lambda **kwargs: None)
    client.prepare_risk(flow="chatgpt_checkout", country="US")
    assert client.timezone == timezone_for("JP")


def test_exit_country_prefers_explicit_exit() -> None:
    assert exit_country_for("PH", "US") == "PH"
    assert exit_country_for("US", "") == "US"
    assert exit_country_for("", "") == ""
    # 非法出口码继续向下兜底到账单国。
    assert exit_country_for("USA", "PH") == "PH"
    assert exit_country_for("japan", "") == ""


def test_quote_currency_prefers_checkout_state_over_unrelated_nested_currency() -> None:
    payload = {
        "marketing": {"currency": "usd"},
        "checkout_state": {
            "currency": "php",
            "total": {
                "total": {"minorUnitsAmount": 110000, "amount": "₱1,100.00"},
                "subtotal": {"minorUnitsAmount": 98214, "amount": "₱982.14"},
            },
        },
    }
    amount, currency = quote_from_payload(payload, "PHP")
    assert currency == "PHP"
    assert amount == Decimal("1100.00")


def test_token_revoked_is_reported_as_actionable_session_error() -> None:
    client = ChatGPTClient("tok")
    response = SimpleNamespace(
        status_code=401,
        text='{"error":{"message":"Encountered invalidated oauth token","code":"token_revoked"}}',
        url="https://chatgpt.com/backend-api/payments/checkout",
    )
    with pytest.raises(DomainError) as exc:
        client._raise(ErrorCode.INVALID_SESSION, response, "创建结账")
    assert exc.value.code is ErrorCode.INVALID_SESSION
    assert "token_revoked" in exc.value.message
    assert "重新登录" in exc.value.message


def test_quote_parses_nested_checkout_session_tax_response() -> None:
    payload = {
        "using_automatic_tax": True,
        "checkout_session": {
            "checkout_state": {
                "currency": "php",
                "total": {"total": {"minorUnitsAmount": 98214, "amount": "₱982.14"}},
            }
        },
    }
    amount, currency = quote_from_payload(payload, "PHP")
    assert currency == "PHP"
    assert amount == Decimal("982.14")


def test_checkout_uses_billing_country_with_free_exit(monkeypatch, verified_us_payer) -> None:
    http = _FakeHttp("")
    client = ChatGPTClient("fixture-token", account_id="fixture-account", _http=http)
    monkeypatch.setattr(client, "prepare_risk", lambda **kwargs: None)
    client.create_checkout(plan="plus", country="PH", billing_country="US", verified_billing=verified_us_payer)
    create = [post for post in http.posts if post["url"].endswith("/checkout")][0]
    # 建单阶段 billing_details 记录的是「当前选中地区」= 出口国（HAR 实测）。
    assert create["json"]["billing_details"] == {"country": "PH", "currency": "PHP"}
    taxes = [post for post in http.posts if post["url"].endswith("/checkout/taxes")][0]
    assert taxes["json"]["billing_country"] == "US"


def test_checkout_tax_quote_submits_exact_verified_address(monkeypatch, verified_us_payer) -> None:
    http = _FakeHttp("")
    client = ChatGPTClient("fixture-token", account_id="fixture-account", _http=http)
    monkeypatch.setattr(client, "prepare_risk", lambda **kwargs: None)
    client.create_checkout(plan="plus", country="US", billing_country="US", verified_billing=verified_us_payer)
    body, = [post["json"] for post in http.posts if post["url"].endswith("/checkout/taxes")]
    assert body["billing_name"] == verified_us_payer["name"]
    assert body["billing_country"] == verified_us_payer["country"]
    assert body["billing_address"] == {key: verified_us_payer[key] for key in (
        "line1", "city", "state", "postal_code", "country",
    )}


@pytest.mark.parametrize("boundary", ["create", "quote", "taxes"])
def test_provider_billing_echo_vetoes_checkout_without_another_request(monkeypatch, verified_us_payer, boundary):
    calls = []
    echo = {"checkout_session": {"billing_details": {"address": {"line1": "foreign-private-address"}}}}

    class Http(_FakeHttp):
        def get(self, url, **kwargs):
            calls.append("quote")
            return _FakeResponse(payload=echo if boundary == "quote" else {})

        def post(self, url, **kwargs):
            stage = "taxes" if url.endswith("/taxes") else "create"
            calls.append(stage)
            return _FakeResponse(payload={**self.payload, **(echo if stage == boundary else {})})

    client = ChatGPTClient("fixture-token", account_id="fixture-account", _http=Http(""))
    monkeypatch.setattr(client, "prepare_risk", lambda **kwargs: None)
    with pytest.raises(DomainError) as exc:
        client.create_checkout(plan="plus", country="US", verified_billing=verified_us_payer)
    assert exc.value.code is ErrorCode.BILLING_COUNTRY_MISMATCH
    assert "foreign-private-address" not in str(exc.value)
    assert calls[-1] == boundary and calls.count(boundary) == 1


def test_pro_lite_cl_price_keeps_us_billing_country() -> None:
    from gss_core.chatgpt import _checkout_body

    body = _checkout_body(plan="pro_lite", country="CL", currency="CLP", billing_country="US")
    assert body["plan_name"] == "chatgptprolite"
    # 出口 CL 同时决定币种与建单 billing_details；固定 US 账单在 taxes 阶段提交。
    assert body["billing_details"] == {"country": "CL", "currency": "CLP"}
    assert body["checkout_ui_mode"] == "custom"
    assert body["checkout_source"] == "chatgpt_web_merged"
    assert body["purchase_attempt_id"]


def test_extract_sentinel_version_from_page() -> None:
    from gss_core.sentinel import extract_sentinel_version

    assert extract_sentinel_version('src="/sentinel/20261101001a/sdk.js"') == "20261101001a"
    assert extract_sentinel_version("frame.html?sv=20260810913b") == "20260810913b"
    assert extract_sentinel_version("<html></html>") == ""


def test_warmup_learns_sentinel_version_and_mint_forwards_it(monkeypatch) -> None:
    captured: dict = {}

    def fake_mint(**kwargs):
        captured.update(kwargs)
        return {"p": "pow", "c": "chal", "t": "turn"}

    monkeypatch.setattr("gss_core.chatgpt.mint", fake_mint)
    client = ChatGPTClient(access_token="tok")
    client._http = _FakeHttp(
        '<script src="/sentinel/20261101001a/sdk.js"></script>'
        ' "webDeploymentAttestation":"sig.payload"'
    )
    client.warmup(page_url="https://chatgpt.com/")
    assert client.sentinel_version == "20261101001a"

    client.mint_sentinel(flow="chatgpt_checkout")
    assert captured["version"] == "20261101001a"


@pytest.mark.parametrize(
    ("version", "expected"),
    [("", "20260810913b"), ("20261101001a", "20261101001a")],
)
def test_mint_forwards_learned_sv_or_bridge_default(monkeypatch, version, expected) -> None:
    from types import SimpleNamespace as _NS

    from gss_core import sentinel as sentinel_mod

    sent_payloads: list[dict] = []

    def fake_run(cmd, *, input=None, **kwargs):  # noqa: A002
        sent_payloads.append(json.loads(input.decode("utf-8")))
        main = json.dumps({"p": "pow", "c": "chal"})
        return _NS(returncode=0, stdout=json.dumps({"main": main, "so": ""}).encode(), stderr=b"")

    monkeypatch.setattr(sentinel_mod.subprocess, "run", fake_run)
    token = sentinel_mod.mint(
        flow="chatgpt_checkout", device_id="did", user_agent="ua", version=version
    )
    assert token["c"] == "chal"
    # 空 sv 一律回退桥内置版本，绝不把空串透传给 frame.html / sdk.js。
    assert sent_payloads[0]["version"] == expected


def test_client_without_learned_version_leaves_it_empty() -> None:
    assert ChatGPTClient(access_token="tok").sentinel_version == ""


def test_sentinel_profile_and_mint_stay_consistent_under_macos_browser_context(monkeypatch) -> None:
    from types import SimpleNamespace

    captured: dict = {}

    def fake_mint(**kwargs):
        captured.update(kwargs)
        return {"p": "pow", "c": "chal"}

    monkeypatch.setattr("gss_core.chatgpt.mint", fake_mint)
    ctx = SimpleNamespace(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.7680.180 Safari/537.36",
        sec_ch_ua='"Google Chrome";v="146", "Chromium";v="146", "Not(A:Brand";v="24"',
        sec_ch_ua_platform='"macOS"',
        locale="en-US",
        timezone_id="America/New_York",
        viewport_width=1440,
        viewport_height=900,
        cookies={},
        headers=lambda: {"sec-ch-ua-platform": '"macOS"', "Referer": "https://chatgpt.com/"},
    )
    client = ChatGPTClient(access_token="tok", device_id="did-mac-ctx")
    client.apply_browser_context(ctx)
    client.mint_sentinel(flow="chatgpt_checkout", ping=False)

    assert captured["platform"] == "MacIntel"
    assert captured["platform_label"] == "macOS"
    assert captured["sec_ch_ua_platform"] == '"macOS"'
    assert captured["sec_ch_ua_arch"] == '"arm"'
    assert captured["sec_ch_ua_bitness"] == '"64"'
    assert captured["sec_ch_ua_platform_version"] == '"14.6.1"'
    # 冷导航头同样必须过滤掉 context.headers() 里的 Referer / Authorization / oai-*
    nav = client._document_nav_headers()
    assert all(k.lower() != "referer" for k in nav)
    assert all(not k.lower().startswith("oai-") for k in nav)


def test_refresh_session_and_anonymous_warmup_keep_clean_headers() -> None:
    seen: list[tuple[str, dict, dict]] = []

    class _Resp:
        status_code = 200
        text = ""
        headers: dict = {}

        def json(self):
            return {"accessToken": "fresh-tok"}

    class _PlainDictHttp:
        def __init__(self, initial: dict):
            self.headers = dict(initial)
            self.cookies = None

        def get(self, url, headers=None, timeout=None):
            seen.append((url, dict(self.headers), dict(headers or {})))
            return _Resp()

    client = ChatGPTClient(access_token="old-tok", session_token="st-val", device_id="", account_id="acct-1")
    client._http = _PlainDictHttp(client._identity_headers())

    # 1) anonymous warmup 在普通 dict session.headers 上也必须摘掉大写 Referer，且无 cookie 时不发空 Cookie 头
    client.warmup(anonymous=True)
    url0, sess0, req0 = seen[0]
    assert url0 == "https://chatgpt.com/"
    assert all(k.lower() != "referer" for k in sess0)
    assert all(k.lower() != "referer" for k in req0)
    assert "Cookie" not in req0

    # 2) /api/auth/session 不发送 x-openai-target-path/route、Authorization、chatgpt-account-id、oai-*
    assert client.refresh_session(trust_statsig=False) is True
    url1, sess1, req1 = seen[1]
    assert url1.endswith("/api/auth/session")
    merged1 = {k.lower(): v for k, v in {**sess1, **req1}.items()}
    for forbidden in ("authorization", "origin", "content-type", "chatgpt-account-id",
                      "x-openai-target-path", "x-openai-target-route", "oai-did", "oai-device-id"):
        assert forbidden not in merged1, f"/api/auth/session 泄露头: {forbidden}"
