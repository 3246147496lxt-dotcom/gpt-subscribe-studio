"""Stable comparison of business intent for a QuickPay admission, not authentication."""

from __future__ import annotations

import hashlib
import json

from gss_core.countries import normalize_country

from ..schemas import QuickPayRequest


def quick_pay_intent_digest(payload: QuickPayRequest, account_fingerprint: str) -> str:
    """v1 receipt: compare only explicit business fields; never serialize the request.

    Changing a card or refreshing a session cannot turn a replay into another
    charge. Callers must separately reject replays with no comparable owner.
    """
    plan = payload.plan.strip().lower()
    mode = payload.mode
    fields = {
        "account": account_fingerprint,
        "plan": "pro_lite" if plan == "pro5" else plan,
        # Order validation chooses Team rules BEFORE it normalizes the plan.
        "team_rules": payload.plan == "team",
        "checkout_only": payload.debug_checkout_only,
        "cancel_renew": payload.auto_cancel_renew,
        # No claim (None) is different from an explicit empty claim.
        "billing_country": None if payload.billing_country is None else normalize_country(payload.billing_country),
        "exit_country": None if payload.exit_country is None else normalize_country(payload.exit_country),
        # The mode enums are case-sensitive. Never lowercase them for comparison.
        "mode": {
            "account_source": mode.account_source,
            "card_source": mode.card_source,
            "billing_mode": mode.billing_mode,
            "risk_acknowledged": mode.risk_acknowledged,
        },
        "seats": payload.seat_quantity,
        "interval": payload.price_interval.lower(),
        "workspace": payload.workspace_name.strip(),
        "promo_code": payload.promo_code.strip(),
        "campaign": payload.promo_campaign_id.strip(),
    }
    # The order validator rejects unmodelled mode fields. Only their presence
    # matters; never read or hash the extra names or supplied values.
    if mode._unknown_fields:
        fields["mode_has_unmodelled_input"] = True
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "v1:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()
