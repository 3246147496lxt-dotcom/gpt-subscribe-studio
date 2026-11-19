from gss_contracts.errors import UpstreamFailureCode
from gss_core.upstream_errors import classify_upstream_failure


def test_unusual_activity():
    result = classify_upstream_failure(status=400, body='{"detail":"Our systems have detected unusual activity"}')
    assert result.code is UpstreamFailureCode.UNUSUAL_ACTIVITY
    assert result.stage == "checkout"


def test_sentinel_challenge():
    result = classify_upstream_failure(status=403, body='{"detail":"sentinel challenge required"}')
    assert result.code is UpstreamFailureCode.SENTINEL_CHALLENGE_FAILED


def test_timeout_and_proxy():
    class ReadTimeout(Exception):
        pass
    assert classify_upstream_failure(exc=ReadTimeout("timed out")).code is UpstreamFailureCode.UPSTREAM_TIMEOUT
    class ProxyError(Exception):
        pass
    assert classify_upstream_failure(exc=ProxyError("CONNECT 404")).code is UpstreamFailureCode.PROXY_TUNNEL_FAILED


def test_billing_country_and_unknown():
    assert classify_upstream_failure(status=409, body="billing_country mismatch").code is UpstreamFailureCode.BILLING_COUNTRY_MISMATCH
    assert classify_upstream_failure(status=500, body="oops").code is UpstreamFailureCode.UNKNOWN_UPSTREAM
