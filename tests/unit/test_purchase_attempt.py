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


def test_purchase_attempt_is_never_leaked_to_upstream_headers():
    from gss_core.chatgpt import ChatGPTClient
    client = ChatGPTClient(access_token="", purchase_attempt_id="pa_test")
    headers = client._route_headers("/backend-api/payments/checkout")
    assert client.purchase_attempt_id == "pa_test"
    assert "X-GSS-Purchase-Attempt" not in headers
    assert "checkout_attempt_id" not in headers
