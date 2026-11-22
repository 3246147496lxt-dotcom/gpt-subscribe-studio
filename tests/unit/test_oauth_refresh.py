"""OAuth refresh tokens are portable across login regions."""
from __future__ import annotations

import json

import pytest

from gss_contracts.errors import DomainError, ErrorCode
from gss_core import chatgpt as cg
from gss_core.chatgpt import OAUTH_TOKEN_URL, ChatGPTClient
from gss_core.session_token import parse_session_bundle

RAW = json.dumps({
    "accessToken": "old-access",
    "refresh_token": "refresh-fixture",
    "statsigContext": {"country": "US", "deviceId": "exported-device"},
})


class Response:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class Session:
    def __init__(self, response: Response):
        self.response = response
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, url, data=None, headers=None):
        self.calls.append((url, dict(data or {})))
        return self.response

    def close(self):
        self.closed = True


def test_refresh_token_is_parsed_and_survives_stripping():
    plain = parse_session_bundle(RAW)
    strict = parse_session_bundle(RAW, strip_ambient=True)

    assert plain.refresh_token == "refresh-fixture"
    assert strict.refresh_token == "refresh-fixture"
    assert strict.device_id == "" and strict.country == ""


def test_oauth_refresh_replaces_access_and_rotates_refresh_token(monkeypatch):
    response = Response(200, {"access_token": "new-access", "refresh_token": "rotated"})
    session = Session(response)
    monkeypatch.setattr(cg, "_new_session", lambda **kwargs: session)
    client = ChatGPTClient(access_token="old-access", refresh_token="refresh-fixture")
    try:
        assert client.refresh_oauth_token(client_id="client-fixture") is True
    finally:
        client.close()

    assert client.access_token == "new-access"
    assert client.refresh_token == "rotated"
    url, form = session.calls[0]
    assert url == OAUTH_TOKEN_URL
    assert form == {"grant_type": "refresh_token",
                    "refresh_token": "refresh-fixture",
                    "client_id": "client-fixture"}
    assert session.closed is True


def test_oauth_refresh_without_a_new_refresh_token_keeps_the_old_one(monkeypatch):
    session = Session(Response(200, {"access_token": "new-access"}))
    monkeypatch.setattr(cg, "_new_session", lambda **kwargs: session)
    client = ChatGPTClient(access_token="old-access", refresh_token="refresh-fixture")
    try:
        assert client.refresh_oauth_token(client_id="client-fixture") is True
    finally:
        client.close()
    assert client.refresh_token == "refresh-fixture"


def test_oauth_rejection_is_a_structured_failure(monkeypatch):
    session = Session(Response(401, {"error": "invalid_grant"}))
    monkeypatch.setattr(cg, "_new_session", lambda **kwargs: session)
    client = ChatGPTClient(access_token="old-access", refresh_token="refresh-fixture")
    try:
        with pytest.raises(DomainError) as caught:
            client.refresh_oauth_token(client_id="client-fixture")
    finally:
        client.close()
    assert caught.value.code is ErrorCode.INVALID_SESSION
    assert caught.value.detail["code"] == "oauth_refresh_rejected"
    assert caught.value.detail["upstream_code"] == "invalid_grant"


def test_oauth_transport_failure_is_a_structured_failure(monkeypatch):
    def broken(**kwargs):
        raise OSError("offline")

    monkeypatch.setattr(cg, "_new_session", broken)
    client = ChatGPTClient(access_token="old-access", refresh_token="refresh-fixture")
    try:
        with pytest.raises(DomainError) as caught:
            client.refresh_oauth_token(client_id="client-fixture")
    finally:
        client.close()
    assert caught.value.detail["code"] == "oauth_refresh_transport"


def test_warm_exit_session_uses_oauth_then_probes(monkeypatch):
    calls: list[str] = []
    client = ChatGPTClient(access_token="a", refresh_token="r")
    monkeypatch.setenv("GSS_WARMUP_INTERVAL", "0")
    monkeypatch.setattr(client, "warmup", lambda **kwargs: calls.append("handshake"))
    monkeypatch.setattr(client, "refresh_oauth_token",
                        lambda **kwargs: (calls.append("oauth"), True)[1])
    monkeypatch.setattr(client, "refresh_session",
                        lambda **kwargs: (calls.append("web_session"), True)[1])
    monkeypatch.setattr(client, "_warm_probe",
                        lambda path, params, *, stage: calls.append(stage))
    try:
        client.warm_exit_session(oauth=True)
    finally:
        client.close()

    assert calls == ["handshake", "oauth", "me", "accounts_check"]


def test_oauth_refresh_does_not_fall_back_to_the_web_cookie(monkeypatch):
    calls: list[str] = []
    client = ChatGPTClient(access_token="a", refresh_token="r", session_token="s")
    monkeypatch.setenv("GSS_WARMUP_INTERVAL", "0")
    monkeypatch.setattr(client, "warmup", lambda **kwargs: calls.append("handshake"))
    monkeypatch.setattr(client, "refresh_oauth_token",
                        lambda **kwargs: (calls.append("oauth"), True)[1])
    monkeypatch.setattr(client, "refresh_session",
                        lambda **kwargs: (calls.append("web_session"), True)[1])
    monkeypatch.setattr(client, "_warm_probe",
                        lambda path, params, *, stage: calls.append(stage))
    try:
        client.warm_exit_session(oauth=True, reissue=True)
    finally:
        client.close()

    assert "web_session" not in calls
