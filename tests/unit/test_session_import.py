"""A session exported in another country must not carry that device here."""
from __future__ import annotations

import json
import uuid

from gss_core.chatgpt import ChatGPTClient
from gss_core.session_token import parse_session_bundle

RAW = json.dumps({
    "accessToken": "access-fixture",
    "sessionToken": "session-fixture",
    "deviceId": "exported-device",
    "sessionId": "exported-session",
    "cookies": {"oai-did": "exported-device", "oai-sc": "exported-sc", "__cf_bm": "cf"},
    "statsigContext": {"deviceId": "exported-device", "country": "US",
                       "userAgent": "Mozilla/5.0 (Windows NT 10.0) Chrome/142.0"},
    "user": {"email": "fixture@example.test"},
})


def test_strip_ambient_keeps_only_credentials():
    bundle = parse_session_bundle(RAW, strip_ambient=True)

    assert bundle.access_token == "access-fixture"
    assert bundle.session_token == "session-fixture"
    assert bundle.device_id == ""
    assert bundle.user_agent == ""
    assert bundle.country == ""
    assert bundle.oai_session_id == ""
    assert bundle.cookies == {"__Secure-next-auth.session-token": "session-fixture"}


def test_default_parse_still_reads_the_exported_identity():
    bundle = parse_session_bundle(RAW)

    assert bundle.device_id == "exported-device"
    assert bundle.country == "US"
    assert bundle.cookies["oai-did"] == "exported-device"


def test_client_import_mints_a_new_device_instead_of_reusing_the_exported_one():
    client = ChatGPTClient.from_raw_session(RAW, strip_ambient=True)
    try:
        assert client.device_id and client.device_id != "exported-device"
        uuid.UUID(client.device_id)
        assert client.session_country == ""
        assert client.user_agent == ""
        assert "oai-did" not in client.extra_cookies
        assert "oai-sc" not in client.extra_cookies
        assert client.session_token == "session-fixture"
    finally:
        client.close()


def test_each_import_gets_its_own_device():
    first = ChatGPTClient.from_raw_session(RAW, strip_ambient=True)
    second = ChatGPTClient.from_raw_session(RAW, strip_ambient=True)
    try:
        assert first.device_id != second.device_id
    finally:
        first.close()
        second.close()
