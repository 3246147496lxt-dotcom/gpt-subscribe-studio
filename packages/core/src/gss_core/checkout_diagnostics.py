"""Public, non-secret metadata from one failed checkout response (never its body)."""
import re

# A response code is server-controlled and sometimes contains free text. Do not
# export unknown values, tokens or arbitrary "code"-shaped strings.
_CODES = frozenset("""
    unusual_activity invalid_session token_revoked invalid_token expired_token
    token_expired session_expired unauthorized authentication_error access_denied
    already_subscribed already_paid subscription_limit billing_country_mismatch
    invalid_request invalid_request_error rate_limit_exceeded rate_limit_error
    too_many_requests insufficient_quota server_error internal_error
    internal_server_error service_unavailable forbidden permission_denied
    account_deactivated account_disabled unsupported_country
    payment_required payment_method_required card_declined
""".split())
_RAY = re.compile(r"[0-9a-f]{16}(?:-[a-z]{3})?", re.ASCII | re.I)
_REQUEST = re.compile(
    r"(?:req[_-])?(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})",
    re.ASCII | re.I,
)


def safe_checkout_diagnostics(raw: object) -> dict:
    """Return only independently validated fields; absent stays absent.

    Never fabricate an upstream_code from a local message/status; never combine
    fields across attempts. Revalidate on read too (rows may predate this rule).
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    status = raw.get("upstream_status")
    if type(status) is str and re.fullmatch(r"[1-5][0-9]{2}", status):
        status = int(status)
    if type(status) is int and 100 <= status <= 599:
        out["upstream_status"] = status
    code = raw.get("upstream_code")
    if type(code) is str and len(code) <= 64 and code in _CODES:
        out["upstream_code"] = code
    for key, pattern in (("cf_ray", _RAY), ("request_id", _REQUEST)):
        val = raw.get(key)
        if (type(val) is str and len(val) <= 48 and pattern.fullmatch(val)
                and not re.search(r"[0-9]{12,}", val.replace("-", ""))):
            out[key] = val
    # Only the response parser can distinguish absent from discarded. Never
    # reconstruct these observations from an older row or combine attempts.
    if "upstream_status" in out:
        for field in ("upstream_code", "request_id"):
            key = field + "_state"
            state = raw.get(key)
            if (type(state) is str and state in ("not_found", "accepted", "filtered")
                    and (state == "accepted") == (field in out)):
                out[key] = state

        source = raw.get("unusual_activity_source")
        wire_code = out.get("upstream_code")
        if (type(source) is str and out["upstream_status"] != 401
                and wire_code not in ("invalid_session", "token_revoked")
                and "upstream_code_state" in out):
            if ((source == "structured_code" and wire_code == "unusual_activity")
                    or (source == "body_match" and wire_code != "unusual_activity")):
                out["unusual_activity_source"] = source
    return out
