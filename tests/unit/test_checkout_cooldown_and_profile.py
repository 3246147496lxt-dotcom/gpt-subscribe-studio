"""P1 回归：默认 Windows 档案、脏 SID 冷却隔离、账号详情设备身份复用。

覆盖《ChatGPT 建单协议对齐改造计划书》P1-1 / P1-2：
1. `_PROFILE_ORDER` 默认收敛为 Windows Chrome；
2. 建单命中 unusual_activity 时把该出口 SID 写进冷却黑名单，并记录脱敏 proxy_last_sid；
3. 随机 SID 生成器跳过冷却中的 SID；
4. account_detail 查询订阅/账单时沿用原 Session 的 device_id / UA。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gss_api.persistence import Persistence
from gss_api.routers import orders
from gss_api.schemas import OrderCreateRequest
from gss_api.store import InMemoryStore
from gss_contracts.errors import DomainError, ErrorCode
from gss_core import proxies
from gss_core.chatgpt import _PROFILE_ORDER, _pick_profile

SID = "87654321"
EXIT_PROXY = f"http://user-region-PH-sid-{SID}:pw@gw.example.com:3010"
SESSION_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.7680.180 Safari/537.36"
)


# ── 1. 默认档案 ──────────────────────────────────────────────


def test_default_profile_order_and_os_profile_override(monkeypatch) -> None:
    from gss_core import runtime_flags

    assert _PROFILE_ORDER == ["windows"]
    runtime_flags.update_flags({"client_os_profile": "windows"})
    try:
        for index in range(50):
            profile = _pick_profile(f"device-{index}")
            assert profile["key"] == "windows"
            assert profile["platform"] == "Win32"
            assert profile["label"] == "Windows"

        runtime_flags.update_flags({"client_os_profile": "macos"})
        for index in range(50):
            profile = _pick_profile(f"device-{index}")
            assert profile["key"] == "macos"
            assert profile["platform"] == "MacIntel"
            assert profile["label"] == "macOS"
    finally:
        runtime_flags.reset_flags(["client_os_profile"])


# ── 2. SID 冷却黑名单 ────────────────────────────────────────


def test_cooling_sid_is_skipped_by_new_proxy_sid(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GSS_PROXY_LEASE_FILE", str(tmp_path / "proxy-leases.json"))
    monkeypatch.delenv("GSS_PROXY_SID", raising=False)
    monkeypatch.delenv("GSS_PROXY_SID_ALNUM", raising=False)

    proxies.cool_proxy(EXIT_PROXY, reason="checkout:unusual_activity", seconds=1800)
    assert SID in proxies.cooling_sids()

    for _ in range(50):
        assert proxies.new_proxy_sid() != SID
        assert proxies.fresh_proxy_sid() != SID
        rotated = proxies.rotate_proxy_sid(EXIT_PROXY, force=True)
        assert proxies.proxy_exit_sid(rotated) != SID


def test_expired_cooldown_is_not_skipped(tmp_path, monkeypatch) -> None:
    path = tmp_path / "proxy-leases.json"
    monkeypatch.setenv("GSS_PROXY_LEASE_FILE", str(path))
    proxies.cool_proxy(EXIT_PROXY, reason="checkout:unusual_activity", seconds=1)
    assert SID in proxies.cooling_sids()

    data = json.loads(path.read_text())
    for row in data["cooldowns"]:
        row["until"] = 1  # 已过期
    path.write_text(json.dumps(data))
    assert SID not in proxies.cooling_sids()


# ── 3. 建单命中风控 → 隔离出口 & 记录脱敏 SID ────────────────


@pytest.fixture
def isolated_order(monkeypatch, verified_us_payer):
    db = Persistence(":memory:")
    store = InMemoryStore()
    store.attach_db(db)
    monkeypatch.setattr(orders, "store", store)
    monkeypatch.setattr(orders, "vault", Mock())
    monkeypatch.setattr(orders.locks, "find_active_lock", lambda *a: None)
    monkeypatch.setattr(orders.locks, "hold_order_unknown", lambda *a, **kw: pytest.fail("unexpected funds lock"))
    monkeypatch.setattr(orders, "checkout_proxy_for", lambda *a, **kw: EXIT_PROXY)
    monkeypatch.setattr(orders, "rotate_proxy_sid", Mock(return_value=EXIT_PROXY))
    monkeypatch.setenv("GSS_ROUTE_PROFILE", "A")
    monkeypatch.setenv("GSS_WARMUP_INTERVAL", "0")
    monkeypatch.delenv("GSS_BILLING_PROFILES_FILE", raising=False)
    checkout = AsyncMock()
    monkeypatch.setattr(orders, "build_pipeline", lambda: SimpleNamespace(create_checkout=checkout))
    try:
        yield db, checkout
    finally:
        db.close()


@pytest.mark.parametrize(
    ("code", "message", "detail", "failure_code"),
    [
        (ErrorCode.CHECKOUT_UPSTREAM_ERROR, "上游报告异常流量",
         {"upstream_code": "unusual_activity"}, "unusual_activity"),
        (ErrorCode.INVALID_SESSION, "会话已失效",
         {"upstream_status": 401, "upstream_code": "token_revoked"}, "invalid_session"),
    ],
)
def test_risk_rejection_cools_exit_without_replay(
    isolated_order, monkeypatch, code, message, detail, failure_code
) -> None:
    db, checkout = isolated_order
    cooled: list[tuple[str, str]] = []

    def fake_cool(proxy, *, reason, seconds=1800):
        cooled.append((proxy, reason))
        return {"cooled": True, "sid": SID, "reason": reason, "until": 0}

    monkeypatch.setattr(orders, "cool_proxy", fake_cool)
    checkout.side_effect = [DomainError(code, message, detail), AssertionError("replay")]

    with pytest.raises(Exception):
        asyncio.run(orders._create_order(
            OrderCreateRequest(
                plan="plus",
                billing_country="US",
                session=json.dumps({"accessToken": "offline-fixture"}),
            ),
            None,
        ))

    # 只发一次建单请求：风控拒绝绝不重放。
    assert checkout.await_count == 1
    assert cooled == [(EXIT_PROXY, f"checkout:{failure_code}")]

    [(order, view, _)] = db.load_orders()
    assert view.failure_code == failure_code
    # 记录的是出口 SID 本体（输出层再经 mask_sid 打码），便于定位脏 SID。
    assert view.proxy_last_sid == SID
    assert SID not in (view.last_error or "")


def test_unrelated_failure_does_not_cool_exit(isolated_order, monkeypatch) -> None:
    _db, checkout = isolated_order
    cooled: list[str] = []
    monkeypatch.setattr(orders, "cool_proxy", lambda proxy, **kw: cooled.append(proxy))
    checkout.side_effect = DomainError(
        ErrorCode.CHECKOUT_UPSTREAM_ERROR, "上游客服端错误", {"request_id": "req_1"}
    )
    with pytest.raises(Exception):
        asyncio.run(orders._create_order(
            OrderCreateRequest(
                plan="plus",
                billing_country="US",
                session=json.dumps({"accessToken": "offline-fixture"}),
            ),
            None,
        ))
    assert cooled == []


# ── 4. account_detail 复用原设备身份 ─────────────────────────


def test_account_detail_reuses_session_device_identity() -> None:
    from gss_api import account_detail

    session = json.dumps({
        "accessToken": "tok",
        "sessionToken": "st",
        "statsigContext": {"deviceId": "did-keep", "userAgent": SESSION_UA, "country": "PH"},
    })
    client = account_detail._detail_read_client(session, None)
    try:
        assert client.device_id == "did-keep"
        assert client.user_agent == SESSION_UA
        assert client.extra_cookies["oai-did"] == "did-keep"
        assert client.language == "en-PH"
        assert client.timezone == "Asia/Manila"
        # 身份头里的 oai-did / UA 与 Session 完全一致。
        headers = client._identity_headers()
        assert headers["oai-did"] == "did-keep"
        assert headers["User-Agent"] == SESSION_UA
    finally:
        client.close()


def test_account_detail_falls_back_to_windows_profile(monkeypatch) -> None:
    from gss_api import account_detail
    from gss_core.chatgpt import default_os_key

    client = account_detail._detail_read_client(
        json.dumps({"accessToken": "tok", "sessionToken": "st"}), None
    )
    try:
        assert client.device_id  # 缺省时才由出口重新生成
        assert client.profile["key"] == default_os_key()
    finally:
        client.close()

    monkeypatch.setenv("GSS_CLIENT_OS_PROFILE", "windows")
    win_client = account_detail._detail_read_client(
        json.dumps({"accessToken": "tok", "sessionToken": "st"}), None
    )
    try:
        assert win_client.profile["key"] == "windows"
    finally:
        win_client.close()


# ── 5. 重颁 → 建单的快照延续（同出口 Cookie / 设备） ─────────


def test_session_snapshot_carries_exit_edge_cookies_to_checkout() -> None:
    """重颁拿到的 __cf_bm / _cfuvid / oai-sc 必须随快照进入建单客户端。

    `_session_json` 把 client.extra_cookies 写进 `cookies`，建单适配器
    （UpstreamCheckoutAdapter._create）用 from_raw_session 非 strip 模式复用，
    因此这些边缘 Cookie 会延续到建单请求，不会重新冷建连。
    """
    from gss_core.chatgpt import ChatGPTClient

    # 形状与 orders.py `_session_json` 的输出一致（statsigContext 携带真实身份）。
    snapshot = json.dumps({
        "accessToken": "tok",
        "sessionToken": "st",
        "deviceId": "did-edge",
        "userAgent": SESSION_UA,
        "statsigContext": {"deviceId": "did-edge", "userAgent": SESSION_UA, "country": "PH"},
        "cookies": {
            "__cf_bm": "edge-bm",
            "_cfuvid": "edge-uvid",
            "oai-sc": "edge-sc",
            "__Secure-next-auth.session-token": "st",
        },
    })
    client = ChatGPTClient.from_raw_session(snapshot)
    try:
        cookie = client._identity_headers()["Cookie"]
        assert "__cf_bm=edge-bm" in cookie
        assert "_cfuvid=edge-uvid" in cookie
        assert "oai-sc=edge-sc" in cookie
        assert client.device_id == "did-edge"
        assert client.user_agent == SESSION_UA
    finally:
        client.close()


# ── 6. 回归：PlusHarProtocol 建单出口国对齐 + 同区 warm_account 契约 ────


def test_plus_har_create_checkout_uses_exit_country_and_clears_sentinel(verified_us_payer) -> None:
    from gss_core.chatgpt import ChatGPTClient
    from gss_core.plus_har_protocol import PlusHarProtocol

    sent: list[dict] = []

    class FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "checkout_session_id": "oaics_test_1",
                "publishable_key": "pk_live_test",
                "processor_entity": "openai_llc",
                "billing_details": {"country": "PH", "currency": "PHP"},
            }

    client = ChatGPTClient(access_token="tok", device_id="did-1")
    client.sentinel_token = '{"p":"1","c":"2"}'
    client.sentinel_so = '{"so":"3","c":"2"}'
    engine = PlusHarProtocol(
        session=json.dumps({"accessToken": "tok"}),
        billing=dict(verified_us_payer),
        country="US",
        exit_country="PH",
        currency="PHP",
        client=client,
    )
    engine._app = lambda method, route, **kw: (sent.append({"method": method, "route": route, **kw}) or FakeResp())
    try:
        engine._create_checkout()
        assert sent[0]["json_body"]["billing_details"] == {"country": "PH", "currency": "PHP"}
        assert client.sentinel_token == ""
        assert client.sentinel_so == ""
    finally:
        client.close()


def test_same_country_warm_account_failure_preserves_session_reissued_contract(
    isolated_order, monkeypatch
) -> None:
    from fastapi import HTTPException
    from gss_core import runtime_flags
    from gss_core.chatgpt import ChatGPTClient

    db, _checkout = isolated_order
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    monkeypatch.setattr(orders, "_guard_refresh_egress", AsyncMock())
    monkeypatch.setattr(
        runtime_flags,
        "get_bool",
        lambda name: True if name in {"reissue_session_on_exit", "warm_account"} else False,
    )

    class FakeReauthClient:
        access_token = "fresh-at"
        session_token = "fresh-st"
        refresh_token = ""
        device_id = "did-same"
        oai_session_id = "sid-same"
        user_agent = SESSION_UA
        extra_cookies = {"__Secure-next-auth.session-token": "fresh-st", "oai-did": "did-same"}

        def warm_account(self):
            raise DomainError(ErrorCode.INVALID_SESSION, "Session 已被上游撤销")

        def close(self):
            pass

    monkeypatch.setattr(ChatGPTClient, "reauthorize_for_country", classmethod(lambda cls, *a, **kw: FakeReauthClient()))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(orders._create_order(
            OrderCreateRequest(
                plan="plus",
                billing_country="US",
                exit_country="PH",
                session=json.dumps({
                    "accessToken": "old-at",
                    "sessionToken": "st-val",
                    "statsigContext": {"country": "PH", "deviceId": "did-orig"},
                }),
            ),
            None,
        ))

    # 同区主动刷新（PH -> PH）绝不把 session_reissued_for_country 标成 True。
    detail = (exc_info.value.detail or {}).get("detail") or {}
    assert not detail.get("session_reissued_for_country")
    [(_order, view, _)] = db.load_orders()
    assert view.session_reissued_for_country is False
    assert view.last_error == "Session 已被上游撤销"


def test_reissue_rotates_proxy_only_on_pre_credential_warmup_tunnel_error(
    isolated_order, monkeypatch
) -> None:
    from fastapi import HTTPException
    from gss_core import runtime_flags
    from gss_core.chatgpt import ChatGPTClient

    _db, checkout = isolated_order
    monkeypatch.setenv("GSS_USE_FAKE", "0")
    monkeypatch.setattr(orders, "_guard_refresh_egress", AsyncMock())
    monkeypatch.setattr(
        runtime_flags,
        "get_bool",
        lambda name: True if name in {"reissue_session_on_exit", "checkout_rotate"} else False,
    )
    rotated_proxy = "http://user-region-PH-sid-99998888:pw@gw.example.com:3010"
    rotate_mock = Mock(return_value=rotated_proxy)
    monkeypatch.setattr(orders, "rotate_proxy_sid", rotate_mock)

    seen_proxies: list[str] = []

    class FakeOkClient:
        access_token = "fresh-at"
        session_token = "fresh-st"
        refresh_token = ""
        device_id = "did-ok"
        oai_session_id = "sid-ok"
        user_agent = SESSION_UA
        extra_cookies = {"__Secure-next-auth.session-token": "fresh-st", "oai-did": "did-ok"}

        def close(self):
            pass

    def fake_reauth(cls, raw, *, proxy=None, country="", **kw):
        seen_proxies.append(proxy)
        if len(seen_proxies) == 1:
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                "重签发前出口代理连通失败",
                detail={"code": "proxy_tunnel_failed", "stage": "reissue", "credentials_sent": False},
            )
        return FakeOkClient()

    monkeypatch.setattr(ChatGPTClient, "reauthorize_for_country", classmethod(fake_reauth))
    checkout.side_effect = DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "stop-at-checkout")

    with pytest.raises(HTTPException):
        asyncio.run(orders._create_order(
            OrderCreateRequest(
                plan="plus",
                billing_country="US",
                exit_country="PH",
                session=json.dumps({
                    "accessToken": "old-at",
                    "sessionToken": "st-val",
                    "statsigContext": {"country": "US"},
                }),
            ),
            None,
        ))
    # 凭证前匿名握手失败 → 自动轮换 1 次新 SID 后重颁成功。
    assert seen_proxies == [EXIT_PROXY, rotated_proxy]
    assert rotate_mock.call_count == 1

    # 一旦已发送凭证（credentials_sent=True）→ 绝不自动换 SID 重放。
    seen_proxies.clear()
    rotate_mock.reset_mock()

    def fake_reauth_after_cred(cls, raw, *, proxy=None, country="", **kw):
        seen_proxies.append(proxy)
        raise DomainError(
            ErrorCode.CHECKOUT_UPSTREAM_ERROR,
            "重签发阶段出口代理连接中断",
            detail={"code": "proxy_tunnel_failed", "stage": "reissue", "credentials_sent": True},
        )

    monkeypatch.setattr(ChatGPTClient, "reauthorize_for_country", classmethod(fake_reauth_after_cred))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(orders._create_order(
            OrderCreateRequest(
                plan="plus",
                billing_country="US",
                exit_country="PH",
                session=json.dumps({
                    "accessToken": "old-at",
                    "sessionToken": "st-val",
                    "statsigContext": {"country": "US"},
                }),
            ),
            None,
        ))
    assert seen_proxies == [EXIT_PROXY]
    assert rotate_mock.call_count == 0
    assert exc_info.value.detail["code"] == "checkout_upstream_error"
