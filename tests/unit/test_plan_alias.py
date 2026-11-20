from gss_api.routers.orders import normalize_plan_name


def test_pro5_maps_to_internal_pro_lite():
    assert normalize_plan_name("pro5") == "pro_lite"
    assert normalize_plan_name("PRO5") == "pro_lite"
    assert normalize_plan_name("pro_lite") == "pro_lite"
    assert normalize_plan_name("plus") == "plus"
