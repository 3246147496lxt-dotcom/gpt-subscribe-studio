"""Validate parser observations an additional time at storage/API boundaries."""
import pytest

from gss_core.checkout_diagnostics import safe_checkout_diagnostics as clean


@pytest.mark.parametrize("value", ["private-arbitrary-text", 1, None, {}, ["accepted"]])
def test_only_fixed_states_can_pass(value):
    assert clean({"upstream_status": 400, "upstream_code_state": value,
                  "request_id_state": value, "unusual_activity_source": value}) == {"upstream_status": 400}


def test_bad_values_do_not_become_accepted_or_seed_a_diagnostic():
    assert clean({"request_id_state": "filtered"}) == {}
    assert clean({"upstream_status": 400, "request_id": "private-id", "request_id_state": "accepted",
                  "upstream_code": "private-code", "upstream_code_state": "accepted",
                  "unusual_activity_source": "structured_code"}) == {"upstream_status": 400}


@pytest.mark.parametrize(("status", "code", "source", "keep"), [
    (400, "unusual_activity", "structured_code", True), (400, "unusual_activity", "body_match", False),
    (400, "forbidden", "body_match", True), (400, "forbidden", "structured_code", False),
    (401, "unusual_activity", "structured_code", False), (400, "token_revoked", "body_match", False),
])
def test_source_is_consistent_and_filter_is_idempotent(status, code, source, keep):
    raw = {"upstream_status": status, "upstream_code": code, "upstream_code_state": "accepted",
           "request_id_state": "not_found", "unusual_activity_source": source}
    out = clean(raw)
    assert out == (raw if keep else {k: v for k, v in raw.items() if k != "unusual_activity_source"})
    assert clean(out) == out
