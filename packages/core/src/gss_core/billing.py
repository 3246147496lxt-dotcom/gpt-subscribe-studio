"""账单地址。美国始终返回同一份免税州固定地址，其它国家才用本地样例模板。

出口代理与账单国家互不绑定：出口可以按订单自由变化，账单只看固定资料。
"""

from __future__ import annotations

import secrets
from typing import Dict, List, Mapping, TypedDict

from gss_core.countries import normalize_country
from gss_core.fixed_billing import BILLING_COUNTRY, fixed_billing_address


class BillingAddress(TypedDict):
    name: str
    line1: str
    city: str
    state: str
    postal_code: str
    country: str


class _Place(TypedDict):
    city: str
    state: str
    postal_code: str
    streets: List[str]


_FIRST = ("James", "David", "Michael", "Daniel", "Emma", "Olivia", "Sofia", "Maria")
_LAST = ("Miller", "Johnson", "Garcia", "Santos", "Kim", "Tanaka", "Silva", "Nguyen")

_PLACES: Dict[str, List[_Place]] = {
    "PH": [
        {"city": "Makati", "state": "NCR", "postal_code": "1226", "streets": ["Ayala Avenue", "Paseo de Roxas"]},
        {"city": "Quezon City", "state": "NCR", "postal_code": "1100", "streets": ["EDSA", "Quezon Avenue"]},
    ],
    "CL": [
        {"city": "Santiago", "state": "RM", "postal_code": "8320000", "streets": ["Avenida Libertador Bernardo O'Higgins", "Avenida Providencia"]},
    ],
    "EG": [
        {"city": "Cairo", "state": "C", "postal_code": "11511", "streets": ["Tahrir Square", "Kasr El Nil Street"]},
    ],
    "JP": [
        {"city": "Shibuya", "state": "Tokyo", "postal_code": "150-0002", "streets": ["Dogenzaka", "Jingumae"]},
        {"city": "Chuo", "state": "Osaka", "postal_code": "541-0041", "streets": ["Bingo-machi", "Kitahama"]},
    ],
    "GB": [
        {"city": "London", "state": "ENG", "postal_code": "SW1A 1AA", "streets": ["Baker Street", "Oxford Street"]},
    ],
    "TR": [
        {"city": "Istanbul", "state": "34", "postal_code": "34433", "streets": ["Istiklal Caddesi", "Bagdat Caddesi"]},
    ],
    "BR": [
        {"city": "Sao Paulo", "state": "SP", "postal_code": "01310-100", "streets": ["Avenida Paulista", "Rua Augusta"]},
    ],
    "MX": [
        {"city": "Ciudad de Mexico", "state": "CMX", "postal_code": "06600", "streets": ["Paseo de la Reforma", "Avenida Insurgentes Sur"]},
    ],
    "IN": [
        {"city": "Mumbai", "state": "MH", "postal_code": "400001", "streets": ["Dr Dadabhai Naoroji Road", "Marine Drive"]},
    ],
    "ID": [
        {"city": "Jakarta", "state": "JK", "postal_code": "12190", "streets": ["Jl. Jend. Sudirman", "Jl. MH Thamrin"]},
    ],
    "VN": [
        {"city": "Ho Chi Minh City", "state": "SG", "postal_code": "700000", "streets": ["Dong Khoi", "Nguyen Hue"]},
    ],
    "TH": [
        {"city": "Bangkok", "state": "10", "postal_code": "10330", "streets": ["Sukhumvit Road", "Silom Road"]},
    ],
    "MY": [
        {"city": "Kuala Lumpur", "state": "KL", "postal_code": "50088", "streets": ["Jalan Ampang", "Jalan Bukit Bintang"]},
    ],
    "SG": [
        {"city": "Singapore", "state": "SG", "postal_code": "018956", "streets": ["Raffles Place", "Orchard Road"]},
    ],
    "KR": [
        {"city": "Seoul", "state": "11", "postal_code": "04524", "streets": ["Jongno", "Gangnam-daero"]},
    ],
    "AU": [
        {"city": "Sydney", "state": "NSW", "postal_code": "2000", "streets": ["George Street", "Pitt Street"]},
    ],
    "CA": [
        {"city": "Toronto", "state": "ON", "postal_code": "M5V 2T6", "streets": ["King Street West", "Queen Street West"]},
    ],
    "DE": [
        {"city": "Berlin", "state": "BE", "postal_code": "10115", "streets": ["Unter den Linden", "Friedrichstrasse"]},
    ],
    "NL": [
        {"city": "Amsterdam", "state": "NH", "postal_code": "1012 JS", "streets": ["Damrak", "Kalverstraat"]},
    ],
    "PL": [
        {"city": "Warsaw", "state": "MZ", "postal_code": "00-001", "streets": ["Nowy Swiat", "Marszalkowska"]},
    ],
    "AR": [
        {"city": "Buenos Aires", "state": "C", "postal_code": "C1002", "streets": ["Avenida 9 de Julio", "Calle Florida"]},
    ],
    "SA": [
        {"city": "Riyadh", "state": "01", "postal_code": "12211", "streets": ["King Fahd Road", "Olaya Street"]},
    ],
    "AE": [
        {"city": "Dubai", "state": "DU", "postal_code": "00000", "streets": ["Sheikh Zayed Road", "Al Wasl Road"]},
    ],
    "ZA": [
        {"city": "Cape Town", "state": "WC", "postal_code": "8001", "streets": ["Long Street", "Adderley Street"]},
    ],
}


def address_for(country: str) -> BillingAddress:
    code = normalize_country(country) or BILLING_COUNTRY
    if code == BILLING_COUNTRY:
        # 固定地址：同一部署的每一笔订单都完全一致，不再随机。
        return dict(fixed_billing_address())
    places = _PLACES.get(code)
    if not places:
        return dict(fixed_billing_address())
    place = secrets.choice(places)
    street = secrets.choice(place["streets"])
    number = secrets.randbelow(180) + 12
    name = f"{secrets.choice(_FIRST)} {secrets.choice(_LAST)}"
    return {
        "name": name,
        "line1": f"{number} {street}",
        "city": place["city"],
        "state": place["state"],
        "postal_code": place["postal_code"],
        "country": code,
    }


def stripe_billing_details(address: Mapping[str, str]) -> Dict[str, object]:
    return {
        "name": address.get("name") or "",
        "address": {
            "line1": address.get("line1") or "",
            "city": address.get("city") or "",
            "state": address.get("state") or "",
            "postal_code": address.get("postal_code") or "",
            "country": address.get("country") or "",
        },
    }


__all__ = ["BillingAddress", "address_for", "fixed_billing_address", "stripe_billing_details"]
