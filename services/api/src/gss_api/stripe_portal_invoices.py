"""Read-only invoice history extraction from a Stripe Customer Portal session.

ChatGPT personal accounts (Plus / Go / Pro) host historical billing records in
Stripe Customer Portal (``GET /backend-api/payments/customer_portal`` ->
``https://billing.stripe.com/p/session/...``) rather than ``/backend-api/payments/invoices``.

Extraction strategy (strictly read-only, zero credential sharing with Stripe):
  1. Pure-HTTP fetch via ``curl_cffi`` (same proxy, Chrome TLS, no ChatGPT headers/cookies):
     inspects JSON responses or embedded HTML bootstrap JSON blobs.
  2. Headless browser fallback via Playwright (bundled Chromium or local Chrome):
     intercepts Stripe Customer Portal XHR JSON responses containing invoice objects,
     and falls back to reading rendered invoice rows from the DOM.

Never logs portal URLs, customer emails, card details, or raw invoice text.
"""
from __future__ import annotations

from datetime import datetime, timezone
import html as html_lib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from gss_core.chatgpt import _new_session

logger = logging.getLogger("gss.account_detail")

_ALLOWED_INVOICE_HOSTS = ("stripe.com", "pay.openai.com", "billing.openai.com", "invoice.openai.com")
_PORTAL_XHR_HOSTS = ("stripe.com", "openai.com", "chatgpt.com")
_PORTAL_PATH_HINTS = ("/billing", "/portal", "/account", "/p/session", "/payments")
_CURRENCY_SYMBOLS = {
    "US$": "USD",
    "CA$": "CAD",
    "A$": "AUD",
    "AU$": "AUD",
    "NZ$": "NZD",
    "HK$": "HKD",
    "S$": "SGD",
    "NT$": "TWD",
    "R$": "BRL",
    "MX$": "MXN",
    "₱": "PHP",
    "¥": "JPY",
    "￥": "JPY",
    "€": "EUR",
    "£": "GBP",
    "₩": "KRW",
    "₹": "INR",
    "₫": "VND",
    "฿": "THB",
    "₺": "TRY",
    "$": "USD",
}
_MONEY_RE = re.compile(
    r"(US\$|CA\$|AU\$|A\$|NZ\$|HK\$|S\$|NT\$|R\$|MX\$|[₱¥￥€£₩₹₫฿₺$])"
    r"\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
    r"|([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*([A-Z]{3})\b"
    r"|\b([A-Z]{3})\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
)
_MONTH_DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b"
    r"|\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{4}\b"
    r"|\b\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?\b",
    re.IGNORECASE,
)


def _host_matches(host: str, domains: Tuple[str, ...]) -> bool:
    return any(host == dom or host.endswith("." + dom) for dom in domains)


def _safe_path_prefix(path: str) -> str:
    parts: List[str] = []
    for seg in [s for s in str(path or "").split("/") if s][:3]:
        if len(seg) <= 24 and re.fullmatch(r"[A-Za-z0-9_-]+", seg) and not re.search(r"[0-9]{4,}|live_|test_|cs_|in_|bps_", seg):
            parts.append(seg)
        else:
            parts.append("<id>")
    return "/" + "/".join(parts) if parts else "/"


def safe_invoice_url(url: Any) -> Optional[str]:
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text.startswith("https://") or len(text) > 2048:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if _host_matches(host, _ALLOWED_INVOICE_HOSTS):
        return text
    return None


def _safe_portal_entry_url(url: Any) -> Optional[str]:
    direct = safe_invoice_url(url)
    if direct:
        return direct
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text.startswith("https://") or len(text) > 2048:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if _host_matches(host, ("chatgpt.com", "openai.com")) and any(
        part in parsed.path for part in _PORTAL_PATH_HINTS
    ):
        return text
    return None


def _looks_like_invoice_obj(item: Dict[str, Any]) -> bool:
    obj_type = str(item.get("object") or "").strip().lower()
    inv_id = str(item.get("id") or "").strip()
    status = str(item.get("status") or "").strip().lower()
    has_amount = any(
        item.get(k) is not None
        for k in ("amount_paid", "amount_due", "total", "subtotal", "amount", "total_amount")
    )
    has_currency = bool(item.get("currency") or item.get("currency_code"))
    has_time_or_link = bool(
        item.get("created") or item.get("effective_at") or item.get("paid_at")
        or item.get("hosted_invoice_url") or item.get("invoice_pdf")
    )
    # Skip unbilled upcoming/draft previews that lack an invoice id and payment timestamp.
    if status == "draft" and not item.get("paid_at") and not inv_id.startswith("in_"):
        return False
    if obj_type == "invoice" or inv_id.startswith("in_"):
        return bool(has_amount or has_time_or_link)
    has_status = status in {
        "paid", "open", "void", "voided", "uncollectible", "refunded", "succeeded", "failed",
    }
    return bool(has_amount and has_currency and (has_time_or_link or has_status))


def _line_description(item: Dict[str, Any]) -> str:
    for key in ("plan_name", "plan", "subscription_plan", "product_name", "description"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    lines = item.get("lines")
    line_items: List[Any] = []
    if isinstance(lines, dict) and isinstance(lines.get("data"), list):
        line_items = lines["data"]
    elif isinstance(lines, list):
        line_items = lines
    for line in line_items:
        if not isinstance(line, dict):
            continue
        for key in ("description", "plan_name", "product_name"):
            val = line.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        price = line.get("price") or line.get("plan")
        if isinstance(price, dict):
            for key in ("nickname", "name"):
                val = price.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            prod = price.get("product")
            if isinstance(prod, dict) and isinstance(prod.get("name"), str) and prod["name"].strip():
                return prod["name"].strip()
    return ""


def _normalize_stripe_invoice(item: Dict[str, Any]) -> Dict[str, Any]:
    transitions = item.get("status_transitions") if isinstance(item.get("status_transitions"), dict) else {}
    paid_at = (
        transitions.get("paid_at")
        or item.get("paid_at")
        or item.get("effective_at")
        or item.get("created")
        or item.get("date")
        or item.get("invoice_date")
        or item.get("created_at")
    )
    amount = item.get("amount_paid")
    if amount is None:
        amount = item.get("total")
    if amount is None:
        amount = item.get("amount_due")
    if amount is None:
        amount = item.get("amount")
    currency = str(item.get("currency") or item.get("currency_code") or "").strip().upper()
    status = str(item.get("status") or item.get("payment_status") or "").strip().lower()
    if not status and (item.get("paid") is True or transitions.get("paid_at") or item.get("paid_at")):
        status = "paid"
    url = safe_invoice_url(item.get("hosted_invoice_url") or item.get("invoice_pdf") or item.get("url"))
    out: Dict[str, Any] = {
        "id": str(item.get("id") or ""),
        "paid_at": paid_at,
        "amount_paid": amount,
        "amount_unit": "minor",
        "currency": currency,
        "status": status,
        "description": _line_description(item),
    }
    if url:
        out["invoice_url"] = url
    return out


def extract_invoices_from_json(payload: Any, *, depth: int = 0) -> List[Dict[str, Any]]:
    """Recursively locate Stripe invoice objects anywhere inside a JSON structure."""
    if depth > 9 or payload is None:
        return []
    found: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        for elem in payload[:100]:
            if isinstance(elem, dict) and _looks_like_invoice_obj(elem):
                found.append(_normalize_stripe_invoice(elem))
            elif isinstance(elem, (dict, list)):
                found.extend(extract_invoices_from_json(elem, depth=depth + 1))
        return found
    if isinstance(payload, dict):
        if _looks_like_invoice_obj(payload):
            return [_normalize_stripe_invoice(payload)]
        # Prioritize likely invoice containers first.
        priority = (
            "invoices", "invoice_history", "billing_history", "past_invoices",
            "customer_invoices", "data", "items", "results",
        )
        visited = set()
        for key in priority:
            if key in payload:
                visited.add(key)
                found.extend(extract_invoices_from_json(payload[key], depth=depth + 1))
        for key, value in payload.items():
            if key in visited:
                continue
            if isinstance(value, (dict, list)):
                found.extend(extract_invoices_from_json(value, depth=depth + 1))
    return found


def _dedupe_raw_invoices(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    unique: List[Dict[str, Any]] = []
    for item in items:
        inv_id = str(item.get("id") or "").strip()
        url = str(item.get("invoice_url") or "").strip()
        sig = (
            ("id", inv_id)
            if inv_id.startswith("in_")
            else ("url", url)
            if url
            else ("fields", str(item.get("paid_at")), str(item.get("amount_paid")), str(item.get("currency")))
        )
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(item)
    return unique


def extract_invoices_from_html(html_text: str) -> List[Dict[str, Any]]:
    """Parse Stripe invoice JSON blobs embedded in portal HTML responses."""
    if not html_text:
        return []
    candidates: List[Dict[str, Any]] = []

    # 1) <script type="application/json"...>{...}</script>
    for match in re.finditer(
        r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        raw = html_lib.unescape(match.group(1)).strip()
        if not raw or len(raw) > 3_000_000:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        candidates.extend(extract_invoices_from_json(parsed))

    # 2) data-initial-data="..." / data-state="..." attributes
    for match in re.finditer(
        r'data-(?:initial-data|bootstrap|state|portal-session)=["\']([^"\']{20,})["\']',
        html_text,
        re.IGNORECASE,
    ):
        raw = html_lib.unescape(match.group(1)).strip()
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        candidates.extend(extract_invoices_from_json(parsed))

    # 3) window.__INITIAL_STATE__ = {...} / __NEXT_DATA__ / similar JSON assignments
    for match in re.finditer(
        r'(?:__INITIAL_STATE__|__BOOTSTRAP__|__PORTAL_STATE__|initialData|portalSession)\s*=\s*(\{.*?\})\s*;',
        html_text,
        re.DOTALL,
    ):
        raw = match.group(1).strip()
        if len(raw) > 3_000_000:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        candidates.extend(extract_invoices_from_json(parsed))

    return _dedupe_raw_invoices(candidates)


def _parse_dom_date(text: str) -> Optional[str]:
    match = _MONTH_DATE_RE.search(text or "")
    if not match:
        return None
    raw = match.group(0).strip()
    # Chinese YYYY年MM月DD日
    cn = re.fullmatch(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", raw)
    if cn:
        try:
            dt = datetime(int(cn.group(1)), int(cn.group(2)), int(cn.group(3)), tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            return None
    normalized = raw.replace("Sept ", "Sep ")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            dt = datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return None


def _parse_dom_money(text: str) -> Tuple[Optional[str], str]:
    match = _MONEY_RE.search(text or "")
    if not match:
        return None, ""
    if match.group(1) and match.group(2):
        sym = match.group(1)
        amt = match.group(2).replace(",", "")
        return amt, _CURRENCY_SYMBOLS.get(sym, "")
    if match.group(3) and match.group(4):
        return match.group(3).replace(",", ""), match.group(4).upper()
    if match.group(5) and match.group(6):
        return match.group(6).replace(",", ""), match.group(5).upper()
    return None, ""


def _parse_dom_status(text: str) -> str:
    low = (text or "").lower()
    if any(k in low for k in ("refunded", "已退款")):
        return "refunded"
    if any(k in low for k in ("void", "已作废")):
        return "void"
    if any(k in low for k in ("failed", "失败")):
        return "failed"
    if any(k in low for k in ("open", "past due", "unpaid", "待支付")):
        return "open"
    if any(k in low for k in ("paid", "已支付", "已付款")):
        return "paid"
    return "paid"


def _parse_dom_plan(text: str) -> str:
    low = (text or "").lower()
    if "pro max" in low:
        return "pro_max"
    if "pro lite" in low:
        return "pro_lite"
    if "chatgpt pro" in low or re.search(r"\bpro\b", low):
        return "pro"
    if "chatgpt team" in low or "chatgpt business" in low:
        return "team"
    if "chatgpt go" in low:
        return "go"
    if "chatgpt plus" in low or re.search(r"\bplus\b", low):
        return "plus"
    return ""


def parse_dom_invoice_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert raw DOM row extracts from Stripe Billing Portal into invoice dicts."""
    parsed: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        href = safe_invoice_url(row.get("href"))
        amt, currency = _parse_dom_money(text)
        paid_at = _parse_dom_date(text)
        if not amt and not paid_at:
            continue
        item: Dict[str, Any] = {
            "id": "",
            "paid_at": paid_at,
            "amount_paid": amt,
            "amount_unit": "major",
            "currency": currency,
            "status": _parse_dom_status(text),
            "description": _parse_dom_plan(text),
        }
        if href:
            item["invoice_url"] = href
        parsed.append(item)
    return _dedupe_raw_invoices(parsed)


def _playwright_proxy(proxy_url: Optional[str]) -> Optional[Dict[str, str]]:
    raw = str(proxy_url or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    cfg: Dict[str, str] = {"server": f"{parsed.scheme}://{parsed.hostname}{port}"}
    if parsed.username is not None:
        cfg["username"] = unquote(parsed.username)
    if parsed.password is not None:
        cfg["password"] = unquote(parsed.password)
    return cfg


def _fetch_portal_invoices_http(
    portal_url: str, *, proxy: Optional[str], timeout: float
) -> List[Dict[str, Any]]:
    session = _new_session(proxy=proxy, timeout=min(float(timeout), 20.0))
    try:
        response = session.get(
            portal_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            allow_redirects=True,
            timeout=min(float(timeout), 20.0),
        )
        status = int(getattr(response, "status_code", 0) or 0)
        final_host = (urlsplit(str(getattr(response, "url", "") or "")).hostname or "").lower()
        if status >= 400:
            logger.info("account_detail_portal_http status=%s final_host=%s", status, final_host)
            return []
        content_type = str((getattr(response, "headers", None) or {}).get("content-type") or "").lower()
        if "json" in content_type:
            try:
                items = _dedupe_raw_invoices(extract_invoices_from_json(response.json()))
                logger.info(
                    "account_detail_portal_http status=%s final_host=%s ctype=json count=%s",
                    status, final_host, len(items),
                )
                return items
            except Exception:  # noqa: BLE001
                return []
        text = str(getattr(response, "text", "") or "")
        if len(text) > 5_000_000:
            return []
        items = extract_invoices_from_html(text)
        logger.info(
            "account_detail_portal_http status=%s final_host=%s ctype=html len=%s count=%s",
            status, final_host, len(text), len(items),
        )
        return items
    except Exception as exc:  # noqa: BLE001
        logger.info("account_detail_portal_http_error type=%s", type(exc).__name__)
        return []
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass


_DOM_EXTRACT_JS = """() => {
  const out = [];
  const seen = new Set();
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const findRowContainer = (el) => {
    const direct = el.closest('tr, li, [role="row"], [data-testid*="invoice"]');
    if (direct && norm(direct.innerText).length <= 400) return direct;
    let cur = el.parentElement;
    for (let i = 0; i < 4 && cur; i++) {
      const txt = norm(cur.innerText);
      if (txt.length >= 15 && txt.length <= 400 && /[0-9]/.test(txt)) return cur;
      cur = cur.parentElement;
    }
    return el;
  };
  const linkSelector = [
    'a[href*="invoice.stripe.com"]',
    'a[href*="pay.openai.com/i/"]',
    'a[href*="pay.openai.com/invoice"]',
    'a[href*="/i/acct_"]',
    'a[href*="/invoices/"]',
    'a[href*="receipt"]',
  ].join(', ');
  const links = Array.from(document.querySelectorAll(linkSelector));
  for (const a of links) {
    const row = findRowContainer(a);
    const text = norm((row && row.innerText) || a.innerText);
    const href = a.href || '';
    const key = href || text;
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push({ text: text.slice(0, 400), href });
  }
  if (out.length > 0) return out;
  const testidRows = Array.from(document.querySelectorAll('[data-testid*="invoice"]'));
  for (const row of testidRows) {
    const text = norm(row.innerText);
    if (!text || text.length < 10 || text.length > 400 || seen.has(text)) continue;
    seen.add(text);
    const link = row.querySelector('a[href]');
    out.push({ text, href: link ? (link.href || '') : '' });
  }
  if (out.length > 0) return out;
  const rows = Array.from(document.querySelectorAll('tr, [role="row"]'));
  for (const row of rows) {
    const text = norm(row.innerText);
    if (!text || text.length > 400 || seen.has(text)) continue;
    seen.add(text);
    const link = row.querySelector('a[href]');
    out.push({ text, href: link ? (link.href || '') : '' });
  }
  return out.slice(0, 60);
}"""


def _launch_chromium(pw: Any) -> Any:
    explicit = os.environ.get("GSS_CHROME_EXECUTABLE", "").strip()
    if explicit and os.path.isfile(explicit):
        return pw.chromium.launch(headless=True, executable_path=explicit)
    try:
        bundled = pw.chromium.executable_path
        if bundled and os.path.exists(bundled):
            return pw.chromium.launch(headless=True)
    except Exception:  # noqa: BLE001
        pass
    for channel in ("chrome", "chromium", "msedge"):
        try:
            return pw.chromium.launch(headless=True, channel=channel)
        except Exception:  # noqa: BLE001
            continue
    return pw.chromium.launch(headless=True)


def _fetch_portal_invoices_browser(
    portal_url: str, *, proxy: Optional[str], timeout: float
) -> Tuple[List[Dict[str, Any]], str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        return [], "browser_unavailable"

    import time

    xhr_invoices: List[Dict[str, Any]] = []
    xhr_diag: List[str] = []
    portal_session_seen = False
    invoices_list_seen = False
    first_invoice_ts: Optional[float] = None
    first_list_ts: Optional[float] = None
    pw = None
    browser = None
    context = None
    goto_timeout_ms = int(max(18.0, min(float(timeout), 35.0)) * 1000)
    wait_budget_s = max(18.0, min(float(timeout), 32.0))
    try:
        pw = sync_playwright().start()
        browser = _launch_chromium(pw)
        ctx_kwargs: Dict[str, Any] = {
            "locale": "en-US",
            "viewport": {"width": 1280, "height": 900},
        }
        pw_proxy = _playwright_proxy(proxy)
        if pw_proxy is not None:
            ctx_kwargs["proxy"] = pw_proxy
        context = browser.new_context(**ctx_kwargs)

        # Drop non-essential static assets & analytics so Stripe Portal JS & XHR
        # finish rapidly even over high-latency residential proxies.
        def _route_filter(route: Any) -> None:
            try:
                req = route.request
                rtype = str(getattr(req, "resource_type", "") or "").lower()
                if rtype in {"image", "media", "font", "stylesheet"}:
                    route.abort()
                    return
                host = (urlsplit(str(getattr(req, "url", "") or "")).hostname or "").lower()
                if host in {"r.stripe.com", "errors.stripe.com", "q.stripe.com"}:
                    route.abort()
                    return
            except Exception:  # noqa: BLE001
                pass
            try:
                route.continue_()
            except Exception:  # noqa: BLE001
                pass

        context.route("**/*", _route_filter)
        page = context.new_page()

        def _on_response(resp: Any) -> None:
            nonlocal portal_session_seen, invoices_list_seen, first_invoice_ts, first_list_ts
            try:
                url = str(resp.url or "")
                parsed = urlsplit(url)
                host = (parsed.hostname or "").lower()
                if not _host_matches(host, _PORTAL_XHR_HOSTS):
                    return
                status = int(resp.status)
                if status >= 400:
                    return
                ctype = str(resp.headers.get("content-type") or "").lower()
                if "json" not in ctype:
                    return
                data = resp.json()
                path_low = parsed.path.lower()
                if "/billing_portal/" in path_low:
                    portal_session_seen = True
                    if (
                        "invoice" in path_low
                        or (isinstance(data, dict) and data.get("object") == "list" and isinstance(data.get("data"), list))
                    ):
                        invoices_list_seen = True
                        if first_list_ts is None:
                            first_list_ts = time.monotonic()
                if len(xhr_diag) < 12:
                    keys = sorted(data.keys())[:8] if isinstance(data, dict) else [type(data).__name__]
                    xhr_diag.append(f"{host}{_safe_path_prefix(parsed.path)}:{status}:{','.join(keys)}")
                extracted = extract_invoices_from_json(data)
                if extracted:
                    xhr_invoices.extend(extracted)
                    if first_invoice_ts is None:
                        first_invoice_ts = time.monotonic()
            except Exception:  # noqa: BLE001
                return

        page.on("response", _on_response)
        page.goto(portal_url, wait_until="domcontentloaded", timeout=goto_timeout_ms)

        # Actively wait for Stripe Portal XHR responses instead of blind fixed timeouts.
        deadline = time.monotonic() + wait_budget_s
        while time.monotonic() < deadline:
            now_m = time.monotonic()
            if xhr_invoices and first_invoice_ts is not None and (now_m - first_invoice_ts) >= 0.9:
                break
            if invoices_list_seen and not xhr_invoices and first_list_ts is not None and (now_m - first_list_ts) >= 1.8:
                break
            page.wait_for_timeout(250)

        final_parsed = urlsplit(str(getattr(page, "url", "") or ""))
        final_host = (final_parsed.hostname or "").lower()
        final_prefix = _safe_path_prefix(final_parsed.path)

        if xhr_invoices:
            deduped = _dedupe_raw_invoices(xhr_invoices)
            logger.info(
                "account_detail_portal_browser stage=browser_xhr final=%s%s count=%s xhr=%s",
                final_host, final_prefix, len(deduped), xhr_diag[:6],
            )
            return deduped, "browser_xhr"

        dom_rows = page.evaluate(_DOM_EXTRACT_JS)
        dom_count = len(dom_rows) if isinstance(dom_rows, list) else 0
        if isinstance(dom_rows, list) and dom_rows:
            parsed_rows = parse_dom_invoice_rows(dom_rows)
            if parsed_rows:
                logger.info(
                    "account_detail_portal_browser stage=browser_dom final=%s%s dom_rows=%s count=%s",
                    final_host, final_prefix, dom_count, len(parsed_rows),
                )
                return parsed_rows, "browser_dom"

        stage = "browser_verified_empty" if (portal_session_seen and invoices_list_seen) else "browser_timeout"
        logger.info(
            "account_detail_portal_browser stage=%s final=%s%s dom_rows=%s portal_seen=%s list_seen=%s xhr=%s",
            stage, final_host, final_prefix, dom_count, portal_session_seen, invoices_list_seen, xhr_diag[:10],
        )
        return [], stage
    except Exception as exc:  # noqa: BLE001
        logger.info("account_detail_portal_browser_error type=%s", type(exc).__name__)
        return [], "browser_error"
    finally:
        for item in (context, browser):
            if item is not None:
                try:
                    item.close()
                except Exception:  # noqa: BLE001
                    pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass


def fetch_portal_invoices(
    portal_url: str,
    *,
    proxy: Optional[str] = None,
    timeout: float = 25.0,
    retry_proxy: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """Fetch invoice records from a validated Stripe Customer Portal URL."""
    url = _safe_portal_entry_url(portal_url)
    if not url:
        return [], "invalid_portal_url"

    http_items = _fetch_portal_invoices_http(url, proxy=proxy, timeout=min(float(timeout), 15.0))
    if http_items:
        logger.info("account_detail_portal_invoices stage=http count=%s", len(http_items))
        return http_items, "http"

    if os.environ.get("GSS_PORTAL_BROWSER_SCRAPE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return [], "browser_disabled"

    browser_items, stage = _fetch_portal_invoices_browser(url, proxy=proxy, timeout=timeout)
    if not browser_items and stage in {"browser_timeout", "browser_error"}:
        # One automatic retry when the residential proxy stalled before Stripe XHR completed.
        next_proxy = retry_proxy if retry_proxy is not None else proxy
        logger.info("account_detail_portal_retry prev_stage=%s", stage)
        browser_items, stage = _fetch_portal_invoices_browser(url, proxy=next_proxy, timeout=timeout)
    logger.info("account_detail_portal_invoices stage=%s count=%s", stage, len(browser_items))
    return browser_items, stage


def _extract_portal_cancel_signals(data: Any) -> Dict[str, Any]:
    """Extract non-secret subscription and cancellation feature flags from Stripe Portal XHR JSON."""
    out: Dict[str, Any] = {
        "cancel_enabled": None,
        "cancel_mode": None,
        "reason_enabled": None,
        "subscriptions": [],
        "canceled_confirmed": False,
    }

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 7:
            return
        if isinstance(node, dict):
            # Billing portal configuration -> features -> subscription_cancel
            features = node.get("features")
            if isinstance(features, dict) and isinstance(features.get("subscription_cancel"), dict):
                sc = features["subscription_cancel"]
                if "enabled" in sc:
                    out["cancel_enabled"] = bool(sc.get("enabled"))
                if isinstance(sc.get("mode"), str):
                    out["cancel_mode"] = sc["mode"]
                cr = sc.get("cancellation_reason")
                if isinstance(cr, dict) and "enabled" in cr:
                    out["reason_enabled"] = bool(cr.get("enabled"))

            obj_type = str(node.get("object") or "").strip().lower()
            node_id = str(node.get("id") or "").strip()
            if obj_type == "subscription" or node_id.startswith("sub_"):
                cap = node.get("cancel_at_period_end")
                cancel_at = node.get("cancel_at")
                status = str(node.get("status") or "").strip().lower()
                sub_summary = {
                    "id_prefix": node_id[:8] if node_id else "",
                    "status": status,
                    "cancel_at_period_end": bool(cap) if cap is not None else False,
                    "has_cancel_at": bool(cancel_at),
                }
                out["subscriptions"].append(sub_summary)
                if sub_summary["cancel_at_period_end"] or sub_summary["has_cancel_at"] or status == "canceled":
                    out["canceled_confirmed"] = True

            for value in node.values():
                if isinstance(value, (dict, list)):
                    _walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:40]:
                if isinstance(item, (dict, list)):
                    _walk(item, depth + 1)

    _walk(data)
    return out


_PORTAL_CANCEL_STATE_JS = """() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const isVisible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity || '1') === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const bodyText = norm(document.body ? document.body.innerText : '').slice(0, 4000);
  const buttons = [];
  const els = Array.from(document.querySelectorAll('button, a[role="button"], a[href*="cancel"], [role="button"]'));
  for (const el of els) {
    if (!isVisible(el)) continue;
    const text = norm(el.innerText || el.getAttribute('aria-label') || '');
    const testid = el.getAttribute('data-testid') || '';
    const disabled = Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true');
    if (!text && !testid) continue;
    buttons.push({ text: text.slice(0, 80), testid: testid.slice(0, 80), disabled });
  }
  const radios = [];
  for (const el of Array.from(document.querySelectorAll('input[type="radio"], [role="radio"]'))) {
    const label = el.closest('label') || (el.id ? document.querySelector(`label[for="${el.id}"]`) : null) || el.parentElement;
    const text = norm((label && label.innerText) || el.getAttribute('aria-label') || el.value || '');
    if (isVisible(el) || (label && isVisible(label))) {
      radios.push({ text: text.slice(0, 80), checked: Boolean(el.checked || el.getAttribute('aria-checked') === 'true') });
    }
  }
  return {
    bodySnippet: bodyText.slice(0, 600),
    hasRenewCue: /\\brenew\\s+(subscription|plan)\\b|will\\s+be\\s+canceled|cancels?\\s+on\\b|cancellation\\s+confirmed|已取消自动续费|重新订阅/i.test(bodyText),
    buttons: buttons.slice(0, 30),
    radios: radios.slice(0, 15),
  };
}"""


_PORTAL_CLICK_JS = """(spec) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const isVisible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity || '1') === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const excludeRe = /don['’]?t\\s+cancel|do\\s+not\\s+cancel|\\bkeep\\b|never\\s*mind|go\\s+back|\\bback\\b|return\\s+to|\\bpause\\b|switch\\s+plan|update\\s+plan|change\\s+plan|upgrade|add\\s+payment|保持|保留|返回|暂停|升级/i;
  const includeRe = new RegExp(spec.includePattern, 'i');

  if (spec.selectRadio) {
    const candidates = Array.from(document.querySelectorAll('input[type="radio"], [role="radio"]'));
    let chosen = null;
    for (const el of candidates) {
      const label = el.closest('label') || (el.id ? document.querySelector(`label[for="${el.id}"]`) : null) || el.parentElement;
      const text = norm((label && label.innerText) || el.getAttribute('aria-label') || el.value || '');
      if (!(isVisible(el) || (label && isVisible(label)))) continue;
      if (/other|unused|no\\s+longer|don['’]?t\\s+need|其他|不再需要/i.test(text)) {
        chosen = label && isVisible(label) ? label : el;
        break;
      }
      if (!chosen) chosen = label && isVisible(label) ? label : el;
    }
    if (chosen) {
      chosen.click();
      return { clicked: true, kind: 'radio', label: norm(chosen.innerText || chosen.value || '').slice(0, 60) };
    }
    return { clicked: false, kind: 'radio' };
  }

  // 1. Prefer matching data-testid when provided
  for (const tid of (spec.testIds || [])) {
    const el = document.querySelector(`[data-testid="${tid}"]`);
    if (el && isVisible(el)) {
      const disabled = Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true');
      const text = norm(el.innerText || el.getAttribute('aria-label') || tid);
      if (disabled) return { clicked: false, disabled: true, label: text.slice(0, 60), testid: tid };
      el.click();
      return { clicked: true, kind: 'testid', label: text.slice(0, 60), testid: tid };
    }
  }

  // 2. Match visible buttons/links by accessible text
  const els = Array.from(document.querySelectorAll('button, a[role="button"], a[href*="cancel"], [role="button"]'));
  let disabledMatch = null;
  for (const el of els) {
    if (!isVisible(el)) continue;
    const text = norm(el.innerText || el.getAttribute('aria-label') || '');
    if (!text || excludeRe.test(text)) continue;
    if (!includeRe.test(text)) continue;
    const disabled = Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true');
    if (disabled) {
      disabledMatch = { clicked: false, disabled: true, label: text.slice(0, 60) };
      continue;
    }
    el.click();
    return { clicked: true, kind: 'text', label: text.slice(0, 60) };
  }
  return disabledMatch || { clicked: false };
}"""


def _cancel_portal_subscription_browser(
    portal_url: str, *, proxy: Optional[str], timeout: float
) -> Dict[str, Any]:
    """Drive OpenAI's Stripe Customer Portal in headless Chromium to cancel auto-renew."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        return {"canceled": False, "stage": "browser_unavailable", "retryable": False}

    import time

    xhr_diag: List[str] = []
    portal_session_seen = False
    subs_seen = False
    cancel_enabled: Optional[bool] = None
    cancel_mode: Optional[str] = None
    subscriptions: List[Dict[str, Any]] = []
    cancel_post_sent = False
    cancel_post_confirmed = False
    cancel_post_error: Optional[str] = None

    pw = None
    browser = None
    context = None
    goto_timeout_ms = int(max(20.0, min(float(timeout), 35.0)) * 1000)
    load_budget_s = max(14.0, min(float(timeout) * 0.5, 20.0))

    try:
        pw = sync_playwright().start()
        browser = _launch_chromium(pw)
        ctx_kwargs: Dict[str, Any] = {
            "locale": "en-US",
            "viewport": {"width": 1280, "height": 900},
        }
        pw_proxy = _playwright_proxy(proxy)
        if pw_proxy is not None:
            ctx_kwargs["proxy"] = pw_proxy
        context = browser.new_context(**ctx_kwargs)

        # Keep CSS/JS intact for accurate interactive clicks; block heavy static assets & analytics.
        def _route_filter(route: Any) -> None:
            try:
                req = route.request
                rtype = str(getattr(req, "resource_type", "") or "").lower()
                if rtype in {"image", "media", "font"}:
                    route.abort()
                    return
                host = (urlsplit(str(getattr(req, "url", "") or "")).hostname or "").lower()
                if host in {"r.stripe.com", "errors.stripe.com", "q.stripe.com"}:
                    route.abort()
                    return
            except Exception:  # noqa: BLE001
                pass
            try:
                route.continue_()
            except Exception:  # noqa: BLE001
                pass

        context.route("**/*", _route_filter)
        page = context.new_page()

        def _on_response(resp: Any) -> None:
            nonlocal portal_session_seen, subs_seen, cancel_enabled, cancel_mode
            nonlocal cancel_post_sent, cancel_post_confirmed, cancel_post_error
            try:
                url = str(resp.url or "")
                parsed = urlsplit(url)
                host = (parsed.hostname or "").lower()
                if not _host_matches(host, _PORTAL_XHR_HOSTS):
                    return
                req = getattr(resp, "request", None)
                method = str(getattr(req, "method", "GET") or "GET").upper()
                status = int(resp.status)
                path_low = parsed.path.lower()
                if "/billing_portal/" not in path_low:
                    return
                portal_session_seen = True
                is_mutation = method in {"POST", "PUT", "PATCH", "DELETE"} and (
                    "subscription" in path_low or "cancel" in path_low
                )
                if is_mutation:
                    cancel_post_sent = True

                ctype = str(resp.headers.get("content-type") or "").lower()
                data: Any = None
                if "json" in ctype:
                    try:
                        data = resp.json()
                    except Exception:  # noqa: BLE001
                        data = None
                if len(xhr_diag) < 16:
                    keys = sorted(data.keys())[:6] if isinstance(data, dict) else [type(data).__name__]
                    xhr_diag.append(f"{method} {host}{_safe_path_prefix(parsed.path)}:{status}:{','.join(keys)}")

                if status >= 400:
                    if is_mutation:
                        err_code = ""
                        if isinstance(data, dict) and isinstance(data.get("error"), dict):
                            err_code = str(data["error"].get("code") or data["error"].get("type") or "")
                        cancel_post_error = f"http_{status}:{err_code}" if err_code else f"http_{status}"
                    return

                if data is not None:
                    sig = _extract_portal_cancel_signals(data)
                    if sig["cancel_enabled"] is not None:
                        cancel_enabled = sig["cancel_enabled"]
                    if sig["cancel_mode"]:
                        cancel_mode = sig["cancel_mode"]
                    if sig["subscriptions"]:
                        subs_seen = True
                        subscriptions.extend(sig["subscriptions"])
                    if is_mutation and (sig["canceled_confirmed"] or status == 200):
                        if sig["canceled_confirmed"] or "cancel" in path_low:
                            cancel_post_confirmed = True
            except Exception:  # noqa: BLE001
                return

        page.on("response", _on_response)
        page.goto(portal_url, wait_until="domcontentloaded", timeout=goto_timeout_ms)

        # Wait for Stripe Portal SPA to render subscription controls
        deadline = time.monotonic() + load_budget_s
        dom_state: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                dom_state = page.evaluate(_PORTAL_CANCEL_STATE_JS)
            except Exception:  # noqa: BLE001
                dom_state = {}
            btns = dom_state.get("buttons") or []
            if portal_session_seen and (btns or subs_seen):
                page.wait_for_timeout(600)
                try:
                    dom_state = page.evaluate(_PORTAL_CANCEL_STATE_JS)
                except Exception:  # noqa: BLE001
                    pass
                break
            page.wait_for_timeout(300)

        if not portal_session_seen and not (dom_state.get("buttons") or []):
            logger.info(
                "account_detail_portal_cancel stage=browser_timeout xhr=%s",
                xhr_diag[:10],
            )
            return {"canceled": False, "stage": "browser_timeout", "retryable": True}

        # Check if subscription is ALREADY scheduled to cancel on Stripe
        active_subs = [s for s in subscriptions if s.get("status") in {"active", "trialing", "past_due"}]
        if active_subs and all(s.get("cancel_at_period_end") or s.get("has_cancel_at") for s in active_subs):
            logger.info(
                "account_detail_portal_cancel stage=already_canceled_xhr subs=%s",
                len(active_subs),
            )
            return {"canceled": True, "already_canceled": True, "stage": "already_canceled"}

        button_labels = [b.get("text", "") for b in (dom_state.get("buttons") or [])[:12]]
        has_cancel_btn = any(
            re.search(r"cancel\s*(subscription|plan)|取消(订阅|方案|套餐)", txt or "", re.IGNORECASE)
            and not re.search(r"don['’]?t|keep|never|保留", txt or "", re.IGNORECASE)
            for txt in button_labels
        )
        if dom_state.get("hasRenewCue") and not has_cancel_btn:
            logger.info(
                "account_detail_portal_cancel stage=already_canceled_dom buttons=%s",
                button_labels[:8],
            )
            return {"canceled": True, "already_canceled": True, "stage": "already_canceled"}

        if cancel_enabled is False and not has_cancel_btn:
            logger.info(
                "account_detail_portal_cancel stage=portal_cancel_disabled buttons=%s xhr=%s",
                button_labels[:10], xhr_diag[:10],
            )
            return {
                "canceled": False,
                "stage": "portal_cancel_disabled",
                "retryable": False,
                "buttons": button_labels[:8],
            }

        # Multi-step Stripe Customer Portal cancellation flow
        steps_taken: List[str] = []

        # Step 1: Enter cancel flow
        step1 = page.evaluate(
            _PORTAL_CLICK_JS,
            {
                "testIds": [
                    "cancel-subscription-button",
                    "cancel-plan-button",
                    "subscription-cancel-button",
                ],
                "includePattern": r"(?:^|\b)cancel\s+(?:subscription|plan)(?:\b|$)|取消(?:订阅|方案|套餐)",
            },
        )
        if not step1.get("clicked"):
            # Try opening a collapsed "Manage" / "Manage plan" entry first
            manage = page.evaluate(
                _PORTAL_CLICK_JS,
                {
                    "testIds": ["manage-subscription-button", "update-subscription-button"],
                    "includePattern": r"^(?:manage|manage\s+subscription|manage\s+plan|view\s+details)$",
                },
            )
            if manage.get("clicked"):
                steps_taken.append(f"manage:{manage.get('label', '')}")
                page.wait_for_timeout(1200)
                step1 = page.evaluate(
                    _PORTAL_CLICK_JS,
                    {
                        "testIds": [
                            "cancel-subscription-button",
                            "cancel-plan-button",
                            "subscription-cancel-button",
                        ],
                        "includePattern": r"(?:^|\b)cancel\s+(?:subscription|plan)(?:\b|$)|取消(?:订阅|方案|套餐)",
                    },
                )

        if not step1.get("clicked"):
            logger.info(
                "account_detail_portal_cancel stage=cancel_button_not_found cancel_enabled=%s buttons=%s xhr=%s",
                cancel_enabled, button_labels[:12], xhr_diag[:10],
            )
            return {
                "canceled": False,
                "stage": "cancel_button_not_found",
                "retryable": False,
                "buttons": button_labels[:8],
            }

        steps_taken.append(f"entry:{step1.get('label', '')}")

        # Steps 2..5: Deflection offers, optional cancellation survey, final confirmation
        for hop in range(4):
            page.wait_for_timeout(1200)
            if cancel_post_confirmed:
                break
            try:
                cur_state = page.evaluate(_PORTAL_CANCEL_STATE_JS)
            except Exception:  # noqa: BLE001
                cur_state = {}
            if cur_state.get("hasRenewCue") and cancel_post_sent:
                break

            # Handle optional retention / deflection step ("No thanks", "Continue to cancel", etc.)
            deflect = page.evaluate(
                _PORTAL_CLICK_JS,
                {
                    "testIds": [
                        "decline-offer-button",
                        "skip-offer-button",
                        "continue-to-cancel-button",
                        "retention-decline-button",
                    ],
                    "includePattern": r"no,?\s*thanks|continue\s+to\s+cancel|continue\s+canceling|cancel\s+anyway|decline\s+offer|skip\s+offer|继续取消|不需要",
                },
            )
            if deflect.get("clicked"):
                steps_taken.append(f"deflect:{deflect.get('label', '')}")
                continue

            # Try clicking final confirm button
            confirm_spec = {
                "testIds": [
                    "confirm-cancel-subscription-button",
                    "cancel-subscription-confirm-button",
                    "cancel-subscription-button",
                    "confirm-cancellation-button",
                    "submit-cancellation-button",
                    "submit-button",
                ],
                "includePattern": r"confirm\s+cancel|cancel\s+(?:my\s+)?(?:subscription|plan)|submit|确认取消|提交",
            }
            confirm = page.evaluate(_PORTAL_CLICK_JS, confirm_spec)
            if confirm.get("disabled"):
                # Required cancellation reason radio not selected yet
                radio = page.evaluate(_PORTAL_CLICK_JS, {"selectRadio": True, "includePattern": "."})
                if radio.get("clicked"):
                    steps_taken.append(f"radio:{radio.get('label', '')}")
                    page.wait_for_timeout(350)
                    confirm = page.evaluate(_PORTAL_CLICK_JS, confirm_spec)
            if confirm.get("clicked"):
                steps_taken.append(f"confirm:{confirm.get('label', '')}")
                # Wait up to 8s for Stripe cancel POST response or UI transition
                confirm_deadline = time.monotonic() + 8.0
                while time.monotonic() < confirm_deadline:
                    if cancel_post_confirmed or cancel_post_error:
                        break
                    try:
                        after = page.evaluate(_PORTAL_CANCEL_STATE_JS)
                        if after.get("hasRenewCue"):
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    page.wait_for_timeout(300)
                if cancel_post_confirmed or cancel_post_error:
                    break
                continue

            # If a required survey radio is visible before a submit button appears
            if cur_state.get("radios") and not any(r.get("checked") for r in cur_state["radios"]):
                radio = page.evaluate(_PORTAL_CLICK_JS, {"selectRadio": True, "includePattern": "."})
                if radio.get("clicked"):
                    steps_taken.append(f"radio:{radio.get('label', '')}")
                    continue
            break

        # Final post-click DOM check
        try:
            final_state = page.evaluate(_PORTAL_CANCEL_STATE_JS)
        except Exception:  # noqa: BLE001
            final_state = {}
        final_buttons = [b.get("text", "") for b in (final_state.get("buttons") or [])[:10]]
        dom_confirmed = bool(final_state.get("hasRenewCue")) and not any(
            re.search(r"^\s*cancel\s+(subscription|plan)\s*$", txt or "", re.IGNORECASE)
            for txt in final_buttons
        )

        if cancel_post_confirmed or (cancel_post_sent and dom_confirmed):
            logger.info(
                "account_detail_portal_cancel stage=portal_canceled steps=%s xhr_confirmed=%s dom_confirmed=%s xhr=%s",
                steps_taken, cancel_post_confirmed, dom_confirmed, xhr_diag[:10],
            )
            return {
                "canceled": True,
                "already_canceled": False,
                "stage": "portal_canceled",
                "xhr_confirmed": cancel_post_confirmed,
                "steps": steps_taken,
            }

        stage = "portal_cancel_error" if cancel_post_error else "portal_cancel_unconfirmed"
        logger.info(
            "account_detail_portal_cancel stage=%s err=%s steps=%s buttons=%s xhr=%s",
            stage, cancel_post_error, steps_taken, final_buttons[:10], xhr_diag[:12],
        )
        return {
            "canceled": False,
            "stage": stage,
            "error": cancel_post_error,
            "retryable": not cancel_post_sent,
            "steps": steps_taken,
            "buttons": final_buttons[:8],
        }
    except Exception as exc:  # noqa: BLE001
        logger.info("account_detail_portal_cancel_error type=%s", type(exc).__name__)
        return {
            "canceled": False,
            "stage": "browser_error",
            "retryable": not cancel_post_sent,
        }
    finally:
        for item in (context, browser):
            if item is not None:
                try:
                    item.close()
                except Exception:  # noqa: BLE001
                    pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass


def cancel_portal_subscription(
    portal_url: str,
    *,
    proxy: Optional[str] = None,
    timeout: float = 35.0,
    retry_proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """Cancel auto-renewal via a validated Stripe Customer Portal URL."""
    url = _safe_portal_entry_url(portal_url)
    if not url:
        return {"canceled": False, "stage": "invalid_portal_url", "retryable": False}

    if os.environ.get("GSS_PORTAL_BROWSER_SCRAPE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return {"canceled": False, "stage": "browser_disabled", "retryable": False}

    result = _cancel_portal_subscription_browser(url, proxy=proxy, timeout=timeout)
    if not result.get("canceled") and result.get("retryable"):
        next_proxy = retry_proxy if retry_proxy is not None else proxy
        logger.info("account_detail_portal_cancel_retry prev_stage=%s", result.get("stage"))
        result = _cancel_portal_subscription_browser(url, proxy=next_proxy, timeout=timeout)
    return result


__all__ = [
    "cancel_portal_subscription",
    "extract_invoices_from_html",
    "extract_invoices_from_json",
    "fetch_portal_invoices",
    "parse_dom_invoice_rows",
    "safe_invoice_url",
]
