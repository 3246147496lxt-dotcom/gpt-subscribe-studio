"""User-present saved-card setup; no payment confirmation or captcha handling."""
from __future__ import annotations

import json
import re
from typing import Any

from gss_contracts.errors import DomainError, ErrorCode
from .chatgpt import APP_BASE, ChatGPTClient, jwt_account_id


def _error(message: str) -> DomainError:
    return DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, message)


def _identifier(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) else ""


def _check_lease(client):
    guard = getattr(client, "credentials_current", None)
    if guard is not None and not guard():
        raise DomainError(ErrorCode.SESSION_EXPIRED, "订单凭证已过期")


def restore_account(
    client: ChatGPTClient, expected: str = "", *, trust_statsig: bool = True, credentials_current=None
) -> str:
    from . import runtime_flags
    from .session_token import is_access_token_expired

    def require_credentials():
        _check_lease(client)
        if credentials_current is not None and not credentials_current():
            raise DomainError(ErrorCode.SESSION_EXPIRED, "订单凭证已过期")

    require_credentials()
    skip_refresh = bool(
        runtime_flags.get_bool("direct_session_checkout")
        and getattr(client, "access_token", "")
        and not is_access_token_expired(client.access_token, leeway_seconds=60)
    )
    if skip_refresh:
        parsed_account = jwt_account_id(client.access_token)
        if parsed_account:
            client.account_id = parsed_account
    else:
        refreshed = (
            client.refresh_session()
            if trust_statsig
            else client.refresh_session(trust_statsig=False)
        )
        if refreshed:
            client.account_id = jwt_account_id(client.access_token)
    require_credentials()
    client.hydrate()
    account_id = str(client.account_id or "")
    if not account_id or (expected and account_id != expected):
        raise _error("保存支付方式的账号与当前订单不匹配")
    return account_id


def verification_session(
    client: ChatGPTClient, expected_account: str, *, trust_statsig: bool = True, credentials_current=None
) -> str:
    """Carry renewed credentials to the normal subscription adapter, only in memory."""
    restore_account(client, expected_account, trust_statsig=trust_statsig, credentials_current=credentials_current)
    return json.dumps({
        "accessToken": client.access_token,
        "sessionToken": client.session_token,
        "deviceId": client.device_id,
        "sessionId": client.oai_session_id,
        "cookies": dict(client.extra_cookies),
    })


def prepare_account_setup(client: ChatGPTClient, context: dict[str, Any], expected_account: str = "") -> None:
    """Store credentials as soon as they arrive, so a lost response can be resumed."""
    account_id = restore_account(client, expected_account)
    _check_lease(client)
    key = client._publishable_key()
    context.update(account_id=account_id, publishable_key=key, prepare_started=True)
    _check_lease(client)
    setup_id, secret = client.prepare_setup_intent()
    if not re.fullmatch(r"seti_[A-Za-z0-9_]+", setup_id or "") or not str(secret).startswith(f"{setup_id}_secret_"):
        raise _error("上游未返回匹配的 SetupIntent 凭证，尚未提交卡片验证")
    context.update(setup_intent_id=setup_id, client_secret=secret, state="requires_payment_method")


def account_payment_methods(client: ChatGPTClient, account_id: str) -> list[dict[str, Any]]:
    route = "/backend-api/payments/payment_methods"
    _check_lease(client)
    http_get = getattr(client, "_http_get", None)
    if callable(http_get):
        response = http_get(route, params={"account_id": account_id})
    else:
        response = client.http.get(
            f"{APP_BASE}{route}", params={"account_id": account_id},
            headers=client._route_headers(route), timeout=client.timeout,
        )
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 401:
        raise DomainError(ErrorCode.INVALID_SESSION, "账号会话已失效，请重新提交")
    if status >= 400:
        client._raise(ErrorCode.CHECKOUT_UPSTREAM_ERROR, response, "查询账号已保存支付方式")
    payload = response.json()
    if isinstance(payload, dict):
        if payload.get("account_id") and payload["account_id"] != account_id:
            raise _error("已保存支付方式返回了不同的账号")
        methods = payload.get("payment_methods", payload.get("data"))
        if isinstance(methods, dict):
            methods = methods.get("payment_methods", methods.get("data"))
    else:
        methods = payload
    if not isinstance(methods, list):
        raise _error("账号支付方式响应格式发生变化，尚未确认卡片已保存")
    return [item if isinstance(item, dict) else {"id": item} for item in methods if isinstance(item, (dict, str))]


def inspect_account_setup(client: ChatGPTClient, context: dict[str, Any]) -> dict[str, Any]:
    """Trust Stripe GET plus the account's saved-method list, not browser flags."""
    account_id = restore_account(client, str(context.get("account_id") or ""))
    setup_id = context["setup_intent_id"]
    _check_lease(client)
    intent = client.retrieve_stripe_intent(
        intent_id=setup_id, client_secret=context["client_secret"], publishable_key=context["publishable_key"],
    )
    if not isinstance(intent, dict) or intent.get("id") != setup_id:
        raise _error("读取 SetupIntent 状态失败，请重新查询；本次未重新提交验证或付款")
    status = str(intent.get("status") or "")
    if status not in {"requires_payment_method", "requires_confirmation", "requires_action", "processing", "canceled", "succeeded"}:
        raise _error("SetupIntent 返回了未识别的状态，尚未确认卡片已保存")
    if status != "succeeded":
        context["state"] = status
        result = {"state": status, "setup_status": status}
        if context.get("protocol_flow") and status == "requires_payment_method" and isinstance(intent.get("last_setup_error"), dict):
            from .chatgpt import redact_sensitive
            error = intent["last_setup_error"]
            result["setup_error"] = {key: redact_sensitive(str(error[key]))[:240]
                                     for key in ("code", "decline_code", "message") if error.get(key)}
        return result
    customer = _identifier(intent.get("customer"))
    payment_method = _identifier(intent.get("payment_method"))
    if not customer.startswith("cus_") or not payment_method.startswith("pm_"):
        raise _error("SetupIntent 尚未返回已关联 Customer 的支付方式")
    previous_customer = context.get("customer_id")
    if previous_customer and previous_customer != customer:
        raise _error("保存支付方式的 Customer 发生变化，已停止刷新结账")
    methods = account_payment_methods(client, account_id)
    method = next((item for item in methods if _identifier(item.get("id") or item.get("payment_method") or item.get("payment_method_id")) == payment_method), None)
    if method is None:
        context["state"] = "sync_pending"
        return {"state": "sync_pending", "setup_status": status, "message": "卡片验证已完成，正在等待账号同步支付方式；可重新查询"}
    if method.get("type") not in {None, "", "card"}:
        raise _error("当前保存卡片流程只支持 card 支付方式")
    listed_customer = _identifier(method.get("customer"))
    if listed_customer and listed_customer != customer:
        raise _error("账号支付方式与 SetupIntent 的 Customer 不匹配")
    card = method.get("card") if isinstance(method.get("card"), dict) else {}
    last4 = str(card.get("last4") or "")
    summary = {"brand": str(card.get("brand") or "")[:32], "last4": last4 if re.fullmatch(r"\d{4}", last4) else ""}
    state = "checkout_ready" if context.get("refreshed_checkout_id") else "saved"
    context.update(state=state, customer_id=customer, payment_method_id=payment_method, card=summary)
    return {"state": state, "setup_status": status, "payment_method_id": payment_method, "card": summary}
