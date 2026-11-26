"""Dynamic FX rate sync and automatic lowest-price region selector.

Architecture:
1. Maintains per-(plan, country) tax-free local currency prices (``local_prices``),
   seeded with verified OpenAI regional prices (US tax-free billing, local VAT
   stripped) and automatically calibrated whenever a real standard checkout
   resolves Stripe's exact tax-free amount.
2. Periodically fetches live USD-base FX rates from free public JSON providers
   (with multi-provider fallback and disk persistence under ``$GSS_CONFIG_DIR``).
   If all FX providers are temporarily unreachable, preserves the last valid
   rates and marks ``stale = True``.
3. Converts every candidate region's local price to USD and CNY, selects the
   cheapest region per plan, calculates discount labels against US base prices,
   and feeds ``GET /api/public/site-config``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

logger = logging.getLogger("gss.reference_pricing")

BEIJING = timezone(timedelta(hours=8))

# US official monthly list prices used as discount baseline.
PLAN_BASE_USD: Dict[str, float] = {
    "go": 8.0,
    "plus": 20.0,
    "pro_lite": 100.0,
    "pro": 200.0,
    "pro_max": 500.0,
}

# Verified regional tax-free local prices (US tax-free billing address, local VAT removed).
# Any real standard checkout automatically refines these values via ``record_observed_price``.
DEFAULT_LOCAL_PRICES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "go": {
        "EG": {"currency": "EGP", "amount": 192.98, "source": "seed"},
        "PH": {"currency": "PHP", "amount": 267.86, "source": "seed"},
        "CL": {"currency": "CLP", "amount": 6714.0, "source": "seed"},
        "JP": {"currency": "JPY", "amount": 1091.0, "source": "seed"},
        "US": {"currency": "USD", "amount": 8.00, "source": "seed"},
    },
    "plus": {
        "PH": {"currency": "PHP", "amount": 982.14, "source": "seed"},
        "EG": {"currency": "EGP", "amount": 877.19, "source": "seed"},
        "CL": {"currency": "CLP", "amount": 16798.0, "source": "seed"},
        "JP": {"currency": "JPY", "amount": 2727.0, "source": "seed"},
        "US": {"currency": "USD", "amount": 20.00, "source": "seed"},
    },
    "pro_lite": {
        "PH": {"currency": "PHP", "amount": 5794.64, "source": "seed"},
        "EG": {"currency": "EGP", "amount": 4736.84, "source": "seed"},
        "CL": {"currency": "CLP", "amount": 86365.0, "source": "seed"},
        "JP": {"currency": "JPY", "amount": 13636.0, "source": "seed"},
        "US": {"currency": "USD", "amount": 100.00, "source": "seed"},
    },
    "pro": {
        "PH": {"currency": "PHP", "amount": 8919.64, "source": "seed"},
        "EG": {"currency": "EGP", "amount": 8771.93, "source": "seed"},
        "CL": {"currency": "CLP", "amount": 168059.0, "source": "seed"},
        "JP": {"currency": "JPY", "amount": 27273.0, "source": "seed"},
        "US": {"currency": "USD", "amount": 200.00, "source": "seed"},
    },
    "pro_max": {
        "PH": {"currency": "PHP", "amount": 29008.93, "source": "seed"},
        "EG": {"currency": "EGP", "amount": 23684.21, "source": "seed"},
        "CL": {"currency": "CLP", "amount": 428563.0, "source": "seed"},
        "JP": {"currency": "JPY", "amount": 68182.0, "source": "seed"},
        "US": {"currency": "USD", "amount": 500.00, "source": "seed"},
    },
}

DEFAULT_FX_RATES: Dict[str, float] = {
    "USD": 1.0,
    "CNY": 6.68,
    "PHP": 63.77,
    "EGP": 54.23,
    "CLP": 1016.25,
    "JPY": 159.24,
    "SEK": 10.24,
}

_REQUIRED_FX_CODES = ("CNY", "PHP", "EGP", "CLP", "JPY")

_FX_PROVIDERS: Tuple[Tuple[str, str], ...] = (
    ("open.er-api.com", "https://open.er-api.com/v6/latest/USD"),
    (
        "fawazahmed0-jsdelivr",
        "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
    ),
    ("api.exchangerate-api.com", "https://api.exchangerate-api.com/v4/latest/USD"),
)

_REGION_LABELS: Dict[str, Tuple[str, str]] = {
    "PH": ("菲律宾", "Philippines"),
    "JP": ("日本", "Japan"),
    "US": ("美国", "United States"),
    "EG": ("埃及", "Egypt"),
    "CL": ("智利", "Chile"),
    "SE": ("瑞典", "Sweden"),
}

_PLAN_ALIASES: Dict[str, str] = {
    "go": "go",
    "chatgptgo": "go",
    "chatgptgoplan": "go",
    "plus": "plus",
    "chatgptplus": "plus",
    "chatgptplusplan": "plus",
    "pro_lite": "pro_lite",
    "prolite": "pro_lite",
    "pro5": "pro_lite",
    "pro": "pro",
    "pro20": "pro",
    "chatgptpro": "pro",
    "pro_max": "pro_max",
    "promax": "pro_max",
    "chatgptpromax": "pro_max",
}


def _canonical_plan(plan: str) -> str:
    key = str(plan or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _PLAN_ALIASES.get(key, key)


def _refresh_interval_s() -> float:
    raw = os.environ.get("GSS_FX_REFRESH_INTERVAL_S", "3600").strip()
    try:
        return max(60.0, float(raw))
    except ValueError:
        return 3600.0


def _refresh_enabled() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return os.environ.get("GSS_FX_REFRESH_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def reference_prices_path() -> Path:
    explicit = os.environ.get("GSS_REFERENCE_PRICES_FILE", "").strip()
    if explicit:
        return Path(explicit)
    config_dir = Path(os.environ.get("GSS_CONFIG_DIR", "config"))
    return config_dir / "reference-prices.json"


def _format_beijing_hm(epoch: Optional[float]) -> str:
    if not epoch or epoch <= 0:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).astimezone(BEIJING).strftime("%m/%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def _format_beijing_full(epoch: Optional[float]) -> str:
    if not epoch or epoch <= 0:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def _format_beijing_date(epoch: Optional[float]) -> str:
    if not epoch or epoch <= 0:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).astimezone(BEIJING).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def _discount_labels(usd: float, base_usd: float) -> Tuple[str, str]:
    if base_usd <= 0 or usd <= 0 or usd >= base_usd:
        return "", ""
    zh_fold = round((usd / base_usd) * 10.0, 1)
    pct_off = round((1.0 - usd / base_usd) * 100.0)
    if zh_fold >= 9.8 or pct_off <= 2:
        return "", ""
    zh_str = f"{int(zh_fold)}折" if abs(zh_fold - round(zh_fold)) < 1e-6 else f"{zh_fold:.1f}折"
    en_str = f"{pct_off}% OFF"
    return zh_str, en_str


def _parse_fx_payload(payload: Any) -> Dict[str, float]:
    if not isinstance(payload, dict):
        return {}
    raw_rates = payload.get("rates")
    if not isinstance(raw_rates, dict):
        raw_rates = payload.get("usd")
    if not isinstance(raw_rates, dict):
        return {}
    parsed: Dict[str, float] = {"USD": 1.0}
    for code, val in raw_rates.items():
        if not isinstance(code, str) or len(code) > 8:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if math.isfinite(num) and num > 0:
            parsed[code.upper()] = num
    if not all(code in parsed for code in _REQUIRED_FX_CODES):
        return {}
    return parsed


def fetch_live_fx_rates(timeout: float = 8.0) -> Tuple[Optional[Dict[str, float]], str]:
    """Fetch USD-based live exchange rates from public JSON feeds with fallback."""
    headers = {"User-Agent": "gss-reference-pricing/1.0", "Accept": "application/json"}
    # trust_env=False avoids failures when ambient shell SOCKS proxy lacks socksio.
    with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
        for name, url in _FX_PROVIDERS:
            try:
                resp = client.get(url, headers=headers)
                if resp.status_code != 200:
                    logger.info("fx_fetch_non_200 provider=%s status=%s", name, resp.status_code)
                    continue
                rates = _parse_fx_payload(resp.json())
                if rates:
                    return rates, name
                logger.info("fx_fetch_incomplete provider=%s", name)
            except Exception as exc:  # noqa: BLE001
                logger.info("fx_fetch_error provider=%s type=%s", name, type(exc).__name__)
    return None, ""


class ReferencePricingService:
    """Thread-safe holder for regional local prices, FX rates, and background sync."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loaded_path: Optional[Path] = None
        self._local_prices: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._fx_rates: Dict[str, float] = dict(DEFAULT_FX_RATES)
        self._fx_updated_at: float = 0.0
        self._fx_source: str = "seed"
        self._updated_at: float = 0.0
        self._stale: bool = True
        self._last_error: Optional[str] = None
        self._task: Optional[asyncio.Task[Any]] = None
        self._stop_event: Optional[asyncio.Event] = None

    def _default_local_copy(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        return json.loads(json.dumps(DEFAULT_LOCAL_PRICES))

    def _ensure_loaded(self) -> None:
        path = reference_prices_path()
        with self._lock:
            if self._loaded_path == path and self._local_prices:
                return
            self._loaded_path = path
            self._local_prices = self._default_local_copy()
            self._fx_rates = dict(DEFAULT_FX_RATES)
            self._fx_updated_at = 0.0
            self._fx_source = "seed"
            self._updated_at = 0.0
            self._stale = True

            if not path.is_file():
                return
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if not isinstance(raw, dict):
                return

            saved_fx = raw.get("fx_rates")
            if isinstance(saved_fx, dict):
                for code, val in saved_fx.items():
                    try:
                        num = float(val)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(num) and num > 0:
                        self._fx_rates[str(code).upper()] = num
                self._fx_rates["USD"] = 1.0
            if isinstance(raw.get("fx_updated_at"), (int, float)) and raw["fx_updated_at"] > 0:
                self._fx_updated_at = float(raw["fx_updated_at"])
            if isinstance(raw.get("fx_source"), str) and raw["fx_source"].strip():
                self._fx_source = raw["fx_source"].strip()[:64]
            if isinstance(raw.get("updated_at"), (int, float)) and raw["updated_at"] > 0:
                self._updated_at = float(raw["updated_at"])
            if isinstance(raw.get("stale"), bool):
                self._stale = raw["stale"]
            elif self._fx_updated_at > 0:
                self._stale = (time.time() - self._fx_updated_at) > (_refresh_interval_s() * 3)

            saved_local = raw.get("local_prices")
            if isinstance(saved_local, dict):
                for plan_key, countries in saved_local.items():
                    canon = _canonical_plan(plan_key)
                    if canon not in PLAN_BASE_USD or not isinstance(countries, dict):
                        continue
                    bucket = self._local_prices.setdefault(canon, {})
                    for cc, item in countries.items():
                        if not isinstance(item, dict):
                            continue
                        cur = str(item.get("currency") or "").strip().upper()
                        try:
                            amt = float(item.get("amount"))
                        except (TypeError, ValueError):
                            continue
                        if not cur or not math.isfinite(amt) or amt <= 0:
                            continue
                        bucket[str(cc).strip().upper()] = {
                            "currency": cur,
                            "amount": round(amt, 2),
                            "updated_at": float(item.get("updated_at") or 0.0),
                            "source": str(item.get("source") or "saved")[:32],
                        }

    def _persist_locked(self) -> None:
        path = reference_prices_path()
        payload = {
            "fx_rates": self._fx_rates,
            "fx_updated_at": self._fx_updated_at,
            "fx_source": self._fx_source,
            "updated_at": self._updated_at,
            "stale": self._stale,
            "local_prices": self._local_prices,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            logger.debug("reference_prices_persist_failed", exc_info=True)

    def refresh_fx_sync(self, *, timeout: float = 8.0) -> Dict[str, Any]:
        """Synchronously fetch fresh live FX rates and update persisted cache."""
        self._ensure_loaded()
        rates, provider = fetch_live_fx_rates(timeout=timeout)
        now = time.time()
        with self._lock:
            if rates:
                self._fx_rates.update(rates)
                self._fx_rates["USD"] = 1.0
                self._fx_updated_at = now
                self._fx_source = provider
                self._updated_at = now
                self._stale = False
                self._last_error = None
                self._persist_locked()
                logger.info(
                    "reference_pricing_fx_updated provider=%s cny=%.4f php=%.4f egp=%.4f clp=%.2f jpy=%.2f",
                    provider,
                    self._fx_rates.get("CNY", 0.0),
                    self._fx_rates.get("PHP", 0.0),
                    self._fx_rates.get("EGP", 0.0),
                    self._fx_rates.get("CLP", 0.0),
                    self._fx_rates.get("JPY", 0.0),
                )
            else:
                self._stale = True
                self._last_error = "fx_providers_unavailable"
                self._persist_locked()
                logger.warning("reference_pricing_fx_stale keeping_previous_rates=true")
            return self.status()

    def record_observed_price(
        self,
        *,
        plan: str,
        country: str,
        currency: str,
        amount: Any,
        source: str = "checkout",
    ) -> bool:
        """Self-calibrate a (plan, country) local tax-free price from a real checkout or manual update."""
        self._ensure_loaded()
        canon_plan = _canonical_plan(plan)
        if canon_plan not in PLAN_BASE_USD:
            return False
        cc = str(country or "").strip().upper()
        cur = str(currency or "").strip().upper()
        if not cc or len(cc) != 2 or not cur or len(cur) > 6:
            return False
        try:
            amt = float(Decimal(str(amount).strip()))
        except (InvalidOperation, ValueError, TypeError):
            return False
        if not math.isfinite(amt) or amt <= 0:
            return False

        with self._lock:
            fx = self._fx_rates.get(cur)
            base_usd = PLAN_BASE_USD[canon_plan]
            # Sanity guard: ignore $0 trials/promos or wildly anomalous values on automatic checkout capture.
            if source == "checkout" and fx and fx > 0:
                usd_equiv = amt / fx
                if usd_equiv < base_usd * 0.2 or usd_equiv > base_usd * 1.5:
                    logger.info(
                        "reference_pricing_skip_outlier plan=%s country=%s cur=%s amt=%.2f usd=%.2f",
                        canon_plan, cc, cur, amt, usd_equiv,
                    )
                    return False

            now = time.time()
            bucket = self._local_prices.setdefault(canon_plan, {})
            bucket[cc] = {
                "currency": cur,
                "amount": round(amt, 2),
                "updated_at": now,
                "source": source[:32],
            }
            self._updated_at = now
            self._persist_locked()
            logger.info(
                "reference_pricing_local_recorded plan=%s country=%s cur=%s amt=%.2f source=%s",
                canon_plan, cc, cur, amt, source,
            )
            return True

    def compute(
        self,
        *,
        regions: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Compute lowest-price winner and full per-region breakdown for every plan."""
        self._ensure_loaded()
        allowed_regions: Dict[str, Tuple[str, str]] = {}
        if regions:
            for item in regions:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code") or "").strip().upper()
                if code:
                    zh = str(item.get("label") or _REGION_LABELS.get(code, (code, code))[0])
                    en = str(item.get("label_en") or _REGION_LABELS.get(code, (code, code))[1])
                    allowed_regions[code] = (zh, en)

        with self._lock:
            fx = dict(self._fx_rates)
            cny_rate = fx.get("CNY") or DEFAULT_FX_RATES["CNY"]
            interval = _refresh_interval_s()
            is_stale = bool(
                self._stale
                or self._fx_updated_at <= 0
                or (time.time() - self._fx_updated_at) > max(interval * 3, 10800.0)
            )
            effective_updated = self._updated_at or self._fx_updated_at

            reference_prices: Dict[str, Dict[str, Any]] = {}
            for plan_id, base_usd in PLAN_BASE_USD.items():
                candidates = self._local_prices.get(plan_id, {})
                region_prices: Dict[str, Dict[str, Any]] = {}
                best: Optional[Dict[str, Any]] = None
                for cc, entry in candidates.items():
                    if allowed_regions and cc not in allowed_regions:
                        continue
                    cur = str(entry.get("currency") or "").upper()
                    amt = float(entry.get("amount") or 0.0)
                    rate = fx.get(cur)
                    if amt <= 0 or not rate or rate <= 0:
                        continue
                    usd = round(amt / rate, 2)
                    cny = round(usd * cny_rate, 2)
                    zh_name, en_name = allowed_regions.get(cc) or _REGION_LABELS.get(cc, (cc, cc))
                    rec = {
                        "country": cc,
                        "region": cur.lower(),
                        "region_name": zh_name,
                        "region_name_en": en_name,
                        "currency": cur,
                        "local_price": round(amt, 2),
                        "usd": usd,
                        "cny": cny,
                        "base": base_usd,
                        "price_updated_at": float(entry.get("updated_at") or 0.0),
                        "updated_at": effective_updated,
                    }
                    region_prices[cc] = {
                        "country": cc,
                        "currency": cur,
                        "local_price": round(amt, 2),
                        "usd": usd,
                        "cny": cny,
                    }
                    if best is None or usd < best["usd"]:
                        best = rec
                if best is not None:
                    zh_disc, en_disc = _discount_labels(best["usd"], base_usd)
                    best["discount"] = zh_disc
                    best["discount_en"] = en_disc
                    best["region_prices"] = region_prices
                    reference_prices[plan_id] = best

            public_fx = {
                code: round(float(rate), 6)
                for code, rate in fx.items()
                if code in ("USD", "CNY", "PHP", "EGP", "CLP", "JPY", "SEK") and rate > 0
            }
            return {
                "usd_cny_rate": round(cny_rate, 4),
                "fx_rates": public_fx,
                "price_updated_at": _format_beijing_hm(effective_updated),
                "reference_prices": reference_prices,
                "reference_price_status": {
                    "enabled": True,
                    "updated_at": effective_updated,
                    "updated_full_label": _format_beijing_full(effective_updated),
                    "fx_updated_at": self._fx_updated_at,
                    "fx_date": _format_beijing_date(self._fx_updated_at or effective_updated),
                    "fx_source": self._fx_source,
                    "stale": is_stale,
                },
            }

    def apply_to_site_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Overlay dynamic FX and lowest-price winners onto portal site config."""
        computed = self.compute(regions=config.get("regions"))
        ref_map = computed["reference_prices"]
        plans: List[Dict[str, Any]] = []
        for plan in config.get("plans") or []:
            if not isinstance(plan, dict):
                continue
            item = dict(plan)
            pid = str(item.get("id") or "")
            winner = ref_map.get(pid)
            if winner and not item.get("coming_soon"):
                item["default_region"] = winner["country"]
                item["usd_price"] = winner["usd"]
                item["cny_price"] = winner["cny"]
                item["local_price"] = winner["local_price"]
                item["local_currency"] = winner["currency"]
                item["base_usd"] = winner["base"]
                item["discount"] = winner["discount"]
                item["discount_en"] = winner["discount_en"]
                item["region_prices"] = winner["region_prices"]
            plans.append(item)

        out = dict(config)
        out["plans"] = plans
        out["usd_cny_rate"] = computed["usd_cny_rate"]
        out["fx_rates"] = computed["fx_rates"]
        if computed["price_updated_at"]:
            out["price_updated_at"] = computed["price_updated_at"]
        out["reference_prices"] = ref_map
        out["reference_price_status"] = computed["reference_price_status"]
        return out

    def status(self) -> Dict[str, Any]:
        self._ensure_loaded()
        with self._lock:
            return {
                "enabled": _refresh_enabled(),
                "running": bool(self._task and not self._task.done()),
                "fx_source": self._fx_source,
                "fx_updated_at": self._fx_updated_at,
                "updated_at": self._updated_at,
                "updated_label": _format_beijing_hm(self._updated_at or self._fx_updated_at),
                "stale": self._stale,
                "usd_cny_rate": round(self._fx_rates.get("CNY", DEFAULT_FX_RATES["CNY"]), 4),
                "last_error": self._last_error,
            }

    async def _loop(self) -> None:
        self._ensure_loaded()
        interval = _refresh_interval_s()
        # Refresh immediately on boot if we don't have fresh FX rates yet.
        needs_initial = (time.time() - self._fx_updated_at) >= interval or self._fx_updated_at <= 0
        if needs_initial:
            try:
                await asyncio.to_thread(self.refresh_fx_sync)
            except Exception:  # noqa: BLE001
                logger.warning("reference_pricing_initial_refresh_failed", exc_info=True)
        while self._stop_event and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                try:
                    await asyncio.to_thread(self.refresh_fx_sync)
                except Exception:  # noqa: BLE001
                    logger.warning("reference_pricing_periodic_refresh_failed", exc_info=True)

    def start(self) -> None:
        self._ensure_loaded()
        if not _refresh_enabled():
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="gss-reference-pricing")

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._stop_event = None


service = ReferencePricingService()


def start() -> None:
    service.start()


async def stop() -> None:
    await service.stop()


def status() -> Dict[str, Any]:
    return service.status()


def refresh_fx_sync(*, timeout: float = 8.0) -> Dict[str, Any]:
    return service.refresh_fx_sync(timeout=timeout)


def apply_to_site_config(config: Dict[str, Any]) -> Dict[str, Any]:
    return service.apply_to_site_config(config)


def record_observed_price(
    *,
    plan: str,
    country: str,
    currency: str,
    amount: Any,
    source: str = "checkout",
) -> bool:
    return service.record_observed_price(
        plan=plan,
        country=country,
        currency=currency,
        amount=amount,
        source=source,
    )


__all__ = [
    "DEFAULT_FX_RATES",
    "DEFAULT_LOCAL_PRICES",
    "PLAN_BASE_USD",
    "ReferencePricingService",
    "apply_to_site_config",
    "fetch_live_fx_rates",
    "record_observed_price",
    "reference_prices_path",
    "refresh_fx_sync",
    "service",
    "start",
    "status",
    "stop",
]
