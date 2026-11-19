from gss_core.route_profiles import (
    browser_decode_fallback_a,
    browser_decode_enabled,
    route_plan_from_env,
)


def test_default_is_a(monkeypatch):
    for key in ("GSS_ROUTE_PROFILE", "GSS_BROWSER_DECODE", "GSS_BROWSER_DECODE_FALLBACK"):
        monkeypatch.delenv(key, raising=False)
    plan = route_plan_from_env()
    assert plan.profile.value == "A"
    assert plan.decode_via == "proxy"


def test_profile_c_and_flags(monkeypatch):
    monkeypatch.setenv("GSS_ROUTE_PROFILE", "C")
    monkeypatch.setenv("GSS_BROWSER_DECODE", "1")
    monkeypatch.setenv("GSS_BROWSER_DECODE_FALLBACK", "A")
    plan = route_plan_from_env()
    assert plan.profile.value == "C"
    assert plan.decode_via == "direct"
    assert browser_decode_enabled() is True
    assert browser_decode_fallback_a() is True


def test_unknown_profile_falls_back_a(monkeypatch):
    monkeypatch.setenv("GSS_ROUTE_PROFILE", "Z")
    assert route_plan_from_env().profile.value == "A"
