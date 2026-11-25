"""Configurable public customer-portal site metadata (brand, announcements, plans, links)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_PORTAL_CONFIG: Dict[str, Any] = {
    "brand_name": "GPT 协议充值",
    "brand_name_en": "GPT Protocol Recharge",
    "footer_slogan": "给学习和灵感续航",
    "footer_slogan_en": "Powering learning and inspiration",
    "hero_title": "公益协议，安全快速",
    "hero_title_en": "Fast, Safe Protocol Recharge",
    "hero_subtitle_1": "优选全球优惠地区，开通自己的 ChatGPT。",
    "hero_subtitle_1_en": "Pick discounted regions and activate your own ChatGPT.",
    "hero_subtitle_2": "享受官方套餐权益，不为同样的订阅多花钱。",
    "hero_subtitle_2_en": "Official subscription benefits without overpaying.",
    "card_shop_url": "",
    "card_shop_label": "去卡网购买",
    "card_shop_label_en": "Buy a card",
    "support_url": "",
    "support_label": "联系客服",
    "support_label_en": "Support",
    "support_note": "如需排查失败订单或获取帮助，请联系站点管理员。",
    "support_note_en": "Need help troubleshooting a failed order? Contact the site administrator.",
    "telegram_login_enabled": False,
    "usd_cny_rate": 6.70,
    "price_updated_at": "11/25 09:00",
    "price_note": "默认匹配当前最低价地区，实际扣款金额以提交后结账回执为准。",
    "price_note_en": "Default matches the lowest reference region; final charge follows checkout confirmation.",
    "announcements": [
        {
            "id": "1",
            "text": "支持粘贴 Session 或拖入文件本地解析，充值完成后自动尝试关闭续费。",
            "text_en": "Paste or drop a Session file locally; auto-renewal cancellation runs right after activation.",
        },
        {
            "id": "2",
            "text": "充值失败或遇到扣款状态待确认时请勿重复下单，可在「我的任务」随时查看进度。",
            "text_en": "Do not resubmit while payment status is pending; track live progress anytime in My Tasks.",
        },
    ],
    "plans": [
        {
            "id": "go",
            "title": "Go",
            "title_en": "Go",
            "full_name": "ChatGPT Go",
            "default_region": "EG",
            "usd_price": 3.59,
            "cny_price": 24.00,
            "discount": "4.5折",
            "discount_en": "55% OFF",
            "available": True,
        },
        {
            "id": "plus",
            "title": "Plus",
            "title_en": "Plus",
            "full_name": "ChatGPT Plus",
            "default_region": "PH",
            "usd_price": 15.35,
            "cny_price": 102.74,
            "discount": "7.7折",
            "discount_en": "23% OFF",
            "available": True,
        },
        {
            "id": "pro_lite",
            "title": "Pro 100",
            "title_en": "Pro 100",
            "full_name": "ChatGPT Pro 100",
            "default_region": "CL",
            "usd_price": 85.17,
            "cny_price": 570.04,
            "discount": "8.5折",
            "discount_en": "15% OFF",
            "available": True,
        },
        {
            "id": "pro",
            "title": "Pro 200",
            "title_en": "Pro 200",
            "full_name": "ChatGPT Pro 200",
            "default_region": "PH",
            "usd_price": 139.40,
            "cny_price": 933.07,
            "discount": "7折",
            "discount_en": "30% OFF",
            "available": True,
        },
        {
            "id": "pro_max",
            "title": "Pro 500",
            "title_en": "Pro 500",
            "full_name": "ChatGPT Pro 500",
            "default_region": "CL",
            "usd_price": 422.61,
            "cny_price": 2828.68,
            "discount": "8.5折",
            "discount_en": "15% OFF",
            "available": True,
        },
        {
            "id": "codex",
            "title": "Codex 点数",
            "title_en": "Codex Credits",
            "full_name": "Codex 点数",
            "default_region": "PH",
            "usd_price": None,
            "cny_price": None,
            "discount": "",
            "discount_en": "",
            "available": False,
            "coming_soon": True,
        },
    ],
    "regions": [
        {"code": "PH", "label": "菲律宾", "label_en": "Philippines", "currency": "PHP"},
        {"code": "JP", "label": "日本", "label_en": "Japan", "currency": "JPY"},
        {"code": "US", "label": "美国", "label_en": "United States", "currency": "USD"},
        {"code": "EG", "label": "埃及", "label_en": "Egypt", "currency": "EGP"},
        {"code": "CL", "label": "智利", "label_en": "Chile", "currency": "CLP"},
    ],
}


def portal_config_path() -> Path:
    explicit = os.environ.get("GSS_PORTAL_CONFIG_FILE", "").strip()
    if explicit:
        return Path(explicit)
    config_dir = Path(os.environ.get("GSS_CONFIG_DIR", "config"))
    return config_dir / "portal-site.json"


def _safe_external_url(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("https://") or text.startswith("http://"):
        return text[:500]
    return ""


def _merge_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(json.dumps(DEFAULT_PORTAL_CONFIG))
    str_keys = (
        "brand_name", "brand_name_en", "footer_slogan", "footer_slogan_en",
        "hero_title", "hero_title_en", "hero_subtitle_1", "hero_subtitle_1_en",
        "hero_subtitle_2", "hero_subtitle_2_en", "card_shop_label", "card_shop_label_en",
        "support_label", "support_label_en", "support_note", "support_note_en",
        "price_updated_at", "price_note", "price_note_en",
    )
    for key in str_keys:
        if isinstance(raw.get(key), str) and raw[key].strip():
            out[key] = raw[key].strip()[:300]
    for url_key in ("card_shop_url", "support_url"):
        if url_key in raw:
            out[url_key] = _safe_external_url(raw.get(url_key))
    if isinstance(raw.get("telegram_login_enabled"), bool):
        out["telegram_login_enabled"] = raw["telegram_login_enabled"]
    if isinstance(raw.get("usd_cny_rate"), (int, float)) and raw["usd_cny_rate"] > 0:
        out["usd_cny_rate"] = round(float(raw["usd_cny_rate"]), 4)
    if isinstance(raw.get("announcements"), list):
        cleaned: List[Dict[str, str]] = []
        for idx, item in enumerate(raw["announcements"][:20], start=1):
            if isinstance(item, str) and item.strip():
                cleaned.append({"id": str(idx), "text": item.strip()[:240], "text_en": item.strip()[:240]})
            elif isinstance(item, dict) and str(item.get("text") or "").strip():
                text = str(item["text"]).strip()[:240]
                text_en = str(item.get("text_en") or text).strip()[:240]
                cleaned.append({"id": str(item.get("id") or idx)[:24], "text": text, "text_en": text_en})
        if cleaned:
            out["announcements"] = cleaned
    if isinstance(raw.get("plans"), list) and raw["plans"]:
        out["plans"] = raw["plans"]
    if isinstance(raw.get("regions"), list) and raw["regions"]:
        out["regions"] = raw["regions"]
    return out


def load_portal_config() -> Dict[str, Any]:
    path = portal_config_path()
    merged: Dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                merged = loaded
        except (OSError, ValueError):
            merged = {}
    config = _merge_config(merged)
    # Optional env overrides for quick deployment tweaks.
    if os.environ.get("GSS_PORTAL_BRAND", "").strip():
        config["brand_name"] = os.environ["GSS_PORTAL_BRAND"].strip()[:80]
    if os.environ.get("GSS_PORTAL_CARD_SHOP_URL", "").strip():
        config["card_shop_url"] = _safe_external_url(os.environ["GSS_PORTAL_CARD_SHOP_URL"])
    if os.environ.get("GSS_PORTAL_SUPPORT_URL", "").strip():
        config["support_url"] = _safe_external_url(os.environ["GSS_PORTAL_SUPPORT_URL"])
    return config


def save_portal_config(update: Dict[str, Any]) -> Dict[str, Any]:
    current = load_portal_config()
    combined = {**current, **(update or {})}
    cleaned = _merge_config(combined)
    path = portal_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return cleaned


__all__ = ["DEFAULT_PORTAL_CONFIG", "load_portal_config", "save_portal_config", "portal_config_path"]
