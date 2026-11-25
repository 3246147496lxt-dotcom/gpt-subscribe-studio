"""HAR-faithful pure-protocol Plus checkout + payment.

Every request in this module maps 1:1 to an entry of the 2026-10-29 capture
(`1029pluschatgpt.com.har`, SHA-256
`230330059dc8633c73ed695440ceb7019e7ef03be3fa419e36e7005bdb4e9e38`):

    #35   POST /backend-api/payments/checkout                    create custom checkout
    #40   GET  /backend-api/payments/checkout/{processor}/{id}   read checkout context
    #65   GET  /v1/elements/sessions                             Elements boot (amount A)
    #83+  POST /r.stripe.com/b                                   SDK event batches
    #89   POST /v1/consumers/sessions/lookup                     payment element surface
    #98   POST /v1/consumers/sessions/lookup                     link surface
    #139  POST /m.stripe.com/6                                   fingerprint v2:1
    #146  POST /v1/consumers/sessions/lookup                     elements controller
    #184  POST /m.stripe.com/6                                   fingerprint v2:2
    #186  POST /m.stripe.com/6                                   mouse-timings-10-v2
    #210  POST /m.stripe.com/6                                   mouse-timings-10
    #215  GET  /edge-internal/card-metadata                      BIN lookup
    #267  POST /backend-api/payments/checkout/taxes             billing/tax quote
    #272  GET  /v1/elements/sessions                             Elements boot (amount B)
    #276  POST /backend-api/payments/checkout/update_tax_id     tax id = null
    #294  POST /v1/confirmation_tokens                           inline card + radar token
    #317  POST /backend-api/sentinel/req                         flow checkout_session_approval
    #328  POST /backend-api/sentinel/req                         flow chatgpt_checkout
    #330  POST /backend-api/payments/checkout/confirm            4-field confirm
    #331  POST /backend-api/sentinel/req                         flow checkout_session_approval
    #332  POST /v1/payment_intents/{id}/confirm                  ctoken + return_url
    #342  GET  /backend-api/payments/checkout/{processor}/{id}   complete/paid
    #350  POST /backend-api/payments/checkout_session            subscription ack
    #482  GET  /backend-api/subscriptions                        entitlement check

The capture's device (macOS / Chrome 156 / Asia/Tokyo / one guid-muid-sid set)
is replaced by a fresh :class:`~gss_core.har_device.DeviceProfile` per attempt;
telemetry comes from :mod:`gss_core.har_telemetry`, which replays the captured
event catalogue on that new device.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlsplit

import httpx

from gss_contracts.adapter import PaymentReceiptEvidence
from .verified_payer import ObservedBillingConflict, check_observed_billing
from gss_contracts.errors import DomainError, ErrorCode
from gss_core.chatgpt import (
    APP_BASE,
    STRIPE_API_VERSION,
    STRIPE_BASE,
    ChatGPTClient,
    _intent_object,
    _PLAN_NAME,
    _json,
    parse_account_check,
    _log_upstream_failure,
    payment_intent_from_confirm,
    public_checkout_id,
    redact_sensitive,
    stripe_verify_url,
)
from gss_core.har_device import (
    DeviceProfile,
    apply_user_agent,
    device_for_account,
    g_state_cookie,
    oai_telemetry_header,
)
from gss_core.har_telemetry import (
    PHASE_CAPTCHA,
    PHASE_CARD_ENTRY,
    PHASE_CONFIRM,
    PHASE_ELEMENTS,
    PHASE_TAXES,
    PHASE_TAX_ID,
    PHASE_TOKEN,
    StripeTelemetry,
    TelemetryContext,
    sentinel_ping,
)
from gss_core.saved_cards import restore_account
from gss_core.saved_card_protocol import _factor, _stripe_error, outcome
from gss_core.session_token import parse_session_bundle
from gss_core.proxies import ProxyLeaseError, claim_sid_lease, cool_proxy, release_sid_lease
from gss_core.stripe_protocol import PaymentOutcome

logger = logging.getLogger("gss.plus_har")

_UNSETTLED_INTENT_STATES = {"requires_payment_method", "canceled", "cancelled"}
_CARD_REJECTION_CODES = {"card_declined", "expired_card", "incorrect_cvc", "incorrect_number"}


def cash_receipt(checkout_id, payment_id, received_minor, currency, expected_minor):
    """Only settled money with an original transaction reference, never the quoted total."""
    if (not checkout_id or not isinstance(payment_id, str) or not payment_id.startswith("pi_")
            or type(received_minor) is not int or received_minor <= 0 or received_minor != expected_minor
            or not currency):
        return None
    code = str(currency).upper()
    return PaymentReceiptEvidence(checkout_id=checkout_id, payment_id=payment_id,
                                  amount=Decimal(received_minor) / _factor(code), currency=code)


def active_plus_entitlement(payload: dict, account_id: str = "") -> bool:
    entitlement = payload.get("entitlement") or {}
    subscription = payload.get("subscription") or {}
    if not isinstance(entitlement, dict) or not isinstance(subscription, dict):
        return False
    plans = [str(v).lower() for v in (payload.get("plan_type"), entitlement.get("subscription_plan"),
                                      subscription.get("plan")) if v]
    flags = [payload.get("is_active"), entitlement.get("has_active_subscription")]
    if not plans or any(v not in {"plus", "chatgptplusplan"} for v in plans):
        return False
    if not any(v is True for v in flags) or any(v is False for v in flags):
        return False
    for record in (payload, entitlement, subscription):
        if record.get("is_delinquent") is True or (record.get("account_id") and record["account_id"] != account_id):
            return False
        if record.get("status") and record["status"] not in {"active", "trialing", "paid"}:
            return False
        if record.get("expires_at") is not None:
            try:
                expiration = datetime.fromisoformat(str(record["expires_at"]).replace("Z", "+00:00"))
                if expiration.tzinfo is None or expiration <= datetime.now(timezone.utc):
                    return False
            except ValueError:
                return False
    return True


def _confirmed_decline(out: PaymentOutcome) -> bool:
    return bool(out.decline_code or out.code in _CARD_REJECTION_CODES)


def _classify_unsuccessful_intent(out: PaymentOutcome) -> None:
    if _confirmed_decline(out):
        out.state = "card_declined"
    else:
        out.state = "unknown"
        if out.intent_status in _UNSETTLED_INTENT_STATES and not out.code:
            out.code = "intent_status_unconfirmed"


_SESSION = re.compile(r"(oaics_[A-Za-z0-9]+|cs_(?:live|test)_[A-Za-z0-9]+)")
_ATTRIBUTION = {
    "merchant_integration_source": "elements",
    "merchant_integration_subtype": "payment-element",
    "merchant_integration_version": "2021",
    "payment_intent_creation_flow": "deferred",
    "payment_method_selection_flow": "merchant_specified",
}
_ADDITIONAL_ELEMENTS = ["expressCheckout", "payment", "linkSignup", "address"]
_LINK_DETAIL_TYPES = ["CARD", "BANK_ACCOUNT", "KLARNA", "BALANCE", "PIX", "CRYPTO",
                      "SEPA_BANK_ACCOUNT", "UPI"]


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "off", "no"}


class PlusHarProtocol:
    """One pure-HTTP payment attempt that mirrors the captured browser flow."""

    def __init__(
        self,
        *,
        session: str,
        checkout_url: str = "",
        publishable_key: str = "",
        card: Optional[Dict[str, str]] = None,
        billing: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
        currency: str = "USD",
        timeout: float = 25.0,
        profile: Optional[Dict[str, Any]] = None,
        customer_session_client_secret: str = "",
        hcaptcha_token: str = "",
        email: str = "",
        country: str = "",
        plan: str = "plus",
        account_id: str = "",
        checkout_id: str = "",
        processor: str = "",
        confirm_return_url: str = "",
        device: Optional[DeviceProfile] = None,
        promo_campaign_id: str = "",
        telemetry: Optional[str] = None,
        client: Optional[ChatGPTClient] = None,
        read_only: bool = False,
        user_agent: str = "",
        device_id: str = "",
        exit_country: str = "",
        pin_proxy: bool = False,
        browser_context: Any = None,
        purchase_attempt_id: str = "",
    ) -> None:
        self.session = session or ""
        self.checkout_url = checkout_url or ""
        self.publishable_key = publishable_key or ""
        self.card = card or {}
        self.billing = billing or {}
        self.proxy = proxy or None
        self.currency = (currency or "USD").upper()
        configured_timeout = os.environ.get("GSS_CONFIRM_TIMEOUT", "").strip()
        if configured_timeout:
            try:
                timeout = float(configured_timeout)
            except ValueError:
                pass
        self.timeout = float(timeout or 25.0)
        self.plan = plan or "plus"
        # billing country drives tax/AVS; exit country drives device timezone/IP.
        self.country = (self.billing.get("country") or country or "US").upper()
        self.exit_country = (exit_country or country or self.country).upper()
        self.pin_proxy = pin_proxy
        self.browser_context = browser_context
        self.purchase_attempt_id = purchase_attempt_id
        self.email = (email or self.billing.get("email") or "").strip()
        self.expected_account = account_id or ""
        self.cuss_secret = customer_session_client_secret or ""
        self.explicit_hcaptcha = hcaptcha_token or ""
        self.promo_campaign_id = promo_campaign_id or ""

        m = _SESSION.search(self.checkout_url or "")
        self.checkout_id = checkout_id or public_checkout_id(m.group(1) if m else "")
        self.processor = processor or self._processor_from_url(self.checkout_url) or "openai_llc"
        self.confirm_return_url = confirm_return_url or ""
        self.is_stripe_custom = self.checkout_id.startswith("cs_")

        if device is None and account_id:
            device = device_for_account(account_id, self.exit_country)
        self.device = device or DeviceProfile.random(
            country=self.exit_country, seed=device_id or None
        )
        if isinstance(profile, dict) and profile.get("os_key"):
            # Only a full har_device dict restores an identity; the legacy
            # build/screen profile cannot describe a coherent device here.
            merged = dict(self.device.to_dict())
            merged.update({k: v for k, v in profile.items() if k in merged})
            self.device = DeviceProfile.from_dict(merged)
        # Session 的真实 UA 优先于随机设备档案。
        if user_agent:
            self.device = apply_user_agent(self.device, user_agent)

        from gss_core import runtime_flags

        self.mode = (telemetry or runtime_flags.get_str("har_telemetry")).lower()
        self.read_only = bool(read_only)
        # True when this attempt re-issued the session for a different country.
        self.session_reissued = False
        self.confirmation_started = False
        self.on_confirmation_start = lambda: None
        self.receipt_expected_minor: int | None = None
        self.receipt_evidence = None
        self.plus_entitlement_verified = False
        self.credentials_current = lambda: True
        self.stage = "checkout"
        self._owns_client = client is None
        self.client: Optional[ChatGPTClient] = client
        self._web: Any = None
        self._stripe: Any = None
        self._m: Any = None
        self._sink = _capture_sink()
        self.telemetry: Optional[StripeTelemetry] = None
        self.t0 = time.time()
        self.amount_minor = 0
        # 用户在页面上确认过的金额（order 金额）；税后高于它就要停下来重新确认。
        self.expected_amount_minor = 0
        self.customer_id = ""
        self.elements_session_id = ""
        self.passive_captcha: Dict[str, str] = {}
        self.ctoken = ""
        # One cached read after the stale-checkout probe, so the same snapshot
        # is not fetched twice in one attempt.
        self._checkout_snapshot: Optional[Dict[str, Any]] = None
        self.discarded_checkout_id = ""
        self._radar_cached = ""
        self.confirmed: Dict[str, Any] = {}
        self.intent_id = ""
        self.intent_secret = ""
        self.last_error: Dict[str, str] = {}
        self.raw: Dict[str, Any] = {}

    # ── lifecycle ──────────────────────────────────────────────────
    @staticmethod
    def _safe_close(handle: Any, timeout_s: float = 2.0) -> None:
        if handle is None:
            return
        try:
            worker = threading.Thread(target=handle.close, daemon=True)
            worker.start()
            worker.join(timeout_s)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        for handle in (self._web, self._stripe, self._m):
            self._safe_close(handle)
        self._web = None
        self._stripe = None
        self._m = None
        if self._owns_client and self.client is not None:
            self._safe_close(self.client)

    @staticmethod
    def _processor_from_url(url: str) -> str:
        parts = urlsplit(url or "")
        match = re.search(r"/checkout/([a-z_]+)/", parts.path or "")
        return match.group(1) if match else ""

    def rotate_proxy_sid(self) -> None:
        from gss_core.proxies import rotate_proxy_sid

        if not self.proxy:
            return
        try:
            self.proxy = rotate_proxy_sid(self.proxy)
        except Exception:  # noqa: BLE001
            return
        logger.info("plus_har_proxy_rotated sid=%s", self.proxy.split("@")[-1])
        self.close()
        self.client = ChatGPTClient.from_raw_session(self.session, proxy=self.proxy)
        self.client.timeout = self.timeout
        self._web = None

    # ── HTTP sessions ──────────────────────────────────────────────
    def _web_session(self) -> Any:
        """curl_cffi session carrying exactly the captured browser headers."""
        if self._web is not None:
            return self._web
        from curl_cffi.requests import Session as CurlSession

        session = CurlSession(impersonate=self.device.impersonate)
        if hasattr(session, "trust_env"):
            session.trust_env = False
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        session.headers.clear()
        web_headers = self.device.web_headers(referer="https://chatgpt.com/")
        if self.browser_context is not None:
            web_headers.update(self.browser_context.headers())
        session.headers.update(web_headers)
        cookies = dict(getattr(self.client, "extra_cookies", {}) or {})
        if self.browser_context is not None:
            from gss_core.browser_context import safe_browser_cookies
            cookies.update(safe_browser_cookies(self.browser_context.cookies))
        if getattr(self.client, "session_token", ""):
            cookies.setdefault("__Secure-next-auth.session-token", self.client.session_token)
        if getattr(self.client, "device_id", ""):
            cookies["oai-did"] = self.client.device_id
        cookies.setdefault("g_state", g_state_cookie(self.device))
        for name, value in cookies.items():
            if not name or value is None:
                continue
            try:
                session.cookies.set(str(name), str(value), domain="chatgpt.com")
            except Exception:  # noqa: BLE001
                continue
        self._web = _RecordingSession(session, self._sink, "web")
        return self._web

    def _browser_session(self, headers: Dict[str, str]) -> Any:
        """curl_cffi session so api.stripe.com sees the same TLS as chatgpt.com.

        Fail closed: without curl_cffi there is no session with this identity,
        so nothing may be sent to the payment endpoints.
        """
        try:
            from curl_cffi.requests import Session as CurlSession
        except ImportError:
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                "缺少 curl_cffi，无法建立与结账身份一致的 Stripe 传输，已停止",
                detail={"code": "curl_cffi_unavailable", "stage": "transport"},
            ) from None

        session = CurlSession(impersonate=self.device.impersonate, timeout=self.timeout)
        if hasattr(session, "trust_env"):
            session.trust_env = False
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        session.headers.clear()
        session.headers.update(headers)
        return _RecordingSession(session, self._sink, "stripe")

    def _stripe_session(self) -> Any:
        if self._stripe is None:
            self._stripe = self._browser_session(self.device.stripe_headers())
        return self._stripe

    def _m_session(self) -> Any:
        if self._m is None:
            self._m = self._browser_session(self.device.fingerprint_headers())
        return self._m

    # ── headers ────────────────────────────────────────────────────
    def _web_headers(self, *, referer: str = "https://chatgpt.com/",
                     json_body: bool = True, telemetry: Optional[str] = None) -> Dict[str, str]:
        headers = self.device.web_headers(referer=referer)
        if json_body:
            headers["content-type"] = "application/json"
        if self.client is None:
            return headers
        if self.client.access_token:
            headers["authorization"] = f"Bearer {self.client.access_token}"
        if self.client.account_id:
            headers["chatgpt-account-id"] = self.client.account_id
        device_id = self.client.device_id or self.device.guid
        headers["oai-did"] = device_id
        headers["oai-device-id"] = device_id
        headers["oai-language"] = self.device.locale
        if telemetry:
            # The capture only sends oai-telemetry on checkout create and confirm.
            headers["oai-telemetry"] = telemetry
        if self.client.sentinel_token:
            headers["openai-sentinel-token"] = self.client.sentinel_token
        if self.client.sentinel_so:
            headers["openai-sentinel-so-token"] = self.client.sentinel_so
        return headers

    def _require_credentials(self) -> None:
        if not self.credentials_current():
            raise DomainError(ErrorCode.SESSION_EXPIRED, "订单凭证已过期，本次未发起新查询")

    def _app(self, method: str, route: str, *, json_body: Any = None,
             params: Optional[Dict[str, Any]] = None, telemetry: Optional[str] = None,
             referer: str = "https://chatgpt.com/") -> Any:
        self._require_credentials()
        url = f"{APP_BASE}{route}"
        headers = self._web_headers(referer=referer, json_body=json_body is not None,
                                    telemetry=telemetry)
        kwargs: Dict[str, Any] = {"headers": headers, "timeout": self.timeout}
        if json_body is not None:
            kwargs["json"] = json_body
        if params:
            kwargs["params"] = params
        return self._web_session().request(method, url, **kwargs)

    # ── 0. account + sentinel ──────────────────────────────────────
    def _claim_exit(self) -> None:
        """One sticky exit serves one order; a quarantined SID stops here."""
        if not self.proxy or not self.purchase_attempt_id:
            return
        try:
            claim_sid_lease(self.proxy, order_id=self.purchase_attempt_id, role="checkout")
        except ProxyLeaseError as exc:
            raise DomainError(
                ErrorCode.CHECKOUT_UPSTREAM_ERROR, str(exc),
                detail={"code": "proxy_sid_unavailable", "stage": "prepare"},
            ) from None

    def _reject_if_already_subscribed(self, accounts: Any) -> None:
        """Stop a new-purchase attempt before /payments/checkout when the
        account already holds an active subscription."""
        if str(self.plan).lower() == "team":
            return
        if not isinstance(accounts, dict) or not accounts:
            return
        try:
            parsed = parse_account_check(accounts, account_id=self.client.account_id or "")
        except (ValueError, TypeError, KeyError):
            logger.warning("account_check_plan_unparsed")
            return
        plan_type = str(parsed.get("current_plan_type") or parsed.get("subscription_plan") or "")
        if parsed.get("is_free"):
            return
        raise DomainError(
            ErrorCode.ALREADY_SUBSCRIBED,
            "该账号当前已有有效订阅，已停止创建新订单。",
            detail={"code": "already_subscribed", "planType": plan_type},
        )

    def _note_exit_rejection(self, status: int, payload: Dict[str, Any],
                             response: Any = None) -> None:
        """Quarantine the bound exit after a risk rejection, before any retry."""
        code = str((payload or {}).get("code") or (payload or {}).get("type") or "")
        # Some upstream refusals carry no structured code, only prose. Classify
        # locally; the text itself is never stored or logged.
        text = str(getattr(response, "text", "") or "")[:600].lower()
        risk = (status in {401, 403}
                or code in {"unusual_activity", "token_revoked", "invalid_session"}
                or "unusual activity" in text or "token_revoked" in text)
        if risk and self.proxy:
            try:
                seconds = int(os.environ.get("GSS_PROXY_COOLDOWN_SECONDS", "1800") or 1800)
            except ValueError:
                seconds = 1800
            cool_proxy(self.proxy, reason=f"{status}:{code or 'risk'}", seconds=max(1, seconds))

    def _prepare(self) -> None:
        self._claim_exit()
        if self.client is None:
            imported = parse_session_bundle(self.session)
            cross_country = bool(
                imported.country and self.exit_country
                and imported.country.upper() != self.exit_country.upper()
            )
            if imported.refresh_token:
                # OAuth credentials are portable: one refresh exchange on this
                # exit replaces both the web cookie and the login country.
                self.client = ChatGPTClient.from_raw_session(
                    self.session, proxy=self.proxy, strip_ambient=True)
                if not self.read_only:
                    self.client.access_token = ""
                self.session_reissued = True
            elif cross_country and imported.session_token:
                # Cross-region takeover: keep only the session cookie and
                # re-issue the token on this order's exit with a new device.
                self.client = ChatGPTClient.reauthorize_for_country(
                    self.session, proxy=self.proxy, country=self.exit_country)
                self.session_reissued = True
            else:
                # Same region or no session cookie: drop the exported device,
                # statsig country and cookie jar, then warm on this exit.
                self.client = ChatGPTClient.from_raw_session(
                    self.session, proxy=self.proxy, strip_ambient=True)
                if not self.read_only and imported.session_token:
                    # Drop the exported AT before warm_exit_session re-issues on this exit.
                    self.client.access_token = ""
                self.session_reissued = False
            self.client.timeout = self.timeout
            self.client.language = self.device.locale
            self.client.timezone = self.device.timezone
            if not self.client.user_agent:
                self.client.user_agent = self.device.ua
            elif self.session_reissued:
                self.device = apply_user_agent(self.device, self.client.user_agent)
            if not self.read_only:
                try:
                    accounts = self.client.warm_exit_session(
                        oauth=bool(imported.refresh_token),
                        reissue=not self.session_reissued,
                    )
                except DomainError as exc:
                    raise DomainError(exc.code, exc.message, detail=exc.detail) from None
                self._reject_if_already_subscribed(accounts)
            try:
                restore_account(self.client, self.expected_account,
                                credentials_current=self.credentials_current)
            except DomainError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise DomainError(ErrorCode.INVALID_SESSION,
                                  f"读取账号失败：{redact_sensitive(str(exc))[:120]}") from exc
        if not self.client.account_id and self.expected_account:
            self.client.account_id = self.expected_account
        self.client.language = self.device.locale
        self.client.timezone = self.device.timezone
        if self.browser_context is not None:
            # Route C headers, Sentinel profile and ChatGPT client must share
            # the same browser identity; never keep a separate session profile.
            self.client.apply_browser_context(self.browser_context)
            self.device.ua = self.browser_context.user_agent or self.device.ua
            self.device.screen_w = int(self.browser_context.viewport_width or self.device.screen_w)
            self.device.screen_h = int(self.browser_context.viewport_height or self.device.screen_h)
            self.device.avail_h = max(1, self.device.screen_h - 80)
            if self.browser_context.sec_ch_ua_platform:
                self.device.ch_platform = f'"{self.browser_context.sec_ch_ua_platform.strip(chr(34))}"'

        self.telemetry = StripeTelemetry(self._m_session(), TelemetryContext(
            device=self.device,
            key=self.publishable_key,
            currency=self.currency,
            amount=self.amount_minor,
            account_id=self.client.account_id,
            device_id=self.client.device_id or self.device.guid,
            email=self.email,
            country=self.country,
            exit_country=self.exit_country,
            page_url="https://chatgpt.com/",
            started_at=self.t0,
        ))
        budget = float(os.environ.get("GSS_HAR_TELEMETRY_BUDGET", "120") or 0)
        if budget > 0 and hasattr(self.telemetry, "deadline"):
            self.telemetry.deadline = time.time() + budget

    def _sentinel(self, flow: str) -> None:
        self._require_credentials()
        started = time.monotonic()
        logger.info("plus_har_sentinel_start flow=%s", flow)
        try:
            self.client.mint_sentinel(flow=flow, page_url="https://chatgpt.com/")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "plus_har_sentinel_failed flow=%s type=%s elapsed=%.2f",
                flow, type(exc).__name__, time.monotonic() - started,
            )
            raise DomainError(
                ErrorCode.SENTINEL_CHALLENGE_FAILED,
                "Sentinel 风控令牌签发失败，已停止确认，未提交支付。",
            ) from exc
        logger.info("plus_har_sentinel_ok flow=%s elapsed=%.2f", flow, time.monotonic() - started)
        ping_started = time.monotonic()
        try:
            sentinel_ping(self._web_session(), self.device, timeout=min(self.timeout, 5.0))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "plus_har_sentinel_ping_failed flow=%s type=%s elapsed=%.2f",
                flow, type(exc).__name__, time.monotonic() - ping_started,
            )
        else:
            logger.info("plus_har_sentinel_ping_ok flow=%s elapsed=%.2f", flow, time.monotonic() - ping_started)

    def _prelude(self) -> None:
        if self.mode == "off":
            return
        try:
            self.telemetry.ces_register(self._web_session(), self._web_headers())
        except Exception:  # noqa: BLE001
            pass

    # ── 1. checkout create / read ──────────────────────────────────
    def _create_checkout(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "plan_name": _PLAN_NAME.get(str(self.plan), "chatgptplusplan"),
            "checkout_source": "chatgpt_web_merged",
            "entry_point": "all_plans_pricing_modal",
            "billing_details": {"country": self.country, "currency": self.currency},
            "supports_saved_payment_link": True,
            "checkout_ui_mode": "custom",
            "checkout_appearance": "light",
            "cancel_url": "https://chatgpt.com/?cta_tab=personal",
            "purchase_attempt_id": self.purchase_attempt_id or str(uuid.uuid4()),
        }
        if self.promo_campaign_id:
            body["promo_campaign"] = {
                "promo_campaign_id": self.promo_campaign_id,
                "is_coupon_from_query_param": False,
            }
        response = self._app("POST", "/backend-api/payments/checkout", json_body=body,
                             telemetry=oai_telemetry_header(),
                             referer="https://chatgpt.com/?cta_tab=personal")
        payload = _json(response)
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 401:
            self._note_exit_rejection(status, payload, response)
            raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效或已过期")
        if status >= 400:
            self._note_exit_rejection(status, payload, response)
            _log_upstream_failure("/backend-api/payments/checkout", response)
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                              f"创建结账失败 HTTP {status}",
                              detail={"upstream_status": status,
                                      "upstream_code": str(payload.get("code") or payload.get("type") or "")})
        check_observed_billing(payload, self.billing, alternate_countries=(self.exit_country,))
        self.checkout_id = public_checkout_id(str(payload.get("checkout_session_id") or ""))
        self.publishable_key = str(payload.get("publishable_key") or self.publishable_key)
        self.processor = str(payload.get("processor_entity") or self.processor)
        self.cuss_secret = str(payload.get("customer_session_client_secret") or self.cuss_secret)
        self.confirm_return_url = str(payload.get("confirm_return_url") or self.confirm_return_url)
        self.raw["create"] = payload
        return payload

    def _read_checkout(self) -> Dict[str, Any]:
        if self._checkout_snapshot is not None:
            payload, self._checkout_snapshot = self._checkout_snapshot, None
            return payload
        route = f"/backend-api/payments/checkout/{self.processor}/{self.checkout_id}"
        response = self._app("GET", route, params={"supports_business_seat_offer": "true"})
        status = int(getattr(response, "status_code", 0) or 0)
        payload = _json(response)
        if status == 401:
            self._note_exit_rejection(status, payload, response)
            raise DomainError(ErrorCode.INVALID_SESSION, "Session 无效或已过期")
        if status >= 400:
            self._note_exit_rejection(status, payload, response)
            _log_upstream_failure(route, response)
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                              f"读取结账失败 HTTP {status}",
                              detail={"upstream_status": status,
                                      "upstream_code": str(payload.get("code") or payload.get("type") or "")})
        if payload.get("publishable_key"):
            self.publishable_key = str(payload["publishable_key"])
        if payload.get("customer_session_client_secret"):
            self.cuss_secret = str(payload["customer_session_client_secret"])
        if payload.get("confirm_return_url"):
            self.confirm_return_url = str(payload["confirm_return_url"])
        self.raw["checkout"] = payload
        return payload

    def _reset_checkout(self) -> None:
        self.checkout_id = ""
        self.checkout_url = ""
        self.cuss_secret = ""
        self.confirm_return_url = ""

    def _discard_stale_checkout(self) -> None:
        """Do not reuse an open checkout minted for another exit or currency.

        A checkout is bound to the country that created it. Reading it again
        under a different exit is what the upstream reports as cross-region
        activity, so an incompatible session is dropped before any new order
        is created. Missing or expired sessions are dropped as well; only a
        credential failure (401/403) stops the attempt.
        """
        if not self.checkout_id:
            return
        try:
            payload = self._read_checkout()
        except DomainError as exc:
            status = (exc.detail or {}).get("upstream_status")
            if status in {400, 404, 410}:
                self.discarded_checkout_id = self.checkout_id
                self._reset_checkout()
                return
            raise
        state = payload.get("checkout_state") if isinstance(payload.get("checkout_state"), dict) else {}
        total = state.get("total") if isinstance(state.get("total"), dict) else {}
        inner = total.get("total") if isinstance(total.get("total"), dict) else {}
        currency = str(inner.get("currency") or payload.get("currency") or "").upper()
        billing = payload.get("billing_details") if isinstance(payload.get("billing_details"), dict) else {}
        country = str(billing.get("country") or "").upper()
        status = str(payload.get("status") or "")
        stale = (status not in {"", "open"}
                 or (currency and currency != self.currency.upper())
                 or (country and country != self.country.upper()))
        if stale:
            self.discarded_checkout_id = self.checkout_id
            logger.info("plus_har_checkout_discarded status=%s currency=%s country=%s",
                        status or "-", currency or "-", country or "-")
            self._reset_checkout()
            return
        self._checkout_snapshot = payload

    @staticmethod
    def _total_minor(payload: Dict[str, Any]) -> int:
        state = payload.get("checkout_state") if isinstance(payload.get("checkout_state"), dict) else {}
        total = state.get("total") if isinstance(state.get("total"), dict) else {}
        inner = total.get("total") if isinstance(total.get("total"), dict) else {}
        value = inner.get("minorUnitsAmount")
        if isinstance(value, bool) or not isinstance(value, int):
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "结账缺少最终应付总额")
        return value

    # ── 2. Stripe Elements ─────────────────────────────────────────
    def _elements_params(self) -> Dict[str, str]:
        params = {
            "client_betas[0]": "elements_link_signup_beta_1",
            "deferred_intent[mode]": "subscription",
            "deferred_intent[amount]": str(self.amount_minor),
            "deferred_intent[currency]": self.currency.lower(),
            "deferred_intent[setup_future_usage]": "off_session",
            "deferred_intent[payment_method_types][0]": "card",
            "deferred_intent[payment_method_types][1]": "link",
            "currency": self.currency.lower(),
            "key": self.publishable_key,
            "_stripe_version": STRIPE_API_VERSION,
            "elements_init_source": "stripe.elements",
            "referrer_host": "chatgpt.com",
            "stripe_js_id": self.device.stripe_js_id,
            "locale": self.device.locale,
            "browser_timezone": self.device.timezone,
            "type": "deferred_intent",
        }
        if self.cuss_secret:
            params["customer_session_client_secret"] = self.cuss_secret
        return params

    def _elements_session(self) -> None:
        logger.info("plus_har_elements amount=%s %s", self.amount_minor, self.currency)
        response = self._stripe_session().get(f"{STRIPE_BASE}/v1/elements/sessions",
                                              params=self._elements_params(),
                                              timeout=self.timeout)
        payload: Dict[str, Any] = {}
        if getattr(response, "content", b""):
            try:
                payload = response.json() or {}
            except Exception:  # noqa: BLE001
                payload = {}
        self.raw.setdefault("elements", []).append(payload)
        session_id = str(payload.get("session_id") or "")
        if session_id:
            self.elements_session_id = session_id
            self.device.elements_session_id = session_id
        customer = payload.get("customer")
        customer_id = ""
        if isinstance(customer, str):
            customer_id = customer
        elif isinstance(customer, dict):
            # Live shape (2026-10-30): customer has no top-level id; the cus_ id
            # sits in customer.customer_session.customer.
            customer_id = str(customer.get("id") or "")
            if not customer_id.startswith("cus_"):
                session = customer.get("customer_session")
                if isinstance(session, dict):
                    inner = session.get("customer")
                    customer_id = str(inner.get("id") if isinstance(inner, dict) else inner or "")
        if customer_id.startswith("cus_"):
            self.customer_id = customer_id
        config_id = str(payload.get("config_id") or payload.get("elements_session_config_id") or "")
        if config_id:
            # The capture reuses this id in the confirmation token attribution.
            self.device.elements_session_config_id = config_id
        cap = payload.get("passive_captcha") if isinstance(payload.get("passive_captcha"), dict) else {}
        if cap:
            self.passive_captcha = {
                "site_key": str(cap.get("site_key") or ""),
                "rqdata": str(cap.get("rqdata") or ""),
            }
        if self.telemetry is not None:
            self.telemetry.ctx.customer_id = self.customer_id
            self.telemetry.ctx.amount = self.amount_minor

    def _consumers_lookup(self, surface: str) -> None:
        body: Dict[str, Any] = {"request_surface": surface}
        if surface == "web_link_authentication_in_payment_element":
            body["transaction_context[link_supported_payment_methods][0]"] = "CARD"
            body["transaction_context[is_recurring]"] = "true"
            body["transaction_context[link_mode]"] = "LINK_CARD_BRAND"
            for index, value in enumerate(_LINK_DETAIL_TYPES):
                body[f"supported_payment_details_types[{index}]"] = value
            if self.customer_id:
                body["customer_id"] = self.customer_id
        body["email_address"] = self.email
        body["email_source"] = "default_value"
        body["session_id"] = self.device.stripe_js_id
        body["key"] = self.publishable_key
        if surface == "web_link_authentication_in_payment_element":
            body["link_global_holdback_data[assignment]"] = "control"
            body["link_global_holdback_data[arb_id]"] = ""
        if surface == "web_elements_controller":
            body["do_not_log_consumer_funnel_event"] = "true"
        try:
            self._stripe_session().post(f"{STRIPE_BASE}/v1/consumers/sessions/lookup",
                                        content=urlencode(body), timeout=self.timeout)
        except Exception:  # noqa: BLE001
            pass

    def _card_metadata(self) -> None:
        number = re.sub(r"\D", "", str(self.card.get("number") or ""))
        if len(number) < 6:
            return
        try:
            self._stripe_session().get(
                f"{STRIPE_BASE}/edge-internal/card-metadata",
                params={"bin_prefix": number[:6], "key": self.publishable_key,
                        "_stripe_version": STRIPE_API_VERSION},
                timeout=self.timeout,
            )
        except Exception:  # noqa: BLE001
            pass

    # ── 3. tax quote ───────────────────────────────────────────────
    def _taxes(self) -> None:
        b = self.billing
        body = {
            "checkout_session_id": self.checkout_id,
            "processor_entity": self.processor,
            "billing_country": self.country,
            "billing_name": str(b.get("name") or ""),
            "billing_address": {
                "line1": str(b.get("line1") or ""),
                "line2": "",
                "city": str(b.get("city") or ""),
                "country": self.country,
                "postal_code": str(b.get("postal_code") or b.get("zip") or ""),
                "state": str(b.get("state") or ""),
            },
            "currency": self.currency.lower(),
            "checkout_email": self.email,
            "tax_id": None,
        }
        response = self._app("POST", "/backend-api/payments/checkout/taxes", json_body=body)
        payload = _json(response)
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "读取账单税费失败")
        check_observed_billing(payload, self.billing, alternate_countries=(self.exit_country,))
        session = payload.get("checkout_session") if isinstance(payload.get("checkout_session"), dict) else {}
        if session.get("publishable_key"):
            self.publishable_key = str(session["publishable_key"])
        before = self.amount_minor
        self.amount_minor = self._total_minor(session or payload)
        tax_delta = (before - self.amount_minor) if before else None
        self.raw["taxes"] = payload
        logger.info(
            "plus_har_tax_recalc exit_country=%s billing_country=%s "
            "before=%s after=%s currency=%s tax_delta=%s",
            self.exit_country,
            self.country,
            before,
            self.amount_minor,
            self.currency,
            tax_delta,
        )

    def _update_tax_id(self) -> None:
        body = {"checkout_session_id": self.checkout_id, "tax_id_info": None}
        try:
            self._app("POST", "/backend-api/payments/checkout/update_tax_id", json_body=body)
        except Exception:  # noqa: BLE001
            pass

    # ── 4. radar token + confirmation token ────────────────────────
    def _radar_token(self) -> str:
        if self.explicit_hcaptcha:
            return self.explicit_hcaptcha
        if self._radar_cached:
            return self._radar_cached
        solver = os.environ.get("GSS_LLM_SOLVER_URL", "").rstrip("/")
        if not solver:
            return ""
        if "host.docker.internal" in solver and not os.path.exists("/.dockerenv"):
            solver = solver.replace("host.docker.internal", "127.0.0.1")
        payload = {
            "sitekey": self.passive_captcha.get("site_key") or "24ed0064-62cf-4d42-9960-5dd1a41d4e29",
            "rqdata": self.passive_captcha.get("rqdata") or "",
            "host": "b.stripecdn.com",
            "proxy": self._solver_proxy(),
            "ua": self.device.ua,
            "platform": self.device.platform,
            # The solver's CDP override must carry the same UA-CH identity as the
            # payment attempt, or hCaptcha sees two different browsers.
            "meta": {
                "brands": [
                    {"brand": "Google Chrome", "version": self.device.chrome_major},
                    {"brand": "Chromium", "version": self.device.chrome_major},
                    {"brand": "Not=A?Brand", "version": "24"},
                ],
                "fullVersionList": [
                    {"brand": "Google Chrome", "version": self.device.chrome_build},
                    {"brand": "Chromium", "version": self.device.chrome_build},
                    {"brand": "Not=A?Brand", "version": "24.0.0.0"},
                ],
                "fullVersion": self.device.chrome_build,
                "platform": (self.device.ch_platform or "").strip('"') or self.device.os_label,
                "platformVersion": self.device.ch_platform_version or "6.8.0",
                "architecture": (self.device.ch_arch or "").strip('"') or "x86",
                "model": "",
                "mobile": False,
                "bitness": (self.device.ch_bitness or "").strip('"') or "64",
                "wow64": False,
            },
            "platform_label": self.device.os_label,
            "screen_w": self.device.screen_w,
            "screen_h": self.device.screen_h,
            "avail_h": self.device.avail_h,
            "cores": self.device.cores,
            "locale": self.device.locale,
            "timezone": self.device.timezone,
        }
        try:
            response = httpx.post(f"{solver}/solve", json=payload,
                                  timeout=httpx.Timeout(180.0, connect=15.0))
            token = str((response.json() or {}).get("token") or "")
            if token:
                self._radar_cached = token
                if self.telemetry is not None:
                    # The capture carries the passive token in its SDK events.
                    self.telemetry.ctx.hcaptcha_token = token
                logger.info("plus_har_hcaptcha_token len=%s", len(token))
            return token
        except Exception as exc:  # noqa: BLE001
            logger.warning("plus_har_hcaptcha_failed err=%s", str(exc)[:120])
            return ""

    def _solver_proxy(self) -> str:
        """Chrome cannot do SOCKS5 auth, so a local HTTP bridge can be pinned."""
        bridge = os.environ.get("GSS_SOLVER_PROXY", "").strip()
        if bridge:
            return bridge
        proxy = str(self.proxy or "")
        if proxy.startswith("socks5h://"):
            return "socks5://" + proxy[len("socks5h://"):]
        return proxy

    def _confirmation_token(self) -> str:
        b = self.billing
        # A locally generated guid/muid/sid is not a Stripe device identity.
        # Fail closed before the confirmation token, payment method or intent:
        # nothing has been sent at this point, so no charge can be in flight.
        if self.mode != "off" and isinstance(self.telemetry, StripeTelemetry):
            if not getattr(self.telemetry, "registered", False):
                raise DomainError(
                    ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                    "Stripe 设备指纹未完成注册，已停止在建单/确认前",
                    detail={"code": "telemetry_unavailable", "stage": "confirmation_token"},
                )
            if not getattr(self.telemetry, "server_ids_adopted", False):
                raise DomainError(
                    ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                    "Stripe 设备指纹未取得服务端 guid/muid/sid，已停止在建单/确认前",
                    detail={"code": "telemetry_unavailable", "stage": "confirmation_token"},
                )
        radar = self._radar_token()
        time_on_page = int((time.time() - self.t0) * 1000)
        form: Dict[str, Any] = {
            "payment_method_data[billing_details][email]": self.email,
            "payment_method_data[billing_details][address][line1]": str(b.get("line1") or ""),
            "payment_method_data[billing_details][address][city]": str(b.get("city") or ""),
            "payment_method_data[billing_details][address][country]": self.country,
            "payment_method_data[billing_details][address][postal_code]": str(
                b.get("postal_code") or b.get("zip") or ""),
            "payment_method_data[billing_details][address][state]": str(b.get("state") or ""),
            "payment_method_data[billing_details][name]": str(b.get("name") or ""),
            "payment_method_data[billing_details][phone]": str(b.get("phone") or ""),
            "payment_method_data[allow_redisplay]": "always",
            "payment_method_data[type]": "card",
            "payment_method_data[card][number]": str(self.card.get("number") or ""),
            "payment_method_data[card][cvc]": str(self.card.get("cvc") or ""),
            "payment_method_data[card][exp_year]": str(self.card.get("exp_year") or ""),
            "payment_method_data[card][exp_month]": str(self.card.get("exp_month") or ""),
            "payment_method_data[pasted_fields]": "number",
            "payment_method_data[payment_user_agent]": self.device.payment_user_agent(),
            "payment_method_data[referrer]": "https://chatgpt.com",
            "payment_method_data[time_on_page]": str(time_on_page),
            "payment_method_data[guid]": self.device.guid,
            "payment_method_data[muid]": self.device.muid,
            "payment_method_data[sid]": self.device.sid,
            "setup_future_usage": "off_session",
            "set_as_default_payment_method": "false",
            "client_context[currency]": self.currency.lower(),
            "client_context[mode]": "subscription",
            "client_context[payment_method_types][0]": "card",
            "client_context[payment_method_types][1]": "link",
            "client_attribution_metadata[client_session_id]": self.device.stripe_js_id,
            "client_attribution_metadata[merchant_integration_source]": "elements",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "2021",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
            "client_attribution_metadata[elements_session_id]": self.elements_session_id or self.device.elements_session_id,
            "client_attribution_metadata[elements_session_config_id]": self.device.elements_session_config_id,
            "key": self.publishable_key,
            "_stripe_version": STRIPE_API_VERSION,
        }
        for index, name in enumerate(_ADDITIONAL_ELEMENTS):
            form[f"client_attribution_metadata[merchant_integration_additional_elements][{index}]"] = name
            form[f"payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][{index}]"] = name
        for key, value in _ATTRIBUTION.items():
            form[f"payment_method_data[client_attribution_metadata][{key}]"] = value
        form["payment_method_data[client_attribution_metadata][client_session_id]"] = self.device.stripe_js_id
        form["payment_method_data[client_attribution_metadata][elements_session_id]"] = (
            self.elements_session_id or self.device.elements_session_id)
        form["payment_method_data[client_attribution_metadata][elements_session_config_id]"] = (
            self.device.elements_session_config_id)
        if self.customer_id:
            form["client_context[customer]"] = self.customer_id
        if radar:
            form["payment_method_data[radar_options][hcaptcha_token]"] = radar
            if self.telemetry is not None:
                self.telemetry.ctx.hcaptcha_token = radar
        response = self._stripe_session().post(f"{STRIPE_BASE}/v1/confirmation_tokens",
                                               content=urlencode(form), timeout=self.timeout)
        payload: Dict[str, Any] = {}
        if getattr(response, "content", b""):
            try:
                payload = response.json() or {}
            except Exception:  # noqa: BLE001
                payload = {}
        self.raw["ctoken"] = payload
        token = str(payload.get("id") or "")
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400 or not token.startswith("ctoken_"):
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            self.last_error = _stripe_error(payload)
            message = redact_sensitive(str(error.get("message") or "confirmation_tokens 失败"))[:200]
            declined = status == 402 or str(error.get("code") or "") in {
                "card_declined", "expired_card", "incorrect_cvc", "incorrect_number",
            }
            raise DomainError(ErrorCode.PAYMENT_DECLINED if declined else ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                              message)
        self.ctoken = token
        logger.info("plus_har_ctoken %s…", token[:14])
        preview = payload.get("payment_method_preview")
        if isinstance(preview, dict) and str(preview.get("id") or "").startswith("pm_"):
            self.raw["payment_method_id"] = preview["id"]
        return token

    # ── 5. confirm ─────────────────────────────────────────────────
    def _snapshot_web_cookies(self) -> None:
        """Carry checkout/ctoken cookies into the next web connection.

        Checkout responses set cookies (notably __cf_bm / oai-sc) only on the
        HAR web session; ``_web_session`` rebuilds from client.extra_cookies.
        """
        web = self._web
        if web is None:
            return
        cookies = getattr(web, "cookies", None)
        gathered: Dict[str, str] = {}
        if cookies is not None:
            getter = getattr(cookies, "get_dict", None)
            if callable(getter):
                try:
                    gathered.update({str(k): str(v) for k, v in (getter() or {}).items()})
                except Exception:  # noqa: BLE001
                    pass
            if not gathered:
                jar = getattr(cookies, "jar", None)
                if jar is not None:
                    try:
                        for cookie in jar:
                            if getattr(cookie, "name", ""):
                                gathered[str(cookie.name)] = str(cookie.value)
                    except Exception:  # noqa: BLE001
                        pass
            if not gathered and hasattr(cookies, "items"):
                try:
                    gathered.update({str(k): str(v) for k, v in cookies.items()})
                except Exception:  # noqa: BLE001
                    pass
        for name, value in gathered.items():
            if name and value is not None:
                self.client.extra_cookies[name] = value
        token = gathered.get("__Secure-next-auth.session-token")
        if token:
            self.client.session_token = token

    def _reset_web_session(self) -> None:
        """Drop a possibly half-dead checkout TCP/TLS tunnel before confirm.

        Proxy, cookies, device identity and fingerprint stay identical; only the
        connection is re-established so a stale keep-alive cannot hang confirm.
        """
        self._snapshot_web_cookies()
        self._safe_close(self._web)
        self._web = None

    def _reset_stripe_session(self) -> None:
        self._safe_close(self._stripe)
        self._stripe = None

    def _hydrate_confirmed_intent(self) -> None:
        """Resolve a string-only payment_intent without minting another charge."""
        if _intent_object(self.confirmed):
            return
        raw = self.confirmed.get("payment_intent") if isinstance(self.confirmed, dict) else ""
        if not isinstance(raw, str) or not raw.startswith("pi_"):
            return
        started = time.monotonic()
        logger.info("plus_har_intent_retrieve_start intent=%s", raw[:14])
        try:
            retrieved = self.client.retrieve_stripe_intent(
                intent_id=raw,
                client_secret=self.intent_secret,
                publishable_key=self.publishable_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "plus_har_intent_retrieve_failed type=%s elapsed=%.2f",
                type(exc).__name__, time.monotonic() - started,
            )
            retrieved = {}
        if isinstance(retrieved, dict) and retrieved.get("id") == raw:
            self.confirmed["payment_intent"] = retrieved
            self.raw["intent"] = retrieved
            logger.info(
                "plus_har_intent_retrieve_ok intent=%s status=%s elapsed=%.2f",
                raw[:14], str(retrieved.get("status") or "-")[:30],
                time.monotonic() - started,
            )
        else:
            logger.warning(
                "plus_har_intent_retrieve_unusable intent=%s elapsed=%.2f",
                raw[:14], time.monotonic() - started,
            )

    def _app_confirm(self) -> Dict[str, Any]:
        body = {
            "checkout_session_id": self.checkout_id,
            "save_payment_method_for_reuse": True,
            "confirm_token": self.ctoken,
            "selected_payment_method_type": "card",
        }
        self._reset_web_session()
        started = time.monotonic()
        logger.info(
            "plus_har_confirm_start route=/backend-api/payments/checkout/confirm "
            "timeout=%.1f checkout=%s ctoken=%s",
            self.timeout, self.checkout_id[:14], bool(self.ctoken),
        )
        try:
            response = self._app("POST", "/backend-api/payments/checkout/confirm", json_body=body,
                                 telemetry=oai_telemetry_header((time.time() - self.t0) * 1000))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "plus_har_confirm_transport_error elapsed=%.2f type=%s",
                time.monotonic() - started, type(exc).__name__,
            )
            raise
        payload = _json(response)
        status = int(getattr(response, "status_code", 0) or 0)
        logger.info(
            "plus_har_confirm_response status=%s elapsed=%.2f payload_status=%s "
            "next_action=%s error_code=%s",
            status, time.monotonic() - started,
            str(payload.get("status") or "")[:40] if isinstance(payload, dict) else "-",
            str((payload.get("next_action") or {}).get("type") or "-")[:40]
            if isinstance(payload, dict) and isinstance(payload.get("next_action"), dict) else "-",
            str(_stripe_error(payload).get("code") or "-")[:60] if isinstance(payload, dict) else "-",
        )
        self.raw["confirm"] = payload
        if status >= 400:
            self.last_error = _stripe_error(payload)
            message = redact_sensitive(str(payload.get("message") or f"confirm HTTP {status}"))[:200]
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, message)
        self.confirmed = payload
        if str(payload.get("status") or "") == "subscription_limit":
            raise DomainError(
                ErrorCode.SUBSCRIPTION_LIMIT,
                "该账号已达到订阅数量上限（subscription_limit），本次未扣款",
            )
        intent_id, secret, return_url = payment_intent_from_confirm(payload)
        if return_url:
            self.confirm_return_url = return_url
        self.intent_id = intent_id
        self.intent_secret = secret
        self._hydrate_confirmed_intent()
        logger.info("plus_har_confirm status=%s intent=%s", payload.get("status"), intent_id[:12])
        return payload

    def _return_url(self) -> str:
        value = self.confirm_return_url or stripe_verify_url(
            self.checkout_id, processor=self.processor, plan=self.plan)
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.hostname != "chatgpt.com":
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "未取得可信的支付返回地址")
        if "source=" not in value:
            value += ("&" if "?" in value else "?") + "source=chatgpt_web_merged"
        return value

    def _intent_confirm(self) -> Dict[str, Any]:
        if not self.intent_id or not self.intent_secret:
            return {}
        kind = "payment_intents" if self.intent_id.startswith("pi_") else "setup_intents"
        form = {
            "return_url": self._return_url(),
            "confirmation_token": self.ctoken,
            "key": self.publishable_key,
            "_stripe_version": STRIPE_API_VERSION,
            "client_attribution_metadata[client_session_id]": self.device.stripe_js_id,
            "client_attribution_metadata[merchant_integration_source]": "l1",
            "client_secret": self.intent_secret,
        }
        self._reset_stripe_session()
        started = time.monotonic()
        logger.info(
            "plus_har_intent_confirm_start kind=%s intent=%s timeout=%.1f",
            kind, self.intent_id[:14], self.timeout,
        )
        try:
            response = self._stripe_session().post(
                f"{STRIPE_BASE}/v1/{kind}/{self.intent_id}/confirm",
                content=urlencode(form), timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "plus_har_intent_confirm_transport_error elapsed=%.2f type=%s",
                time.monotonic() - started, type(exc).__name__,
            )
            raise
        payload: Dict[str, Any] = {}
        if getattr(response, "content", b""):
            try:
                payload = response.json() or {}
            except Exception:  # noqa: BLE001
                payload = {}
        self.raw["intent"] = payload
        logger.info(
            "plus_har_intent_confirm_response status=%s elapsed=%.2f intent_status=%s "
            "next_action=%s error_code=%s",
            int(getattr(response, "status_code", 0) or 0),
            time.monotonic() - started,
            str(payload.get("status") or "")[:40] if isinstance(payload, dict) else "-",
            str((payload.get("next_action") or {}).get("type") or "-")[:40]
            if isinstance(payload, dict) and isinstance(payload.get("next_action"), dict) else "-",
            str(_stripe_error(payload).get("code") or "-")[:60] if isinstance(payload, dict) else "-",
        )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            self.last_error = _stripe_error(payload)
        return payload

    # ── 6. settle ──────────────────────────────────────────────────
    def _subscription_active(self) -> bool:
        try:
            self._require_credentials()
            payload = self.client.subscription_payload()
        except Exception:  # noqa: BLE001
            return False
        self.plus_entitlement_verified = active_plus_entitlement(payload, self.expected_account or self.client.account_id)
        return self.plus_entitlement_verified

    def _ack_checkout(self) -> bool:
        body = {"stripe_checkout_session_id": self.checkout_id,
                "processor_entity": self.processor}
        try:
            response = self._app("POST", "/backend-api/payments/checkout_session", json_body=body)
            payload = _json(response)
            self.raw["ack"] = payload
            return int(getattr(response, "status_code", 0) or 0) < 400 and payload.get("success") is True
        except Exception:  # noqa: BLE001
            return False

    def _bound_page(self, page: dict) -> bool:
        if self.receipt_expected_minor is None or not self.checkout_id or page.get("checkout_session_id") != self.checkout_id:
            return False
        if page.get("account_id") and page["account_id"] != (self.expected_account or getattr(self.client, "account_id", "")):
            return False
        state = page.get("checkout_state") or {}
        if not isinstance(state, dict):
            return False
        currencies = [str(c).upper() for c in (page.get("currency"), state.get("currency")) if c]
        if not currencies or any(currency != self.currency for currency in currencies):
            return False
        try:
            amount = self._total_minor(page)
        except DomainError:
            return False
        if amount < 0 or amount != self.receipt_expected_minor or (page.get("payment_status") == "no_payment_required" and amount != 0):
            return False
        if any(type(page[k]) is not int or page[k] != amount for k in ("amount_received", "amount_paid") if k in page):
            return False
        if page.get("id") and str(page["id"]).startswith(("cs_", "oaics_")) and page["id"] != self.checkout_id:
            return False
        for pi in (page.get("payment_intent"), self.raw.get("intent")):
            if not isinstance(pi, dict):
                continue
            if pi.get("id") and self.intent_id and pi["id"] != self.intent_id:
                return False
            if pi.get("currency") and str(pi["currency"]).upper() != self.currency:
                return False
            if "amount_received" in pi and (type(pi["amount_received"]) is not int or pi["amount_received"] != amount):
                return False
        referred = page.get("payment_intent_id") or (
            page.get("payment_intent") if isinstance(page.get("payment_intent"), str) else None)
        return not (referred and self.intent_id and referred != self.intent_id)

    def _page_receipt(self, page):
        page_intent, _, _ = payment_intent_from_confirm(page)
        page_intent = page_intent or page.get("payment_intent_id")
        if self.intent_id and page_intent and self.intent_id != page_intent:
            return None
        nested = page.get("payment_intent")
        nested = nested if isinstance(nested, dict) else {}
        received = page.get("amount_paid", page.get("amount_received", nested.get("amount_received")))
        currency = page.get("currency") or (page.get("checkout_state") or {}).get("currency")
        return cash_receipt(self.checkout_id, page_intent, received, currency, self.receipt_expected_minor)

    def _bound_intent(self, intent: dict) -> bool:
        amount = intent.get("amount_received")
        return bool(intent.get("status") == "succeeded" and self.intent_id.startswith("pi_") and self.intent_id == intent.get("id")
                    and type(amount) is int and amount > 0 and amount == self.receipt_expected_minor
                    and str(intent.get("currency") or "").upper() == self.currency)

    def _settle(self) -> PaymentOutcome:
        out = PaymentOutcome()
        out.raw = self.raw
        out.payment_method_id = str(self.raw.get("payment_method_id") or "")
        intent_payload = self.raw.get("intent") if isinstance(self.raw.get("intent"), dict) else {}
        confirm_payload = self.raw.get("confirm") if isinstance(self.raw.get("confirm"), dict) else {}
        if str(confirm_payload.get("status") or "") == "subscription_limit" and not str(
                confirm_payload.get("payment_intent_id") or confirm_payload.get("payment_intent") or ""):
            out.state = "rejected"
            out.code = "subscription_limit"
            out.message = "该账号已达到订阅数量上限（subscription_limit），本次未扣款"
            return out
        intent_status = str(intent_payload.get("status") or "")
        out.intent_id = str(intent_payload.get("id") or self.intent_id or "")
        error = intent_payload.get("last_payment_error") if isinstance(intent_payload.get("last_payment_error"), dict) else {}
        if not error and isinstance(intent_payload.get("error"), dict):
            error = intent_payload["error"]
        pi_obj = error.get("payment_intent") if isinstance(error.get("payment_intent"), dict) else {}
        reported_id = str((pi_obj or intent_payload).get("id") or "")
        if reported_id and self.intent_id and reported_id != self.intent_id:
            out.message = "无法将回执关联到原付款，请核对原结果"
            return out
        if pi_obj:
            intent_status = str(pi_obj.get("status") or intent_status)
            out.intent_id = reported_id or out.intent_id
        if error:
            out.code = str(error.get("code") or "")
            out.decline_code = str(error.get("decline_code") or "")
            out.message = redact_sensitive(str(error.get("message") or "Stripe 拒绝本次付款"))[:240]
            out.intent_status = intent_status or None
            last_error = pi_obj.get("last_payment_error") if isinstance(pi_obj.get("last_payment_error"), dict) else {}
            charge = str(error.get("charge") or last_error.get("charge") or "")
            if charge:
                out.charge_id = charge
            pm_obj = error.get("payment_method") or last_error.get("payment_method")
            pm_id = str(pm_obj.get("id") or "") if isinstance(pm_obj, dict) else str(pm_obj or "")
            if pm_id.startswith("pm_"):
                out.payment_method_id = pm_id
            amount = pi_obj.get("amount")
            if (isinstance(amount, int) and not isinstance(amount, bool)
                    and self.amount_minor and amount != self.amount_minor):
                out.state = "unknown"
                out.message = f"Intent 金额 {amount} 与结账应付额 {self.amount_minor} 不一致；{out.message}"
                return out
            _classify_unsuccessful_intent(out)
            return out
        if self.last_error:
            out.code = self.last_error.get("code") or ""
            out.decline_code = self.last_error.get("decline_code") or ""
            out.message = self.last_error.get("message") or ""
            out.intent_status = intent_status or None
            _classify_unsuccessful_intent(out)
            return out

        page: Dict[str, Any] = {}
        try:
            page = self._read_checkout()
        except Exception:  # noqa: BLE001
            page = {}
        self.stage = "payment"
        status = str(page.get("status") or "")
        payment_status = str(page.get("payment_status") or "")
        logger.info(
            "plus_har_settle status=%s payment_status=%s intent_status=%s",
            status or "-", payment_status or "-", intent_status or "-",
        )

        # PaymentIntent success is the authoritative financial fact. Page
        # echoes are secondary; missing amount_received/id must not turn a
        # successful charge into an unknown result.
        if intent_status == "succeeded":
            bound = self._bound_intent(intent_payload)
            if bound:
                self.receipt_evidence = cash_receipt(
                    self.checkout_id, self.intent_id, intent_payload["amount_received"],
                    intent_payload["currency"], self.receipt_expected_minor,
                )
            else:
                logger.warning(
                    "plus_har_intent_succeeded_unbound intent=%s expected=%s",
                    self.intent_id[:14], self.receipt_expected_minor,
                )
            # A successful intent still gets the normal checkout acknowledgement
            # when the page is already complete; never block on secondary fields.
            if not self.read_only and status == "complete" and payment_status in {"paid", "no_payment_required"}:
                self._ack_checkout()
            if self._subscription_active():
                out.state = "paid"
                out.message = "Plus 订阅已生效"
            else:
                out.state = "pending"
                out.message = "付款已确认，等待 Plus 订阅生效"
            out.intent_status = "succeeded"
            return out
        if status == "complete" and payment_status in {"paid", "no_payment_required"}:
            if not self._bound_page(page):
                out.message = "回执与原结账或报价未匹配，不能确认扣款"
                return out
            if page.get("payment_status") == "no_payment_required" and self.receipt_expected_minor == 0:
                out.state, out.message = "no_payment_required", "本单无需扣款"
                return out
            self.receipt_evidence = self._page_receipt(page)
            if self.receipt_evidence is None and self._bound_intent(intent_payload):
                self.receipt_evidence = cash_receipt(self.checkout_id, self.intent_id, intent_payload["amount_received"],
                                                    intent_payload["currency"], self.receipt_expected_minor)
            if self.receipt_evidence is None:
                out.message = "缺少可对应原订单的实收回执"
                return out
            out.intent_id = self.receipt_evidence.payment_id
            if not self.read_only:
                self._ack_checkout()
            if self._subscription_active():
                out.state = "paid"
                out.intent_status = intent_status or "succeeded"
                out.message = "Plus 订阅已生效"
            else:
                out.state = "pending"
                out.intent_status = intent_status or "succeeded"
                out.message = "付款已确认，等待 Plus 订阅生效"
            return out
        if intent_status == "succeeded":
            if not self._bound_intent(intent_payload):
                out.message = "实收凭据未与本单金额及币种匹配"
                return out
            self.receipt_evidence = cash_receipt(self.checkout_id, self.intent_id, intent_payload["amount_received"],
                                                intent_payload["currency"], self.receipt_expected_minor)
            out.state = "pending"
            out.intent_status = intent_status
            out.message = "付款已确认，等待结账状态更新"
            return out
        if intent_status in {"requires_payment_method", "canceled", "cancelled"}:
            out.state = "card_declined"
            out.intent_status = intent_status
            out.message = out.message or "Stripe 未完成本次付款"
            return out
        if intent_status == "requires_action":
            out.state = "challenge_required"
            out.intent_status = intent_status
            out.message = "Stripe 要求继续验证，纯协议停在 requires_action"
            logger.info("plus_har_settle_requires_action intent=%s", self.intent_id[:14])
            return out
        out.state = "unknown"
        out.intent_status = intent_status or None
        out.message = out.message or f"未预期的结案状态：{status or intent_status or '空'}"
        return out

    # ── main ───────────────────────────────────────────────────────
    def pay(self) -> PaymentOutcome:
        if not self.checkout_url and not self.checkout_id:
            raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR, "缺少结账会话")
        self._require_credentials()
        if self.read_only or self.confirmation_started:
            # Never mint a new checkout or send any confirmation during reconciliation.
            if not self.checkout_id:
                return PaymentOutcome(state="unknown", message="缺少原结账标识，请核对原付款")
            self.read_only = True
            self._prepare()
            self._read_checkout()
            return self._read_only_settle()
        self._prepare()
        self._prelude()
        self._sentinel("chatgpt_checkout")

        if self.checkout_id and self.session_reissued:
            # Never reuse an open session minted for another exit/currency.
            # Same-region attempts keep the bound session; cross-region and
            # OAuth takeovers must not carry the old checkout across.
            self._discard_stale_checkout()
        if not self.checkout_id:
            self._create_checkout()
        payload = self._read_checkout()
        # capture entries 41/42: the checkout page re-mints the chatgpt_checkout
        # sentinel right after reading the session, before Elements boots.
        self._sentinel("chatgpt_checkout")
        page_status, payment_status = payload.get("status"), payload.get("payment_status")
        if (page_status != "open" or payment_status != "unpaid"
                or payload.get("payment_intent") or payload.get("payment_intent_id")):
            # No fresh financial write when the merchant reports a prior attempt.
            self.confirmation_started = True
            self.on_confirmation_start()
            self.read_only = True
            self.stage = "payment"
            if page_status != "complete":
                return self._read_only_settle()
            out = self._settle()
            if out.state == "unknown":
                out.state = "pending"
                out.message = "原结账已完成，正在核对订阅"
            return out
        check_observed_billing(payload, self.billing, alternate_countries=(self.exit_country,))
        self.amount_minor = self._total_minor(payload)
        logger.info("plus_har_checkout id=%s status=%s total=%s %s",
                    self.checkout_id, payload.get("status"), self.amount_minor, self.currency)
        if self.telemetry is not None:
            self.telemetry.ctx.amount = self.amount_minor
        self._elements_session()
        if self.mode != "off":
            # SDK/iframe resources first, then the passive token, matching the
            # capture's Elements -> hCaptcha -> events order.
            self.telemetry.warm_assets()
            self._radar_token()
        self._flush(PHASE_ELEMENTS)
        self._consumers_lookup("web_payment_element")
        self._consumers_lookup("web_link_authentication_in_payment_element")
        if self.mode != "off":
            self.telemetry.register()
        self._consumers_lookup("web_elements_controller")
        self._flush(PHASE_CAPTCHA)
        self._card_metadata()
        self._flush(PHASE_CARD_ENTRY)

        self._taxes()
        self._flush(PHASE_TAXES)
        stopped = self._quote_guard()
        if stopped is not None:
            return stopped
        if self.telemetry is not None:
            # Events captured after the tax step carry the taxed amount.
            self.telemetry.ctx.amount = self.amount_minor
        self._elements_session()
        self._update_tax_id()
        self._flush(PHASE_TAX_ID)

        self.stage = "setup"
        self._confirmation_token()
        self._flush(PHASE_TOKEN)
        self.stage = "payment"

        self._sentinel("checkout_session_approval")
        self._sentinel("chatgpt_checkout")
        # Set the fence before I/O: a lost reply cannot prove the request was not applied.
        self._require_credentials()
        if self.receipt_expected_minor is None:
            self.receipt_expected_minor = self.amount_minor
        self.confirmation_started = True
        self.on_confirmation_start()
        self._app_confirm()
        observed = _intent_object(self.confirmed)
        if not observed and self.intent_id.startswith("pi_") and self.intent_secret:
            # Some app responses expose only the string id. The GET above
            # failed, but the client secret still authorizes the original
            # confirmation; never invent a new intent id.
            observed = {"id": self.intent_id, "status": "requires_confirmation"}
        # A successful application response, an intent ID or a secret alone is
        # not permission to confirm again. Only the original, unsubmitted PI
        # explicitly requiring confirmation may take the existing initial step.
        if (observed.get("status") != "requires_confirmation"
                or not self.intent_id.startswith("pi_") or observed.get("id") != self.intent_id
                or not self.intent_secret.startswith(self.intent_id + "_secret_")
                or self.confirmed.get("payment_status") == "paid"
                or self.confirmed.get("status") in {"complete", "completed", "processing", "succeeded", "requires_action"}):
            self.raw["intent"] = observed
            self.read_only = True
            return self._timed_settle()
        self._sentinel("checkout_session_approval")
        self._require_credentials()
        self._intent_confirm()
        self._flush(PHASE_CONFIRM)

        out = self._timed_settle()
        logger.info("plus_har_result state=%s intent=%s decline=%s",
                    out.state, out.intent_status, out.decline_code)
        if self.mode != "off" and self.telemetry is not None:
            self.telemetry.beat(2)
        return out

    def _timed_settle(self) -> PaymentOutcome:
        started = time.monotonic()
        try:
            return self._settle()
        finally:
            logger.info("plus_har_settle_elapsed elapsed=%.2f", time.monotonic() - started)

    def _quote_guard(self) -> Optional[PaymentOutcome]:
        """有明确订单金额时，任何报价变化都需先确认，不以变化后的金额作为原收据依据。

        HAR 里这是 UI 自动刷新金额后由人再点一次；纯协议没有这一步，所以这里
        主动返回 pending/checkout，前端已经有 `quoteChanged` 分支接这个状态。
        无订单快照的旧调用保留单向报价上限。
        """
        if self.receipt_expected_minor is not None:
            if self.amount_minor == self.receipt_expected_minor:
                return None
        elif not self.expected_amount_minor or self.amount_minor <= self.expected_amount_minor:
            return None
        self.stage = "checkout"
        out = PaymentOutcome()
        out.state = "pending"
        out.message = "账单税费已更新，请确认当前应付金额后再提交付款"
        out.raw = self.raw
        logger.info("plus_har_quote_changed expected=%s actual=%s",
                    self.expected_amount_minor, self.amount_minor)
        return out

    def _read_only_settle(self) -> PaymentOutcome:
        """Status query: read the checkout (and the intent when known), never pay."""
        out = PaymentOutcome()
        out.raw = self.raw
        page = self.raw.get("checkout") if isinstance(self.raw.get("checkout"), dict) else {}
        status = str(page.get("status") or "")
        payment_status = str(page.get("payment_status") or "")
        if status == "complete" and payment_status in {"paid", "no_payment_required"}:
            self.stage = "payment"
            if not self._bound_page(page):
                out.message = "回执与原结账或报价未匹配，不能确认扣款"
                return out
            if page.get("payment_status") == "no_payment_required" and self.receipt_expected_minor == 0:
                out.state, out.message = "no_payment_required", "本单无需扣款"
                return out
            self.receipt_evidence = self._page_receipt(page)
            if self.receipt_evidence is None:
                out.message = "缺少可对应原订单的实收回执"
                return out
            out.intent_id = self.receipt_evidence.payment_id
            out.state = "paid" if self._subscription_active() else "pending"
            out.intent_status = "succeeded"
            out.message = "原结账已完成" if out.state == "paid" else "付款已确认，等待订阅生效"
            return out
        if self.intent_id and self.intent_secret:
            self.stage = "payment"
            out.intent_id = self.intent_id
            try:
                self._require_credentials()
                intent = self.client.retrieve_stripe_intent(
                    intent_id=self.intent_id, client_secret=self.intent_secret,
                    publishable_key=self.publishable_key)
            except Exception:  # noqa: BLE001
                intent = {}
            matches = self.intent_id.startswith("pi_") and intent.get("id") == self.intent_id
            state = str(intent.get("status") or "") if matches else ""
            if state == "succeeded":
                if not self._bound_intent(intent):
                    out.message = "实收凭据未与本单金额及币种匹配"
                    return out
                self.receipt_evidence = cash_receipt(self.checkout_id, self.intent_id, intent["amount_received"],
                                                    intent["currency"], self.receipt_expected_minor)
                out.state = "pending"
                out.intent_status = state
                out.message = "付款已确认，等待结账状态更新"
                return out
            if state in _UNSETTLED_INTENT_STATES:
                error = intent.get("last_payment_error")
                if isinstance(error, dict):
                    out.code = str(error.get("code") or "") or None
                    out.decline_code = str(error.get("decline_code") or "") or None
                out.intent_status = state
                _classify_unsuccessful_intent(out)
                out.message = ("Stripe 拒绝本次付款" if out.state == "card_declined"
                               else "原付款结果待核对，请勿重复支付")
                return out
            out.state = "pending" if state in {"processing", "requires_capture"} else "unknown"
            out.intent_status = state or None
            out.message = "原付款结果待核对，请勿重复支付"
            return out
        if self.confirmation_started:
            self.stage = "payment"
            out.state = "unknown"
            out.message = "原付款结果待核对，请勿重复支付"
        else:
            out.state = "pending"
            out.message = "原结账尚未提交付款"
        return out

    def _flush(self, phase: int) -> None:
        if self.mode == "off" or self.telemetry is None:
            return
        try:
            self.telemetry.emit_through(phase)
        except Exception:  # noqa: BLE001
            pass

    def pay_from_url(self, retries: int = 3) -> PaymentOutcome:
        """Run a single attempt; a transport exception must never replay a payment."""
        try:
            return self.pay()
        finally:
            # The exit is leased for this attempt only. A risk rejection has
            # already moved it into cooldown, which also drops the lease.
            if self.proxy and self.purchase_attempt_id:
                release_sid_lease(self.purchase_attempt_id, role="checkout")



def outcome_from_payment(result: PaymentOutcome, *, stage: str = "payment") -> Dict[str, Any]:
    """Map the engine result onto the dict contract the API routers already use."""
    payload = outcome(result.state, stage, result.message or "", intent_status=result.intent_status)
    if result.code:
        payload["code"] = result.code
    if result.decline_code:
        payload["decline_code"] = result.decline_code
    if result.charge_id:
        payload["charge_id"] = result.charge_id
    if result.payment_method_id:
        payload["payment_method_id"] = result.payment_method_id
    if result.intent_id:
        payload["payment_intent_id"] = result.intent_id
    return payload


__all__ = ["PlusHarProtocol", "outcome_from_payment", "run_har_context", "har_engine_enabled"]


def run_har_context(
    client: ChatGPTClient,
    context: Dict[str, Any],
    spec: Dict[str, Any],
    *,
    card: Optional[Dict[str, str]] = None,
    billing: Optional[Dict[str, str]] = None,
    read_only: bool = False,
    exit_country: str = "",
    browser_context: Any = None,
    purchase_attempt_id: str = "",
) -> Dict[str, Any]:
    """Drive one HAR-faithful attempt on an existing checkout context.

    Returns the dict contract (`state` / `stage` / `message` / ...) that the
    `bind-and-pay-protocol` router already speaks, and keeps the device plus the
    Stripe ids in `context` so a follow-up status query uses the same device.
    """
    try:
        if not spec.get("credentials_current", lambda: True)():
            raise DomainError(ErrorCode.SESSION_EXPIRED, "订单凭证已过期")
        restore_account(client, str(spec.get("account_id") or ""), credentials_current=spec.get("credentials_current"))
    except DomainError as exc:
        payload = outcome("unknown", "checkout", redact_sensitive(exc.message)[:240],
                          code=str(exc.code))
        payload["stage"] = "checkout"
        return payload
    except Exception as exc:  # noqa: BLE001
        payload = outcome("unknown", "checkout", redact_sensitive(str(exc))[:240])
        payload["stage"] = "checkout"
        return payload
    read_only = read_only or bool(context.get("app_confirmation_started") or context.get("intent_id"))
    if client.account_id:
        context["account_id"] = client.account_id
    device = None
    stored = context.get("har_device")
    if isinstance(stored, dict) and stored:
        device = DeviceProfile.from_dict(stored)
        if exit_country:
            device.align_exit(exit_country)
    currency = str(spec.get("currency") or "USD").upper()
    country = str(spec.get("country") or (billing or {}).get("country") or "US").upper()
    engine = PlusHarProtocol(
        client=client,
        session="",
        checkout_url=str(spec.get("checkout_url") or ""),
        checkout_id=str(context.get("checkout_id") or spec.get("checkout_id") or ""),
        publishable_key=str(spec.get("publishable_key") or ""),
        card={} if read_only else (card or {}),
        billing={} if read_only else (billing or {}),
        proxy=getattr(client, "proxy", None),
        currency=currency,
        customer_session_client_secret=str(spec.get("customer_session_client_secret") or ""),
        email=str((billing or {}).get("email") or ""),
        country=country,
        exit_country=exit_country,
        account_id=str(client.account_id or spec.get("account_id") or ""),
        processor=str(spec.get("processor") or ""),
        confirm_return_url=str(spec.get("confirm_return_url") or ""),
        device=device,
        read_only=read_only,
        user_agent=str(getattr(client, "user_agent", "") or ""),
        device_id=str(getattr(client, "device_id", "") or ""),
        hcaptcha_token=str(context.get("hcaptcha_token") or ""),
        browser_context=browser_context or context.get("browser_context"),
        purchase_attempt_id=purchase_attempt_id or str(context.get("purchase_attempt_id") or ""),
    )
    engine.credentials_current = spec.get("credentials_current", lambda: True)
    engine.confirmation_started = bool(context.get("app_confirmation_started") or context.get("intent_id"))
    engine.on_confirmation_start = lambda: context.update(app_confirmation_started=True)
    if context.get("intent_id"):
        engine.intent_id = str(context["intent_id"])
        engine.intent_secret = str(context.get("intent_secret") or "")
    if context.get("confirmation_token"):
        engine.ctoken = str(context["confirmation_token"])
    expected = spec.get("expected_amount") if spec.get("expected_amount") is not None else spec.get("amount")
    if expected is not None:
        quoted = Decimal(str(expected)) * _factor(currency)
        if quoted.is_finite() and quoted >= 0 and quoted == quoted.to_integral_value():
            engine.receipt_expected_minor = int(quoted)
    if context.get("receipt_expected_minor") is not None:
        engine.receipt_expected_minor = context["receipt_expected_minor"]
    if context.get("amount_minor") is not None:
        engine.amount_minor = int(context["amount_minor"])
        engine.expected_amount_minor = engine.amount_minor
    try:
        result = engine.pay()
    except ObservedBillingConflict as exc:
        return outcome("integration_error", "checkout", exc.message, code=str(exc.code))
    except DomainError as exc:
        code = str(exc.code)
        if code == str(ErrorCode.SUBSCRIPTION_LIMIT):
            out_state = "rejected"
        elif code == str(ErrorCode.SENTINEL_CHALLENGE_FAILED):
            out_state = "failed"
        else:
            out_state = "card_declined" if code == str(ErrorCode.PAYMENT_DECLINED) else "unknown"
        payload = outcome(out_state, engine.stage,
                          redact_sensitive(exc.message)[:240], code=code)
        payload["stage"] = engine.stage
        return payload
    finally:
        context["har_device"] = engine.device.to_dict()
        if engine.ctoken:
            context["confirmation_token"] = engine.ctoken
        if engine.intent_id:
            context["intent_id"] = engine.intent_id
            context["intent_secret"] = engine.intent_secret
        if engine.amount_minor:
            context["amount_minor"] = engine.amount_minor
            # 税费后的应付额回写给订单视图（HAR 里 UI 也是按税后金额收款）
            context["quoted"] = True
            context["amount"] = Decimal(engine.amount_minor) / _factor(currency)
            context["currency"] = currency
        if engine.customer_id:
            context["customer_id"] = engine.customer_id
        pm = (engine.raw or {}).get("payment_method_id")
        if pm:
            context["payment_method_id"] = pm
        context["har_stage"] = engine.stage
        if engine.confirmation_started or engine.confirmed:
            context["app_confirmation_started"] = True
            if engine.receipt_expected_minor is not None:
                context.setdefault("receipt_expected_minor", engine.receipt_expected_minor)
        engine.close()
    payload = outcome_from_payment(result, stage=engine.stage)
    payload["_receipt_evidence"] = engine.receipt_evidence
    payload["_plus_verified"] = engine.plus_entitlement_verified
    payload["_verified_zero"] = result.state == "no_payment_required" and engine.receipt_expected_minor == 0
    payload["stage"] = engine.stage
    return payload


def har_engine_enabled() -> bool:
    """`GSS_PLUS_ENGINE=legacy`（或策略开关面板选择 legacy）恢复旧版 checkout_v2。"""
    from gss_core import runtime_flags

    return runtime_flags.get_str("plus_engine") != "legacy"


class _RecordingSession:
    """Transparent wrapper that dumps every Stripe/ChatGPT exchange.

    Enabled with `GSS_HAR_CAPTURE=/some/dir`; one JSON line per request with the
    status, the form/JSON body that went out and the body that came back.  Card
    data, CVC, emails and bearer/sentinel tokens are masked before writing.
    """

    def __init__(self, inner: Any, sink: Any, label: str) -> None:
        self._inner = inner
        self._sink = sink
        self._label = label

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _dump(self, method: str, url: str, kwargs: Dict[str, Any], response: Any) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            body = redact_sensitive(str(getattr(response, "text", "") or "")[:4000])
            logger.warning(
                "upstream_error where=har_%s method=%s status=%s url=%s body=%s",
                self._label, method, status, url, body,
            )
        if self._sink is None:
            return
        body = kwargs.get("content") or kwargs.get("data") or kwargs.get("json") or ""
        if not isinstance(body, (str, bytes)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        text = ""
        try:
            text = str(getattr(response, "text", "") or "")
        except Exception:  # noqa: BLE001
            text = ""
        try:
            self._sink(self._label, method, url, int(getattr(response, "status_code", 0) or 0),
                       _mask_payload(body), _mask_payload(text))
        except Exception:  # noqa: BLE001
            pass

    def get(self, url: str, **kwargs: Any) -> Any:
        response = self._inner.get(url, **kwargs)
        self._dump("GET", url, kwargs, response)
        return response

    def post(self, url: str, **kwargs: Any) -> Any:
        response = self._inner.post(url, **kwargs)
        self._dump("POST", url, kwargs, response)
        return response

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self._inner.request(method, url, **kwargs)
        self._dump(method, url, kwargs, response)
        return response


_MASK_PATTERNS = (
    (re.compile(r"(card\[number\]=)[^&]*"), r"\1<pan>"),
    (re.compile(r"(card%5Bnumber%5D=)[^&]*"), r"\1<pan>"),
    (re.compile(r"(card\[cvc\]=)[^&]*"), r"\1<cvc>"),
    (re.compile(r"(card%5Bcvc%5D=)[^&]*"), r"\1<cvc>"),
    (re.compile(r"(\"number\"\s*:\s*\")[^\"]*"), r"\1<pan>"),
    (re.compile(r"(\"cvc\"\s*:\s*\")[^\"]*"), r"\1<cvc>"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"), r"\1<token>"),
    (re.compile(r"([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})"), r"<masked>@\2"),
)


def _mask_payload(text: str) -> str:
    if not text:
        return text
    out = text
    for pattern, replacement in _MASK_PATTERNS:
        out = pattern.sub(replacement, out)
    return out[:200000]


def _capture_sink():
    target = os.environ.get("GSS_HAR_CAPTURE", "").strip()
    if not target:
        return None
    from pathlib import Path

    path = Path(target)
    if path.suffix.lower() in {".jsonl", ".log"}:
        path.parent.mkdir(parents=True, exist_ok=True)
        file = path.open("a", encoding="utf-8")

        def write(label: str, method: str, url: str, status: int, body: str, response: str) -> None:
            file.write(json.dumps({
                "ts": round(time.time(), 3), "label": label, "method": method,
                "url": url, "status": status, "request_body": body, "response_body": response,
            }, ensure_ascii=False) + "\n")
            file.flush()

        return write
    path.mkdir(parents=True, exist_ok=True)

    def write_dir(label: str, method: str, url: str, status: int, body: str, response: str) -> None:
        name = re.sub(r"[^A-Za-z0-9]+", "_", f"{label}_{method}_{url.split('?')[0]}")[:120]
        stamp = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        (path / f"{stamp}_{name}.json").write_text(json.dumps({
            "ts": round(time.time(), 3), "label": label, "method": method, "url": url,
            "status": status, "request_body": body, "response_body": response,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    return write_dir
