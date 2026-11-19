"""单元测试：脱敏层（宪法第 I 条，tasks.md T008）。"""

from __future__ import annotations

import ast
import json

from gss_core import MASK, redact, redact_mapping


def test_bearer_header_redacted() -> None:
    out = redact("Authorization: Bearer abc123def456ghi")
    assert "abc123def456ghi" not in out
    assert MASK in out


def test_access_token_value_redacted() -> None:
    out = redact('{"access_token": "super-secret-token-value"}')
    assert "super-secret-token-value" not in out


def test_session_token_redacted() -> None:
    out = redact('session_token=abcdef1234567890abcdef')
    assert "abcdef1234567890abcdef" not in out


def test_proxy_password_redacted_but_user_kept() -> None:
    out = redact("http://region-JP-sid-abc:hunter2@gateway.example:8080")
    assert "hunter2" not in out
    assert "region-JP-sid-abc" in out
    assert "gateway.example" in out


def test_cvv_and_card_number_redacted() -> None:
    out = redact("cvv=123 card_number: 4242 4242 4242 4242")  # release-check: ignore
    assert "123" not in out
    assert "4242 4242 4242 4242" not in out  # release-check: ignore
    assert out.count(MASK) >= 2


def test_jwt_redacted() -> None:
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"  # release-check: ignore
    out = redact(f"token={jwt}")
    assert jwt not in out


def test_redact_mapping_nested() -> None:
    payload = {
        "order": {"id": "abc", "note": "Bearer zzzzzzzzzz"},
        "cookies": ["session_token=0123456789abcdef"],
    }
    out = redact_mapping(payload)
    assert out["order"]["id"] == "abc"  # 非敏感字段保持原样
    assert "zzzzzzzzzz" not in out["order"]["note"]
    assert out["cookies"] == MASK


def test_non_sensitive_text_unchanged() -> None:
    text = "订单已创建，状态 created，账单国家 US"
    assert redact(text) == text


def test_stripe_client_secret_and_pi_secret_redacted() -> None:
    out = redact(
        '{"client_secret":"pi_3AbcDef_secret_9Zyx",'
        '"payment_intent":"pi_3AbcDef_secret_9Zyx"}'
    )
    assert "pi_3AbcDef_secret_9Zyx" not in out
    assert MASK in out


def test_pii_email_and_address_redacted() -> None:
    out = redact(
        '{"email":"buyer@example.test","line1":"98 EDSA","postal_code":"1100"}'
    )
    assert "buyer@example.test" not in out
    assert "98 EDSA" not in out
    assert "1100" not in out


def test_order_error_masks_rotated_session_in_json_and_python_repr() -> None:
    for encode, decode in ((json.dumps, json.loads), (repr, ast.literal_eval)):
        payload = {"accessToken": "a+/=~b", "__Secure-next-auth.session-token.0": "r%2Fd",
                   "sessionToken": "c'd\"e", "status": 503}
        assert decode(redact(encode(payload))) == {
            "accessToken": MASK, "__Secure-next-auth.session-token.0": MASK,
            "sessionToken": MASK, "status": 503,
        }


def test_token_keys_after_punctuation_and_multiline_json_are_masked() -> None:
    assert redact("client.accessToken=first x-access-token=second") == f"client.accessToken={MASK} x-access-token={MASK}"
    assert json.loads(redact('{"accessToken":\n  "third", "status": 403}')) == {"accessToken": MASK, "status": 403}


def test_byte_token_in_python_repr_is_not_partially_masked() -> None:
    assert ast.literal_eval(redact(repr({"accessToken": b"bytes token", "status": 503}))) == {
        "accessToken": MASK.encode(), "status": 503,
    }


def test_cookie_headers_mask_every_value_without_swallowing_next_header() -> None:
    diagnostic = (
        "HTTP 429\r\nCookie: first=opaque; __Secure-next-auth.session-token.0=part-a; other=last\r\n"
        "Set-Cookie: __Secure-next-auth.session-token.1=part-b; Path=/; HttpOnly\r\n"
        "Retry-After: 60"
    )
    assert redact(diagnostic) == f"HTTP 429\r\nCookie: {MASK}\r\nSet-Cookie: {MASK}\r\nRetry-After: 60"


def test_client_error_masks_standalone_cookie_chunks_and_truncated_access_token() -> None:
    assert redact("reason: __Secure-next-auth.session-token.1=a+/=~b; status=503") == (
        f"reason: __Secure-next-auth.session-token.1={MASK}; status=503"
    )
    assert redact("""HTTP 500 {"accessToken": "incomplete+/%""") == f'HTTP 500 {{"accessToken": "{MASK}"'


def test_credential_fields_in_nested_mappings_are_not_inspected_or_echoed() -> None:
    payload = {
        "history": [{"ACCESS_TOKEN": "x", "Session": {"info": "private"}}],
        "headers": {"Set-Cookie": ["part one", "part two"], "unrelated": "public"},
        "__Secure-next-auth.session-token.23": "short",
        "checkout_session_id": "preserve",
        "session_reissued_for_country": True,
    }
    assert redact_mapping(payload) == {
        "history": [{"ACCESS_TOKEN": MASK, "Session": MASK}],
        "headers": {"Set-Cookie": MASK, "unrelated": "public"},
        "__Secure-next-auth.session-token.23": MASK,
        "checkout_session_id": "preserve",
        "session_reissued_for_country": True,
    }
