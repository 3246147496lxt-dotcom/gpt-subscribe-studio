"""Both status and duplicate POST return the original receipt when custody expires."""
from unittest.mock import Mock

import pytest

from gss_api.routers import protocol_cards
from gss_core.vault import vault
from test_checkout_protocol_api import flow, post


@pytest.mark.parametrize("state", ["processing", "requires_action", "succeeded"])
def test_expired_attempt_returns_receipt_without_any_upstream_call(flow, monkeypatch, state):
    monkeypatch.setenv("GSS_PLUS_ENGINE", "legacy")
    flow.state.update(after_confirm=state, active=False)
    first = post(flow)
    assert first["payment_status"] in {"processing", "succeeded"}
    vault.drop(flow.oid)
    worker = Mock(side_effect=AssertionError("no new authentication or payment"))
    monkeypatch.setattr(protocol_cards, "_run", worker)
    for response in (flow.api.get(flow.root), flow.api.post(flow.root, json=flow.body)):
        assert response.status_code == 200
        assert response.json()["receipt"] == first["receipt"]
        assert response.json()["order"]["status"] == first["order"]["status"]
    worker.assert_not_called()

def test_legacy_late_bound_receipt_preserved_when_vault_expires_in_flight(flow, monkeypatch):
    monkeypatch.setenv("GSS_PLUS_ENGINE", "legacy")
    original = protocol_cards._spec
    checked_after_confirmation = []

    def spec(*args, **kwargs):
        result = original(*args, **kwargs)
        live = result["credentials_current"]

        def expires_after_confirmation():
            if any(row["name"] == "pi_confirm" for row in flow.state["calls"]):
                checked_after_confirmation.append(len(flow.state["calls"]))
                vault.drop(flow.oid)
            return live()
        result["credentials_current"] = expires_after_confirmation
        return result

    monkeypatch.setattr(protocol_cards, "_spec", spec)
    response = post(flow)
    assert checked_after_confirmation and checked_after_confirmation[0] == len(flow.state["calls"])
    assert response["order"]["payment_status"] == "succeeded"
    assert response["order"]["status_summary"] == "已扣款，待核验"
    assert response["order"]["status"] == "verifying"
    assert flow.api.get(flow.root).json()["receipt"] == response["receipt"]

@pytest.mark.parametrize("reported", [
    {"state": "paid", "stage": "payment", "intent_status": "succeeded", "_plus_verified": True},
    {"state": "pending", "stage": "payment", "intent_status": "succeeded"},
    {"state": "no_payment_required", "stage": "payment"},
])
def test_bare_protocol_state_is_not_an_order_bound_receipt(flow, monkeypatch, reported):
    monkeypatch.setattr(protocol_cards, "_run", lambda *args, **kwargs: dict(reported))
    result = post(flow)
    assert result["order"]["status"] == "unknown"
    assert result["payment_status"] == "processing"
    assert result["delivery_status"] != "delivered"
    assert result["order"]["paid_amount"] is None


def test_free_checkout_does_not_claim_a_charge(flow, monkeypatch):
    from gss_api.store import store
    monkeypatch.setenv("GSS_PLUS_ENGINE", "legacy")
    flow.state.update(minor=0, page_status="complete", payment_status="no_payment_required")
    store.view(flow.oid).amount = "0"
    flow.body["expected_amount"] = "0"
    result = post(flow)
    assert result["state"] == "no_payment_required"
    assert result["payment_status"] == "unpaid" and result["order"]["paid_amount"] is None
    assert "无需扣款" in result["receipt"]["summary"]
    assert post(flow)["payment_status"] == "unpaid"
    assert not any(c["name"] in {"pi_confirm", "app_confirm"} for c in flow.state["calls"])
