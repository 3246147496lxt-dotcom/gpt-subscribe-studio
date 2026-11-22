"""session 刷新对齐：用 Session 签发地出口请求 /api/auth/session，换取与本次 IP 同批次的凭证。

结账地区由 statsigContext.country 决定，session 本身不绑定国家；刷新是风控缓解措施，
必须软失败（不阻断结账/支付）。
"""

from __future__ import annotations

import asyncio

import pytest
from curl_cffi.requests import Cookies

from gss_checkout import UpstreamCheckoutAdapter
from gss_contracts import BillingMode, CheckoutRequest, PaymentRequest, Plan
from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import ChatGPTClient
from gss_payment.adapter import UpstreamPaymentAdapter

from test_chatgpt_protocol import _jwt


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", payload: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


class _SessionHttp:
    """可编程的 /api/auth/session 桩。"""

    def __init__(self, *, payload: dict | None = None, status: int = 200, raises: bool = False) -> None:
        self.headers: dict = {}
        self.cookies: dict = {"oai-sc": "sc-from-refresh"}
        self.posts: list = []
        self.gets: list = []
        self._payload = payload or {}
        self._status = status
        self._raises = raises

    def get(self, url: str, **kwargs):
        if self._raises:
            raise RuntimeError("network down")
        self.gets.append(url)
        return _FakeResponse(status_code=self._status, payload=self._payload)

    def post(self, url: str, **kwargs):
        self.posts.append({"url": url})
        return _FakeResponse(payload={})


def _client_with_session_token() -> ChatGPTClient:
    client = ChatGPTClient.from_raw_session(_jwt("acct-refresh"))
    client.session_token = "sess-token"
    return client


def test_refresh_session_updates_token_and_cookies() -> None:
    client = _client_with_session_token()
    client._http = _SessionHttp(payload={"accessToken": _jwt("acct-refreshed")})

    assert client.refresh_session() is True
    assert client.access_token == _jwt("acct-refreshed")
    assert client.extra_cookies["oai-sc"] == "sc-from-refresh"
    assert any("/api/auth/session" in url for url in client._http.gets)


def test_refresh_session_drops_expired_copies_without_restoring_them() -> None:
    session_cookie = "__Secure-next-auth.session-token"
    jar = Cookies()
    jar.set(session_cookie, "sess-token", domain="chatgpt.com", secure=True)
    jar.set("old-cookie", "old-value", domain="chatgpt.com")
    jar.set("old-cookie", "different-path", domain="chatgpt.com", path="/unrelated")
    jar.set("keep-cookie", "keep-value", domain="chatgpt.com")
    for cookie in jar.jar:
        if cookie.path == "/" and cookie.name in {session_cookie, "old-cookie"}:
            cookie.expires = 1

    class RefreshHttp(_SessionHttp):
        def get(self, url, **kwargs):
            jar.jar.clear_expired_cookies()
            return super().get(url, **kwargs)

    client = _client_with_session_token()
    client.extra_cookies = {session_cookie: "sess-token", "old-cookie": "old-value"}
    client._http = RefreshHttp(payload={"accessToken": _jwt("acct-refreshed")})
    client._http.cookies = jar

    assert client.refresh_session() is True
    assert client.session_token == ""
    assert client.extra_cookies == {"keep-cookie": "keep-value"}
    header = client._http.headers["Cookie"]
    assert session_cookie not in header
    assert "old-cookie" not in header


def test_refresh_session_soft_fails_on_network_error() -> None:
    client = _client_with_session_token()
    original = client.access_token
    client._http = _SessionHttp(raises=True)

    assert client.refresh_session() is False
    assert client.access_token == original


def test_refresh_session_soft_fails_on_upstream_error() -> None:
    client = _client_with_session_token()
    original = client.access_token
    client._http = _SessionHttp(status=500)

    assert client.refresh_session() is False
    assert client.access_token == original


def test_refresh_session_ignores_expired_session_token() -> None:
    """session-token 失效时上游返回 200 + 空 accessToken，不能拿空 token 覆盖。"""

    client = _client_with_session_token()
    original = client.access_token
    client._http = _SessionHttp(payload={})

    assert client.refresh_session() is False
    assert client.access_token == original


def test_refresh_session_without_session_token_is_noop() -> None:
    client = ChatGPTClient.from_raw_session(_jwt("acct-refresh"))
    client._http = _SessionHttp(payload={"accessToken": _jwt("acct-other")})

    assert client.refresh_session() is False
    assert client.access_token == _jwt("acct-refresh")
    assert client._http.gets == []


class _StubCheckoutClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def refresh_session(self) -> bool:
        self.calls.append("refresh")
        return True

    def create_checkout(self, **kwargs):
        self.calls.append("create")
        from gss_core.chatgpt import CheckoutSession

        return CheckoutSession(
            checkout_session_id="cs_test_1",
            publishable_key="pk_live_test",
            account_id="acct-stub",
            checkout_url="https://chatgpt.com/checkout/openai_llc/cs_test_1",
            currency="USD",
        )

    def close(self) -> None:
        self.calls.append("close")


def test_checkout_adapter_never_rotates_session(monkeypatch: pytest.MonkeyPatch, verified_us_payer) -> None:
    stub = _StubCheckoutClient()

    class _Factory:
        @staticmethod
        def from_raw_session(raw: str, proxy=None) -> _StubCheckoutClient:
            return stub

    monkeypatch.setattr("gss_checkout.adapter.ChatGPTClient", _Factory)

    result = asyncio.run(
        UpstreamCheckoutAdapter().create_checkout(
            CheckoutRequest(
                session="sess",
                plan=Plan.PLUS,
                billing_country="US",
                verified_billing=verified_us_payer,
                billing_mode=BillingMode.STANDARD,
            )
        )
    )

    assert result.checkout_session_id == "cs_test_1"
    assert "refresh" not in stub.calls
    assert stub.calls.count("create") == 1


class _StubPaymentClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def refresh_session(self) -> bool:
        self.calls.append("refresh")
        return True

    def warmup(self, *, page_url: str = "") -> None:
        self.calls.append("warmup")

    def confirm_checkout(self, **kwargs):
        self.calls.append("confirm")
        raise DomainError(ErrorCode.PAYMENT_DECLINED, "declined on purpose")


def test_payment_adapter_never_rotates_session(monkeypatch: pytest.MonkeyPatch, verified_us_payer) -> None:
    stub = _StubPaymentClient()

    class _Factory:
        @staticmethod
        def from_raw_session(raw: str, proxy=None) -> _StubPaymentClient:
            return stub

    monkeypatch.setattr("gss_payment.adapter.ChatGPTClient", _Factory)

    with pytest.raises(DomainError):
        asyncio.run(
            UpstreamPaymentAdapter().pay(
                PaymentRequest(
                    checkout_url="https://chatgpt.com/checkout/openai_llc/cs_test_1",
                    country="US",
                    billing=verified_us_payer,
                    payment_method_token="ctoken_1",
                    session="sess",
                    checkout_session_id="cs_test_1",
                    publishable_key="pk_live_test",
                    plan="plus",
                )
            )
        )

    assert "refresh" not in stub.calls
    assert stub.calls.index("warmup") < stub.calls.index("confirm")


def test_refresh_session_logs_status_but_no_response_or_url(caplog) -> None:
    class ErrorResponse:
        status_code = 503
        text = '{"error":"upstream-body-marker"}'
        url = "https://chatgpt.com/api/auth/session?secret=should-not-leak"

        def json(self):
            return {}

    class ErrorHttp:
        headers: dict = {}
        cookies = None

        def get(self, url, **kwargs):
            return ErrorResponse()

    client = _client_with_session_token()
    client._http = ErrorHttp()
    with caplog.at_level("WARNING"):
        assert client.refresh_session() is False
    assert "upstream_error where=/api/auth/session status=503" in caplog.text
    assert "body=" not in caplog.text
    assert "upstream-body-marker" not in caplog.text
    assert "url=" not in caplog.text
    assert "secret=should-not-leak" not in caplog.text


def test_refresh_transport_exception_is_not_an_audit_record(caplog):
    from unittest.mock import Mock

    client = _client_with_session_token()
    client._http = Mock(headers={}, cookies={})
    client._http.get.side_effect = RuntimeError("unlabelled-private-canary")
    assert client.refresh_session() is False
    assert "session_refresh_transport_error" in caplog.text
    assert "unlabelled-private-canary" not in caplog.text


def test_failure_logger_rejects_dynamic_paths_and_body(caplog):
    from types import SimpleNamespace
    from gss_core.chatgpt import _log_upstream_failure

    canary = "unlabelled-private-canary"
    url = f"https://test.invalid/checkout/{canary}?{canary}=1"
    _log_upstream_failure(url, SimpleNamespace(status_code=403, text=canary, url=url))
    assert "where=upstream status=403" in caplog.text
    assert canary not in caplog.text
