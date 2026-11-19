from gss_contracts.route import RoutePlan, RouteProfile


def test_route_a_is_legacy():
    plan = RoutePlan.for_profile("A")
    assert plan.decode_via == "proxy"
    assert plan.checkout_via == "proxy"
    assert plan.confirm_via == "proxy"
    assert plan.use_browser_context is False


def test_route_c_separates_decode_but_not_payment():
    plan = RoutePlan.for_profile("C")
    assert plan.profile is RouteProfile.C
    assert plan.decode_via == "direct"
    assert plan.checkout_via == "proxy"
    assert plan.confirm_via == "proxy"
    assert plan.use_browser_context is True
