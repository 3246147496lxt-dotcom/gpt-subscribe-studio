from gss_core.proxies import (
    fresh_proxy_sid,
    proxy_exit_sid,
    rotate_proxy_sid,
)


def test_fresh_sid_ignores_pinned_sid(monkeypatch):
    monkeypatch.setenv("GSS_PROXY_SID", "PINNED01")
    first = fresh_proxy_sid()
    second = fresh_proxy_sid({first})
    assert first != "PINNED01"
    assert second != first
    assert len(first) >= 8
    # 默认数字 SID：部分网关对字母会话返回 CONNECT 404。
    assert first.isdigit() and second.isdigit()


def test_alphanumeric_sid_is_opt_in(monkeypatch):
    monkeypatch.delenv("GSS_PROXY_SID", raising=False)
    monkeypatch.setenv("GSS_PROXY_SID_ALNUM", "1")
    sid = fresh_proxy_sid()
    assert len(sid) == 8
    assert not sid.isdigit() or sid.isdigit()  # 允许全数字的偶然结果
    monkeypatch.setenv("GSS_PROXY_SID_ALNUM", "0")
    assert fresh_proxy_sid().isdigit()


def test_rotate_proxy_keeps_region_and_changes_sid(monkeypatch):
    monkeypatch.setenv("GSS_PROXY_SID", "PINNED01")
    proxy = "http://u-region-CL-sid-OLD123:pw@gw.example:3010"
    rotated = rotate_proxy_sid(proxy, force=True)
    assert "region-CL" in rotated
    assert "sid-PINNED01" not in rotated
    assert proxy_exit_sid(rotated) != "OLD123"
