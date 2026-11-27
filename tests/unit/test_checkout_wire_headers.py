"""Regression tests for real wire HTTP headers & Sentinel shim consistency.

Verifies on a real local loopback HTTP server using curl_cffi that:
1. ChatGPTClient never emits duplicate Cookie headers, duplicate mixed-case
   sentinel/attestation headers, custom X-GSS-* headers, or curl_cffi
   document-navigation defaults (Upgrade-Insecure-Requests / Sec-Fetch-User)
   on CORS API requests.
2. sentinel_curl_fetch disables curl_cffi default navigation headers so
   POST /backend-api/sentinel/req carries proper CORS sec-fetch-* headers.
3. sentinel_bootstrap.js aligns origin, visualViewport, and userAgentData brand
   with chatgpt.com and sec-ch-ua.
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import socketserver
import subprocess
import threading
from pathlib import Path

from gss_core.chatgpt import ChatGPTClient, _accept_language

ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = ROOT / "packages/core/src/gss_core/sentinel_assets"


def _load_sentinel_curl_fetch():
    path = ASSETS_DIR / "sentinel_curl_fetch.py"
    spec = importlib.util.spec_from_file_location("sentinel_curl_fetch_wire", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_accept_language_deduplicates_english():
    assert _accept_language("en-US") == "en-US,en;q=0.9"
    assert _accept_language("en-PH") == "en-PH,en;q=0.9"
    assert _accept_language("ja-JP") == "ja-JP,ja;q=0.9,en;q=0.8"


def test_chatgpt_client_wire_headers_and_single_cookie_after_warmup():
    captured: list[tuple[str, str, list[tuple[str, str]]]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            captured.append(("GET", self.path, list(self.headers.items())))
            self.send_response(200)
            self.send_header("Set-Cookie", "__cf_bm=edge_bm_1; Path=/")
            self.send_header("Set-Cookie", "oai-sc=edge_sc_1; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html></html>")

        def do_POST(self):
            captured.append(("POST", self.path, list(self.headers.items())))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    client = ChatGPTClient(
        access_token="tok_wire",
        device_id="a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
        session_token="st_wire",
        oai_session_id="b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
        language="en-PH",
        timezone="Asia/Manila",
        purchase_attempt_id="pa_internal_only",
    )
    client.attestation = "att.wire"
    client.sentinel_token = '{"p":"1","c":"2"}'
    client.sentinel_so = '{"so":"3","c":"2"}'
    build = client.chrome_build

    try:
        client.warmup(page_url=f"http://127.0.0.1:{port}/")
        resp = client.http.post(
            f"http://127.0.0.1:{port}/backend-api/payments/checkout",
            json={"plan_name": "chatgptplusplan"},
            headers=client._route_headers("/backend-api/payments/checkout"),
        )
        assert resp.status_code == 200
    finally:
        client.close()
        srv.shutdown()
        srv.server_close()

    assert len(captured) == 2
    _, _, warmup_hdrs = captured[0]
    warmup_map = {k.lower(): v for k, v in warmup_hdrs}
    assert warmup_map["sec-fetch-mode"] == "navigate"
    assert warmup_map["sec-fetch-dest"] == "document"
    assert warmup_map["upgrade-insecure-requests"] == "1"
    assert "authorization" not in warmup_map
    assert "origin" not in warmup_map
    assert "content-type" not in warmup_map
    # 顶层文档导航：真 Chrome 不发 API 专有头，`sec-fetch-site: none` 时不带 Referer。
    nav_leaks = sorted(k for k in warmup_map if k.startswith("oai-") or k == "chatgpt-account-id")
    assert nav_leaks == [], f"文档导航混入 API 头: {nav_leaks}"
    assert "referer" not in warmup_map
    assert warmup_map["sec-fetch-site"] == "none"
    assert warmup_map["sec-fetch-user"] == "?1"
    # 冷导航只发低熵 UA-CH：高熵 hints 需要站点通过 Accept-CH 持久化策略后才上送。
    for hint in ("sec-ch-ua-full-version", "sec-ch-ua-full-version-list",
                 "sec-ch-ua-arch", "sec-ch-ua-bitness", "sec-ch-ua-platform-version",
                 "sec-ch-ua-model"):
        assert hint not in warmup_map, f"冷导航不应携带高熵 hint {hint}"
    assert warmup_map["sec-ch-ua"] == client.sec_ch_ua
    assert warmup_map["sec-ch-ua-platform"] == client._effective_hints()["sec-ch-ua-platform"]
    assert "text/html" in warmup_map["accept"]
    # HTTP/1.1 回环上 libcurl 不写 priority（RFC 9218 里它是 H2/H3 语义）；
    # 这里显式断言「没发出」，H2 线上的表现属于未验证边界，不做假绿断言。
    assert "priority" not in warmup_map

    _, _, post_hdrs = captured[1]
    lower_names = [k.lower() for k, _ in post_hdrs]
    post_map = {k.lower(): v for k, v in post_hdrs}

    # Exactly one Cookie header containing both original and Set-Cookie values.
    assert lower_names.count("cookie") == 1
    assert "oai-did=a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d" in post_map["cookie"]
    assert "__cf_bm=edge_bm_1" in post_map["cookie"]
    assert "oai-sc=edge_sc_1" in post_map["cookie"]

    # No navigation default headers leaked onto CORS POST.
    assert "upgrade-insecure-requests" not in post_map
    assert "sec-fetch-user" not in post_map
    assert post_map["sec-fetch-site"] == "same-origin"
    assert post_map["sec-fetch-mode"] == "cors"
    assert post_map["sec-fetch-dest"] == "empty"

    # No duplicate mixed-case sentinel/attestation headers and no X-GSS-* leak.
    # 2026-11-26 真机实测：POST /payments/checkout 只发 openai-sentinel-token，不发 so-token / oai-session-id。
    assert lower_names.count("openai-sentinel-token") == 1
    assert "openai-sentinel-so-token" not in post_map
    assert "oai-session-id" not in post_map
    assert lower_names.count("oai-web-deployment-attestation") == 1
    assert "x-gss-purchase-attempt" not in post_map

    # Telemetry and Accept-Language match real Chrome capture.
    assert post_map["oai-telemetry"] == "[1,null]"
    assert post_map["accept-language"] == "en-PH,en;q=0.9"
    assert post_map["referer"] == "https://chatgpt.com/"

    # 2026-11-26 真机实测：chatgpt.com 未下发 Accept-CH，默认只发低熵三件套，不发高熵 hints。
    grease_brand = client._grease_brand()[0]
    assert f'"{grease_brand}"' in post_map["sec-ch-ua"]
    assert f';v="{build.split(".")[0]}"' in post_map["sec-ch-ua"]
    assert post_map["sec-ch-ua-mobile"] == "?0"
    assert post_map["sec-ch-ua-platform"] == client._effective_hints()["sec-ch-ua-platform"]
    for he_hint in (
        "sec-ch-ua-full-version",
        "sec-ch-ua-full-version-list",
        "sec-ch-ua-arch",
        "sec-ch-ua-bitness",
        "sec-ch-ua-platform-version",
        "sec-ch-ua-model",
    ):
        assert he_hint not in post_map, f"未收到 Accept-CH 时不应发送高熵头 {he_hint}"
    assert f'"{grease_brand}"' in client.sec_ch_ua_full_list
    assert post_map["x-openai-rate-limit-join-workspace"] == "1"
    assert post_map["x-openai-rejoin-pro-cta"] == "1"

    # HAR 的 payments/* 上没有这些头：多发即指纹偏差。
    assert "x-oai-mcp-form-version" not in post_map
    assert "x-openai-target-path" not in post_map
    assert "x-openai-target-route" not in post_map

    # 未从页面解析到构建号时不得发送过期 build number。
    assert "oai-client-build-number" not in post_map
    assert "oai-client-version" not in post_map


def test_sentinel_ping_wire_omits_auth_content_type_and_restores_session_headers(monkeypatch):
    """curl_cffi 会把 session.headers 合并进单次请求；_sentinel_ping 必须真正屏蔽 Bearer/JSON/SPA 头并恢复。"""
    import gss_core.chatgpt as chatgpt_mod

    captured: list[tuple[str, str, list[tuple[str, str]], bytes]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            captured.append(("POST", self.path, list(self.headers.items()), self.rfile.read(length)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setattr(chatgpt_mod, "APP_BASE", f"http://127.0.0.1:{port}")
    monkeypatch.delenv("GSS_HAR_TELEMETRY", raising=False)
    client = ChatGPTClient(
        access_token="tok_ping_wire",
        account_id="acct_ping_wire",
        device_id="a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
        session_token="st_ping_wire",
        oai_session_id="b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
        language="en-PH",
        timezone="Asia/Manila",
    )
    client.client_version = "prod-wire"
    client.client_build_number = "123456"

    try:
        # 先初始化 curl_cffi session.headers（模拟 warmup 后真实状态）。
        _ = client.http
        client._sentinel_ping(page_url=f"http://127.0.0.1:{port}/?cta_tab=personal")
        resp = client.http.post(
            f"http://127.0.0.1:{port}/backend-api/payments/checkout",
            json={"plan_name": "chatgptplusplan"},
            headers=client._route_headers("/backend-api/payments/checkout"),
        )
        assert resp.status_code == 200
    finally:
        client.close()
        srv.shutdown()
        srv.server_close()

    assert len(captured) == 2
    _, ping_path, ping_hdrs, ping_body = captured[0]
    ping_map = {k.lower(): v for k, v in ping_hdrs}
    assert ping_path == "/backend-api/sentinel/ping"
    assert ping_body == b""
    assert ping_map["cache-control"] == "no-cache"
    assert ping_map["pragma"] == "no-cache"
    for forbidden in (
        "authorization",
        "content-type",
        "chatgpt-account-id",
        "oai-did",
        "oai-device-id",
        "oai-language",
        "oai-session-id",
        "oai-client-version",
        "oai-client-build-number",
    ):
        assert forbidden not in ping_map, f"/sentinel/ping 不应携带 {forbidden}"

    # 紧随其后的 /payments/checkout 必须完整恢复 session.headers。
    _, checkout_path, checkout_hdrs, _ = captured[1]
    checkout_map = {k.lower(): v for k, v in checkout_hdrs}
    assert checkout_path == "/backend-api/payments/checkout"
    assert checkout_map["authorization"] == "Bearer tok_ping_wire"
    assert checkout_map["content-type"] == "application/json"
    assert checkout_map["chatgpt-account-id"] == "acct_ping_wire"
    assert checkout_map["oai-did"] == "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"


def test_sentinel_curl_fetch_sends_cors_headers_without_navigation_defaults():
    scf = _load_sentinel_curl_fetch()
    captured: list[list[tuple[str, str]]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append(list(self.headers.items()))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"token":"ok"}')

        def log_message(self, *args):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    try:
        res = scf.handle({
            "method": "POST",
            "url": f"http://127.0.0.1:{port}/backend-api/sentinel/req",
            "headers": {
                "content-type": "text/plain;charset=UTF-8",
                "origin": "https://chatgpt.com",
                "referer": "https://chatgpt.com/backend-api/sentinel/frame.html?sv=20260810913b",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.7680.180 Safari/537.36"
                ),
                "accept": "*/*",
                "accept-encoding": "gzip, deflate, br, zstd",
                "accept-language": "en-PH,en;q=0.9",
                "priority": "u=1, i",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "sec-ch-ua": '"Google Chrome";v="146", "Chromium";v="146", "Not(A:Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
            "body": "{}",
            "impersonate": "chrome146",
        })
        assert res["status"] == 200
    finally:
        srv.shutdown()
        srv.server_close()

    assert len(captured) == 1
    hdrs = {k.lower(): v for k, v in captured[0]}
    assert hdrs["sec-fetch-site"] == "same-origin"
    assert hdrs["sec-fetch-mode"] == "cors"
    assert hdrs["sec-fetch-dest"] == "empty"
    assert "upgrade-insecure-requests" not in hdrs
    assert "sec-fetch-user" not in hdrs


def test_sentinel_bootstrap_origin_viewport_and_brand_consistency():
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const bootstrap = fs.readFileSync(path.join(process.cwd(), "sentinel_bootstrap.js"), "utf8");
const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
  URL, URLSearchParams, TextEncoder, TextDecoder, crypto: globalThis.crypto,
  __UA__: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.7680.180 Safari/537.36",
  __CORES__: 8,
  __SDK_URL__: "https://chatgpt.com/sentinel/20260810913b/sdk.js",
  __PAGE_URL__: "https://chatgpt.com/",
  __LANGUAGE__: "en-PH",
  __LANGUAGES__: ["en-PH", "en"],
  __TIMEZONE__: "Asia/Manila",
  __PLATFORM__: "MacIntel",
  __PLATFORM_LABEL__: "macOS",
  __SCREEN_W__: 1512,
  __SCREEN_H__: 982,
  __AVAIL_H__: 938,
  __MAX_TOUCH__: 0,
};
vm.createContext(sandbox);
vm.runInContext(bootstrap, sandbox);
console.log(JSON.stringify({
  origin: sandbox.origin,
  vw: sandbox.visualViewport.width,
  vh: sandbox.visualViewport.height,
  oh: sandbox.outerHeight,
  ah: sandbox.screen.availHeight,
  brands: sandbox.navigator.userAgentData.brands,
  dpr: sandbox.devicePixelRatio,
  languages: sandbox.navigator.languages,
}));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=str(ASSETS_DIR),
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout.strip())
    assert data["origin"] == "https://chatgpt.com"
    assert data["vw"] == 1512
    # 真窗口有标签栏+地址栏：innerHeight 必须显著小于 availHeight，macOS 单屏无 +16 DWM 边框。
    assert data["vh"] < data["ah"]
    assert data["oh"] - data["vh"] >= 80
    assert data["languages"] == ["en-PH", "en"]
    assert {"brand": "Not-A.Brand", "version": "24"} in data["brands"]
    assert {"brand": "Google Chrome", "version": "146"} in data["brands"]
    # macOS Retina 档案的 dpr 必须是 2，不能写死 1。
    assert data["dpr"] == 2


def test_sentinel_assets_share_one_fingerprint_registry():
    """PoW 生成器与 sdk.js 沙箱必须写出同一份指纹。

    历史缺陷：`p` 里写着 sentinel.openai.com 的 sdkUrl、全新 sid、4395630592 的
    heapLimit 和 `(Asia/Manila)` 时区名，而几百毫秒前 /sentinel/req 上报的
    sdk.js 指纹是另一套 → 同一 flow 两条互相矛盾的设备记录。
    """
    bridge = (ASSETS_DIR / "sentinel_bridge.js").read_text()
    bootstrap = (ASSETS_DIR / "sentinel_bootstrap.js").read_text()

    # 堆上限必须与 bootstrap 的 performance.memory.jsHeapSizeLimit 一致。
    assert "4294705152" in bootstrap
    assert "4294705152" in bridge
    assert "4395630592" not in bridge

    # sdkUrl 必须来自 sandbox.__SDK_URL__，不能写死 sentinel.openai.com。
    assert "sandbox.__SDK_URL__" in bridge
    assert "sentinel.openai.com/backend-api/sentinel/sdk.js" not in bridge

    # sid / 启动基准共用沙箱里那一份。
    assert "sandbox.__SENTINEL_SID__" in bridge
    assert "__SENTINEL_SID__" in bootstrap
    assert "sandbox.__LOAD_TS__" in bridge

    # 时区名必须走本地化长名，不能输出 IANA 标识。
    assert 'timeZoneName: "long"' in bootstrap
    assert 'timeZoneName: "long"' in bridge


def test_sentinel_bootstrap_behaviour_traces_follow_flow():
    script = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const bootstrap = fs.readFileSync(path.join(process.cwd(), "sentinel_bootstrap.js"), "utf8");

function run(flow) {
  const sandbox = {
    console, setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
    URL, URLSearchParams, TextEncoder, TextDecoder, crypto: globalThis.crypto,
    __UA__: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.7871.189 Safari/537.36",
    __CORES__: 8,
    __SDK_URL__: "https://chatgpt.com/sentinel/20260810913b/sdk.js",
    __PAGE_URL__: "https://chatgpt.com/?cta_tab=personal",
    __LANGUAGE__: "en-PH",
    __LANGUAGES__: ["en-PH", "en"],
    __TIMEZONE__: "Asia/Manila",
    __PLATFORM__: "Win32",
    __PLATFORM_LABEL__: "Windows",
    __SCREEN_W__: 1920,
    __SCREEN_H__: 1080,
    __AVAIL_H__: 1032,
    __MAX_TOUCH__: 0,
    __FLOW__: flow,
  };
  vm.createContext(sandbox);
  vm.runInContext(bootstrap, sandbox);
  sandbox.__DATE__ = vm.runInContext("Date.prototype.toString.call(new Date())", sandbox);
  return {
    sid: sandbox.__SENTINEL_SID__,
    loadTs: sandbox.__LOAD_TS__,
    heap: sandbox.performance.memory.jsHeapSizeLimit,
    tzName: sandbox.__TZ_DISPLAY_NAME__,
    date: sandbox.__DATE__,
    keydowns: sandbox.__oai_so_h.length,
    keys: sandbox.__oai_so_k,
    scrolls: sandbox.__oai_so_sn,
    pointers: sandbox.__oai_so_pc,
    clicks: sandbox.__oai_so_cn,
  };
}

console.log(JSON.stringify({
  checkout: run("chatgpt_checkout"),
  approval: run("checkout_session_approval"),
  signup: run("oauth_create_account"),
}));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=str(ASSETS_DIR),
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout.strip())

    for key in ("checkout", "approval", "signup"):
        row = data[key]
        assert row["heap"] == 4294705152
        assert row["sid"] and row["loadTs"] > 0
        assert row["tzName"] == "Philippine Standard Time"
        assert "(Philippine Standard Time)" in row["date"]
        assert "Asia/Manila" not in row["date"]

    # 结账流：没有任何键盘输入，只有浏览/移动/点击轨迹。
    for key in ("checkout", "approval"):
        assert data[key]["keydowns"] == 0
        assert data[key]["keys"] == 0
        assert data[key]["scrolls"] > 0
        assert data[key]["pointers"] > 0
        assert data[key]["clicks"] == 2

    # 注册流仍保留姓名 + 生日输入轨迹（含大写 Shift keydown，长度逐单随机）。
    assert 11 <= data["signup"]["keydowns"] <= 36
    assert data["signup"]["keys"] == data["signup"]["keydowns"]


def test_stripe_http_carries_storage_access_and_tracks_browser_context():
    from types import SimpleNamespace

    client = ChatGPTClient(
        access_token="tok",
        device_id="did-stripe-ctx",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.7680.180 Safari/537.36"
        ),
    )
    try:
        hdrs0 = {k.lower(): v for k, v in client.stripe_http.headers.items()}
        assert hdrs0["sec-fetch-storage-access"] == "active"
        assert hdrs0["sec-ch-ua-platform"] == '"Windows"'

        client.apply_browser_context(
            SimpleNamespace(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.7680.180 Safari/537.36"
                ),
                sec_ch_ua='"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
                sec_ch_ua_platform="macOS",
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                cookies={},
                headers=lambda: {"sec-ch-ua-platform": '"macOS"'},
            )
        )
        hdrs1 = {k.lower(): v for k, v in client.stripe_http.headers.items()}
        assert hdrs1["sec-fetch-storage-access"] == "active"
        assert hdrs1["sec-ch-ua-platform"] == '"macOS"'
        assert hdrs1["sec-ch-ua"] == '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"'
        assert hdrs1["accept-language"] == "ja-JP,ja;q=0.9,en;q=0.8"
    finally:
        client.close()
