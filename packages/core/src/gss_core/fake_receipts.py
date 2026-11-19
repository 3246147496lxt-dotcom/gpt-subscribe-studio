"""Stateless synthetic settlement facts shared only by the offline fake adapters."""

import re
from decimal import Decimal
from uuid import uuid4

from gss_contracts.adapter import PaymentReceiptEvidence


def checkout_id_for(*, hosted: bool, amount: Decimal, currency: str) -> str:
    prefix = "cs_test_fake" if hosted else "oaics_fake"
    return f"{prefix}_{uuid4().hex}_{currency}_{amount:f}"


def settlement_for(checkout_id: str) -> PaymentReceiptEvidence | None:
    match = re.fullmatch(r"(?:cs_test_fake|oaics_fake)_([a-f0-9]{32})_([A-Z]{3})_([0-9]+(?:[.][0-9]+)?)", checkout_id)
    if not match:
        return None
    unique, currency, amount = match.groups()
    return PaymentReceiptEvidence(checkout_id, "pi_fake_" + unique, Decimal(amount), currency,
                                  "ws_fake_" + unique)
