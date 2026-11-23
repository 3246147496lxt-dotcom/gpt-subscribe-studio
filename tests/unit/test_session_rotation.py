"""Session rotation: the cookie returned by /api/auth/session must be adopted."""
from __future__ import annotations

import json
from http.cookiejar import CookieJar

from gss_core.chatgpt import ChatGPTClient


class FakeResponse:
    def __init__(self, payload: dict, headers: dict | None = None, status: int = 200):
        self.status_code = status
        self.payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class FakeJar:
    def __init__(self):
        self.jar = CookieJar()
        self.set_calls: list[tuple[str, str, dict]] = []

    def set(self, name, value, **kwargs):
        self.set_calls.append((name, value, kwargs))

    def get_dict(self, **kwargs):
        return {}


class FakeHttp:
    def __init__(self, response: FakeResponse):
        self.cookies = FakeJar()
        self.headers: dict = {}
        self.response = response

    def get(self, *args, **kwargs):
        return self.response


def client_with(response: FakeResponse) -> tuple[ChatGPTClient, FakeHttp]:
    client = ChatGPTClient(access_token="old", session_token="old-session", device_id="did")
    http = FakeHttp(response)
    client._http = http
    return client, http


def test_refresh_adopts_session_token_from_the_body():
    client, http = client_with(FakeResponse({"accessToken": "new", "sessionToken": "rotated"}))
    try:
        assert client.refresh_session() is True
    finally:
        client.close()

    assert client.session_token == "rotated"
    assert client.extra_cookies["__Secure-next-auth.session-token"] == "rotated"
    assert http.cookies.set_calls == [
        ("__Secure-next-auth.session-token", "rotated", {"domain": "chatgpt.com"})
    ]


def test_refresh_adopts_session_token_from_set_cookie():
    response = FakeResponse(
        {"accessToken": "new"},
        headers={"set-cookie": "__Secure-next-auth.session-token=header-rotated; Path=/; HttpOnly"},
    )
    client, _http = client_with(response)
    try:
        assert client.refresh_session() is True
    finally:
        client.close()
    assert client.session_token == "header-rotated"


def test_rotation_is_adopted_even_without_a_new_access_token():
    client, _http = client_with(FakeResponse({"sessionToken": "rotated-only"}))
    try:
        assert client.refresh_session() is False
    finally:
        client.close()
    assert client.session_token == "rotated-only"


def test_chunked_set_cookie_is_joined():
    response = FakeResponse(
        {"accessToken": "new"},
        headers={"set-cookie": (
            "__Secure-next-auth.session-token.0=part-one; Path=/, "
            "__Secure-next-auth.session-token.1=part-two; Path=/"
        )},
    )
    client, _http = client_with(response)
    try:
        client.refresh_session()
    finally:
        client.close()
    assert client.session_token == "part-onepart-two"
