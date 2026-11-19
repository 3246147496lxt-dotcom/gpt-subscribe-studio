"""HTTP boundary for verified facts. Errors never echo the submitted or expected address."""
from fastapi import HTTPException

from gss_core.verified_payer import BillingMismatch, MissingProfile, VerifiedBilling, verify_claims


def require_verified_billing(*claims, order_billing=None, verification_id=None, profile=None) -> VerifiedBilling:
    try:
        return verify_claims(*claims, order_billing=order_billing, verification_id=verification_id, profile=profile)
    except MissingProfile:
        raise HTTPException(503, detail={"code": "BILLING_PROFILE_UNAVAILABLE",
                                        "message": "已核验账单资料不可用，不能提交新的结账或付款。"}) from None
    except BillingMismatch:
        raise HTTPException(409, detail={"code": "BILLING_PROFILE_MISMATCH",
                                        "message": "账单与已核验资料不符，未提交新的结账或付款。"}) from None
