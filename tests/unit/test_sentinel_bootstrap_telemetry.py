"""P0 回归：sentinel_bootstrap.js 的时基 / 行为遥测 / 可枚举面必须自洽。

历史缺陷（`unusual_activity` 的真因之一）：
  1. `performance.timeOrigin` 钉在沙箱启动瞬间，`performance.now()` 从 0 起算，
     于是 sdk 指纹数组第 13 位（采集时的 now()）只有个位数毫秒 —— 等于「页面刚
     打开 4ms 就提交建单」；
  2. 行为遥测用 `t0 = now() - dwell` 反推进页时刻，now() 太小导致 `__oai_so_t0`
     与全部 pointermove.t 变成负数；
  3. `__oai_so_sw` 写死 1440，与 1920 的屏幕档案矛盾；
  4. `Object.keys(document)` 暴露全部 shim 方法（p[11] 随机报出 createTextNode /
     scripts），`Object.keys(window)` 暴露桥接构建参数（p[12] 可能报出 __UA__）。

本测试在 Node 里跑真实 sentinel_bootstrap.js + 真实 sentinel_sdk.js，
拦住该 sdk 发往 /backend-api/sentinel/req 的指纹数组做断言。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = ROOT / "packages/core/src/gss_core/sentinel_assets"

HARNESS = r"""
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// node -e <src> <assets> <flow> <screenW>：-e 模式下 argv 里没有脚本路径，
// 取末尾三个参数最稳。
const cliArgs = process.argv.slice(-3);
const assets = cliArgs[0];
const flow = cliArgs[1];
const screenW = Number(cliArgs[2] || 1920);
const bootstrapSrc = fs.readFileSync(path.join(assets, "sentinel_bootstrap.js"), "utf8");
const sdkSrc = fs.readFileSync(path.join(assets, "sentinel_sdk.js"), "utf8");

function decodeP(p) {
  let b = String(p || "");
  for (const pre of ["gAAAAAC", "gAAAAAB"]) {
    if (b.startsWith(pre)) { b = b.slice(pre.length); break; }
  }
  if (b.endsWith("~S")) b = b.slice(0, -2);
  return JSON.parse(Buffer.from(b, "base64").toString("utf8"));
}

const captured = [];
const sandbox = {
  console,
  setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
  fetch: (url, opts) => {
    captured.push(opts && opts.body);
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        persona: "chatgpt-freeaccount",
        token: "fixture-challenge",
        turnstile: { required: false },
        proofofwork: { required: true, seed: "0.5", difficulty: "ffffffff" },
        force_login: false,
      }),
      text: () => Promise.resolve("{}"),
      headers: { get: () => null },
    });
  },
  TextEncoder, TextDecoder,
  btoa: (s) => Buffer.from(String(s), "latin1").toString("base64"),
  atob: (s) => Buffer.from(String(s), "base64").toString("latin1"),
  crypto: globalThis.crypto,
  URL, URLSearchParams,
  __UA__: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/150.0.7871.189 Safari/537.36",
  __CORES__: 8,
  __FLOW__: flow,
  __SDK_URL__: "https://chatgpt.com/sentinel/20260810913b/sdk.js",
  __SEED_DID_KEY__: "oai-did",
  __SEED_DID_VAL__: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  __PAGE_URL__: "https://chatgpt.com/?cta_tab=personal",
  __LANGUAGE__: "en-PH",
  __LANGUAGES__: ["en-PH", "en"],
  __TIMEZONE__: "Asia/Manila",
  __COOKIE_HEADER__: "oai-did=a1b2c3d4",
  __PLATFORM__: "Win32",
  __PLATFORM_LABEL__: "Windows",
  __SCREEN_W__: screenW,
  __SCREEN_H__: 1080,
  __AVAIL_H__: 1032,
  __MAX_TOUCH__: 0,
};

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(bootstrapSrc, sandbox, { filename: "sentinel_bootstrap.js" });
  vm.runInContext(
    sdkSrc + "\n;Object.defineProperty(globalThis,'__SENTINEL_SDK__'," +
      "{value:SentinelSDK,enumerable:false,writable:true,configurable:true});",
    sandbox,
    { filename: "sentinel_sdk.js" }
  );
  await Promise.resolve(sandbox.__SENTINEL_SDK__.__proto2(flow));

  const body = captured.filter(Boolean).map(String)
    .map((raw) => { try { return JSON.parse(raw); } catch (_) { return null; } })
    .filter((v) => v && v.p)[0];
  const p = decodeP(body.p);
  const soP = sandbox.__oai_so_p || [];
  const soH = sandbox.__oai_so_h || [];
  const windowKeys = Object.keys(sandbox);
  const perfNow = sandbox.performance.now();
  // 连续高频调用 >1200 次验证 performance.now() 绝不因 % 1000 回绕倒退。
  let prevP = perfNow;
  let monotonicOk = true;
  for (let i = 0; i < 1250; i++) {
    const cur = sandbox.performance.now();
    if (!(cur > prevP)) { monotonicOk = false; break; }
    prevP = cur;
  }
  const out = {
    p,
    perfNow,
    monotonicOk,
    timeOrigin: sandbox.performance.timeOrigin,
    soT0: sandbox.__oai_so_t0,
    soSw: sandbox.__oai_so_sw,
    soHt: sandbox.__oai_so_ht,
    soLx: sandbox.__oai_so_lx,
    soLy: sandbox.__oai_so_ly,
    pointerTimes: soP.map((e) => e.t),
    pointerCoords: soP.map((e) => [e.clientX, e.clientY]),
    keyTimes: soH.map((e) => e.t),
    documentKeys: Object.keys(sandbox.document),
    windowKeys,
    screenWidth: sandbox.screen.width,
    availHeight: sandbox.screen.availHeight,
    innerWidth: sandbox.innerWidth,
  };
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((err) => {
  process.stderr.write(String((err && err.stack) || err) + "\n");
  process.exit(1);
});
"""

DWELL_BOUNDS = {
    "chatgpt_checkout": (5200.0, 9500.0),
    "oauth_create_account": (6800.0, 11500.0),
}

# 真 Chrome 的 window 上允许存在的应用级全局（React Router / 站点自建哨兵状态）。
APP_GLOBALS = {
    "__reactRouterContext",
    "__sentinel_init_pending",
    "__sentinel_token_pending",
}


def _run(flow: str, screen_w: int = 1920) -> dict:
    proc = subprocess.run(
        ["node", "-e", HARNESS, str(ASSETS_DIR), flow, str(screen_w)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 运行 sentinel 沙箱")
@pytest.mark.parametrize("flow", ["chatgpt_checkout", "oauth_create_account"])
def test_fingerprint_dwell_and_behaviour_telemetry_are_positive(flow):
    data = _run(flow)
    lo, hi = DWELL_BOUNDS[flow]
    p = data["p"]

    # 1) 采集时刻（p[13]）必须是「进页停留数秒之后」，而不是沙箱启动后的几毫秒，且严格单调。
    assert lo <= p[13] <= hi, f"p[13]={p[13]} 不在合理区间 [{lo}, {hi}] 内"
    assert data["monotonicOk"] is True, "performance.now() 高频调用出现回绕倒退"
    # 2) 页面导航起点与 now() 互补。
    assert abs(data["perfNow"] - p[13]) < 2000
    assert abs((p[17] + data["perfNow"]) - __import__("time").time() * 1000) < 60000
    assert data["timeOrigin"] == p[17]

    # 3) 行为遥测全部落在 (0, now] 内：负数时间戳 = 页面还没打开就产生了输入事件。
    assert data["soT0"] > 0, f"__oai_so_t0={data['soT0']} 不能是负数"
    assert data["pointerTimes"], "pointermove 事件序列为空"
    assert all(t > 0 for t in data["pointerTimes"]), "pointermove 出现负数时间戳"
    times = data["pointerTimes"]
    assert times == sorted(times), "pointermove 时间戳必须单调递增"
    assert times[0] >= data["soT0"], "首个 pointermove 早于进页时刻"
    assert times[-1] <= data["perfNow"], "最后一个 pointermove 晚于采集时刻"

    if flow == "oauth_create_account":
        assert data["keyTimes"], "注册流必须带 keydown 序列"
        assert all(t > 0 for t in data["keyTimes"]), "keydown 出现负数时间戳"
        assert data["keyTimes"] == sorted(data["keyTimes"])
        assert data["keyTimes"][-1] <= data["perfNow"]
        # 注册流的最后一次交互是「提交」，紧跟在打完名字之后。
        assert data["perfNow"] - data["keyTimes"][-1] < 1200
        # 鼠标先落在输入框，之后才出现按键。
        assert times[-1] < data["keyTimes"][0]
    else:
        # 结账流的最后一次交互是点「升级」：采集点紧跟最后的 pointermove。
        assert data["perfNow"] - times[-1] < 1200
        # 结账流没有键盘输入，带 keydown 就与真实结账页直接矛盾。
        assert data["keyTimes"] == []

    # 4) 视口宽度与屏幕档案同源。
    assert data["soSw"] == data["screenWidth"] == data["innerWidth"]


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 运行 sentinel 沙箱")
def test_fingerprint_surface_exposes_no_shim_internals():
    data = _run("chatgpt_checkout", screen_w=1536)
    p = data["p"]

    # document 在真 Chrome 上只有 location 一个自有可枚举键（WebIDL [LegacyUnforgeable]）。
    assert data["documentKeys"] == ["location"], data["documentKeys"]
    assert p[11] == "location", f"p[11]={p[11]!r} 应为 location"

    # window 上不能出现桥接构建参数 / 自造运行时状态（__UA__ / __FLOW__ / __SENTINEL_* …）。
    leaked = []
    for key in data["windowKeys"]:
        if not (key.startswith("__") and key.endswith("__")):
            continue
        if key.startswith("__oai_so_") or key in APP_GLOBALS:
            continue
        leaked.append(key)
    assert leaked == [], f"window 上泄露了内部键: {leaked}"
    assert isinstance(p[12], str) and not p[12].startswith("__SENTINEL"), p[12]

    # __oai_so_* 必须保持可枚举：真页面 collector 就是普通赋值写进去的，
    # so.snapshot_dx 依赖它们仍在 window 上。
    assert any(k.startswith("__oai_so_") for k in data["windowKeys"])
    # 视口宽度随档案变化（不再是写死的 1440）。
    assert data["soSw"] == 1536


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 运行 sentinel 沙箱")
@pytest.mark.parametrize("screen_w", [1366, 1440, 1920])
def test_pointer_trajectory_stays_inside_viewport(screen_w):
    data = _run("chatgpt_checkout", screen_w=screen_w)
    assert data["soSw"] == screen_w
    assert 0 < data["soLx"] < screen_w, f"soLx={data['soLx']} 超出视口宽度 {screen_w}"
    assert 0 < data["soLy"] < data["availHeight"], f"soLy={data['soLy']} 超出视口高度"
    for x, y in data["pointerCoords"]:
        assert 0 < x < screen_w, f"pointermove x={x} 超出视口宽度 {screen_w}"
        assert 0 < y < data["availHeight"], f"pointermove y={y} 超出视口高度"
