"""One sticky exit serves one order, and a risk rejection quarantines it."""
from __future__ import annotations

import pytest

from gss_core import proxies

PROXY = "http://user-region-PH-sid-1234:pw@global.example:1111"
OTHER = "http://user-region-PH-sid-9999:pw@global.example:1111"


@pytest.fixture(autouse=True)
def _lease_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GSS_PROXY_LEASE_FILE", str(tmp_path / "leases.json"))


def test_sid_is_read_from_the_gateway_username():
    assert proxies.proxy_sid(PROXY) == "1234"
    assert proxies.proxy_sid("http://host:8080") == ""


def test_one_exit_is_leased_to_one_order():
    first = proxies.claim_sid_lease(PROXY, order_id="order-a")
    assert first["leased"] is True

    with pytest.raises(proxies.ProxyLeaseError) as caught:
        proxies.claim_sid_lease(PROXY, order_id="order-b")
    assert "另一笔" in str(caught.value)


def test_same_order_renews_its_own_lease():
    first = proxies.claim_sid_lease(PROXY, order_id="order-a")
    again = proxies.claim_sid_lease(PROXY, order_id="order-a")
    assert again["lease_id"] == first["lease_id"]
    assert again["reused"] is True


def test_release_hands_the_exit_to_the_next_order():
    proxies.claim_sid_lease(PROXY, order_id="order-a")
    assert proxies.release_sid_lease("order-a") is True
    assert proxies.claim_sid_lease(PROXY, order_id="order-b")["leased"] is True


def test_risk_rejection_quarantines_the_exit():
    proxies.claim_sid_lease(PROXY, order_id="order-a")
    result = proxies.cool_proxy(PROXY, reason="400:unusual_activity", seconds=600)
    assert result["cooled"] is True

    assert proxies.proxy_cooldown(PROXY) is not None
    with pytest.raises(proxies.ProxyLeaseError) as caught:
        proxies.claim_sid_lease(PROXY, order_id="order-b")
    assert "冷却" in str(caught.value)


def test_other_exits_stay_available_after_one_is_quarantined():
    proxies.cool_proxy(PROXY, reason="401:token_revoked", seconds=600)
    assert proxies.claim_sid_lease(OTHER, order_id="order-b")["leased"] is True


def test_expired_lease_does_not_block_a_new_order():
    proxies.claim_sid_lease(PROXY, order_id="order-a", ttl_s=0)
    assert proxies.claim_sid_lease(PROXY, order_id="order-b")["leased"] is True
