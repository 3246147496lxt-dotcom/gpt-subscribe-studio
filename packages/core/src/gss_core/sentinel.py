"""签发 ChatGPT OpenAI-Sentinel 令牌。用本仓库的 Node 桥，不依赖其它本地项目。"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from .redaction import redact_sensitive

logger = logging.getLogger("gss.sentinel")

_ASSETS = Path(__file__).with_name("sentinel_assets")
_BRIDGE = _ASSETS / "sentinel_bridge.js"
_BRIDGE_VERSION = "20260810913b"


def extract_attestation(html: str) -> str:
    import re

    text = str(html or "")
    patterns = (
        r'"webDeploymentAttestation"\s*:\s*"([^"]+)"',
        r'[^A-Za-z0-9]webDeploymentAttestation[=:]\s*["\']?([A-Za-z0-9._-]+)',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip()
            if value and "." in value:
                return value
    return ""


def extract_build(html: str) -> tuple[str, str, str]:
    import re

    text = str(html or "")
    prod = re.search(r"prod-[0-9a-f]{40}", text)
    version_match = re.search(r'data-build="([^"]+)"', text)
    seq_match = re.search(r'data-seq="([^"]+)"', text) or re.search(
        r'"buildNumber"\s*:\s*"?(\d+)"?', text
    )
    session_match = re.search(r'"sessionId"\s*:\s*"([0-9a-fA-F-]{36})"', text)
    return (
        prod.group(0) if prod else (version_match.group(1) if version_match else ""),
        seq_match.group(1) if seq_match else "",
        session_match.group(1) if session_match else "",
    )


_SENTINEL_VERSION = re.compile(
    r"/sentinel/([0-9a-z]{6,32})/sdk\.js|[?&]sv=([0-9a-z]{6,32})"
)


def extract_sentinel_version(html: str) -> str:
    """从页面/脚本里解析当前 sv（sentinel 脚本版本），解析不到返回空串。

    写死的 20260810913b 只是兜底；拿得到新版 sv 时 PoW 与 frame.html 都用新版，
    否则 frame_url 与 sdkUrl 会指向不同的 sv，等于自报两套脚本。
    """
    match = _SENTINEL_VERSION.search(str(html or ""))
    if not match:
        return ""
    return (match.group(1) or match.group(2) or "").strip()


def _compact_bridge_error(text: str) -> str:
    """Keep the actionable tail (exception line), not the head of a traceback."""
    raw = str(text or "").strip()
    if "reqfail:" in raw:
        body = raw.split("reqfail:", 1)[1]
        try:
            parsed, _ = json.JSONDecoder().raw_decode(body)
            inner = str(parsed.get("e") or "")
            line = next((item for item in reversed(inner.splitlines()) if item.strip()), "")
            if line:
                return ("proxy fetch: " + line.strip())[:320]
        except (ValueError, AttributeError, TypeError):
            pass
    lines = [item for item in raw.splitlines() if item.strip()]
    return (lines[-1] if lines else raw)[:320]


def header_value(token: Dict[str, Any]) -> str:
    if not isinstance(token, dict) or not token.get("p") or not token.get("c"):
        return ""
    payload = {key: token[key] for key in token if not str(key).startswith("_")}
    try:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def so_header_value(token: Dict[str, Any]) -> str:
    so_token = token.get("_so") if isinstance(token, dict) else None
    if not isinstance(so_token, dict) or not so_token.get("so"):
        return ""
    try:
        return json.dumps(so_token, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def mint(
    *,
    flow: str,
    device_id: str,
    user_agent: str,
    proxy: str = "",
    page_url: str = "https://chatgpt.com/",
    language: str = "en-US",
    timezone: str = "America/New_York",
    cookie_header: str = "",
    timeout_s: float = 90.0,
    platform: str = "Linux x86_64",
    platform_label: str = "Linux",
    screen_w: int = 1920,
    screen_h: int = 1080,
    avail_h: int = 1032,
    cores: int = 8,
    impersonate: str = "",
    sec_ch_ua: str = "",
    sec_ch_ua_platform: str = "",
    sec_ch_ua_arch: str = "",
    sec_ch_ua_bitness: str = "",
    sec_ch_ua_platform_version: str = "",
    version: str = "",
    data_build: str = "",
) -> Dict[str, Any]:
    """返回哨兵 JSON；失败返回空 dict，由调用方决定是否中止。"""

    if not _BRIDGE.is_file():
        return {"_error": f"sentinel bridge missing: {_BRIDGE}"}
    payload = json.dumps(
        {
            "ua": user_agent,
            "cores": cores,
            "deviceId": device_id,
            "flow": flow,
            "proxy": proxy or "",
            "version": str(version or "").strip() or _BRIDGE_VERSION,
            "dataBuild": str(data_build or "").strip(),
            "pageUrl": page_url,
            "language": language,
            "timezone": timezone,
            "platform": platform,
            "platformLabel": platform_label,
            "screenW": screen_w,
            "screenH": screen_h,
            "availH": avail_h,
            "maxTouchPoints": 0,
            "cookieHeader": cookie_header,
            "sentinelOrigin": "https://chatgpt.com",
            "impersonate": impersonate or "",
            "secChUa": sec_ch_ua or "",
            "secChUaPlatform": sec_ch_ua_platform or "",
            # 传入沙箱 navigator.userAgentData.getHighEntropyValues；
            # HTTP 线缆层默认只发低熵三件套（与 2026-11-26 实测一致）。
            "secChUaArch": sec_ch_ua_arch or "",
            "secChUaBitness": sec_ch_ua_bitness or "",
            "secChUaPlatformVersion": sec_ch_ua_platform_version or "",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    env = os.environ.copy()
    env.setdefault("PYTHON_BIN", sys.executable)
    env.setdefault("SENTINEL_PYTHON", sys.executable)
    try:
        process = subprocess.run(
            [os.environ.get("SENTINEL_NODE") or "node", str(_BRIDGE)],
            input=payload,
            capture_output=True,
            timeout=timeout_s,
            cwd=str(_ASSETS),
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return {"_error": "node executable not found"}
    except subprocess.TimeoutExpired:
        return {"_error": "sentinel bridge timed out"}
    output = (process.stdout or b"").decode("utf-8", "replace").strip()
    stderr = (process.stderr or b"").decode("utf-8", "replace").strip()
    if process.returncode != 0:
        logger.warning("sentinel_bridge_exit code=%s stderr=%s",
                       process.returncode, redact_sensitive(stderr)[:1500])
        return {"_error": f"node exit {process.returncode}: {_compact_bridge_error(stderr)}"}
    if not output:
        logger.warning("sentinel_bridge_empty stderr=%s", redact_sensitive(stderr)[:1500])
        return {"_error": f"empty node output: {_compact_bridge_error(stderr)}"}
    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        logger.warning("sentinel_bridge_bad_json output=%s", redact_sensitive(output)[:1500])
        return {"_error": f"bad node json: {output[:120]}"}
    if result.get("error"):
        raw_error = str(result.get("error"))
        logger.warning("sentinel_bridge_error %s", redact_sensitive(raw_error)[:1500])
        return {"_error": _compact_bridge_error(raw_error)}
    if not result.get("main"):
        return {"_error": "node returned no main token"}
    try:
        token = json.loads(str(result.get("main") or ""))
    except json.JSONDecodeError:
        return {"_error": "bad main token json"}
    if not isinstance(token, dict):
        return {"_error": "main token is not an object"}
    so_raw = str(result.get("so") or "")
    if so_raw:
        try:
            token["_so"] = json.loads(so_raw)
        except json.JSONDecodeError:
            pass
    return token


__all__ = [
    "extract_attestation",
    "extract_build",
    "extract_sentinel_version",
    "header_value",
    "so_header_value",
    "mint",
]
