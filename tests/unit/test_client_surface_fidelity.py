"""审计回归：模拟客户端的「对象形状 / 几何 / 时序 / 行为语义 / UA-CH 同源 / GET 线缆」。

对应复审全部回归与不变量（`T1`–`T6` / `R1`–`R5` / `L2` / `L5` / `L9` / `L10`）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import http.server
import socketserver
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = ROOT / "packages/core/src/gss_core/sentinel_assets"

SEC_CH_UA = '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"'
SEC_CH_UA_FULL = (
    '"Not;A=Brand";v="8.0.0.0", "Chromium";v="150.0.7871.189", '
    '"Google Chrome";v="150.0.7871.189"'
)

HARNESS = r"""
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { performance: hostPerf } = require("perf_hooks");

const args = process.argv.slice(-4);
const assets = args[0];
const flow = args[1];
const randMode = args[2];
const platformLabel = args[3] || "Windows";
const bootstrapSrc = fs.readFileSync(path.join(assets, "sentinel_bootstrap.js"), "utf8");
const sdkSrc = fs.readFileSync(path.join(assets, "sentinel_sdk.js"), "utf8");

const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
  URL, URLSearchParams, TextEncoder, TextDecoder, crypto: globalThis.crypto,
  __HOST_NOW__: () => hostPerf.now(),
  btoa: (s) => Buffer.from(String(s), "latin1").toString("base64"),
  atob: (s) => Buffer.from(String(s), "base64").toString("latin1"),
  fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }),
  __UA__: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.7871.189 Safari/537.36",
  __CORES__: 8,
  __FLOW__: flow,
  __SDK_URL__: "https://chatgpt.com/sentinel/20260810913b/sdk.js",
  __PAGE_URL__: "https://chatgpt.com/?cta_tab=personal",
  __LANGUAGE__: "en-PH",
  __LANGUAGES__: ["en-PH", "en"],
  __TIMEZONE__: "Asia/Manila",
  __COOKIE_HEADER__: "oai-did=d1",
  __PLATFORM__: platformLabel === "macOS" ? "MacIntel" : "Win32",
  __PLATFORM_LABEL__: platformLabel,
  __SCREEN_W__: platformLabel === "macOS" ? 1512 : 1920,
  __SCREEN_H__: platformLabel === "macOS" ? 982 : 1080,
  __AVAIL_H__: platformLabel === "macOS" ? 938 : 1032,
  __MAX_TOUCH__: 0,
  __SEC_CH_UA__: process.env.SEC_CH_UA,
  __SEC_CH_UA_FULL_LIST__: process.env.SEC_CH_UA_FULL,
  __CH_ARCH__: platformLabel === "macOS" ? "arm" : "x86",
  __CH_BITNESS__: "64",
  __CH_PLATFORM_VERSION__: platformLabel === "macOS" ? "14.6.1" : "10.0.0",
};
vm.createContext(sandbox);
if (randMode === "min" || randMode === "max") {
  const value = randMode === "min" ? 0.0001 : 0.9999;
  vm.runInContext("Math.random = function () { return " + value + "; };", sandbox);
}
vm.runInContext(bootstrapSrc, sandbox, { filename: "sentinel_bootstrap.js" });

function forInKeys(obj) {
  const keys = [];
  for (const k in obj) keys.push(k);
  return keys;
}

const nav = sandbox.performance.getEntriesByType("navigation")[0];
const resEntries = sandbox.performance.getEntriesByType("resource");

const protoProbe = vm.runInContext(`(() => {
  const navDesc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(navigator), "userAgent");
  let illegalErr = false;
  try { Object.getPrototypeOf(navigator).userAgent; } catch (e) { illegalErr = e instanceof TypeError && /Illegal invocation/.test(e.message); }
  navigator.userAgent = "mutated";
  const navOwnAfterWrite = Object.getOwnPropertyNames(navigator);
  Object.getPrototypeOf(screen).width = 9999;
  const scrWidthAfterWrite = screen.width;
  const prevHref = document.location.href;
  document.location = "https://other.example/sub?k=1";
  const locReactive = {
    href: document.location.href,
    str: String(document.location),
    path: document.location.pathname,
    url: document.URL,
    origin: window.origin,
  };
  document.location = prevHref;
  const el1 = document.createElement("div");
  el1.innerText = "mmmm";
  el1.style.fontFamily = "Arial";
  el1.style.fontSize = "16px";
  const r1 = el1.getBoundingClientRect().width;
  const el2 = document.createElement("div");
  el2.innerText = "mmmm";
  el2.style.fontFamily = "Courier New";
  el2.style.fontSize = "20px";
  const r2 = el2.getBoundingClientRect().width;
  return {
    uaIsAccessor: typeof navDesc.get === "function" && !("value" in navDesc),
    illegalErr,
    navOwnAfterWrite,
    scrWidthAfterWrite,
    locReactive,
    tags: [
      Object.prototype.toString.call(document),
      Object.prototype.toString.call(navigator),
      Object.prototype.toString.call(screen),
      Object.prototype.toString.call(performance),
    ],
    instanceofOk: (
      document instanceof HTMLDocument &&
      document instanceof Document &&
      document instanceof Node &&
      document instanceof EventTarget &&
      screen instanceof EventTarget &&
      performance instanceof EventTarget
    ),
    nativeToString: [
      navigator.getBattery.toString(),
      Function.prototype.toString.call(navigator.getBattery),
      Object.keys(navigator.getBattery),
    ],
    fontWidthsDistinct: r1 !== r2,
  };
})()`, sandbox);

const lsValues = Object.keys(sandbox.localStorage).map((k) => sandbox.localStorage.getItem(k));
const tightNow = [];
const wall0 = Date.now();
for (let i = 0; i < 200; i++) tightNow.push(sandbox.performance.now());
const wall1 = Date.now();

const out = {
  flow,
  randMode,
  document: {
    own: Object.getOwnPropertyNames(sandbox.document),
    keys: Object.keys(sandbox.document),
    forIn: forInKeys(sandbox.document),
    location: (() => {
      const d = Object.getOwnPropertyDescriptor(sandbox.document, "location");
      return d && {
        accessor: typeof d.get === "function" && typeof d.set === "function",
        enumerable: d.enumerable,
        configurable: d.configurable,
        readable: String(sandbox.document.location),
      };
    })(),
  },
  navigator: {
    own: Object.getOwnPropertyNames(sandbox.navigator).length,
    forIn: forInKeys(sandbox.navigator),
    hasOwnUserAgent: Object.prototype.hasOwnProperty.call(sandbox.navigator, "userAgent"),
  },
  screen: {
    own: Object.getOwnPropertyNames(sandbox.screen).length,
    forIn: forInKeys(sandbox.screen),
  },
  performance: {
    own: Object.getOwnPropertyNames(sandbox.performance).length,
    forIn: forInKeys(sandbox.performance),
  },
  protoProbe,
  lsValues,
  windowKeys: Object.keys(sandbox),
  geometry: {
    screen: [sandbox.screen.width, sandbox.screen.height, sandbox.screen.availWidth, sandbox.screen.availHeight],
    inner: [sandbox.innerWidth, sandbox.innerHeight],
    outer: [sandbox.outerWidth, sandbox.outerHeight],
    pos: [sandbox.screenX, sandbox.screenY],
    visual: [sandbox.visualViewport.width, sandbox.visualViewport.height],
    dpr: sandbox.devicePixelRatio,
  },
  nav,
  resEntries,
  tightNow,
  tightWallMs: wall1 - wall0,
  driftMs: (sandbox.performance.timeOrigin + tightNow[tightNow.length - 1]) - Date.now(),
  telemetry: {
    t0: sandbox.__oai_so_t0,
    wheel: sandbox.__oai_so_we,
    wheelSum: sandbox.__oai_so_wb,
    scrollSum: sandbox.__oai_so_s,
    scrollTop: sandbox.__oai_so_st,
    scrollCount: sandbox.__oai_so_sn,
    ss: sandbox.__oai_so_ss,
    ss2: sandbox.__oai_so_ss2,
    cs: sandbox.__oai_so_cs,
    cs2: sandbox.__oai_so_cs2,
    cn: sandbox.__oai_so_cn,
    fs: sandbox.__oai_so_fs,
    fs2: sandbox.__oai_so_fs2,
    focus: sandbox.__oai_so_fn,
    kd: sandbox.__oai_so_h || [],
    inputs: sandbox.__oai_so_i || [],
    pointerCoords: (sandbox.__oai_so_p || []).map((e) => [e.clientX, e.clientY]),
    pointerTimes: (sandbox.__oai_so_p || []).map((e) => e.t),
    keyTimes: (sandbox.__oai_so_h || []).map((e) => e.t),
    sw: sandbox.__oai_so_sw,
    htot: sandbox.__oai_so_ht,
  },
  uaData: {
    brands: sandbox.navigator.userAgentData.brands,
  },
};

sandbox.navigator.userAgentData
  .getHighEntropyValues(["architecture", "bitness", "platformVersion", "fullVersionList"])
  .then((v) => {
    out.uaData.highEntropy = v;
    if (flow === "chatgpt_checkout" && randMode === "default" && platformLabel === "Windows") {
      vm.runInContext(sdkSrc, sandbox, { filename: "sentinel_sdk.js" });
      out.windowKeysAfterSdk = Object.keys(sandbox);
    }
    process.stdout.write(JSON.stringify(out) + "\n");
  });
"""


def _run(flow: str = "chatgpt_checkout", rand_mode: str = "default",
         platform_label: str = "Windows") -> dict:
    env = {**os.environ, "SEC_CH_UA": SEC_CH_UA, "SEC_CH_UA_FULL": SEC_CH_UA_FULL}
    proc = subprocess.run(
        ["node", "-e", HARNESS, str(ASSETS_DIR), flow, rand_mode, platform_label],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip())


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 运行 sentinel 沙箱")


@needs_node
def test_document_and_navigator_have_real_chrome_object_shape():
    data = _run()
    doc = data["document"]
    assert doc["own"] == ["location"], doc["own"]
    assert doc["keys"] == ["location"]
    assert doc["location"]["accessor"] is True
    assert doc["location"]["configurable"] is False
    assert doc["location"]["enumerable"] is True
    assert doc["location"]["readable"] == "https://chatgpt.com/?cta_tab=personal"
    assert len(doc["forIn"]) >= 200, len(doc["forIn"])
    assert "cookie" in doc["forIn"] and "createElement" in doc["forIn"]

    nav = data["navigator"]
    assert nav["own"] == 0, "navigator 实例不应有自有属性"
    assert nav["hasOwnUserAgent"] is False
    for member in ("userAgent", "language", "languages", "hardwareConcurrency", "plugins",
                   "userAgentData", "platform", "deviceMemory"):
        assert member in nav["forIn"], f"for...in navigator 丢了 {member}"
    assert len(nav["forIn"]) >= 65, len(nav["forIn"])

    assert data["screen"]["own"] == 0
    assert set(data["screen"]["forIn"]) >= {"width", "height", "availWidth", "availHeight", "addEventListener"}
    assert data["performance"]["own"] == 0
    assert {"now", "addEventListener", "getEntriesByType"} <= set(data["performance"]["forIn"])

    probe = data["protoProbe"]
    assert probe["uaIsAccessor"] is True
    assert probe["illegalErr"] is True
    assert probe["navOwnAfterWrite"] == []
    assert probe["scrWidthAfterWrite"] == 1920
    assert probe["locReactive"] == {
        "href": "https://other.example/sub?k=1",
        "str": "https://other.example/sub?k=1",
        "path": "/sub",
        "url": "https://other.example/sub?k=1",
        "origin": "https://other.example",
    }
    assert probe["tags"] == [
        "[object HTMLDocument]",
        "[object Navigator]",
        "[object Screen]",
        "[object Performance]",
    ]
    assert probe["instanceofOk"] is True
    assert probe["nativeToString"] == [
        "function getBattery() { [native code] }",
        "function getBattery() { [native code] }",
        [],
    ]
    assert probe["fontWidthsDistinct"] is True
    assert not all(v == "1" for v in data["lsValues"]), data["lsValues"]


@needs_node
def test_window_surface_order_and_internal_keys():
    data = _run()
    keys = data["windowKeys"]
    assert keys[:6] == ["window", "self", "document", "name", "location", "customElements"], keys[:10]
    assert len(keys) >= 220, len(keys)
    on_handlers = [k for k in keys if k.startswith("on")]
    assert len(on_handlers) >= 120, len(on_handlers)
    leaked = [k for k in keys if k.startswith("__") and k.endswith("__") and not k.startswith("__oai_so_")]
    assert leaked == [], leaked
    assert "SentinelSDK" in data["windowKeysAfterSdk"]


@needs_node
def test_window_geometry_has_browser_chrome_and_platform_dpr():
    win = _run(platform_label="Windows")["geometry"]
    mac = _run(platform_label="macOS")["geometry"]

    # Windows 最大化：DWM 8px 隐形边框
    assert win["pos"] == [-8, -8], win
    assert win["outer"] == [win["screen"][2] + 16, win["screen"][3] + 16], win
    assert win["inner"][0] == win["screen"][2]
    assert win["outer"][1] - win["inner"][1] >= 96, win
    assert win["visual"] == win["inner"]
    assert win["dpr"] == 1

    # macOS 单屏：无 +16 DWM 边框，outerWidth === innerWidth <= availWidth
    assert mac["pos"][0] == 0 and mac["pos"][1] >= 0, mac
    assert mac["outer"][0] == mac["inner"][0] <= mac["screen"][2], mac
    assert mac["outer"][1] < mac["screen"][3], mac
    assert mac["outer"][1] - mac["inner"][1] >= 80, mac
    assert mac["visual"] == mac["inner"]
    assert mac["dpr"] == 2


@needs_node
def test_behaviour_telemetry_is_semantically_consistent():
    checkout = _run("chatgpt_checkout")
    signup = _run("oauth_create_account")

    c = checkout["telemetry"]
    assert len(c["kd"]) == 0 and len(c["inputs"]) == 0
    assert c["scrollCount"] > 0 and c["scrollSum"] >= c["scrollTop"] > 0
    assert c["wheel"] > 0 and c["wheelSum"] > 0
    assert c["focus"] >= 1 and c["fs"] > 0
    assert c["ss2"] >= (c["ss"] ** 2) / c["scrollCount"]
    assert c["cs2"] >= (c["cs"] ** 2) / c["cn"]
    assert c["fs2"] >= (c["fs"] ** 2) / c["focus"]

    s = signup["telemetry"]
    assert len(s["kd"]) > len(s["inputs"]) > 0
    shift_events = [e for e in s["kd"] if e["key"] == "Shift"]
    upper_events = [i for i, e in enumerate(s["kd"]) if e["key"] != "Shift" and e["shiftKey"]]
    assert len(shift_events) == len(upper_events) >= 2
    for idx in upper_events:
        assert idx > 0 and s["kd"][idx - 1]["key"] == "Shift"
    assert s["focus"] >= 2 and s["fs"] > 0
    assert s["fs2"] >= (s["fs"] ** 2) / s["focus"]
    assert s["cs2"] >= (s["cs"] ** 2) / s["cn"]
    assert len(s["inputs"][-1]["value"]) >= 9
    assert max(s["pointerTimes"]) < min(s["keyTimes"])

    for geo, tel in ((checkout["geometry"], c), (signup["geometry"], s)):
        iw, ih = geo["inner"]
        assert tel["sw"] == iw
        assert all(0 < x < iw and 0 < y < ih for x, y in tel["pointerCoords"])


@needs_node
@pytest.mark.parametrize("flow", ["chatgpt_checkout", "oauth_create_account"])
@pytest.mark.parametrize("rand_mode", ["min", "max"])
def test_timeline_invariants_hold_at_random_extremes(flow, rand_mode):
    data = _run(flow, rand_mode=rand_mode)
    tel = data["telemetry"]
    nav = data["nav"]
    chain = [
        nav["fetchStart"], nav["domainLookupStart"], nav["domainLookupEnd"],
        nav["connectStart"], nav["secureConnectionStart"], nav["connectEnd"],
        nav["requestStart"], nav["responseStart"], nav["responseEnd"],
        nav["domInteractive"], nav["domContentLoadedEventEnd"],
        nav["domComplete"], nav["loadEventEnd"], nav["duration"],
    ]
    assert all(chain[i] <= chain[i + 1] for i in range(len(chain) - 1)), chain
    assert nav["transferSize"] >= nav["encodedBodySize"]
    assert nav["decodedBodySize"] >= nav["encodedBodySize"]

    res = data["resEntries"]
    assert len(res) >= 1
    assert res[0]["name"] == "https://chatgpt.com/sentinel/20260810913b/sdk.js"
    assert res[0]["startTime"] >= nav["responseStart"]
    assert res[0]["responseEnd"] <= nav["loadEventEnd"]
    assert res[0]["transferSize"] >= res[0]["encodedBodySize"]

    assert tel["t0"] > nav["loadEventEnd"], (tel["t0"], nav["loadEventEnd"])
    assert tel["t0"] > nav["domInteractive"]
    times = tel["pointerTimes"]
    assert times and all(t > 0 for t in times)
    assert times == sorted(times)
    assert times[0] > tel["t0"], (times[0], tel["t0"])
    if tel["kd"]:
        assert all(t > 0 for t in tel["keyTimes"])
        assert tel["keyTimes"] == sorted(tel["keyTimes"])
        assert max(times) < min(tel["keyTimes"])


@needs_node
def test_performance_now_is_coarsened_and_zero_drift():
    data = _run()
    samples = data["tightNow"]
    assert samples == sorted(samples)
    same = sum(1 for i in range(1, len(samples)) if samples[i] == samples[i - 1])
    assert same >= len(samples) // 2, same
    assert abs(data["driftMs"]) < 50.0, data["driftMs"]
    for value in samples[:10]:
        assert abs(round(value * 10) - value * 10) < 1e-6, value


@needs_node
def test_user_agent_data_matches_injected_client_hints():
    data = _run()
    brands = data["uaData"]["brands"]
    assert {"brand": "Not;A=Brand", "version": "8"} in brands
    assert {"brand": "Google Chrome", "version": "150"} in brands
    high = data["uaData"]["highEntropy"]
    assert high["architecture"] == "x86"
    assert high["bitness"] == "64"
    assert high["platformVersion"] == "10.0.0"
    assert {"brand": "Not;A=Brand", "version": "8.0.0.0"} in high["fullVersionList"]
    assert "uaFullVersion" not in high


def test_python_side_client_hints_follow_chromium_algorithm_and_preserve_overrides():
    from gss_core import ua_ch
    from gss_core.chatgpt import ChatGPTClient

    expected_by_major = {
        "133": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
        "136": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "142": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        "145": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "146": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "150": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        "159": '"Google Chrome";v="159", "Not/A)Brand";v="8", "Chromium";v="159"',
    }
    for major, expected in expected_by_major.items():
        assert ua_ch.sec_ch_ua(major) == expected

    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/146.0.7680.180 Safari/537.36")
    client = ChatGPTClient(access_token="t", user_agent=ua)
    brand, version = client._grease_brand()
    assert (brand, version) == ("Not-A.Brand", "24")
    assert client.sec_ch_ua == expected_by_major["146"]
    assert client.sec_ch_ua_full_list == (
        '"Chromium";v="146.0.7680.180", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.180"'
    )

    reordered = '"Google Chrome";v="146", "Not-A.Brand";v="99", "Chromium";v="146"'
    assert ua_ch.sec_ch_ua("146", reordered) == reordered
    assert ua_ch.sec_ch_ua_full_version_list("146.0.7680.180", reordered) == (
        '"Google Chrome";v="146.0.7680.180", "Not-A.Brand";v="99.0.0.0", "Chromium";v="146.0.7680.180"'
    )

    edge_ch = '"Microsoft Edge";v="146", "Chromium";v="146", "Not.A/Brand";v="24"'
    assert ua_ch.grease_brand("146", edge_ch) == ("Not.A/Brand", "24")
    assert ua_ch.sec_ch_ua("146", edge_ch) == edge_ch
    assert ua_ch.sec_ch_ua_full_version_list("146.0.7680.180", edge_ch) == (
        '"Microsoft Edge";v="146.0.7680.180", "Chromium";v="146.0.7680.180", "Not.A/Brand";v="24.0.0.0"'
    )

    client.apply_browser_context(
        type("Ctx", (), {
            "user_agent": ua,
            "sec_ch_ua": '"Not:A-Brand";v="99", "Chromium";v="146", "Google Chrome";v="146"',
            "sec_ch_ua_platform": "macOS",
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "cookies": {},
            "viewport_width": 1440,
            "viewport_height": 900,
            "headers": lambda self: {"sec-ch-ua": self.sec_ch_ua, "sec-ch-ua-platform": '"macOS"'},
        })()
    )
    assert client.sec_ch_ua == '"Not:A-Brand";v="99", "Chromium";v="146", "Google Chrome";v="146"'
    assert '"Not:A-Brand";v="99.0.0.0"' in client.sec_ch_ua_full_list
    ident = {k.lower(): v for k, v in client._identity_headers().items()}
    assert ident["sec-ch-ua-platform"] == '"macOS"'
    assert "sec-ch-ua-arch" not in ident
    assert client.sentinel_profile["sec_ch_ua_arch"] == '"arm"'
    assert client.sentinel_profile["sec_ch_ua_platform_version"] == '"14.6.1"'
    # 收到 Accept-CH 协商后按 RFC 8942 输出高熵 hints
    client._accepted_ch.update({"sec-ch-ua-arch", "sec-ch-ua-platform-version"})
    ident_ch = {k.lower(): v for k, v in client._identity_headers().items()}
    assert ident_ch["sec-ch-ua-arch"] == '"arm"'
    assert ident_ch["sec-ch-ua-platform-version"] == '"14.6.1"'


def test_same_origin_get_headers_and_wire_requests_drop_origin_and_content_type(monkeypatch):
    import gss_core.chatgpt as cg
    import gss_core.saved_cards as sc
    from gss_core.chatgpt import ChatGPTClient

    captured: list[tuple[str, dict[str, str]]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            captured.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if "stripe_client_bootstrap" in self.path:
                self.wfile.write(b'{"publishable_key":"pk_test_wire"}')
            elif "accounts/check" in self.path:
                self.wfile.write(b'{"accounts":{"acct_w":{"account":{"account_id":"acct_w","plan_type":"free"}}}}')
            elif "/backend-api/me" in self.path:
                self.wfile.write(b'{"id":"u1","accounts":{"acct_w":{"account":{"account_id":"acct_w"}}}}')
            elif "payment_methods" in self.path:
                self.wfile.write(b'{"data":[]}')
            else:
                self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(cg, "APP_BASE", base)
    monkeypatch.setattr(sc, "APP_BASE", base)

    client = ChatGPTClient(access_token="t", account_id="acct_w", device_id="did", session_token="st")
    get_headers = {k.lower(): v for k, v in client._get_headers().items()}
    post_headers = {k.lower(): v for k, v in client._identity_headers().items()}
    assert "origin" not in get_headers
    assert "content-type" not in get_headers
    assert "authorization" in get_headers
    assert post_headers["origin"] and post_headers["content-type"]

    try:
        client.warm_account(full=True)
        client._warm_probe("/backend-api/me", None, stage="me")
        client.promo_eligibility()
        client._publishable_key()
        client._quote_from_context(
            processor="openai_llc", checkout_id="oaics_1", page_url=f"{base}/", currency="USD", billing={}
        )
        client.subscription_payload()
        sc.account_payment_methods(client, "acct_w")
        client.account_id = ""
        client.hydrate()
        # 紧随其后 session.headers 上的 Origin / Content-Type 仍保持恢复，不破坏 POST。
        assert client.http.headers.get("Origin") == base
        assert client.http.headers.get("Content-Type") == "application/json"
    finally:
        client.close()
        srv.shutdown()
        srv.server_close()

    assert len(captured) >= 12
    for path, hdrs in captured:
        assert "origin" not in hdrs, f"GET {path} 仍携带 Origin"
        assert "content-type" not in hdrs, f"GET {path} 仍携带 Content-Type"


@needs_node
def test_webidl_subobjects_methods_constructors_timing_and_tab_focus():
    script = r"""
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { performance: hostPerf } = require("perf_hooks");
const assets = process.argv[process.argv.length - 1];
const bootstrapSrc = fs.readFileSync(path.join(assets, "sentinel_bootstrap.js"), "utf8");
const sdkSrc = fs.readFileSync(path.join(assets, "sentinel_sdk.js"), "utf8");

function makeSandbox(extra) {
  const sb = Object.assign({
    console, setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
    URL, URLSearchParams, TextEncoder, TextDecoder, crypto: globalThis.crypto,
    __HOST_NOW__: () => hostPerf.now(),
    btoa: (s) => Buffer.from(String(s), "latin1").toString("base64"),
    atob: (s) => Buffer.from(String(s), "base64").toString("latin1"),
    fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ token: "c" }), text: () => Promise.resolve("{}"), headers: { get: () => null } }),
    __UA__: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.7871.189 Safari/537.36",
    __CORES__: 8,
    __FLOW__: "oauth_create_account",
    __SDK_URL__: "https://chatgpt.com/sentinel/20260810913b/sdk.js",
    __PAGE_URL__: "https://chatgpt.com/?cta_tab=personal",
    __LANGUAGE__: "en-US",
    __TIMEZONE__: "America/New_York",
    __PLATFORM__: "Win32",
    __PLATFORM_LABEL__: "Windows",
    __SCREEN_W__: 1920,
    __SCREEN_H__: 1080,
    __AVAIL_H__: 1032,
  }, extra || {});
  vm.createContext(sb);
  vm.runInContext(bootstrapSrc, sb, { filename: "sentinel_bootstrap.js" });
  return sb;
}

(async () => {
  const sb = makeSandbox();
  const navObjectTags = {};
  const bareObjectProps = [];
  for (const k in sb.navigator) {
    const v = sb.navigator[k];
    if (v && typeof v === "object" && !Array.isArray(v)) {
      const tag = Object.prototype.toString.call(v);
      navObjectTags[k] = tag;
      if (tag === "[object Object]") bareObjectProps.push(k);
    }
  }
  const winKeys = Object.keys(sb);
  const uppercaseWinKeys = winKeys.filter((k) => /^[A-Z]/.test(k));
  const checkNonCtor = (fn) => {
    const hasProto = Object.prototype.hasOwnProperty.call(fn, "prototype");
    let ctorThrows = false;
    try { new fn(); } catch (e) { ctorThrows = e instanceof TypeError; }
    return { hasProto, ctorThrows, str: String(fn) };
  };
  const protoIllegal = vm.runInContext(`(() => {
    const calls = [
      () => Document.prototype.createElement("div"),
      () => Performance.prototype.getEntries(),
      () => Navigator.prototype.getBattery(),
      () => EventTarget.prototype.addEventListener("x", () => {}),
    ];
    return calls.map((fn) => {
      try { fn(); return false; } catch (e) { return e instanceof TypeError && /Illegal invocation/.test(e.message); }
    });
  })()`, sb);

  const battery = await sb.navigator.getBattery();
  const sbJa = makeSandbox({ __LANGUAGE__: "ja-JP" });
  const nav = sb.performance.getEntriesByType("navigation")[0];
  const timing = sb.performance.timing;

  // 30 live sdk.js runs checking p[10] and p[12] distribution
  const p10Samples = [];
  const p12Samples = [];
  for (let i = 0; i < 30; i++) {
    let capBody = null;
    const runSb = makeSandbox({
      __FLOW__: "chatgpt_checkout",
      fetch: (url, opts) => {
        if (opts && opts.body) capBody = opts.body;
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({ token: "c", proofofwork: { required: false } }),
          text: () => Promise.resolve("{}"),
          headers: { get: () => null },
        });
      },
    });
    vm.runInContext(
      sdkSrc + "\n;Object.defineProperty(globalThis,'__SDK__',{value:SentinelSDK,enumerable:false});",
      runSb,
      { filename: "sentinel_sdk.js" }
    );
    await Promise.resolve(runSb.__SDK__.__proto2("chatgpt_checkout"));
    const parsed = JSON.parse(String(capBody));
    let b = parsed.p.replace(/^gAAAAAC|^gAAAAAB/, "").replace(/~S$/, "");
    const cfg = JSON.parse(Buffer.from(b, "base64").toString("utf8"));
    p10Samples.push(cfg[10]);
    p12Samples.push(cfg[12]);
  }

  const out = {
    bareObjectProps,
    navObjectCount: Object.keys(navObjectTags).length,
    uppercaseWinKeys,
    ctorsReachable: typeof sb.AbortController === "function" && typeof sb.URL === "function" && typeof sb.TextEncoder === "function",
    nonCtorChecks: {
      getBattery: checkNonCtor(sb.navigator.getBattery),
      perfNow: checkNonCtor(sb.performance.now),
      fnToString: checkNonCtor(vm.runInContext("Function.prototype.toString", sb)),
      getLayoutMap: checkNonCtor(sb.navigator.keyboard.getLayoutMap),
    },
    batteryTag: Object.prototype.toString.call(battery),
    javaEnabled: sb.navigator.javaEnabled(),
    gamepadsLen: sb.navigator.getGamepads().length,
    bodyTag: Object.prototype.toString.call(sb.document.body),
    divTag: Object.prototype.toString.call(sb.document.createElement("div")),
    protoIllegal,
    defaultLangsEn: Array.from(sb.navigator.languages),
    defaultLangsJa: Array.from(sbJa.navigator.languages),
    navL2: {
      dclStart: nav.domContentLoadedEventStart,
      dclEnd: nav.domContentLoadedEventEnd,
      loadStart: nav.loadEventStart,
      loadEnd: nav.loadEventEnd,
      workerStart: nav.workerStart,
      redirectStart: nav.redirectStart,
      redirectEnd: nav.redirectEnd,
      unloadEventStart: nav.unloadEventStart,
      unloadEventEnd: nav.unloadEventEnd,
      responseStatus: nav.responseStatus,
    },
    timingKeys: Object.keys(timing).length,
    timingChainOk: (
      timing.navigationStart <= timing.fetchStart &&
      timing.fetchStart <= timing.domInteractive &&
      timing.domInteractive <= timing.domContentLoadedEventStart &&
      timing.domContentLoadedEventStart <= timing.domContentLoadedEventEnd &&
      timing.domContentLoadedEventEnd <= timing.domComplete &&
      timing.domComplete <= timing.loadEventStart &&
      timing.loadEventStart <= timing.loadEventEnd
    ),
    tabKeys: (sb.__oai_so_h || []).filter((e) => e.key === "Tab").length,
    p10BareObj: p10Samples.filter((s) => String(s).includes("[object Object]")),
    p12Upper: p12Samples.filter((s) => /^[A-Z]/.test(String(s)) && s !== "SentinelSDK"),
  };
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""
    proc = subprocess.run(["node", "-e", script, str(ASSETS_DIR)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    data = json.loads(proc.stdout.strip())

    assert data["navObjectCount"] >= 27
    assert data["bareObjectProps"] == [], data["bareObjectProps"]
    assert data["uppercaseWinKeys"] == [], data["uppercaseWinKeys"]
    assert data["ctorsReachable"] is True
    for name, info in data["nonCtorChecks"].items():
        assert info["hasProto"] is False, (name, info)
        assert info["ctorThrows"] is True, (name, info)
        assert "[native code]" in info["str"], (name, info)
    assert data["batteryTag"] == "[object BatteryManager]"
    assert data["javaEnabled"] is False
    assert data["gamepadsLen"] == 4
    assert data["bodyTag"] == "[object HTMLBodyElement]"
    assert data["divTag"] == "[object HTMLDivElement]"
    assert data["protoIllegal"] == [True, True, True, True]
    assert data["defaultLangsEn"] == ["en-US", "en"]
    assert data["defaultLangsJa"] == ["ja-JP", "ja", "en"]
    l2 = data["navL2"]
    assert l2["dclStart"] <= l2["dclEnd"] and l2["loadStart"] <= l2["loadEnd"]
    assert l2["workerStart"] == 0 and l2["redirectStart"] == 0 and l2["responseStatus"] == 200
    assert data["timingKeys"] >= 19 and data["timingChainOk"] is True
    assert data["tabKeys"] == 1
    assert data["p10BareObj"] == [], data["p10BareObj"]
    assert data["p12Upper"] == [], data["p12Upper"]


def test_browser_context_headers_and_client_plus_har_version_sync():
    from gss_core.browser_context import BrowserContext
    from gss_core.chatgpt import ChatGPTClient
    from gss_core.plus_har_protocol import PlusHarProtocol

    # 1) 平台头引号去重 + 英语 Accept-Language 不重复 en
    ctx_quoted = BrowserContext(sec_ch_ua_platform='"macOS"', locale="en-US")
    h_quoted = ctx_quoted.headers()
    assert h_quoted["sec-ch-ua-platform"] == '"macOS"'
    assert h_quoted["Accept-Language"] == "en-US,en;q=0.9"
    assert BrowserContext(locale="en-PH").headers()["Accept-Language"] == "en-PH,en;q=0.9"
    assert BrowserContext(locale="ja-JP").headers()["Accept-Language"] == "ja-JP,ja;q=0.9,en;q=0.8"

    # 2) ChatGPTClient 直接传入 browser_context 时同步 UA / _sec_ch_ua_override / 版本 / 品牌
    edge_ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.7444.175 Safari/537.36 Edg/142.0.0.0"
    )
    edge_ch = '"Microsoft Edge";v="142", "Chromium";v="142", "Not_A Brand";v="99"'
    edge_ctx = BrowserContext(
        user_agent=edge_ua,
        sec_ch_ua=edge_ch,
        sec_ch_ua_platform='"macOS"',
        locale="en-US",
        timezone_id="America/New_York",
        viewport_width=1512,
        viewport_height=982,
    )
    direct_client = ChatGPTClient(access_token="tok", browser_context=edge_ctx)
    assert direct_client.ua == edge_ua
    assert direct_client.chrome_build == "142.0.7444.175"
    assert direct_client.chrome_major == "142"
    assert direct_client.impersonate == "chrome142"
    assert direct_client.sec_ch_ua == edge_ch
    ident = {k.lower(): v for k, v in direct_client._identity_headers().items()}
    assert ident["user-agent"] == edge_ua
    assert ident["sec-ch-ua"] == edge_ch
    assert ident["sec-ch-ua-platform"] == '"macOS"'
    assert ident["accept-language"] == "en-US,en;q=0.9"
    assert "sec-ch-ua-full-version-list" not in ident
    assert direct_client.sec_ch_ua_full_list == (
        '"Microsoft Edge";v="142.0.7444.175", "Chromium";v="142.0.7444.175", "Not_A Brand";v="99.0.0.0"'
    )

    # 3) PlusHarProtocol 用 Chrome 150 client + Chrome 142 BrowserContext 时同步版本与 impersonate
    ua_150 = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.7871.189 Safari/537.36"
    )
    client_150 = ChatGPTClient(access_token="tok", user_agent=ua_150)
    proto = PlusHarProtocol(
        session=json.dumps({"accessToken": "tok"}),
        client=client_150,
        browser_context=edge_ctx,
        read_only=True,
    )
    try:
        assert proto.device.ua == edge_ua
        assert proto.device.chrome_build == "142.0.7444.175"
        assert proto.device.chrome_major == "142"
        assert proto.device.impersonate == "chrome142"
        assert proto.device.sec_ch_ua == edge_ch
        assert '"Microsoft Edge";v="142.0.7444.175"' in proto.device.sec_ch_ua_full_version_list
        # 合并 BrowserContext 头时必须大小写归一化，绝不在同一请求里发两份 User-Agent / Accept-Language。
        for web_h in (proto._web_headers(json_body=True), proto._web_headers(json_body=False)):
            lows = [k.lower() for k in web_h]
            assert len(lows) == len(set(lows)), sorted(k for k in set(lows) if lows.count(k) > 1)
        from gss_core.har_device import DeviceProfile
        assert DeviceProfile.random(locale="en").accept_language() == "en"
    finally:
        proto.close()
        client_150.close()


def test_wire_sentinel_ping_stripe_gets_and_warm_assets_drop_leaked_headers(monkeypatch):
    import gss_core.chatgpt as cg
    import gss_core.checkout_protocol as cp
    import gss_core.har_telemetry as ht
    import gss_core.plus_har_protocol as php
    from gss_core.chatgpt import ChatGPTClient
    from gss_core.har_telemetry import StripeTelemetry, TelemetryContext, sentinel_ping
    from gss_core.plus_har_protocol import PlusHarProtocol

    captured: list[tuple[str, str, dict[str, str]]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _reply(self, method: str):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            captured.append((method, self.path, {k.lower(): v for k, v in self.headers.items()}))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"id":"pi_1","session_id":"es_1"}')

        def do_GET(self):
            self._reply("GET")

        def do_POST(self):
            self._reply("POST")

        def log_message(self, *args):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    monkeypatch.setattr(cg, "STRIPE_BASE", base)
    monkeypatch.setattr(cp, "STRIPE_BASE", base)
    monkeypatch.setattr(php, "STRIPE_BASE", base)
    monkeypatch.setattr(ht, "SENTINEL_PING", f"{base}/backend-api/sentinel/ping")

    client = ChatGPTClient(access_token="tok", device_id="did", account_id="acct_1")
    eng = PlusHarProtocol(
        session=json.dumps({"accessToken": "tok"}),
        client=client,
        publishable_key="pk_test_123",
        card={"number": "4242424242424242"},  # release-check: ignore
        read_only=True,
    )
    try:
        # 1) har_telemetry.sentinel_ping 走 PlusHarProtocol._web_session()（含 _RecordingSession 包装）
        web_sess = eng._web_session()
        sentinel_ping(web_sess, eng.device, timeout=5.0)
        assert web_sess.headers.get("x-openai-checkout-source") == "chatgpt_web_merged"
        assert web_sess.headers.get("x-openai-rate-limit-join-workspace") == "1"

        # 2) ChatGPTClient / checkout_protocol / PlusHarProtocol 全部 Stripe GET
        client._fetch_elements_session(
            session_id="cs_test_1", publishable_key="pk_test_123", amount=2000, currency="USD", stripe_js_id="s1"
        )
        client.retrieve_stripe_intent(intent_id="pi_123", client_secret="sec", publishable_key="pk_test_123")
        cp._read_elements(client, {"publishable_key": "pk_test_123", "amount": 20, "currency": "USD"})
        assert client.stripe_http.headers.get("Content-Type") == "application/x-www-form-urlencoded"

        eng._elements_session()
        eng._card_metadata()
        assert eng._stripe_session().headers.get("content-type") == "application/x-www-form-urlencoded"

        # 3) StripeTelemetry.warm_assets() 静态资源 GET（.js / .html）
        telem = StripeTelemetry(
            eng._m_session(),
            TelemetryContext(device=eng.device, key="pk_test_123", currency="USD", amount=2000),
        )
        monkeypatch.setattr(
            telem,
            "ASSETS",
            (
                (f"{base}/basil/stripe.js", "https://chatgpt.com/"),
                (f"{base}/v3/controller.html", "https://chatgpt.com/"),
            ),
        )
        telem.warm_assets()
    finally:
        eng.close()
        client.close()
        srv.shutdown()
        srv.server_close()

    by_path = {path.split("?", 1)[0]: (method, hdrs) for method, path, hdrs in captured}

    # sentinel_ping 校验
    m_ping, h_ping = by_path["/backend-api/sentinel/ping"]
    assert m_ping == "POST"
    assert "content-type" not in h_ping, h_ping
    assert "x-openai-checkout-source" not in h_ping, h_ping
    assert "x-openai-rate-limit-join-workspace" not in h_ping, h_ping
    assert "authorization" not in h_ping, h_ping

    # 所有 Stripe API GET 校验：无 content-type，保留 origin
    stripe_gets = [
        (p, h) for m, p, h in captured
        if m == "GET" and (p.startswith("/v1/") or p.startswith("/edge-internal/"))
    ]
    assert len(stripe_gets) >= 5, stripe_gets
    for path, hdrs in stripe_gets:
        assert "content-type" not in hdrs, (path, hdrs)
        assert hdrs.get("origin") == "https://js.stripe.com", (path, hdrs)

    # warm_assets 校验：无 origin / content-type / cookie
    for asset_path in ("/basil/stripe.js", "/v3/controller.html"):
        m_a, h_a = by_path[asset_path]
        assert m_a == "GET"
        assert "origin" not in h_a, (asset_path, h_a)
        assert "content-type" not in h_a, (asset_path, h_a)
        assert "cookie" not in h_a, (asset_path, h_a)
