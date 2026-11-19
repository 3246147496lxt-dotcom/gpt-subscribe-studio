"""The account warm-up must use the live refreshed credentials, not HTTP cache."""
from __future__ import annotations

from gss_core.chatgpt import ChatGPTClient


class _Response:
    status_code = 200
    content = b"{}"
    text = "{}"


class _HTTP:
    def __init__(self) -> None:
        self.headers = {}
        self.cookies = None
        self.calls = []

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        return _Response()


def test_warm_account_uses_new_bearer_and_cookie():
    http = _HTTP()
    client = ChatGPTClient(
        access_token="NEW_ACCESS",
        session_token="NEW_SESSION",
        device_id="NEW_DID",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
        ),
        extra_cookies={
            "__Secure-next-auth.session-token": "NEW_SESSION",
            "oai-did": "NEW_DID",
        },
        _http=http,
    )

    client.warm_account()

    assert len(http.calls) == 1
    url, headers = http.calls[0]
    assert url.endswith("/backend-api/me")
    assert headers["Authorization"] == "Bearer NEW_ACCESS"
    assert "__Secure-next-auth.session-token=NEW_SESSION" in headers["Cookie"]
    assert "oai-did=NEW_DID" in headers["Cookie"]
    assert headers["oai-device-id"] == "NEW_DID"
