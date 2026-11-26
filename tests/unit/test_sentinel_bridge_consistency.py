"""Sentinel PoW 与 sdk.js 沙箱指纹一致性（端到端，本地回环，无真实上游）。

真浏览器里 `/sentinel/req` 的 p 与 PoW 的 p 是同一份 config 数组的两次快照：
只有计数器（[3]）和耗时（[9]）会变，sdkUrl / sid / heapLimit / 时区名必须一致。
这里用一个「假 sdk.js」把自己的指纹数组发到 /sentinel/req，再验证 bridge 解出的
PoW p 逐字段复用了它。
"""
from __future__ import annotations

import base64
import http.server
import json
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = ROOT / "packages/core/src/gss_core/sentinel_assets"

SDK_STUB = r"""
var SentinelSDK = {
  __proto2: function (flow) {
    var g = globalThis;
    var cfg = [
      g.screen.width + g.screen.height,      // 0
      new Date().toString(),                 // 1
      g.performance.memory.jsHeapSizeLimit,  // 2
      0,                                     // 3
      g.navigator.userAgent,                 // 4
      g.__SDK_URL__,                         // 5
      null,                                  // 6
      g.navigator.language,                  // 7
      g.navigator.languages.join(","),       // 8
      0,                                     // 9
      "getBattery",                          // 10
      "location",                            // 11
      "onbeforeunload",                      // 12
      777.5,                                 // 13
      g.__SENTINEL_SID__,                    // 14
      "",                                    // 15
      g.navigator.hardwareConcurrency,       // 16
      g.__LOAD_TS__                          // 17
    ];
    try {
      var body = JSON.stringify({
        p: "gAAAAAC" + btoa(JSON.stringify(cfg)) + "~S",
        id: "fixture-device",
        flow: flow
      });
      fetch("/backend-api/sentinel/req", { method: "POST", body: body })
        .catch(function () {});
    } catch (e) {}
    return JSON.stringify({
      c: "fixture-challenge",
      t: "fixture-turn",
      seed: "fixture-seed",
      diff: "0",
      powReq: true,
      so: "fixture-so"
    });
  }
};
"""


def _decode_p(p: str, *, allowed_prefixes: tuple[str, ...] = ("gAAAAAB", "gAAAAAC")) -> list:
    assert p.endswith("~S") and any(p.startswith(prefix) for prefix in allowed_prefixes), p[:16]
    body = p[7:-len("~S")]
    return json.loads(base64.b64decode(body).decode("utf-8"))


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 运行 sentinel bridge")
def test_bridge_pow_reuses_sdk_req_fingerprint(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("sentinel_bootstrap.js", "sentinel_bridge.js"):
        shutil.copy(ASSETS_DIR / name, assets / name)
    (assets / "sentinel_sdk.js").write_text(SDK_STUB)
    # 一次性模式需要能 require 到这些模块（无代理时走 Node 内置 fetch）。
    shutil.copy(ASSETS_DIR / "package.json", assets / "package.json")

    seen: list[bytes] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen.append(self.rfile.read(length))
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
    origin = f"http://127.0.0.1:{port}"

    request = {
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.7871.189 Safari/537.36"
        ),
        "cores": 8,
        "deviceId": "fixture-device",
        "flow": "chatgpt_checkout",
        "proxy": "",
        "version": "20260810913b",
        "pageUrl": "https://chatgpt.com/?cta_tab=personal",
        "language": "en-PH",
        "timezone": "Asia/Manila",
        "sentinelOrigin": origin,
        "platform": "Win32",
        "platformLabel": "Windows",
        "screenW": 1920,
        "screenH": 1080,
        "availH": 1032,
    }
    try:
        proc = subprocess.run(
            ["node", str(assets / "sentinel_bridge.js")],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(assets),
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip())
    assert "error" not in out, out

    # sdk.js 侧的指纹（发往 /sentinel/req 的那一份）。
    assert seen, "bridge 未通过 fetch 桥把 /sentinel/req 发出去"
    req_payload = json.loads(seen[0].decode("utf-8"))
    assert req_payload["flow"] == "chatgpt_checkout"
    req_cfg = _decode_p(req_payload["p"])

    token = json.loads(out["main"])
    pow_cfg = _decode_p(token["p"])

    # 同一份指纹：除了计数器与耗时，逐字段必须相同。
    for index in (0, 1, 2, 4, 5, 7, 8, 10, 11, 12, 14, 16, 17):
        assert pow_cfg[index] == req_cfg[index], f"config[{index}] 与 sdk.js 不一致"

    # 关键字段的语义检查（历史缺陷正是这四处自相矛盾）。
    assert pow_cfg[2] == 4294705152
    assert pow_cfg[5] == f"{origin}/sentinel/20260810913b/sdk.js"
    assert pow_cfg[14] == req_cfg[14] and pow_cfg[14]
    assert "Philippine Standard Time" in pow_cfg[1]
    assert "Asia/Manila" not in pow_cfg[1]

    # 两个 sentinel 头都带同一 challenge，so 轨迹也随 flow 生成。
    assert token["c"] == "fixture-challenge"
    assert out["hasSo"] is True
    assert out["powReq"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 运行 sentinel bridge")
def test_bridge_pow_reuses_real_sentinel_sdk_req_fingerprint():
    """直接运行仓库内真实 sentinel_sdk.js（非假 stub），防止前缀或字段演化导致静默回退。"""
    seen_reqs: list[tuple[dict[str, str], bytes]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            hdrs = {k.lower(): v for k, v in self.headers.items()}
            seen_reqs.append((hdrs, self.rfile.read(length)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({
                    "persona": "chatgpt-freeaccount",
                    "token": "real-sdk-challenge",
                    "turnstile": {"required": False},
                    "proofofwork": {
                        "required": True,
                        "seed": "0.123456789",
                        "difficulty": "0fffff",
                    },
                    "force_login": False,
                }).encode("utf-8")
            )

        def log_message(self, *args):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{port}"
    build = "150.0.7871.189"
    request = {
        "ua": (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{build} Safari/537.36"
        ),
        "cores": 8,
        "deviceId": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
        "flow": "chatgpt_checkout",
        "proxy": "",
        "version": "20260810913b",
        "pageUrl": "https://chatgpt.com/?cta_tab=personal",
        "language": "en-PH",
        "timezone": "Asia/Manila",
        "sentinelOrigin": origin,
        "platform": "Win32",
        "platformLabel": "Windows",
        "screenW": 1920,
        "screenH": 1080,
        "availH": 1032,
        "secChUa": '"Google Chrome";v="150", "Chromium";v="150", "Not(A:Brand";v="24"',
        "secChUaPlatform": '"Windows"',
    }
    try:
        proc = subprocess.run(
            ["node", str(ASSETS_DIR / "sentinel_bridge.js")],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ASSETS_DIR),
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip())
    assert "error" not in out, out
    assert seen_reqs, "真实 sentinel_sdk.js 未发出 /sentinel/req"

    req_hdrs, raw_body = seen_reqs[0]
    # 对齐 HAR：/sentinel/req 带高熵 Client Hints。
    assert req_hdrs.get("sec-ch-ua-full-version") == f'"{build}"'
    assert req_hdrs.get("sec-ch-ua-model") == '""'
    assert f'"Google Chrome";v="{build}"' in req_hdrs.get("sec-ch-ua-full-version-list", "")

    req_payload = json.loads(raw_body.decode("utf-8"))
    assert req_payload["p"].startswith("gAAAAAC"), "真实 sdk.js 前缀应为 gAAAAAC"
    req_cfg = _decode_p(req_payload["p"], allowed_prefixes=("gAAAAAC",))

    token = json.loads(out["main"])
    assert token["p"].startswith("gAAAAAB")
    pow_cfg = _decode_p(token["p"], allowed_prefixes=("gAAAAAB",))

    # 除 [3] 计数器与 [9] 耗时外，PoW 必须逐字段复用真实 sdk.js 发往 /sentinel/req 的指纹数组。
    for index in (0, 1, 2, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15, 16, 17):
        assert pow_cfg[index] == req_cfg[index], f"真实 sdk.js config[{index}] 不一致: {pow_cfg[index]!r} != {req_cfg[index]!r}"
    assert pow_cfg[2] == 4294705152
    assert pow_cfg[5] == f"{origin}/sentinel/20260810913b/sdk.js"
    assert "Philippine Standard Time" in pow_cfg[1]
    assert "Asia/Manila" not in pow_cfg[1]
    assert isinstance(pow_cfg[17], int)
