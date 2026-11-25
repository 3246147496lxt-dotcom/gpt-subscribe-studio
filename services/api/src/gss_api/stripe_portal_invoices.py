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

_ALLOWED_INVOICE_HOSTS = ("stripe.com",)
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
    if any(host == dom or host.endswith("." + dom) for dom in _ALLOWED_INVOICE_HOSTS):
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
        if status >= 400:
            logger.info("account_detail_portal_http status=%s", status)
            return []
        content_type = str((getattr(response, "headers", None) or {}).get("content-type") or "").lower()
        if "json" in content_type:
            try:
                return _dedupe_raw_invoices(extract_invoices_from_json(response.json()))
            except Exception:  # noqa: BLE001
                return []
        text = str(getattr(response, "text", "") or "")
        if len(text) > 5_000_000:
            return []
        return extract_invoices_from_html(text)
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
  const links = Array.from(document.querySelectorAll('a[href*="invoice.stripe.com"], a[href*="/invoices/"]'));
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

    xhr_invoices: List[Dict[str, Any]] = []
    pw = None
    browser = None
    context = None
    timeout_ms = int(max(8.0, min(float(timeout), 25.0)) * 1000)
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
        page = context.new_page()

        def _on_response(resp: Any) -> None:
            try:
                url = str(resp.url or "")
                host = (urlsplit(url).hostname or "").lower()
                if not (host == "stripe.com" or host.endswith(".stripe.com")):
                    return
                if int(resp.status) >= 400:
                    return
                ctype = str(resp.headers.get("content-type") or "").lower()
                if "json" not in ctype:
                    return
                data = resp.json()
                extracted = extract_invoices_from_json(data)
                if extracted:
                    xhr_invoices.extend(extracted)
            except Exception:  # noqa: BLE001
                return

        page.on("response", _on_response)
        page.goto(portal_url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_selector(
                'a[href*="invoice.stripe.com"], a[href*="/invoices/"]',
                timeout=min(timeout_ms, 12000),
            )
        except Exception:  # noqa: BLE001
            pass
        if not xhr_invoices:
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:  # noqa: BLE001
                pass

        if xhr_invoices:
            return _dedupe_raw_invoices(xhr_invoices), "browser_xhr"

        dom_rows = page.evaluate(_DOM_EXTRACT_JS)
        if isinstance(dom_rows, list) and dom_rows:
            parsed = parse_dom_invoice_rows(dom_rows)
            if parsed:
                return parsed, "browser_dom"
        return [], "browser_empty"
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
    timeout: float = 20.0,
) -> Tuple[List[Dict[str, Any]], str]:
    """Fetch invoice records from a validated Stripe Customer Portal URL."""
    url = safe_invoice_url(portal_url)
    if not url:
        return [], "invalid_portal_url"

    http_items = _fetch_portal_invoices_http(url, proxy=proxy, timeout=timeout)
    if http_items:
        logger.info("account_detail_portal_invoices stage=http count=%s", len(http_items))
        return http_items, "http"

    if os.environ.get("GSS_PORTAL_BROWSER_SCRAPE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return [], "browser_disabled"

    browser_items, stage = _fetch_portal_invoices_browser(url, proxy=proxy, timeout=timeout)
    logger.info("account_detail_portal_invoices stage=%s count=%s", stage, len(browser_items))
    return browser_items, stage


__all__ = [
    "extract_invoices_from_html",
    "extract_invoices_from_json",
    "fetch_portal_invoices",
    "parse_dom_invoice_rows",
    "safe_invoice_url",
]
