"""协议层必须使用 Session 的真实 UA，不能再落到 Linux/Chrome 默认值。"""

from __future__ import annotations

import json

from gss_core.captcha import verify_challenge
from gss_core.plus_har_protocol import PlusHarProtocol
from gss_core.plus_protocol import PlusProtocol
from gss_core.stripe_protocol import StripeProtocol

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.7444.60 Safari/537.36"
)


def test_stripe_protocol_uses_session_user_agent():
    protocol = StripeProtocol(user_agent=UA)
    try:
        assert protocol.ua == UA
        assert protocol.headers["User-Agent"] == UA
        assert protocol.platform == "Win32"
        assert protocol.screen == (1920, 1080, 1032)
        assert protocol._http().headers["User-Agent"] == UA
    finally:
        protocol.close()


def test_plus_protocol_uses_session_user_agent():
    protocol = PlusProtocol(
        session=json.dumps({"accessToken": "tok"}),
        checkout_url="https://chatgpt.com/checkout/openai_llc/oaics_fixture",
        publishable_key="pk_test_fixture",
        user_agent=UA,
    )
    try:
        assert protocol.ua == UA
        assert protocol.headers["User-Agent"] == UA
        assert protocol.platform == "Win32"
    finally:
        protocol.close()


def test_har_protocol_overrides_device_ua():
    protocol = PlusHarProtocol(
        session=json.dumps({"accessToken": "tok"}),
        user_agent=UA,
        device_id="did_fixture",
        account_id="acct_fixture",
    )
    try:
        assert protocol.device.ua == UA
        assert protocol.device.chrome_build == "142.0.7444.60"
        assert protocol.device.chrome_major == "142"
        assert protocol.device.impersonate == "chrome142"
        assert protocol.device.platform == "Win32"
    finally:
        protocol.close()


def test_verify_challenge_uses_session_user_agent():
    class Response:
        status_code = 200
        text = '{"status":"succeeded"}'

        def json(self):
            return {"status": "succeeded"}

    class Session:
        def __init__(self):
            self.headers = {}

        def post(self, url, data=None, headers=None, timeout=None):
            self.headers = dict(headers or {})
            return Response()

    session = Session()
    verify_challenge(
        session,
        intent_id="pi_fixture",
        client_secret="pi_fixture_secret",
        token="token_fixture",
        publishable_key="pk_test_fixture",
        api_version="2026-03-25.dahlia",
        user_agent=UA,
    )
    assert session.headers["User-Agent"] == UA
