import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "sanitize_har.py"
spec = importlib.util.spec_from_file_location("sanitize_har", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_filters_static_and_redacts_sensitive_values():
    har = {"log": {"entries": [
        {
            "request": {
                "method": "POST",
                "url": "https://chatgpt.com/backend-api/payments/checkout?secret=1",
                "headers": [
                    {"name": "Cookie", "value": "session=secret"},
                    {"name": "Authorization", "value": "Bearer secret"},
                    {"name": "Content-Type", "value": "application/json"},
                    {"name": "x-openai-target-route", "value": "/backend-api/payments/checkout"},
                ],
                "postData": {"mimeType": "application/json", "text": json.dumps({
                    "plan_name": "chatgptprolite",
                    "status": "open",
                    "card": {"number": "4242424242424242", "cvc": "123"},
                    "email": "secret@example.com",
                })},
            },
            "response": {"status": 200, "content": {"mimeType": "application/json", "text": json.dumps({
                "status": "success", "currency": "CLP", "amount": 1000,
                "client_secret": "pi_secret", "detail": "private",
            })}},
            "time": 12.5,
        },
        {
            "request": {"method": "GET", "url": "https://chatgpt.com/assets/app.js"},
            "response": {"status": 200, "content": {"mimeType": "application/javascript", "text": "secret"}},
            "time": 1,
        },
    ]}}
    clean = module.sanitize(har)
    assert clean["meta"]["entries"] == 1
    entry = clean["entries"][0]
    assert entry["path"] == "/backend-api/payments/checkout"
    assert entry["status"] == 200
    assert entry["time_ms"] == 12.5
    assert entry["request_headers"] == {
        "content-type": "application/json",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }
    assert entry["request_json"]["plan_name"] == "chatgptprolite"
    assert entry["request_json"]["card"] == module.REDACTED
    assert entry["request_json"]["email"] == module.REDACTED
    assert entry["response_json"]["status"] == "success"
    assert entry["response_json"]["currency"] == "CLP"
    assert entry["response_json"]["amount"] == 1000
    assert entry["response_json"]["client_secret"] == module.REDACTED
    assert "4242424242424242" not in json.dumps(clean)
    assert "secret@example.com" not in json.dumps(clean)
