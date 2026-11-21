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


def test_unusual_activity_without_http_status():
    result = classify_upstream_failure(status=None, body="ChatGPT 判定异常流量")
    assert result.code is UpstreamFailureCode.UNUSUAL_ACTIVITY


def test_already_subscribed_is_terminal_not_retryable():
    for body in ("The account is already paid", "该账号已是付费订阅"):
        assert classify_upstream_failure(status=400, body=body).code is UpstreamFailureCode.ALREADY_SUBSCRIBED


def test_insufficient_funds_and_card_declined():
    assert classify_upstream_failure(
        status=402, body='{"code":"insufficient_funds"}'
    ).code is UpstreamFailureCode.INSUFFICIENT_FUNDS
    assert classify_upstream_failure(
        status=402, body='{"code":"card_declined","decline_code":"card_velocity_exceeded"}'
    ).code is UpstreamFailureCode.CARD_DECLINED
    assert classify_upstream_failure(
        status=402, body="Your card's balance, credit limit, or transaction amount limit was exceeded"
    ).code is UpstreamFailureCode.CARD_DECLINED
