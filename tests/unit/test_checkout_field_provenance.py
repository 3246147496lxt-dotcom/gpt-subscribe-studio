"""First-refusal provenance tests; no HTTP client, real account or database needed."""
from types import SimpleNamespace

import pytest

from gss_contracts.errors import DomainError
from gss_core.chatgpt import ChatGPTClient


_PRIVATE = "PRIVATE-FIELD-THAT-MUST-NOT-PERSIST"
_ID = "req_0123456789abcdef0123456789abcdef"


def reject(status, payload, headers, message):
    response = SimpleNamespace(status_code=status, headers=headers, text=message)
    client = object.__new__(ChatGPTClient)
    with pytest.raises(DomainError) as captured:
        client._reject_checkout(response, payload)
    return captured.value


@pytest.mark.parametrize(("payload", "headers", "expected"), [
    # An unrelated key isn't a recognized candidate, even if it looks similar.
    ({"error": {"type": _PRIVATE}}, {"other-request-id": _PRIVATE}, ("not_found", "not_found")),
    # An explicitly null or empty candidate is distinguishable from absence.
    ({"detail": {"code": None}}, {"REQUEST-ID": ""}, ("filtered", "filtered")),
    ({"code": []}, {"Request-ID": None}, ("filtered", "filtered")),
    ({"error": {"code": _PRIVATE}}, {"x-request-id": _PRIVATE}, ("filtered", "filtered")),
    # Preserve precedence: an ineligible type/empty first candidate admits a fallback.
    ({"error": {"code": 3}, "detail": {"code": "forbidden"}}, {"X-Request-ID": "", "Request-ID": _ID},
     ("accepted", "accepted")),
    # A non-empty higher priority candidate blocks a lower priority one, even when rejected.
    ({"error": {"code": _PRIVATE}, "code": "forbidden"},
     {"X-REQUEST-ID": _PRIVATE, "Request-Id": _ID}, ("filtered", "filtered")),
])
def test_field_states_preserve_parser_precedence(payload, headers, expected, caplog):
    err = reject(400, payload, headers, "ordinary response: " + _PRIVATE)
    d = err.detail
    assert (d["upstream_code_state"], d["request_id_state"]) == expected
    assert ("upstream_code" in d) == (expected[0] == "accepted")
    assert ("request_id" in d) == (expected[1] == "accepted")
    assert "unusual_activity_source" not in d
    assert _PRIVATE not in str(err.to_payload()) + caplog.text


@pytest.mark.parametrize(("status", "code", "message", "source"), [
    (400, "unusual_activity", "ordinary", "structured_code"),
    (400, "unusual_activity", "Unusual Activity in text too", "structured_code"),
    (400, _PRIVATE, "Unusual Activity", "body_match"),
    (400, "forbidden", "Unusual Activity", "body_match"),
    (400, None, "Unusual Activity", "body_match"),
    (400, None, "ordinary", None),
    (400, None, "a" * 500 + " unusual activity", None),
    (401, "unusual_activity", "Unusual Activity", None),
    (400, "token_revoked", "Unusual Activity", None),
])
def test_source_is_the_branch_that_actually_won(status, code, message, source, caplog):
    payload = {"error": {"code": code}} if code is not None else {}
    err = reject(status, payload, {}, message + " " + _PRIVATE)
    if source is None:
        assert "unusual_activity_source" not in err.detail
    else:
        assert err.detail["unusual_activity_source"] == source
    assert _PRIVATE not in str(err.to_payload()) + caplog.text
