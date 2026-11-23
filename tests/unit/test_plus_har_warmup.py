"""Session migration: clean import, warm on the bound exit, then check out."""
from __future__ import annotations

import json

import pytest

from gss_contracts.errors import DomainError, ErrorCode
from gss_core import plus_har_protocol as har
from gss_core import proxies
from gss_core.chatgpt import ChatGPTClient
from gss_core.plus_har_protocol import PlusHarProtocol

PROXY = "http://user-region-PH-sid-1234:pw@global.example:1111"
CHECKOUT = "https://chatgpt.com/checkout/openai_llc/oaics_fixture"
SESSION_US = json.dumps({
    "accessToken": "access-fixture",
    "sessionToken": "session-fixture",
    "statsigContext": {"country": "US", "deviceId": "exported-device"},
})


class StubClient:
    def __init__(self, *, fail_warmup: bool = False, accounts=None):
        self.timeout = 25.0
        self.language = "en-US"
        self.timezone = "UTC"
        self.account_id = "account-fixture"
        self.device_id = "device-fixture"
        self.user_agent = ""
        self.session_token = "session-fixture"
        self.calls = []
        self.fail_warmup = fail_warmup
        self.accounts = accounts

    def warm_exit_session(self, *, reissue: bool = True, oauth: bool = False):
        self.calls.append(("warm_exit_session", reissue, oauth))
        if self.fail_warmup:
            raise DomainError(ErrorCode.INVALID_SESSION, "预热被拒绝",
                              detail={"code": "warmup_rejected", "stage": "me"})
        return self.accounts

    def close(self):
        pass


class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = b"{}"

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("GSS_PROXY_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("GSS_WARMUP_INTERVAL", "0")
    monkeypatch.setattr(har, "restore_account", lambda *a, **k: None)


def build(session: str = SESSION_US, **overrides) -> PlusHarProtocol:
    params = dict(
        session=session,
        proxy=PROXY,
        purchase_attempt_id="order-1",
        exit_country="PH",
        checkout_url=CHECKOUT,
        publishable_key="pk_live_fixture",
        currency="PHP",
        billing={"country": "US"},
    )
    params.update(overrides)
    return PlusHarProtocol(**params)


def _install(monkeypatch, factory: str, stub: StubClient, seen: list) -> None:
    def make(*args, **kwargs):
        seen.append((factory, kwargs))
        return stub

    monkeypatch.setattr(har.ChatGPTClient, factory, staticmethod(make))


def test_cross_country_import_reissues_before_any_checkout(monkeypatch):
    stub = StubClient()
    seen: list = []
    _install(monkeypatch, "reauthorize_for_country", stub, seen)
    _install(monkeypatch, "from_raw_session", stub, seen)
    engine = build()

    engine._prepare()

    assert [name for name, _ in seen] == ["reauthorize_for_country"]
    assert seen[0][1]["country"] == "PH"
    assert stub.calls == [("warm_exit_session", False, False)]  # already re-issued
    assert engine.session_reissued is True


def test_same_country_import_strips_the_exported_identity_and_warms(monkeypatch):
    stub = StubClient()
    seen: list = []
    _install(monkeypatch, "reauthorize_for_country", stub, seen)
    _install(monkeypatch, "from_raw_session", stub, seen)
    engine = build(exit_country="US")

    engine._prepare()

    assert [name for name, _ in seen] == ["from_raw_session"]
    assert seen[0][1]["strip_ambient"] is True
    assert stub.calls == [("warm_exit_session", True, False)]
    assert engine.session_reissued is False


def test_warmup_rejection_stops_before_the_checkout_call(monkeypatch):
    stub = StubClient(fail_warmup=True)
    _install(monkeypatch, "reauthorize_for_country", stub, [])
    engine = build()

    def forbidden(*args, **kwargs):
        raise AssertionError("结账接口不应在预热失败后被调用")

    engine._app = forbidden

    with pytest.raises(DomainError) as caught:
        engine._prepare()
    assert caught.value.detail["code"] == "warmup_rejected"
    assert caught.value.detail["stage"] == "me"


def test_risk_rejection_quarantines_the_bound_exit(monkeypatch):
    engine = build()
    engine.client = StubClient()
    engine._app = lambda *a, **k: FakeResponse(400, {"code": "unusual_activity"})

    with pytest.raises(DomainError):
        engine._create_checkout()

    assert proxies.proxy_cooldown(PROXY) is not None
    with pytest.raises(proxies.ProxyLeaseError):
        proxies.claim_sid_lease(PROXY, order_id="order-2")


def test_warm_exit_session_runs_handshake_reissue_then_probes(monkeypatch):
    calls: list[str] = []
    client = ChatGPTClient(access_token="a", session_token="s")
    monkeypatch.setenv("GSS_WARMUP_INTERVAL", "0")
    monkeypatch.setattr(client, "warmup", lambda **kwargs: calls.append("handshake"))
    monkeypatch.setattr(client, "refresh_session",
                        lambda **kwargs: (calls.append("auth_session"), True)[1])
    monkeypatch.setattr(client, "_warm_probe",
                        lambda path, params, *, stage: calls.append(stage))
    try:
        client.warm_exit_session()
    finally:
        client.close()

    assert calls == ["handshake", "auth_session", "me", "accounts_check"]


def test_cross_country_reissue_is_not_repeated():
    calls: list[str] = []
    client = ChatGPTClient(access_token="a", session_token="s")
    client.warmup = lambda **kwargs: calls.append("handshake")
    client.refresh_session = lambda **kwargs: (calls.append("auth_session"), True)[1]
    client._warm_probe = lambda path, params, *, stage: calls.append(stage)
    try:
        client.warm_exit_session(reissue=False)
    finally:
        client.close()

    assert calls == ["handshake", "me", "accounts_check"]


class _Http:
    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status = status
        self.payload = payload or {}
        self.paths: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.paths.append(url.split("chatgpt.com", 1)[-1])
        return FakeResponse(self.status, self.payload)


def test_warm_probe_requests_the_account_endpoint_and_accepts_200(monkeypatch):
    client = ChatGPTClient(access_token="a")
    http = _Http(200, {"email": "fixture@example.test"})
    monkeypatch.setattr(client, "_http", http)
    monkeypatch.setattr(client, "_route_headers", lambda path: {})
    monkeypatch.setattr(client, "_identity_headers", lambda: {})
    monkeypatch.setattr(client, "_merge_cookies", lambda *a, **k: None)
    try:
        client._warm_probe("/backend-api/me", None, stage="me")
    finally:
        client.close()
    assert http.paths == ["/backend-api/me"]


def test_warm_probe_turns_401_into_a_structured_rejection(monkeypatch):
    client = ChatGPTClient(access_token="a")
    http = _Http(401, {"code": "token_revoked"})
    monkeypatch.setattr(client, "_http", http)
    monkeypatch.setattr(client, "_route_headers", lambda path: {})
    monkeypatch.setattr(client, "_identity_headers", lambda: {})
    monkeypatch.setattr(client, "_merge_cookies", lambda *a, **k: None)
    try:
        with pytest.raises(DomainError) as caught:
            client._warm_probe("/backend-api/me", None, stage="me")
    finally:
        client.close()
    assert caught.value.detail["code"] == "warmup_rejected"
    assert caught.value.detail["stage"] == "me"


def test_text_only_risk_rejection_also_quarantines_the_exit():
    engine = build()
    engine.client = StubClient()
    response = FakeResponse(400, {})
    response.text = "We detected unusual activity on this account"
    engine._app = lambda *a, **k: response

    with pytest.raises(DomainError):
        engine._create_checkout()

    assert proxies.proxy_cooldown(PROXY) is not None


SESSION_OAUTH = json.dumps({
    "accessToken": "access-fixture",
    "refresh_token": "refresh-fixture",
    "statsigContext": {"country": "US", "deviceId": "exported-device"},
})


def test_oauth_credentials_refresh_on_the_exit_before_probing(monkeypatch):
    stub = StubClient()
    seen: list = []
    _install(monkeypatch, "reauthorize_for_country", stub, seen)
    _install(monkeypatch, "from_raw_session", stub, seen)
    engine = build(session=SESSION_OAUTH)

    engine._prepare()

    assert [name for name, _ in seen] == ["from_raw_session"]
    assert stub.calls == [("warm_exit_session", False, True)]
    assert engine.session_reissued is True


def test_cross_region_drops_an_open_checkout_for_another_currency():
    engine = build()
    engine.session_reissued = True
    engine.checkout_id = "oaics_old"
    engine.currency = "USD"
    engine._app = lambda *a, **k: FakeResponse(200, {
        "status": "open",
        "checkout_state": {"total": {"total": {"currency": "PHP"}}},
    })

    engine._discard_stale_checkout()

    assert engine.checkout_id == ""
    assert engine.discarded_checkout_id == "oaics_old"


def test_matching_open_checkout_is_kept_for_reuse():
    engine = build()
    engine.session_reissued = True
    engine.checkout_id = "oaics_same"
    engine.currency = "PHP"
    engine.country = "US"
    engine._app = lambda *a, **k: FakeResponse(200, {
        "status": "open",
        "billing_details": {"country": "US"},
        "checkout_state": {"total": {"total": {"currency": "PHP"}}},
    })

    engine._discard_stale_checkout()

    assert engine.checkout_id == "oaics_same"
    assert engine._checkout_snapshot is not None
    assert engine.discarded_checkout_id == ""


def test_missing_old_checkout_is_dropped_without_aborting():
    engine = build()
    engine.session_reissued = True
    engine.checkout_id = "oaics_gone"
    engine._app = lambda *a, **k: FakeResponse(404, {})

    engine._discard_stale_checkout()

    assert engine.checkout_id == ""
    assert engine.discarded_checkout_id == "oaics_gone"

def test_existing_subscription_blocks_checkout_before_creation(monkeypatch):
    stub = StubClient(accounts={"accounts": {"default": {"account": {"plan_type": "plus"}}}})
    _install(monkeypatch, "from_raw_session", stub, [])
    engine = build(exit_country="US")

    with pytest.raises(DomainError) as caught:
        engine._prepare()

    assert caught.value.code is ErrorCode.ALREADY_SUBSCRIBED
    assert caught.value.detail["code"] == "already_subscribed"
    assert caught.value.detail["planType"] == "plus"


def test_free_account_still_reaches_the_checkout_path(monkeypatch):
    stub = StubClient(accounts={"accounts": {"default": {"account": {"plan_type": "free"}}}})
    _install(monkeypatch, "from_raw_session", stub, [])
    engine = build(exit_country="US")

    engine._prepare()  # must not raise
    assert engine.client is stub
