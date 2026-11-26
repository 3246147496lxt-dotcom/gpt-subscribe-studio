"""Read-only projections of non-Team payment state. Internal provider payloads are not API schemas."""
from __future__ import annotations

import re

from gss_core.redaction import redact

# A checkout page, customer secret or PaymentMethod can authorize further writes.
# Drop those even when an older adapter echoed them in a human-readable error.
_CAPABILITY = re.compile(r"https?://|_secret_|(?:^|[^a-zA-Z0-9])(?:pm|ctoken|cuss|oaics|cs_live|cs_test)_[a-z0-9]", re.I)
_RECEIPT = ("payment_intent_id", "amount", "currency", "tax_delta", "plan_type", "payment_status", "delivery_status", "summary")
_PROTOCOL = ("state", "stage", "message", "code", "decline_code", "intent_status", "charge_id",
             "payment_status", "delivery_status", "payment_intent_id", "amount", "currency",
             "tax_delta", "plan_type", "status_summary", "upstream_message", "upstream_status",
             "upstream_code", "remediation")
# Never publish an arbitrary "next_action.url" as an issuer challenge. Only an
# explicit 3DS redirect to the processor's dedicated authentication path, with
# no query/fragment/credentials or link back to hosted checkout, is supported.
_AUTH_REDIRECT = re.compile(r"https://hooks\.stripe\.com/redirect/authenticate/(?:src|tdsrc)_[A-Za-z0-9]+")
_ACTION_TYPES = {"3ds_redirect", "redirect_to_url", "use_stripe_sdk", "requires_action"}


def read_text(value):
    if not isinstance(value, str):
        return value
    if _CAPABILITY.search(value):
        return "相关链接或支付标识已隐藏，请核对原订单。"
    return redact(value)


def _fields(data, keys):
    if not isinstance(data, dict):
        return {}
    return {k: read_text(data[k]) for k in keys if k in data and (data[k] is None or isinstance(data[k], (str, int)))}


def receipt_view(data):
    return _fields(data, _RECEIPT) or None


def issuer_action(action, *, allow_redirect=False, checkout_url=None):
    if not isinstance(action, dict) or not isinstance(action.get("type"), str) or action["type"] not in _ACTION_TYPES:
        return None
    public = {"type": action["type"]}
    url = action.get("url")
    if (allow_redirect and action["type"] in {"3ds_redirect", "redirect_to_url"}
            and isinstance(url, str) and _AUTH_REDIRECT.fullmatch(url)
            and url != checkout_url and url != action.get("checkout_url")):
        public["url"] = url
    return public


def protocol_view(data):
    public = _fields(data, _PROTOCOL)
    if "receipt" in data:
        public["receipt"] = receipt_view(data["receipt"])
    action = issuer_action(data.get("next_action"))  # protocol flows do not hand off authentication
    if action is not None:
        public["next_action"] = action
    return public


def saved_view(data):
    public = _fields(data, ("state", "setup_status", "message"))
    card = data.get("card")
    if isinstance(card, dict):
        public["card"] = {k: card[k] for k, pattern in (("brand", r"[A-Za-z][A-Za-z -]{0,31}"), ("last4", r"[0-9]{4}"))
                          if isinstance(card.get(k), str) and re.fullmatch(pattern, card[k])}
    return public


_BRAND_ALIASES = {
    "visa": "VISA",
    "mastercard": "Mastercard",
    "master card": "Mastercard",
    "mc": "Mastercard",
    "amex": "Amex",
    "american express": "Amex",
    "discover": "Discover",
    "jcb": "JCB",
    "unionpay": "UnionPay",
    "diners": "Diners",
    "diners club": "Diners",
    "cartes_bancaires": "Cartes Bancaires",
    "eftpos_au": "Eftpos",
}


def normalize_card_brand(brand: object) -> str | None:
    if not isinstance(brand, str):
        return None
    cleaned = brand.strip()
    if not cleaned:
        return None
    mapped = _BRAND_ALIASES.get(cleaned.lower())
    if mapped:
        return mapped
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,23}", cleaned):
        return cleaned
    return None


def detect_card_brand(card_number: object) -> str | None:
    """Derive only the non-secret card network brand from a card number prefix."""
    if not isinstance(card_number, str):
        return None
    digits = re.sub(r"\D", "", card_number)
    if len(digits) < 4:
        return None
    if digits.startswith("4"):
        return "VISA"
    if re.match(r"^(?:5[1-5]|222[1-9]|22[3-9]\d|2[3-6]\d{2}|27[01]\d|2720)", digits):
        return "Mastercard"
    if digits.startswith(("34", "37")):
        return "Amex"
    if digits.startswith("62"):
        return "UnionPay"
    if re.match(r"^(?:6011|65|64[4-9])", digits):
        return "Discover"
    if re.match(r"^35(?:2[89]|[3-8]\d)", digits):
        return "JCB"
    if re.match(r"^(?:36|38|30[0-5])", digits):
        return "Diners"
    return None
