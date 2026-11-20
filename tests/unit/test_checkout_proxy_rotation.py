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


def test_rotate_proxy_keeps_region_and_changes_sid(monkeypatch):
    monkeypatch.setenv("GSS_PROXY_SID", "PINNED01")
    proxy = "http://u-region-CL-sid-OLD123:pw@gw.example:3010"
    rotated = rotate_proxy_sid(proxy, force=True)
    assert "region-CL" in rotated
    assert "sid-PINNED01" not in rotated
    assert proxy_exit_sid(rotated) != "OLD123"
