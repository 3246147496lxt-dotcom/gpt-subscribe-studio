"""UA-CH（User-Agent Client Hints）单一来源。

低熵 `sec-ch-ua`、高熵 `sec-ch-ua-full-version-list` 与 JS 侧
`navigator.userAgentData` 必须共用同一个 GREASE 品牌、版本与排列顺序。

实现直接对齐 Chromium `GenerateBrandVersionList`：
* `greasey_chars = " (:-./);=?_"`，按 `major % 11` 与 `(major + 1) % 11` 拼接 `Not{c0}A{c1}Brand`；
* 版本号按 `major % 3` 从 `("8", "99", "24")` 轮换；
* 三元组排列顺序按 `major % 6` 从 6 种置换中选取；
* 传入 `override`（如真实浏览器采集的 `sec-ch-ua`）时原样保留品牌与顺序，兼容 Edge 等非 Chrome 品牌。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

_GREASEY_CHARS = " (:-./);=?_"
GREASE_BRANDS = tuple(
    f"Not{_GREASEY_CHARS[i]}A{_GREASEY_CHARS[(i + 1) % len(_GREASEY_CHARS)]}Brand"
    for i in range(len(_GREASEY_CHARS))
)
GREASE_VERSIONS = ("8", "99", "24")
_ORDERS = (
    (0, 1, 2),
    (0, 2, 1),
    (1, 0, 2),
    (1, 2, 0),
    (2, 0, 1),
    (2, 1, 0),
)

_TOKEN = re.compile(r'"([^"]*)"\s*;\s*v="([^"]*)"')
_GREASE_BRAND_RE = re.compile(r"Not.A.Brand")

REAL_BRANDS = (
    "Chromium",
    "Google Chrome",
    "Microsoft Edge",
    "HeadlessChrome",
    "Opera",
    "Brave",
    "Vivaldi",
)


def major_of(version_or_major: str) -> int:
    try:
        return int(str(version_or_major or "").split(".", 1)[0])
    except (TypeError, ValueError):
        return 150


def _parse_tokens(raw: Optional[str]) -> List[Tuple[str, str]]:
    text = str(raw or "").strip()
    if not text:
        return []
    return [(brand, version) for brand, version in _TOKEN.findall(text) if brand]


def _is_grease_brand(brand: str, version: str, expected_major: str) -> bool:
    if _GREASE_BRAND_RE.fullmatch(brand):
        return True
    if brand in REAL_BRANDS:
        return False
    return bool(version) and version != expected_major


def chromium_grease(major: str) -> Tuple[str, str]:
    seed = major_of(major)
    n = len(_GREASEY_CHARS)
    brand = f"Not{_GREASEY_CHARS[seed % n]}A{_GREASEY_CHARS[(seed + 1) % n]}Brand"
    version = GREASE_VERSIONS[seed % len(GREASE_VERSIONS)]
    return brand, version


def grease_brand(major: str, override: Optional[str] = None) -> Tuple[str, str]:
    """返回 (GREASE 品牌, 版本)。

    `override` 是上游/浏览器上下文提供的完整 `sec-ch-ua`：仅识别其中的 GREASE
    品牌，绝不把 `Microsoft Edge` 等真实浏览器品牌误认成 GREASE。
    """
    tokens = _parse_tokens(override)
    m = str(major_of(major))
    for brand, version in tokens:
        if _GREASE_BRAND_RE.fullmatch(brand) and version:
            return brand, version
    for brand, version in tokens:
        if _is_grease_brand(brand, version, m):
            return brand, version
    return chromium_grease(major)


def brand_list(major: str, override: Optional[str] = None) -> List[Tuple[str, str]]:
    tokens = _parse_tokens(override)
    if tokens:
        return tokens
    seed = major_of(major)
    m = str(major or "").split(".", 1)[0] or str(seed)
    items = [
        chromium_grease(m),
        ("Chromium", m),
        ("Google Chrome", m),
    ]
    order = _ORDERS[seed % len(_ORDERS)]
    return [item for _, item in sorted(zip(order, items), key=lambda pair: pair[0])]


def full_version_brand_list(build: str, override: Optional[str] = None) -> List[Tuple[str, str]]:
    b = str(build or "").strip() or "150.0.7871.189"
    m = b.split(".", 1)[0] or "150"
    tokens = _parse_tokens(override)
    if tokens:
        out: List[Tuple[str, str]] = []
        for brand, version in tokens:
            if _is_grease_brand(brand, version, m):
                full_ver = version if "." in version else f"{version}.0.0.0"
                out.append((brand, full_ver))
            else:
                out.append((brand, b))
        return out
    seed = major_of(m)
    grease_b, grease_v = chromium_grease(m)
    grease_full = grease_v if "." in grease_v else f"{grease_v}.0.0.0"
    items = [
        (grease_b, grease_full),
        ("Chromium", b),
        ("Google Chrome", b),
    ]
    order = _ORDERS[seed % len(_ORDERS)]
    return [item for _, item in sorted(zip(order, items), key=lambda pair: pair[0])]


def format_brand_list(entries: List[Tuple[str, str]]) -> str:
    return ", ".join(f'"{brand}";v="{version}"' for brand, version in entries)


def sec_ch_ua(major: str, override: Optional[str] = None) -> str:
    """低熵 `sec-ch-ua`；有 `override` 时原样保序，否则按 Chromium 规则生成。"""
    text = str(override or "").strip()
    if text:
        return text
    return format_brand_list(brand_list(major))


def sec_ch_ua_full_version_list(build: str, override: Optional[str] = None) -> str:
    """高熵 `sec-ch-ua-full-version-list`，与低熵同源且保序。"""
    return format_brand_list(full_version_brand_list(build, override))


__all__ = [
    "GREASE_BRANDS",
    "GREASE_VERSIONS",
    "brand_list",
    "chromium_grease",
    "format_brand_list",
    "full_version_brand_list",
    "grease_brand",
    "major_of",
    "sec_ch_ua",
    "sec_ch_ua_full_version_list",
]
