from gss_core.purchase_attempt import new_purchase_attempt_id, stable_purchase_attempt_id


def test_new_attempt_is_unique_and_prefixed():
    a, b = new_purchase_attempt_id(), new_purchase_attempt_id()
    assert a.startswith("pa_") and b.startswith("pa_")
    assert a != b


def test_existing_attempt_is_stable():
    value = new_purchase_attempt_id()
    assert stable_purchase_attempt_id(value) == value
    assert stable_purchase_attempt_id("") != ""


def test_invalid_existing_attempt_is_replaced():
    assert stable_purchase_attempt_id("bad").startswith("pa_")


def test_purchase_attempt_is_sent_as_internal_header_only():
    from gss_core.chatgpt import ChatGPTClient
    client = ChatGPTClient(access_token="", purchase_attempt_id="pa_test")
    headers = client._route_headers("/backend-api/payments/checkout")
    assert headers["X-GSS-Purchase-Attempt"] == "pa_test"
    assert "checkout_attempt_id" not in headers
