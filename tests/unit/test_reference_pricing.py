"""Dynamic FX rate sync, automatic lowest-price selection, and self-calibration tests."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gss_api.routers import public_portal
from gss_api.services import reference_pricing


def _fresh_service(monkeypatch, tmp_path):
    price_file = tmp_path / "reference-prices.json"
    monkeypatch.setenv("GSS_REFERENCE_PRICES_FILE", str(price_file))
    svc = reference_pricing.ReferencePricingService()
    return svc, price_file


def test_fx_refresh_computes_winners_discounts_and_persists(monkeypatch, tmp_path):
    svc, price_file = _fresh_service(monkeypatch, tmp_path)
    fake_rates = {
        "USD": 1.0,
        "CNY": 6.68,
        "PHP": 63.77,
        "EGP": 54.23,
        "CLP": 1016.25,
        "JPY": 159.24,
    }
    monkeypatch.setattr(reference_pricing, "fetch_live_fx_rates", lambda timeout=8.0: (fake_rates, "mock-fx"))

    status = svc.refresh_fx_sync()
    assert status["stale"] is False
    assert status["fx_source"] == "mock-fx"
    assert price_file.is_file()

    out = svc.compute()
    refs = out["reference_prices"]
    assert refs["go"]["country"] == "EG"
    assert refs["go"]["usd"] == round(192.98 / 54.23, 2)
    assert refs["go"]["discount"] == "4.5折"

    assert refs["plus"]["country"] == "PH"
    assert refs["plus"]["usd"] == round(982.14 / 63.77, 2)
    assert refs["plus"]["discount"] == "7.7折"

    assert refs["pro_lite"]["country"] == "CL"
    assert set(refs["pro_lite"]["region_prices"].keys()) >= {"PH", "EG", "CL"}
    assert refs["pro"]["country"] == "PH"
    assert set(refs["pro"]["region_prices"].keys()) >= {"PH", "EG", "CL"}
    assert refs["pro_max"]["country"] == "CL"
    assert set(refs["pro_max"]["region_prices"].keys()) >= {"PH", "EG", "CL"}
    assert out["reference_price_status"]["stale"] is False
    assert out["price_updated_at"] != ""


def test_winner_dynamically_flips_when_fx_moves(monkeypatch, tmp_path):
    svc, _ = _fresh_service(monkeypatch, tmp_path)
    # Weak JPY makes JP (2727 JPY / 220 = $12.40) cheaper than PH (982.14 / 63.77 = $15.40).
    weak_jpy = {
        "USD": 1.0,
        "CNY": 7.0,
        "PHP": 63.77,
        "EGP": 54.23,
        "CLP": 1016.25,
        "JPY": 220.0,
    }
    monkeypatch.setattr(reference_pricing, "fetch_live_fx_rates", lambda timeout=8.0: (weak_jpy, "mock-weak-jpy"))
    svc.refresh_fx_sync()

    out = svc.compute()
    assert out["reference_prices"]["plus"]["country"] == "JP"
    assert out["reference_prices"]["pro_lite"]["country"] == "JP"
    assert out["reference_prices"]["pro_max"]["country"] == "JP"


def test_failed_fx_keeps_last_valid_rates_and_marks_stale(monkeypatch, tmp_path):
    svc, _ = _fresh_service(monkeypatch, tmp_path)
    good_rates = {
        "USD": 1.0,
        "CNY": 6.80,
        "PHP": 64.0,
        "EGP": 54.0,
        "CLP": 1000.0,
        "JPY": 155.0,
    }
    monkeypatch.setattr(reference_pricing, "fetch_live_fx_rates", lambda timeout=8.0: (good_rates, "primary"))
    svc.refresh_fx_sync()

    monkeypatch.setattr(reference_pricing, "fetch_live_fx_rates", lambda timeout=8.0: (None, ""))
    status = svc.refresh_fx_sync()
    assert status["stale"] is True
    assert status["usd_cny_rate"] == 6.80


def test_observed_checkout_calibrates_local_price_and_rejects_outliers(monkeypatch, tmp_path):
    svc, price_file = _fresh_service(monkeypatch, tmp_path)
    rates = {"USD": 1.0, "CNY": 6.70, "PHP": 63.77, "EGP": 54.23, "CLP": 1016.25, "JPY": 159.24}
    monkeypatch.setattr(reference_pricing, "fetch_live_fx_rates", lambda timeout=8.0: (rates, "mock"))
    svc.refresh_fx_sync()

    # Promo $0 or $1 outlier must be rejected for Plus ($20 base).
    assert svc.record_observed_price(plan="plus", country="PH", currency="PHP", amount="10.00", source="checkout") is False
    assert svc.record_observed_price(plan="plus", country="PH", currency="PHP", amount="99999", source="checkout") is False

    # Realistic observed checkout updates local price and survives reload from disk.
    assert svc.record_observed_price(plan="plus", country="EG", currency="EGP", amount="750.00", source="checkout") is True
    reloaded = reference_pricing.ReferencePricingService()
    out = reloaded.compute()
    # 750 / 54.23 = $13.83 < PH $15.40 -> EG becomes the new Plus lowest-price winner!
    assert out["reference_prices"]["plus"]["country"] == "EG"
    assert out["reference_prices"]["plus"]["local_price"] == 750.0
    assert price_file.is_file()


def test_public_site_config_and_admin_refresh_endpoints(monkeypatch, tmp_path):
    price_file = tmp_path / "api-ref-prices.json"
    monkeypatch.setenv("GSS_REFERENCE_PRICES_FILE", str(price_file))
    monkeypatch.setenv("GSS_PORTAL_CONFIG_FILE", str(tmp_path / "portal-site.json"))
    fresh = reference_pricing.ReferencePricingService()
    monkeypatch.setattr(reference_pricing, "service", fresh)
    monkeypatch.setattr(
        reference_pricing,
        "fetch_live_fx_rates",
        lambda timeout=8.0: (
            {"USD": 1.0, "CNY": 6.72, "PHP": 64.0, "EGP": 55.0, "CLP": 1020.0, "JPY": 160.0},
            "mock-api",
        ),
    )

    app = FastAPI()
    app.include_router(public_portal.router)
    app.include_router(public_portal.admin_router)
    client = TestClient(app)

    res = client.post("/api/settings/reference-prices/refresh")
    assert res.status_code == 200
    body = res.json()
    assert body["reference_price_status"]["fx_source"] == "mock-api"
    assert body["reference_price_status"]["stale"] is False

    cfg = client.get("/api/public/site-config")
    assert cfg.status_code == 200
    data = cfg.json()
    assert data["usd_cny_rate"] == 6.72
    assert data["reference_price_status"]["stale"] is False
    plus = next(p for p in data["plans"] if p["id"] == "plus")
    assert plus["default_region"] == "PH"
    assert plus["usd_price"] == round(982.14 / 64.0, 2)
    assert "EG" in plus["region_prices"]
