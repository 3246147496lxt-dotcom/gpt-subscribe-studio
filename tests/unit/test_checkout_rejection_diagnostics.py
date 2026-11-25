"""The checkout POST can be diagnosed without persisting an upstream response body."""
from types import SimpleNamespace

import pytest

from gss_contracts.errors import DomainError, ErrorCode
from gss_core import chatgpt


_SECRET = "PRIVATE-RESPONSE-MARKER"
_RAY = "0123456789abcdef-IAD"
_RID = "req_0123456789abcdef0123456789abcdef"


def _reject(monkeypatch, *, status=400, payload=None, message="", headers=None, plan="team"):
    payload = {} if payload is None else payload
    response = SimpleNamespace(status_code=status, headers=dict(headers or {}), text=message,
                               json=lambda: payload, content=message.encode())
    http = SimpleNamespace(headers={}, post=lambda *args, **kwargs: response)
    client = chatgpt.ChatGPTClient(access_token="fixture", account_id="fixture", _http=http)
    monkeypatch.setattr(client, "prepare_risk", lambda **kwargs: None)
    monkeypatch.setattr(client, "_identity_headers", lambda: {})
    monkeypatch.setattr(client, "_route_headers", lambda *args, **kwargs: {})
    monkeypatch.setattr(chatgpt, "checkout_address", lambda *args, **kwargs: {})
    with pytest.raises(DomainError) as caught:
        client.create_checkout(plan=plan, country="US")
    return caught.value


@pytest.mark.parametrize(("status", "wire", "local"), [
    (401, "token_revoked", ErrorCode.INVALID_SESSION),
    (400, "invalid_session", ErrorCode.INVALID_SESSION),
    (403, "unusual_activity", ErrorCode.CHECKOUT_UPSTREAM_ERROR),
])
def test_wire_codes_and_correlation_headers_survive(monkeypatch, caplog, status, wire, local):
    e = _reject(monkeypatch, status=status,
                headers={"CF-RaY": _RAY, "X-REQUEST-ID": _RID, "Content-Type": "application/json"},
                payload={"error": {"code": wire, "message": _SECRET}}, message=_SECRET)
    assert e.code is local
    expected = dict(upstream_status=status, upstream_code=wire, cf_ray=_RAY, request_id=_RID,
                    upstream_code_state="accepted", request_id_state="accepted")
    if wire == "unusual_activity":
        expected["unusual_activity_source"] = "structured_code"
    assert e.detail == expected
    assert _SECRET not in str(e.to_payload()) + caplog.text


@pytest.mark.parametrize(("status", "body", "local"), [
    (401, "Invalidated OAuth token", ErrorCode.INVALID_SESSION),
    (400, "Our systems detected unusual activity.", ErrorCode.CHECKOUT_UPSTREAM_ERROR),
    (422, "billing country must match", ErrorCode.BILLING_COUNTRY_MISMATCH),
    (400, "already paid", ErrorCode.ALREADY_SUBSCRIBED),
    (400, "only for internal checkout sessions", ErrorCode.CHECKOUT_UPSTREAM_ERROR),
    (403, "", ErrorCode.CHECKOUT_UPSTREAM_ERROR),
    (429, "", ErrorCode.CHECKOUT_UPSTREAM_ERROR),
    (503, "", ErrorCode.CHECKOUT_UPSTREAM_ERROR),
])
def test_body_and_http_classification_do_not_invent_wire_code(monkeypatch, caplog, status, body, local):
    e = _reject(monkeypatch, status=status, headers={"CF-Ray": _RAY, "x-request-id": _RID},
                payload={"detail": body, "debug": _SECRET}, message=f"{body} {_SECRET}")
    expected = dict(upstream_status=status, cf_ray=_RAY, request_id=_RID,
                    upstream_code_state="not_found", request_id_state="accepted")
    if "unusual activity" in body:
        expected["unusual_activity_source"] = "body_match"
    assert e.detail == expected
    assert e.code is local
    if "unusual activity" in body:
        assert "异常流量" in str(e)
    assert _SECRET not in str(e.to_payload()) + caplog.text


@pytest.mark.parametrize("plan", ["team", "plus"])
def test_unusable_success_response_no_payload_echo(monkeypatch, caplog, plan):
    e = _reject(monkeypatch, status=200, plan=plan, payload={"private_field": _SECRET},
                headers={"Request-ID": _RID}, message=_SECRET)
    assert e.detail == dict(upstream_status=200, request_id=_RID,
                            upstream_code_state="not_found", request_id_state="accepted")
    assert _SECRET not in str(e.to_payload()) + caplog.text


def test_invalid_wire_fields_are_omitted_not_truncated(monkeypatch, caplog):
    value = _SECRET + "\nAuthorization: Bearer test"
    e = _reject(monkeypatch, status=400, payload={"error": {"code": value}},
                headers={"cf-ray": value, "X-Request-ID": value}, message=_SECRET)
    assert e.detail == dict(upstream_status=400, upstream_code_state="filtered", request_id_state="filtered")
    assert _SECRET not in str(e.to_payload()) + caplog.text

def test_error_type_or_message_is_not_an_explicit_provider_code(monkeypatch, caplog):
    e = _reject(monkeypatch, status=401, headers={"Content-Type": "application/json"},
                payload={"error": {"type": "token_revoked", "message": _SECRET}}, message=_SECRET)
    assert e.detail == dict(upstream_status=401, upstream_code_state="not_found", request_id_state="not_found")
    assert "upstream_error_code=token_revoked" not in caplog.text


def test_billing_conflict_keeps_response_evidence_without_payload(monkeypatch, caplog):
    from gss_core.verified_payer import ObservedBillingConflict

    def conflict(*args, **kwargs):
        raise ObservedBillingConflict()

    monkeypatch.setattr(chatgpt, "check_observed_billing", conflict)
    e = _reject(monkeypatch, status=200, plan="plus", payload={"billing": _SECRET},
                headers={"request-id": _RID}, message=_SECRET)
    assert isinstance(e, ObservedBillingConflict)
    assert e.detail == dict(upstream_status=200, request_id=_RID,
                            upstream_code_state="not_found", request_id_state="accepted")
    assert _SECRET not in str(e.to_payload()) + caplog.text


@pytest.mark.parametrize("preflight", ["me", "accounts_check", "warm_account", "checkout_warmup", "key_lookup"])
def test_401_before_or_outside_checkout_post_keeps_safe_original_response(monkeypatch, caplog, preflight):
    from unittest.mock import Mock

    response = SimpleNamespace(
        status_code=401,
        headers={"CF-Ray": _RAY, "REQUEST-ID": _RID, "set-cookie": _SECRET},
        text=_SECRET,
        json=lambda: {"detail": {"code": "token_revoked"}, "accessToken": _SECRET},
    )
    first = SimpleNamespace(status_code=404, text=_SECRET)
    http = Mock(headers={})
    http.get.side_effect = [first, response] if preflight == "accounts_check" else [response]
    client = chatgpt.ChatGPTClient("local-fixture", _http=http)
    if preflight != "checkout_warmup":
        monkeypatch.setattr(client, "prepare_risk", lambda **kwargs: None)
    monkeypatch.setattr(client, "mint_sentinel", lambda **_: pytest.fail("continued after 401"))
    with pytest.raises(DomainError) as caught:
        if preflight == "key_lookup":
            client._publishable_key()
        elif preflight == "warm_account":
            client.warm_account()
        else:
            client.create_checkout(plan="team", country="US")
    http.post.assert_not_called()
    assert http.get.call_count == (2 if preflight == "accounts_check" else 1)
    assert caught.value.code is ErrorCode.INVALID_SESSION
    assert caught.value.detail == {
        "upstream_status": 401, "upstream_code": "token_revoked", "cf_ray": _RAY, "request_id": _RID,
        "upstream_code_state": "accepted", "request_id_state": "accepted",
    }
    assert _SECRET not in caplog.text + str(caught.value.to_payload())
