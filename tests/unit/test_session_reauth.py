"""跨区重签发：丢弃旧 accessToken / 旧 statsig，只带 sessionToken 取新 token。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gss_core.chatgpt import ChatGPTClient

OLD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)


class _Jar:
    def __init__(self) -> None:
        self.data = {}

    def set(self, name, value, domain=None, **kwargs):
        self.data[name] = value

    def get_dict(self, **kwargs):
        return dict(self.data)


class _Http:
    def __init__(self, access_token: str) -> None:
        self.headers = {}
        self.cookies = SimpleNamespace(jar=_Jar())
        self.gets = []
        self._payload = {
            "accessToken": access_token,
            # 服务端可能回带旧 statsig；重签发时必须忽略。
            "statsigContext": {
                "deviceId": "old-device-id",
                "userAgent": OLD_UA,
                "country": "US",
            },
        }

    def get(self, url, **kwargs):
        self.gets.append({"url": url, **kwargs})
        return SimpleNamespace(
            status_code=200,
            text=json.dumps(self._payload),
            url=url,
            json=lambda: self._payload,
        )

    def close(self):
        pass


def _session():
    return json.dumps(
        {
            "accessToken": "old-access-token",
            "sessionToken": "core-session-token",
            "statsigContext": {
                "deviceId": "old-device-id",
                "userAgent": OLD_UA,
                "country": "US",
            },
            "cookies": {
                "__cf_bm": "old-cloudflare",
                "_cfuvid": "old-cfuvid",
                "oai-did": "old-device-id",
                "oai-sc": "old-sc",
                "__Secure-next-auth.session-token": "core-session-token",
            },
        }
    )


def test_reauthorize_drops_old_identity_and_uses_target_proxy(monkeypatch):
    # 拟人停顿在单测里必须为零延迟。
    monkeypatch.setenv("GSS_WARMUP_INTERVAL", "0")
    created = {}
    http = _Http("new-access-token")

    def fake_new_session(*, proxy, timeout, impersonate="chrome150"):
        created["proxy"] = proxy
        return http

    monkeypatch.setattr("gss_core.chatgpt._new_session", fake_new_session)
    client = ChatGPTClient.reauthorize_for_country(
        _session(), proxy="http://user-region-PH-sid-PIN:pw@gw.example.com:3010", country="PH"
    )
    try:
        assert client.access_token == "new-access-token"
        assert client.session_token == "core-session-token"
        assert client.device_id and client.device_id != "old-device-id"
        assert client.user_agent and client.user_agent != OLD_UA
        assert client.user_agent.startswith("Mozilla/5.0")
        assert client.session_country == "PH"
        assert created["proxy"] == "http://user-region-PH-sid-PIN:pw@gw.example.com:3010"

        # 先同连接首页边缘握手，再打 /api/auth/session（冷连接裸发 ST 易被边缘判异常）。
        assert [call["url"] for call in http.gets][0] == "https://chatgpt.com/"
        assert http.gets[-1]["url"].endswith("/api/auth/session")
        assert len(http.gets) == 2
        # 身份头挂在会话上，随请求一起发出。
        headers = http.headers
        assert headers["Authorization"] == "Bearer new-access-token"
        assert "old-access-token" not in headers["Authorization"]
        assert "core-session-token" in headers["Cookie"]
        assert f"oai-did={client.device_id}" in headers["Cookie"]
        assert headers["User-Agent"] == client.user_agent
        # 旧 statsig 的 deviceId/UA 不能覆盖新生成的身份。
        assert client.device_id != "old-device-id"
        assert client.extra_cookies["oai-did"] == client.device_id
        # Old Cloudflare / device-state cookies must not cross the region boundary.
        assert "__cf_bm" not in client.extra_cookies
        assert "_cfuvid" not in client.extra_cookies
        assert client.extra_cookies.get("oai-sc") is None
    finally:
        client.close()


def test_reauthorize_requires_session_token():
    raw = json.dumps({"accessToken": "old", "statsigContext": {"country": "US"}})
    with pytest.raises(Exception) as exc:
        ChatGPTClient.reauthorize_for_country(raw, proxy=None, country="PH")
    assert "sessionToken" in str(exc.value) or "session-token" in str(exc.value)


def test_reauthorize_warmup_is_anonymous_and_sanitizes_cookie_crlf(monkeypatch):
    monkeypatch.setenv("GSS_WARMUP_INTERVAL", "0")
    http = _Http("new-access-token")
    monkeypatch.setattr("gss_core.chatgpt._new_session", lambda **kw: http)

    raw = json.dumps({
        "accessToken": "old-at",
        "sessionToken": "line1-token\r\nline2-token",
        "statsigContext": {"country": "US"},
    })
    client = ChatGPTClient.reauthorize_for_country(raw, proxy=None, country="PH")
    try:
        assert len(http.gets) == 2
        warmup_cookie = http.gets[0]["headers"]["Cookie"]
        assert "__Secure-next-auth.session-token" not in warmup_cookie
        assert "line1-token" not in warmup_cookie
        assert f"oai-did={client.device_id}" in warmup_cookie

        # 清洗掉换行符，确保最终 Cookie 与 session_token 均不含 CR/LF。
        assert client.session_token == "line1-tokenline2-token"
        assert "\r" not in http.headers["Cookie"] and "\n" not in http.headers["Cookie"]
        assert "__Secure-next-auth.session-token=line1-tokenline2-token" in http.headers["Cookie"]
    finally:
        client.close()


def test_reauthorize_distinguishes_transport_errors_from_invalid_session(monkeypatch, caplog):
    from gss_contracts.errors import DomainError, ErrorCode

    monkeypatch.setenv("GSS_WARMUP_INTERVAL", "0")

    class FakeCurlErr(RuntimeError):
        code = 56

    # 1) 匿名 warmup_edge 断连：未发送任何凭证（credentials_sent=False），归类为上游传输错误。
    class WarmupFailHttp(_Http):
        def get(self, url, **kwargs):
            raise FakeCurlErr("http://user:secret-proxy-pw@gw.invalid:1111 tunnel failed")

    monkeypatch.setattr("gss_core.chatgpt._new_session", lambda **kw: WarmupFailHttp("tok"))
    with caplog.at_level("WARNING"):
        with pytest.raises(DomainError) as exc1:
            ChatGPTClient.reauthorize_for_country(_session(), proxy="http://p.invalid:80", country="PH")
    assert exc1.value.code is ErrorCode.CHECKOUT_UPSTREAM_ERROR
    assert exc1.value.detail["credentials_sent"] is False
    assert "warmup_transport_error type=FakeCurlErr code=56" in caplog.text
    assert "secret-proxy-pw" not in caplog.text

    # 2) /api/auth/session 断连：已发送凭证（credentials_sent=True），绝不误报 invalid_session，
    #    也不得泄漏代理口令。
    caplog.clear()

    class RefreshFailHttp(_Http):
        def get(self, url, **kwargs):
            if url.endswith("/api/auth/session"):
                raise FakeCurlErr("http://user:secret-proxy-pw@gw.invalid:1111 reset")
            return super().get(url, **kwargs)

    monkeypatch.setattr("gss_core.chatgpt._new_session", lambda **kw: RefreshFailHttp("tok"))
    with caplog.at_level("WARNING"):
        with pytest.raises(DomainError) as exc2:
            ChatGPTClient.reauthorize_for_country(_session(), proxy="http://p.invalid:80", country="PH")
    assert exc2.value.code is ErrorCode.CHECKOUT_UPSTREAM_ERROR
    assert exc2.value.detail["credentials_sent"] is True
    assert "session_refresh_transport_error type=FakeCurlErr code=56" in caplog.text
    assert "secret-proxy-pw" not in caplog.text
