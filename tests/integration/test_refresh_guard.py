"""Fail closed before presenting credentials; transport is always synthetic here."""

import json
from unittest.mock import Mock

import httpx
import pytest

from fastapi.testclient import TestClient

from gss_api.main import create_app
from gss_api.routers import orders
from gss_core.proxies import ProxyPreflightError, verify_refresh_egress


pytestmark = pytest.mark.usefixtures("verified_us_payer")

@pytest.mark.parametrize("answer,expected_status", [
    (204, None), (302, 503), (403, 403), (429, 429), (503, 503),
    (httpx.ConnectError("SSL EOF private-marker"), 503),
    (httpx.ReadTimeout("private-marker"), 503),
])
def test_probe_uses_one_uncredentialed_attempt(monkeypatch, answer, expected_status):
    calls, configs = [], []
    # Use the same origin even for echo services to exercise cookie isolation.
    monkeypatch.setenv("GSS_PROXY_PREFLIGHT_IP_URL", "https://chatgpt.com/fixture/ip")
    monkeypatch.setenv("GSS_PROXY_PREFLIGHT_COUNTRY_URL", "https://chatgpt.com/fixture/country/{ip}")

    def handle(request):
        assert not {"cookie", "authorization", "proxy-authorization"}.intersection(request.headers)
        calls.append((request.method, request.url.path))
        if request.method == "HEAD":
            if isinstance(answer, Exception):
                raise answer
            return httpx.Response(answer, headers={"location": "/never-follow"}, text="private-marker")
        data = {"ip": "192.0.2.1"} if request.url.path.endswith("/ip") else {"country": "US"}
        return httpx.Response(200, json=data, headers={"set-cookie": "fixture=should-not-leak; Path=/"})

    client_class = httpx.Client

    def factory(**kwargs):
        configs.append(kwargs.copy())
        kwargs.pop("proxy")
        return client_class(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    proxy = "http://gateway.invalid:8080"
    if expected_status is None:
        assert verify_refresh_egress(proxy, "US", step_delay_s=0, interval_s=0) == "192.0.2.1"
    else:
        with pytest.raises(ProxyPreflightError) as caught:
            verify_refresh_egress(proxy, "US", step_delay_s=0, interval_s=0)
        assert caught.value.status_code == expected_status
        assert "private-marker" not in str(caught.value)
    assert calls == [("GET", "/fixture/ip"), ("GET", "/fixture/country/192.0.2.1")] * 2 + [("HEAD", "/")]
    assert configs == [dict(proxy=proxy, timeout=15, follow_redirects=False, verify=True, trust_env=False)]


@pytest.mark.parametrize("original_country", ["US", "JP"])
@pytest.mark.parametrize("status_code", [403, 429, 503])
def test_preflight_failure_sends_no_credentials(monkeypatch, original_country, status_code):
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    monkeypatch.setenv("GSS_PROXY_PREFLIGHT", "1")
    guard = Mock(side_effect=ProxyPreflightError("fixture refusal", status_code))
    monkeypatch.setattr(orders, "verify_refresh_egress", guard)
    monkeypatch.setattr(orders, "checkout_proxy_for", lambda *a, **kw: "http://p.invalid:80")
    stop = lambda *a, **kw: pytest.fail("refresh/rotate/checkout attempted after preflight failure")
    monkeypatch.setattr(orders.ChatGPTClient, "from_raw_session", stop)
    monkeypatch.setattr(orders.ChatGPTClient, "reauthorize_for_country", stop)
    monkeypatch.setattr("gss_core.proxies.rotate_proxy_sid", stop)
    monkeypatch.setattr(orders, "build_pipeline", stop)
    session = ("eyJ-fixture..iv.cipher.tag" if original_country == "US" else json.dumps({
        "sessionToken": "fixture-only", "accessToken": "fixture-old", "statsigContext": {"country": original_country},
    }))
    result = TestClient(create_app()).post("/api/orders", json={
        "session": session, "billing_country": "US", "exit_country": "US", "plan": "plus",
    })
    guard.assert_called_once_with("http://p.invalid:80", "US")
    assert result.status_code == status_code
    assert "未发送会话凭证" in result.json()["detail"]["message"]
    assert "fixture-only" not in result.text


def test_fake_refresh_without_explicit_doubles_never_networks(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    monkeypatch.setenv("GSS_PROXY_PREFLIGHT", "1")
    stop = lambda *a, **kw: pytest.fail("network attempted in offline mode")
    monkeypatch.setattr(orders, "verify_refresh_egress", stop)
    monkeypatch.setattr(orders.ChatGPTClient, "from_raw_session", stop)
    monkeypatch.setattr(orders.ChatGPTClient, "reauthorize_for_country", stop)
    result = TestClient(create_app()).post("/api/orders", json={
        "session": "eyJ-fixture..iv.cipher.tag",
        "billing_country": "US", "plan": "plus",
    })
    assert result.status_code == 503
    assert "离线模式" in result.json()["detail"]["message"]


def test_missing_exit_never_falls_back_to_direct_transport(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("attempted direct connection"))
    with pytest.raises(ProxyPreflightError) as caught:
        verify_refresh_egress("", "US")
    assert caught.value.status_code == 503


def test_ambiguous_authentication_failure_is_never_replayed(monkeypatch):
    from gss_contracts.errors import DomainError, ErrorCode

    monkeypatch.setenv("GSS_USE_FAKE", "0")
    monkeypatch.setattr(orders, "verify_refresh_egress", Mock(return_value="192.0.2.1"))
    monkeypatch.setattr(orders, "checkout_proxy_for", lambda *a, **kw: "http://p.invalid:80")
    fail = Mock(side_effect=DomainError(ErrorCode.INVALID_SESSION, "无法在目标国家确认结果"))
    monkeypatch.setattr(orders.ChatGPTClient, "reauthorize_for_country", fail)
    monkeypatch.setattr("gss_core.proxies.rotate_proxy_sid", lambda p: pytest.fail("unexpected new exit"))
    session = {"sessionToken": "fixture-only", "accessToken": "fixture-old", "statsigContext": {"country": "JP"}}
    response = TestClient(create_app()).post("/api/orders", json={
        "session": json.dumps(session), "billing_country": "US", "exit_country": "US", "plan": "plus",
    })
    assert response.status_code == 401
    fail.assert_called_once()


@pytest.mark.parametrize("original_country", ["US", "JP"])
def test_order_reissue_401_exports_only_safe_response_fields(monkeypatch, caplog, original_country):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from gss_core import chatgpt

    secret = "private-auth-response-canary"
    ray = "abcdef0123456789-IAD"
    rid = "req_0123456789abcdef0123456789abcdef"
    refusal = SimpleNamespace(status_code=401, text=secret,
                              headers={"Set-Cookie": secret, "CF-Ray": ray, "X-Request-ID": rid},
                              json=lambda: {"error": {"code": "token_revoked", "message": secret}})
    transport = Mock(headers={}, cookies={})
    transport.get.return_value = refusal
    monkeypatch.setattr(chatgpt, "_new_session", lambda **kw: transport)
    monkeypatch.setattr(orders, "_guard_refresh_egress", AsyncMock())
    monkeypatch.setattr(orders, "checkout_proxy_for", lambda *a, **kw: "http://unused.invalid")
    monkeypatch.setattr(orders, "build_pipeline", lambda: pytest.fail("checkout after 401"))
    stages = []
    monkeypatch.setattr(orders, "_mark", lambda _progress, stage: stages.append(stage))
    session = ("eyJ-fixture..iv.cipher.tag" if original_country == "US" else json.dumps({
        "sessionToken": "fixture-only", "accessToken": "fixture-old", "statsigContext": {"country": original_country},
    }))
    result = TestClient(create_app()).post("/api/orders", json={
        "session": session, "billing_country": "US", "exit_country": "US", "plan": "plus",
    })
    assert result.status_code == 401
    assert result.json()["detail"]["code"] == "invalid_session"
    assert result.json()["detail"]["detail"] == {
        "upstream_status": 401, "upstream_code": "token_revoked", "cf_ray": ray, "request_id": rid,
        "upstream_code_state": "accepted", "request_id_state": "accepted",
    }
    transport.get.assert_called_once()
    transport.close.assert_called_once()
    assert stages == ["session_refresh" if original_country == "US" else "reissuing"]
    assert secret not in result.text + caplog.text
