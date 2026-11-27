"""ST -> 新出口环境重颁 AT、AT 过期检测、OAuth 刷新与 HAR UA 一致性测试。"""
from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from gss_api import locks, persistence
from gss_api.main import create_app
from gss_api.routers import orders as orders_router
from gss_api.runtime import build_pipeline
from gss_api.store import store
from gss_core import chatgpt as cg
from gss_core import plus_har_protocol as har
from gss_core.chatgpt import ChatGPTClient
from gss_core.plus_har_protocol import PlusHarProtocol, run_har_context
from gss_core.session_token import (
    access_token_exp_remaining,
    is_access_token_expired,
    parse_session_bundle,
)
from gss_core.stripe_protocol import PaymentOutcome
from gss_core.vault import vault

OLD_MAC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)
EXIT_WIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.7871.189 Safari/537.36"
)


def _make_jwt(*, account_id: str = "acct-reissue", exp_offset_s: int = 3600) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({
        "exp": int(time.time()) + exp_offset_s,
        "sub": account_id,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def test_session_token_only_and_refresh_token_only_json_are_accepted():
    st_only = parse_session_bundle(json.dumps({"sessionToken": "st-fixture-1"}))
    assert st_only.access_token == ""
    assert st_only.session_token == "st-fixture-1"
    assert st_only.cookies["__Secure-next-auth.session-token"] == "st-fixture-1"

    cookie_only = parse_session_bundle(
        json.dumps({"cookies": {"__Secure-next-auth.session-token": "st-cookie-2"}})
    )
    assert cookie_only.access_token == ""
    assert cookie_only.session_token == "st-cookie-2"

    rt_only = parse_session_bundle(json.dumps({"refresh_token": "rt-fixture-3"}))
    assert rt_only.access_token == ""
    assert rt_only.refresh_token == "rt-fixture-3"

    with pytest.raises(ValueError, match="accessToken"):
        parse_session_bundle(json.dumps({"user": {"email": "a@example.invalid"}}))


def test_jwt_expiry_helpers_distinguish_expired_valid_and_synthetic():
    valid = _make_jwt(exp_offset_s=3600)
    expiring = _make_jwt(exp_offset_s=30)
    expired = _make_jwt(exp_offset_s=-120)

    assert access_token_exp_remaining("opaque-synthetic-token") is None
    assert is_access_token_expired("opaque-synthetic-token") is False
    assert is_access_token_expired(valid, leeway_seconds=60) is False
    assert is_access_token_expired(expiring, leeway_seconds=60) is True
    assert is_access_token_expired(expired) is True


def test_refresh_session_strips_stale_authorization_and_updates_account_id():
    seen_auth_during_get: list[bool] = []
    fresh_at = _make_jwt(account_id="acct-fresh-42", exp_offset_s=86400)

    class _Http:
        def __init__(self, status: int, payload: dict):
            self.headers: dict = {"Authorization": "Bearer stale-revoked-at"}
            self.cookies: dict = {"oai-sc": "fresh-sc"}
            self.status = status
            self.payload = payload

        def get(self, url, **kwargs):
            seen_auth_during_get.append("Authorization" in self.headers)
            return SimpleNamespace(
                status_code=self.status,
                text=json.dumps(self.payload),
                url=url,
                headers={},
                json=lambda: self.payload,
            )

        def close(self):
            pass

    # 1) 成功时：请求 /api/auth/session 不带旧 Authorization，完成后写入新 AT 与新 account_id
    client = ChatGPTClient(access_token="stale-revoked-at", session_token="st-live", device_id="did-new")
    http = _Http(200, {"accessToken": fresh_at})
    client._http = http
    assert client.refresh_session() is True
    assert seen_auth_during_get == [False]
    assert client.access_token == fresh_at
    assert client.account_id == "acct-fresh-42"
    assert http.headers["Authorization"] == f"Bearer {fresh_at}"

    # 2) 软失败时：请求同样不带旧 Authorization，失败后恢复原头不破坏软失败语义
    seen_auth_during_get.clear()
    fail_client = ChatGPTClient(access_token="stale-revoked-at", session_token="st-live", device_id="did-new")
    fail_http = _Http(503, {})
    fail_client._http = fail_http
    assert fail_client.refresh_session() is False
    assert seen_auth_during_get == [False]
    assert fail_http.headers["Authorization"] == "Bearer stale-revoked-at"


def test_refresh_session_does_not_overwrite_stripped_device_with_echoed_statsig():
    raw = json.dumps({
        "sessionToken": "st-clean",
        "statsigContext": {"deviceId": "old-did", "userAgent": OLD_MAC_UA, "country": "US"},
    })
    client = ChatGPTClient.from_raw_session(raw, strip_ambient=True)
    minted_did = client.device_id
    fresh_at = _make_jwt(account_id="acct-clean")

    class _Http:
        headers: dict = {}
        cookies: dict = {}

        def get(self, url, **kwargs):
            payload = {
                "accessToken": fresh_at,
                "statsigContext": {"deviceId": "old-did", "userAgent": OLD_MAC_UA, "country": "US"},
            }
            return SimpleNamespace(status_code=200, text=json.dumps(payload), headers={}, json=lambda: payload)

        def close(self):
            pass

    client._http = _Http()
    try:
        assert client.refresh_session(trust_statsig=True) is True
        assert client.device_id == minted_did
        assert client.user_agent != OLD_MAC_UA
        assert client.access_token == fresh_at
    finally:
        client.close()


def test_reauthorize_for_country_can_preserve_session_ua(monkeypatch):
    raw = json.dumps({
        "accessToken": "old-at",
        "sessionToken": "st-keep-ua",
        "statsigContext": {"deviceId": "old-did", "userAgent": OLD_MAC_UA, "country": "US"},
        "cookies": {"__cf_bm": "stale-cf", "oai-sc": "stale-sc"},
    })
    fresh_at = _make_jwt(account_id="acct-keep-ua")

    class _Http:
        def __init__(self):
            self.headers: dict = {}
            self.cookies = SimpleNamespace(
                jar=SimpleNamespace(),
                set=lambda *a, **k: None,
                get_dict=lambda **k: {},
            )

        def get(self, url, **kwargs):
            payload = {"accessToken": fresh_at}
            return SimpleNamespace(status_code=200, text=json.dumps(payload), headers={}, json=lambda: payload)

        def close(self):
            pass

    monkeypatch.setattr(cg, "_new_session", lambda **kw: _Http())
    client = ChatGPTClient.reauthorize_for_country(
        raw, proxy="http://exit.invalid:8080", country="US", preserve_user_agent=True,
    )
    try:
        assert client.access_token == fresh_at
        assert client.user_agent == OLD_MAC_UA
        assert client.device_id != "old-did"
        assert "__cf_bm" not in client.extra_cookies
        assert "oai-sc" not in client.extra_cookies
    finally:
        client.close()


def test_mac_exported_session_stays_macos_across_reauthorize_oauth_and_plushar(monkeypatch):
    from gss_core import runtime_flags
    from gss_core.session_token import parse_session_bundle

    monkeypatch.setenv("GSS_WARMUP_INTERVAL", "0")
    fresh_at = _make_jwt(account_id="acct-mac-parity")
    mac_reduced_ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
    win_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )

    class _Http:
        def __init__(self):
            self.headers: dict = {}
            self.cookies = SimpleNamespace(
                jar=SimpleNamespace(),
                set=lambda *a, **k: None,
                get_dict=lambda **k: {},
            )

        def get(self, url, **kwargs):
            payload = {"accessToken": fresh_at}
            return SimpleNamespace(status_code=200, text=json.dumps(payload), headers={}, json=lambda: payload)

        def post(self, url, data=None, headers=None):
            payload = {"access_token": fresh_at, "refresh_token": "rt-mac-2"}
            return SimpleNamespace(status_code=200, text=json.dumps(payload), json=lambda: payload)

        def close(self):
            pass

    monkeypatch.setattr(cg, "_new_session", lambda **kw: _Http())

    # 1) 顶层 userAgent + 平台字段均可被识别，Mac session 重颁默认不退化成 Windows
    raw_mac_top = json.dumps({
        "accessToken": "old-at",
        "sessionToken": "st-mac-top",
        "userAgent": mac_reduced_ua,
    })
    bundle_mac = parse_session_bundle(raw_mac_top)
    assert bundle_mac.user_agent == mac_reduced_ua
    assert bundle_mac.os_hint == "macos"

    client_clean = ChatGPTClient.reauthorize_for_country(
        raw_mac_top, proxy="http://exit.invalid:8080", country="PH", preserve_user_agent=False,
    )
    try:
        assert "Macintosh" in client_clean.user_agent
        assert client_clean.user_agent != mac_reduced_ua
        ident = {k.lower(): v for k, v in client_clean._identity_headers().items()}
        assert ident["sec-ch-ua-platform"] == '"macOS"'
        assert "sec-ch-ua-arch" not in ident
        assert client_clean.sentinel_profile["sec_ch_ua_arch"] == '"arm"'
        assert client_clean.sentinel_profile["platform"] == "MacIntel"
        assert client_clean.sentinel_profile["platform_label"] == "macOS"
    finally:
        client_clean.close()

    # 2) preserve_user_agent=True 保留缩减 UA，高熵版本号属性自动展开 .0.0.0 -> 真实构建号
    client_keep = ChatGPTClient.reauthorize_for_country(
        raw_mac_top, proxy="http://exit.invalid:8080", country="PH", preserve_user_agent=True,
    )
    try:
        assert client_keep.user_agent == mac_reduced_ua
        assert client_keep.chrome_build == "146.0.7680.180"
        assert '"Google Chrome";v="146.0.7680.180"' in client_keep.sec_ch_ua_full_list
        assert "146.0.0.0" not in client_keep.sec_ch_ua_full_list
        client_keep._accepted_ch.update({"sec-ch-ua-full-version", "sec-ch-ua-full-version-list"})
        ident = {k.lower(): v for k, v in client_keep._identity_headers().items()}
        assert ident["sec-ch-ua-full-version"] == '"146.0.7680.180"'
        assert '"Google Chrome";v="146.0.7680.180"' in ident["sec-ch-ua-full-version-list"]
    finally:
        client_keep.close()

    # 2b) 超 3933B 的真实 NextAuth sessionToken 自动拆分为 .0/.1 分片 Cookie 并可无缝重组
    big_st = "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0.." + ("a" * 16) + "." + ("b" * 4270) + "." + ("c" * 22)  # release-check: ignore
    assert len(big_st) > 3933
    chunked_raw = json.dumps({"accessToken": "old-at", "sessionToken": big_st, "userAgent": mac_reduced_ua})
    parsed_big = parse_session_bundle(chunked_raw)
    assert parsed_big.session_token == big_st
    assert "__Secure-next-auth.session-token.0" in parsed_big.cookies
    assert len(parsed_big.cookies["__Secure-next-auth.session-token.0"]) == 3933
    assert "__Secure-next-auth.session-token.1" in parsed_big.cookies
    c_big = ChatGPTClient.from_raw_session(chunked_raw)
    hdr_cookie = c_big._cookie_header()
    assert "__Secure-next-auth.session-token.0=" in hdr_cookie
    assert "__Secure-next-auth.session-token.1=" in hdr_cookie
    assert "__Secure-next-auth.session-token=" not in hdr_cookie
    assert "__Secure-next-auth.session-token" not in c_big._cookie_header(include_session_token=False)
    # 仅带分片 Cookie 字典也能还原完整 sessionToken
    reassembled_bundle = parse_session_bundle(json.dumps({"accessToken": "old-at", "cookies": parsed_big.cookies}))
    assert reassembled_bundle.session_token == big_st

    # 3) Windows 导出 session 在 auto 模式下（即使跑在 Mac 宿主机上）仍发 Windows 请求头
    raw_win = json.dumps({"accessToken": "old-at", "sessionToken": "st-win", "userAgent": win_ua})
    client_win = ChatGPTClient.reauthorize_for_country(
        raw_win, proxy="http://exit.invalid:8080", country="PH", preserve_user_agent=False,
    )
    try:
        assert "Windows NT" in client_win.user_agent
        assert client_win._identity_headers()["sec-ch-ua-platform"] == '"Windows"'
    finally:
        client_win.close()

    # 4) OAuth 重颁同样保持 macOS，不强切 Windows
    raw_oauth_mac = json.dumps({
        "refresh_token": "rt-mac-1",
        "statsigContext": {"userAgent": mac_reduced_ua, "country": "US"},
    })
    client_oauth = ChatGPTClient.reauthorize_oauth(raw_oauth_mac, proxy="http://exit.invalid:8080", country="JP")
    try:
        assert "Macintosh" in client_oauth.user_agent
        assert client_oauth._identity_headers()["sec-ch-ua-platform"] == '"macOS"'
    finally:
        client_oauth.close()

    # 5) 裸 ST 无 UA 时，面板切「强制 macOS」全链路生效（含 Linux 服务器场景）
    runtime_flags.update_flags({"client_os_profile": "macos"})
    try:
        raw_bare_st = json.dumps({"sessionToken": "st-bare-mac"})
        client_forced = ChatGPTClient.reauthorize_for_country(
            raw_bare_st, proxy="http://exit.invalid:8080", country="JP",
        )
        try:
            assert "Macintosh" in client_forced.user_agent
            assert client_forced._identity_headers()["sec-ch-ua-platform"] == '"macOS"'
            assert client_forced.sentinel_profile["platform"] == "MacIntel"
        finally:
            client_forced.close()

        from gss_core.har_device import DeviceProfile, apply_user_agent
        dev = DeviceProfile.random(country="JP")
        assert dev.os_key == "macos"
        apply_user_agent(dev, mac_reduced_ua)
        assert dev.chrome_build == "146.0.7680.180"
    finally:
        runtime_flags.reset_flags(["client_os_profile"])

    # 6) PlusHarProtocol 同区 read_only 路径也把 Mac session 同步到 client + device
    proto = PlusHarProtocol(session=raw_mac_top, exit_country="US", read_only=True)
    monkeypatch.setattr(har, "restore_account", lambda *a, **k: None)
    try:
        proto._prepare()
        assert proto.client is not None
        assert "Macintosh" in proto.client.user_agent
        assert proto.device.os_key == "macos"
        assert proto.device.ch_platform == '"macOS"'
    finally:
        proto.close()


def test_reauthorize_oauth_exchanges_refresh_token_on_target_exit(monkeypatch):
    fresh_at = _make_jwt(account_id="acct-oauth")

    class _Session:
        def __init__(self):
            self.calls = []

        def post(self, url, data=None, headers=None):
            self.calls.append((url, dict(data or {})))
            payload = {"access_token": fresh_at, "refresh_token": "rt-rotated"}
            return SimpleNamespace(status_code=200, text=json.dumps(payload), json=lambda: payload)

        def close(self):
            pass

    sess = _Session()
    monkeypatch.setattr(cg, "_new_session", lambda **kw: sess)
    raw = json.dumps({
        "accessToken": _make_jwt(exp_offset_s=-60),
        "refresh_token": "rt-initial",
        "statsigContext": {"country": "US", "deviceId": "old-did"},
    })
    client = ChatGPTClient.reauthorize_oauth(raw, proxy="http://exit.invalid:8080", country="PH")
    try:
        assert client.access_token == fresh_at
        assert client.refresh_token == "rt-rotated"
        assert client.account_id == "acct-oauth"
        assert client.session_country == "PH"
        assert client.device_id != "old-did"
        assert sess.calls and sess.calls[0][1]["refresh_token"] == "rt-initial"
    finally:
        client.close()


@pytest.mark.usefixtures("verified_us_payer")
def test_same_country_proactively_reissues_at_from_st_in_real_mode(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    monkeypatch.delenv("GSS_REISSUE_SESSION_ON_EXIT", raising=False)

    # Use fake checkout adapter via build_pipeline override while keeping real-mode order router behavior.
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    fake_pipeline = build_pipeline()
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: fake_pipeline)
    monkeypatch.setattr(orders_router, "_guard_refresh_egress", AsyncMock())
    monkeypatch.setattr(orders_router, "checkout_proxy_for", lambda *a, **kw: "http://exit.invalid:8080")

    reauth_calls = []
    fresh_at = _make_jwt(account_id="acct-same-country", exp_offset_s=86400)

    def fake_reauth(cls, raw, *, proxy, country, **kwargs):
        reauth_calls.append({"proxy": proxy, "country": country, "kwargs": kwargs})
        return SimpleNamespace(
            access_token=fresh_at,
            session_token="st-rotated",
            refresh_token="",
            device_id="clean-exit-did",
            user_agent=EXIT_WIN_UA,
            oai_session_id="clean-exit-sid",
            extra_cookies={
                "__Secure-next-auth.session-token": "st-rotated",
                "oai-did": "clean-exit-did",
            },
            close=lambda: None,
        )

    monkeypatch.setattr(ChatGPTClient, "reauthorize_for_country", classmethod(fake_reauth))
    stages: list[str] = []
    monkeypatch.setattr(orders_router, "_mark", lambda _p, stage: stages.append(stage))

    old_at = _make_jwt(account_id="acct-same-country", exp_offset_s=3600)
    submitted = json.dumps({
        "accessToken": old_at,
        "sessionToken": "st-original",
        "statsigContext": {"country": "US", "deviceId": "old-did", "userAgent": OLD_MAC_UA},
    })
    store.clear()
    try:
        res = TestClient(create_app()).post(
            "/api/orders",
            json={"plan": "plus", "billing_country": "US", "exit_country": "US", "session": submitted},
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["session_reissued_for_country"] is False
        assert len(reauth_calls) == 1
        assert reauth_calls[0]["country"] == "US"
        assert "session_refresh" in stages and "session_refreshed" in stages

        secrets = vault.get(body["id"])
        assert secrets is not None
        snap = json.loads(secrets.session)
        assert snap["accessToken"] == fresh_at
        assert snap["sessionToken"] == "st-rotated"
        assert snap["deviceId"] == "clean-exit-did"
        assert snap["userAgent"] == EXIT_WIN_UA
        assert secrets.user_agent == EXIT_WIN_UA
        assert secrets.device_id == "clean-exit-did"
    finally:
        store.clear()


@pytest.mark.usefixtures("verified_us_payer")
def test_expired_at_reissues_with_st_and_fails_fast_without_st(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    fake_pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: fake_pipeline)
    monkeypatch.setattr(orders_router, "_guard_refresh_egress", AsyncMock())

    expired_at = _make_jwt(account_id="acct-exp", exp_offset_s=-300)
    fresh_at = _make_jwt(account_id="acct-exp", exp_offset_s=7200)
    reauth_calls = []

    def fake_reauth(cls, raw, *, proxy, country, **kwargs):
        reauth_calls.append(country)
        return SimpleNamespace(
            access_token=fresh_at,
            session_token="st-live",
            refresh_token="",
            device_id="did-after-exp",
            user_agent=EXIT_WIN_UA,
            oai_session_id="sid-after-exp",
            extra_cookies={"__Secure-next-auth.session-token": "st-live", "oai-did": "did-after-exp"},
            close=lambda: None,
        )

    monkeypatch.setattr(ChatGPTClient, "reauthorize_for_country", classmethod(fake_reauth))
    store.clear()
    api = TestClient(create_app())
    try:
        # 1) 过期 AT + 无 ST -> 立即 401 session_expired，不打建单
        r_no_st = api.post(
            "/api/orders",
            json={"plan": "plus", "billing_country": "US", "exit_country": "US",
                  "session": json.dumps({"accessToken": expired_at})},
        )
        assert r_no_st.status_code == 401
        assert r_no_st.json()["detail"]["code"] == "session_expired"
        assert reauth_calls == []

        # 2) 过期 AT + 有 ST（即使同区）-> 自动用 ST 重颁新 AT 并建单成功
        r_with_st = api.post(
            "/api/orders",
            json={"plan": "plus", "billing_country": "US", "exit_country": "US",
                  "session": json.dumps({
                      "accessToken": expired_at,
                      "sessionToken": "st-live",
                      "statsigContext": {"country": "US"},
                  })},
        )
        assert r_with_st.status_code == 201, r_with_st.text
        assert reauth_calls == ["US"]
        secrets = vault.get(r_with_st.json()["id"])
        assert secrets is not None and fresh_at in secrets.session
    finally:
        store.clear()


@pytest.mark.usefixtures("verified_us_payer")
def test_st_only_json_refreshes_and_enforces_account_lock_after_refresh(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    db = persistence.Persistence(":memory:")
    monkeypatch.setattr(persistence, "_db", db)
    fake_pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: fake_pipeline)
    monkeypatch.setattr(orders_router, "_guard_refresh_egress", AsyncMock())

    locked_at = _make_jwt(account_id="acct-locked-st", exp_offset_s=3600)
    fp = locks.account_fingerprint(locked_at)
    db.acquire_lock(lock_id="lk-st", lock_key=f"account:{fp}", lock_type="account", reason="unknown_payment")

    def fake_refresh(self, *, reject_unauthorized=False) -> bool:
        self.access_token = locked_at
        return True

    monkeypatch.setattr(ChatGPTClient, "refresh_session", fake_refresh)
    store.clear()
    try:
        res = TestClient(create_app()).post(
            "/api/orders",
            json={"plan": "plus", "billing_country": "US", "exit_country": "US",
                  "session": json.dumps({"sessionToken": "st-only-json-fixture"})},
        )
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "payment_unknown_lock"
        assert fake_pipeline.deps.checkout.calls == []
    finally:
        store.clear()
        db.close()


@pytest.mark.usefixtures("verified_us_payer")
def test_oauth_refresh_token_order_reissues_before_checkout(monkeypatch):
    monkeypatch.setenv("GSS_USE_FAKE", "1")
    fake_pipeline = build_pipeline()
    monkeypatch.setattr(orders_router, "build_pipeline", lambda: fake_pipeline)
    monkeypatch.setattr(orders_router, "_guard_refresh_egress", AsyncMock())

    fresh_at = _make_jwt(account_id="acct-oauth-order", exp_offset_s=7200)
    oauth_calls = []

    def fake_oauth(cls, raw, *, proxy, country):
        oauth_calls.append(country)
        return SimpleNamespace(
            access_token=fresh_at,
            session_token="",
            refresh_token="rt-rotated-order",
            device_id="oauth-did",
            user_agent=EXIT_WIN_UA,
            oai_session_id="oauth-sid",
            extra_cookies={"oai-did": "oauth-did"},
            close=lambda: None,
        )

    monkeypatch.setattr(ChatGPTClient, "reauthorize_oauth", classmethod(fake_oauth))
    store.clear()
    try:
        res = TestClient(create_app()).post(
            "/api/orders",
            json={"plan": "plus", "billing_country": "US", "exit_country": "PH",
                  "session": json.dumps({"refresh_token": "rt-initial-order"})},
        )
        assert res.status_code == 201, res.text
        assert oauth_calls == ["PH"]
        secrets = vault.get(res.json()["id"])
        assert secrets is not None
        snap = json.loads(secrets.session)
        assert snap["accessToken"] == fresh_at
        assert snap["refresh_token"] == "rt-rotated-order"
    finally:
        store.clear()


def test_run_har_context_propagates_client_user_agent_and_device(monkeypatch):
    observed = {}

    def fake_pay(self):
        observed["ua"] = self.device.ua
        observed["os_key"] = self.device.os_key
        return PaymentOutcome(state="pending")

    monkeypatch.setattr(PlusHarProtocol, "pay", fake_pay)
    monkeypatch.setattr(har, "restore_account", lambda *a, **k: None)

    client = SimpleNamespace(
        account_id="acct-har-ua",
        proxy=None,
        user_agent=EXIT_WIN_UA,
        device_id="reissued-did-123",
    )
    context: dict = {}
    spec = {
        "checkout_url": "https://chatgpt.com/checkout/openai_llc/oaics_fixture",
        "checkout_id": "oaics_fixture",
        "publishable_key": "pk_live_fixture",
        "currency": "USD",
        "country": "US",
        "processor": "openai_llc",
    }
    run_har_context(client, context, spec, card={"number": "4242"}, billing={"country": "US"})
    assert observed["ua"] == EXIT_WIN_UA
    assert observed["os_key"] == "windows"
    assert context["har_device"]["ua"] == EXIT_WIN_UA


def test_plus_har_prepare_clears_stale_at_and_syncs_reissued_ua(monkeypatch, tmp_path):
    monkeypatch.setenv("GSS_PROXY_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setattr(har, "restore_account", lambda *a, **k: None)

    # 1) 同区带 ST：在 warm_exit_session 重颁前先清空旧 AT
    same_stub = SimpleNamespace(
        access_token="old-stale-at",
        timeout=25.0,
        language="en-US",
        timezone="UTC",
        account_id="acct-1",
        device_id="did-1",
        user_agent="",
        session_token="st-1",
        seen_at_before_warm=[],
        close=lambda: None,
    )

    def warm_same(*, reissue=True, oauth=False):
        same_stub.seen_at_before_warm.append(same_stub.access_token)
        same_stub.access_token = "fresh-at"
        return {}

    same_stub.warm_exit_session = warm_same
    monkeypatch.setattr(har.ChatGPTClient, "from_raw_session", staticmethod(lambda *a, **k: same_stub))

    eng_same = PlusHarProtocol(
        session=json.dumps({
            "accessToken": "old-stale-at",
            "sessionToken": "st-1",
            "statsigContext": {"country": "US"},
        }),
        exit_country="US",
        checkout_url="https://chatgpt.com/checkout/openai_llc/oaics_fixture",
        publishable_key="pk_live_fixture",
        currency="USD",
        billing={"country": "US"},
    )
    eng_same._prepare()
    assert same_stub.seen_at_before_warm == [""]

    # 2) 跨区重颁后：self.device 的 UA 与 reauthorize_for_country 签发 AT 时的 UA 保持一致
    cross_stub = SimpleNamespace(
        access_token="fresh-ph-at",
        timeout=25.0,
        language="en-PH",
        timezone="Asia/Manila",
        account_id="acct-2",
        device_id="did-ph",
        user_agent=EXIT_WIN_UA,
        session_token="st-2",
        warm_exit_session=lambda **kw: {},
        close=lambda: None,
    )
    monkeypatch.setattr(har.ChatGPTClient, "reauthorize_for_country", staticmethod(lambda *a, **k: cross_stub))
    eng_cross = PlusHarProtocol(
        session=json.dumps({
            "accessToken": "old-us-at",
            "sessionToken": "st-2",
            "statsigContext": {"country": "US", "userAgent": OLD_MAC_UA},
        }),
        exit_country="PH",
        checkout_url="https://chatgpt.com/checkout/openai_llc/oaics_fixture",
        publishable_key="pk_live_fixture",
        currency="PHP",
        billing={"country": "US"},
    )
    eng_cross._prepare()
    assert eng_cross.device.ua == EXIT_WIN_UA
    assert eng_cross.device.os_key == "windows"
