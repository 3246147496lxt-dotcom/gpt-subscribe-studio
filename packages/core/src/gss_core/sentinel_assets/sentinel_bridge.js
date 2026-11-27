#!/usr/bin/env node
/**
 * sentinel_bridge.js —— 用真·sdk.js（Node/V8 运行）生成 create_account 需要的
 * openai-sentinel-token（真 t）+ openai-sentinel-so-token（真 so 设备指纹）。
 *
 * 纯 Python/httpx 拿不到 so-token（它由混淆 sdk.js 采集浏览器传感器算出），
 * 这里在 Node 里铺好浏览器 shim（sentinel_bootstrap.js）+ 加载真 sdk.js，
 * 调 SentinelSDK.__proto2(flow) 拿 {t, so, c, seed, diff, powReq}，
 * Node 侧解 PoW 拼装两个 header 值，JSON 打到 stdout。
 *
 * 用法：把 {ua, cores, deviceId, flow, proxy, version} 以 JSON 从 stdin 传入。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

// Resolve the Python used by the project before falling back to PATH. The
// proxy transport helper is launched from this Node process, and Windows
// installations commonly have only the project venv (no `python.exe` PATH
// entry).
function resolvePython() {
  const configured = String(process.env.PYTHON_BIN || process.env.SENTINEL_PYTHON || "").trim();
  if (configured) return configured;
  let dir = __dirname;
  for (let i = 0; i < 7; i++) {
    const local = process.platform === "win32"
      ? path.join(dir, ".venv", "Scripts", "python.exe")
      : path.join(dir, ".venv", "bin", "python");
    if (fs.existsSync(local)) return local;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return process.platform === "win32" ? "python" : "python3";
}

function readStdin() {
  return new Promise((resolve) => {
    let buf = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (d) => (buf += d));
    process.stdin.on("end", () => resolve(buf));
  });
}

// ---- PoW（对齐 sentinel.go：configArray(answer=true) + fnv1a32 + b64） ----
function fnv1a32(buf) {
  let h = 0x811c9dc5 >>> 0;
  for (let i = 0; i < buf.length; i++) {
    h ^= buf[i];
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  h ^= h >>> 16;
  h = Math.imul(h, 2246822507) >>> 0;
  h ^= h >>> 13;
  h = Math.imul(h, 3266489909) >>> 0;
  h ^= h >>> 16;
  return h >>> 0;
}

function b64Compact(v) {
  return Buffer.from(JSON.stringify(v), "utf8").toString("base64");
}

function uuidv4() {
  const b = require("crypto").randomBytes(16);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => (x + 0x100).toString(16).slice(1));
  return `${h[0]}${h[1]}${h[2]}${h[3]}-${h[4]}${h[5]}-${h[6]}${h[7]}-${h[8]}${h[9]}-${h[10]}${h[11]}${h[12]}${h[13]}${h[14]}${h[15]}`;
}

const SEP = "\u2212"; // U+2212 MINUS SIGN

// 真 Chrome 的 Date.toString() 括号内是本地化长时区名（Philippine Standard Time），
// 不是 IANA 标识（Asia/Manila）。bootstrap 侧用同一套 Intl 调用，两段必须一致。
const _tzNameCache = new Map();
function localizedTimeZoneName(timezone) {
  const tz = String(timezone || "America/Chicago");
  if (_tzNameCache.has(tz)) return _tzNameCache.get(tz);
  let name = "";
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      timeZoneName: "long",
    }).formatToParts(new Date());
    const hit = parts.find((part) => part.type === "timeZoneName");
    if (hit && hit.value) name = hit.value;
  } catch (_) {}
  if (!name) name = tz;
  _tzNameCache.set(tz, name);
  return name;
}

function languageList(language) {
  const loc = String(language || "en-US").trim() || "en-US";
  const primary = loc.split("-")[0];
  const out = [loc];
  if (primary && !out.includes(primary)) out.push(primary);
  if (primary !== "en" && !out.includes("en")) out.push("en");
  return out;
}

function acceptLanguageHeader(language) {
  const loc = String(language || "en-US").trim() || "en-US";
  const primary = loc.split("-")[0];
  let value = primary && primary !== loc ? `${loc},${primary};q=0.9` : loc;
  if (primary !== "en") {
    value += ",en;q=0.8";
  }
  return value;
}

function makeGenerator(ua, cores, language, timezone, screenW, screenH, opts) {
  const overrides = opts || {};
  const initialPerformance =
    typeof overrides.initialPerformance === "number"
      ? overrides.initialPerformance
      : 350.0 + Math.random() * 2200.0;
  const sw = Number(screenW) || 1920;
  const sh = Number(screenH) || 1080;
  const loc = language || "en-US";
  const tz = timezone || "America/Chicago";
  const startedAt = Date.now();
  return {
    ua,
    cores: cores || 16,
    language: loc,
    languages: languageList(loc),
    timezone: tz,
    tzName: localizedTimeZoneName(tz),
    // 与 sdk.js 沙箱里那份指纹共用同一 sid / 启动基准，避免同一 flow 两次上报不同设备。
    sid: overrides.sid || uuidv4(),
    heapLimit: Number(overrides.heapLimit) || 4294705152,
    sdkUrl: String(overrides.sdkUrl || ""),
    dataBuild: String(overrides.dataBuild || ""),
    pageUrl: String(overrides.pageUrl || ""),
    // 与真实浏览器一致：screen.width + screen.height（指纹数组 [0]）
    screenNum: sw + sh,
    nine: 0,
    loadTs:
      typeof overrides.loadTs === "number" && overrides.loadTs > 0
        ? overrides.loadTs
        : startedAt - initialPerformance,
    initialPerformance,
    startedAt,
    probeNav: "getBattery" + SEP + "function getBattery() { [native code] }",
    reactKey: "location",
    eventName: "onbeforeunload",
  };
}

function formatConfigDate(g, now) {
  let offsetMinutes = 0;
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: g.timezone,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(now).reduce((out, part) => {
      if (part.type !== "literal") out[part.type] = Number(part.value);
      return out;
    }, {});
    const localAsUtc = Date.UTC(
      parts.year, parts.month - 1, parts.day,
      parts.hour, parts.minute, parts.second
    );
    offsetMinutes = Math.round((localAsUtc - now.getTime()) / 60000);
  } catch (_) {}
  const d = new Date(now.getTime() + offsetMinutes * 60000);
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()];
  const mo = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()];
  const p2 = (n) => (n < 10 ? "0" : "") + n;
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const offset = Math.abs(offsetMinutes);
  return (
    `${wd} ${mo} ${p2(d.getUTCDate())} ${d.getUTCFullYear()} ` +
    `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())} ` +
    `GMT${sign}${p2(Math.floor(offset / 60))}${p2(offset % 60)} (${g.tzName || g.timezone})`
  );
}

function queryKeyString(pageUrl) {
  try {
    if (!pageUrl) return "";
    return [...new URL(pageUrl).searchParams.keys()].join(",");
  } catch (_) {
    return "";
  }
}

function configArray(g) {
  const perfNow = g.initialPerformance + Math.max(0, Date.now() - g.startedAt);
  const tstr = formatConfigDate(g, new Date());
  return [
    g.screenNum, // 0
    tstr, // 1
    g.heapLimit, // 2
    0, // 3 PoW 计数器
    g.ua, // 4
    g.sdkUrl, // 5 与 sandbox.__SDK_URL__ 完全一致
    g.dataBuild || null, // 6 首页 <html data-build="..."> 构建哈希
    g.language, // 7
    g.languages.join(","), // 8
    g.nine, // 9
    g.probeNav, // 10
    g.reactKey, // 11
    g.eventName, // 12
    perfNow, // 13
    g.sid, // 14
    queryKeyString(g.pageUrl), // 15 与 sdk.js [...new URLSearchParams(location.search).keys()].join(",") 对齐
    g.cores, // 16
    g.loadTs, // 17
    0, 0, 0, 0, 0, 0, 0, // 18..24
  ];
}

/* 解码 sdk.js 自己发往 /backend-api/sentinel/req 的 p（base64 指纹数组）。
 * 这是同一 flow 的「第一份」指纹：PoW 的 p 必须与它逐字段一致
 * （sdkUrl / sid / heapLimit / 时区名 / UA / 语言 / 屏幕），否则服务端会看到
 * 两条互相矛盾的设备记录。
 * 注意：真实 sdk.js 发往 /sentinel/req 的 p 前缀为 "gAAAAAC"（请求前置指纹），
 * 解完 PoW 的回传答案才用 "gAAAAAB"；两者都必须接受。
 */
function decodeReqConfig(rawBody) {
  try {
    const body = typeof rawBody === "string" ? rawBody : String(rawBody || "");
    if (!body) return null;
    const parsed = JSON.parse(body);
    const p = String((parsed && parsed.p) || "");
    let b64 = "";
    if (p.startsWith("gAAAAAC")) {
      b64 = p.slice("gAAAAAC".length);
    } else if (p.startsWith("gAAAAAB")) {
      b64 = p.slice("gAAAAAB".length);
    } else {
      return null;
    }
    if (b64.endsWith("~S")) b64 = b64.slice(0, -2);
    const arr = JSON.parse(Buffer.from(b64, "base64").toString("utf8"));
    return Array.isArray(arr) && arr.length >= 18 ? arr : null;
  } catch (_) {
    return null;
  }
}

function powBaseConfig(g, captured) {
  if (Array.isArray(captured) && captured.length >= 18) {
    const base = captured.slice();
    // 计数器与耗时逐次覆盖，其余字段保持 /sentinel/req 里的原值。
    base[3] = 0;
    base[9] = g.nine;
    return base;
  }
  return configArray(g);
}

const MAX_ATTEMPTS = 500000;
const ERR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D";

function solvePoW(g, seed, difficulty, captured) {
  if (!difficulty) difficulty = "0";
  const data = powBaseConfig(g, captured);
  const seedB = Buffer.from(seed, "ascii");
  const dl = difficulty.length;
  const startedAt = Date.now();
  for (let i = 0; i < MAX_ATTEMPTS; i++) {
    data[3] = i;
    data[9] = Math.round(Date.now() - startedAt);
    const payload = b64Compact(data);
    const h = fnv1a32(Buffer.concat([seedB, Buffer.from(payload, "ascii")]));
    const hex = h.toString(16).padStart(8, "0");
    const cmpLen = Math.min(dl, hex.length);
    if (hex.slice(0, cmpLen) <= difficulty.slice(0, cmpLen)) {
      return "gAAAAAB" + payload + "~S";
    }
  }
  return "gAAAAAB" + ERR_PREFIX + b64Compact(null) + "~S";
}


const _GREASEY_CHARS = " (:-./);=?_";
const _GREASE_VERSIONS = ["8", "99", "24"];
const _BRAND_ORDERS = [
  [0, 1, 2],
  [0, 2, 1],
  [1, 0, 2],
  [1, 2, 0],
  [2, 0, 1],
  [2, 1, 0],
];
const _KNOWN_REAL_BRANDS = new Set([
  "Chromium",
  "Google Chrome",
  "Microsoft Edge",
  "HeadlessChrome",
  "Opera",
  "Brave",
  "Vivaldi",
]);

function chromiumBrandEntries(major) {
  const seed = parseInt(String(major || "150"), 10) || 150;
  const m = String(major || seed);
  const n = _GREASEY_CHARS.length;
  const greaseBrand = `Not${_GREASEY_CHARS[seed % n]}A${_GREASEY_CHARS[(seed + 1) % n]}Brand`;
  const greaseVer = _GREASE_VERSIONS[seed % _GREASE_VERSIONS.length];
  const items = [
    { brand: greaseBrand, version: greaseVer, isGrease: true },
    { brand: "Chromium", version: m, isGrease: false },
    { brand: "Google Chrome", version: m, isGrease: false },
  ];
  const order = _BRAND_ORDERS[seed % _BRAND_ORDERS.length];
  return [0, 1, 2].map((pos) => items[order.indexOf(pos)]);
}

function parseSecChUaTokens(secChUa) {
  const re = /"([^"]*)"\s*;\s*v="([^"]*)"/g;
  const out = [];
  let m;
  while ((m = re.exec(String(secChUa || "")))) {
    if (m[1]) out.push({ brand: m[1], version: m[2] });
  }
  return out;
}

function isGreaseBrand(brand, version, major) {
  if (/^Not.A.Brand$/.test(brand)) return true;
  if (_KNOWN_REAL_BRANDS.has(brand)) return false;
  return Boolean(version) && version !== major;
}

const _KNOWN_FULL_BUILDS = {
  "150": "150.0.7871.189",
  "146": "146.0.7680.180",
  "145": "145.0.7632.116",
  "142": "142.0.7444.175",
  "136": "136.0.7103.114",
  "133": "133.0.6943.141",
};

function expandReducedBuild(build) {
  const s = String(build || "").trim();
  const parts = s.split(".");
  if (parts.length === 4 && parts[1] === "0" && parts[2] === "0" && parts[3] === "0") {
    if (_KNOWN_FULL_BUILDS[parts[0]]) return _KNOWN_FULL_BUILDS[parts[0]];
    const majorInt = parseInt(parts[0], 10);
    if (Number.isFinite(majorInt)) {
      const branch = Math.max(6000, 6943 + (majorInt - 133) * 55);
      return `${parts[0]}.0.${branch}.112`;
    }
  }
  return s;
}

function highEntropyClientHints(ua, extra, secChUa) {
  const m = String(ua || "").match(/(?:Chrome|CriOS)\/(\d+(?:\.\d+)*)/);
  if (!m) return {};
  const build = expandReducedBuild(m[1]);
  const major = build.split(".")[0] || "150";
  const hints = Object.assign({}, extra || {});
  const tokens = parseSecChUaTokens(secChUa);
  let fullEntries;
  if (tokens.length) {
    fullEntries = tokens.map((entry) => {
      if (isGreaseBrand(entry.brand, entry.version, major)) {
        const v = String(entry.version || "24");
        return { brand: entry.brand, version: v.indexOf(".") === -1 ? `${v}.0.0.0` : v };
      }
      return { brand: entry.brand, version: build };
    });
  } else {
    fullEntries = chromiumBrandEntries(major).map((entry) => ({
      brand: entry.brand,
      version: entry.isGrease ? `${entry.version}.0.0.0` : build,
    }));
  }
  const fullList = fullEntries.map((e) => `"${e.brand}";v="${e.version}"`).join(", ");
  return {
    ...hints,
    "sec-ch-ua-full-version": `"${build}"`,
    "sec-ch-ua-full-version-list": fullList,
    "sec-ch-ua-model": '""',
  };
}

function makeFetch(ua, language, cookieHeader, dispatcher, fetchImpl, sentinelOrigin, frameUrl, secChUa, secChUaPlatform, capture, clientHints, sendHighEntropyHints) {
  // sdk 里的 fetch 桥到 fetchImpl：改写 URL 到 sentinelOrigin、补头、走代理。
  // 2026-11-26 真机实测：chatgpt.com 未下发 Accept-CH，/backend-api/sentinel/req
  // 仅发低熵三件套（sec-ch-ua / -mobile / -platform），仅在显式开启时附加高熵 hints。
  const extraHints = (secChUa && sendHighEntropyHints) ? highEntropyClientHints(ua, clientHints, secChUa) : {};
  return function (url, opts) {
    opts = opts || {};
    let u = String(url);
    if (u.indexOf("/backend-api/sentinel/req") !== -1) {
      u = sentinelOrigin + "/backend-api/sentinel/req";
      // 记下 sdk.js 自己的指纹（p 里的 config 数组），PoW 必须复用它。
      if (capture && opts.body != null) {
        capture.reqBody = typeof opts.body === "string" ? opts.body : String(opts.body);
      }
    } else if (u.charAt(0) === "/") {
      u = sentinelOrigin + u;
    }
    const method = String(opts.method || "GET").toUpperCase();
    const hasBody = opts.body != null || (method !== "GET" && method !== "HEAD");
    const headers = Object.assign(
      {
        ...(hasBody ? { "content-type": "text/plain;charset=UTF-8", origin: sentinelOrigin } : {}),
        referer: frameUrl,
        "user-agent": ua,
        accept: "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": acceptLanguageHeader(language),
        priority: "u=1, i",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        ...(secChUa ? { "sec-ch-ua": secChUa, "sec-ch-ua-mobile": "?0", ...extraHints } : {}),
        ...(secChUaPlatform ? { "sec-ch-ua-platform": secChUaPlatform } : {}),
        ...(cookieHeader ? { cookie: cookieHeader } : {}),
      },
      opts.headers || {}
    );
    const fopts = { method, headers };
    if (opts.body != null) fopts.body = opts.body;
    if (dispatcher) fopts.dispatcher = dispatcher;
    return fetchImpl(u, fopts);
  };
}

function makeCurlFetch(proxy) {
  const { spawn } = require("child_process");
  const marker = "\n__SENTINEL_HTTP_STATUS__:";
  return function curlFetch(url, opts) {
    opts = opts || {};
    return new Promise((resolve, reject) => {
      const args = [
        "-sS",
        "--compressed",
        "--connect-timeout", "30",
        "--max-time", "120",
        // Windows 系统 curl 使用 Schannel；部分代理/TLS 链路会在 TLS1.3
        // 凭据初始化阶段报 SEC_E_NO_CREDENTIALS。固定 TLS1.2 并关闭
        // Schannel 吊销检查，避免 Sentinel 请求在生成风控令牌前被中断。
        "--tlsv1.2",
        "--tls-max", "1.2",
        "--ssl-no-revoke",
        "--http1.1",
        "--proxy", proxy,
        "-X", opts.method || "GET",
      ];
      Object.entries(opts.headers || {}).forEach(([name, value]) => {
        args.push("-H", `${name}: ${value}`);
      });
      if (opts.body != null) {
        args.push("--data-binary", String(opts.body));
      }
      args.push("-w", marker + "%{http_code}", String(url));
      const child = spawn(process.env.SENTINEL_CURL || "curl", args, {
        stdio: ["ignore", "pipe", "pipe"],
      });
      const stdout = [];
      const stderr = [];
      child.stdout.on("data", (chunk) => stdout.push(chunk));
      child.stderr.on("data", (chunk) => stderr.push(chunk));
      child.on("error", reject);
      child.on("close", (code) => {
        const raw = Buffer.concat(stdout).toString("utf8");
        const markerAt = raw.lastIndexOf(marker);
        const body = markerAt >= 0 ? raw.slice(0, markerAt) : raw;
        const status = markerAt >= 0 ? Number(raw.slice(markerAt + marker.length)) : 0;
        if (code !== 0) {
          reject(new Error(Buffer.concat(stderr).toString("utf8").slice(0, 4000) || `curl exit ${code}`));
          return;
        }
        resolve({
          status,
          ok: status >= 200 && status < 300,
          headers: { get: () => null },
          text: async () => body,
          json: async () => JSON.parse(body),
        });
      });
    });
  };
}

/* ── 常驻 curl 助手（可选加速）────────────────────────────────────────
 * SOCKS 代理时每次请求都要冷启动一个 ~32MB 的 Python(curl_cffi) 进程。
 * 常驻化后进程复用，请求/响应仍走与 stdin/stdout 完全一致的 JSON 协议。
 * 任何失败都会回退到下面的一次性进程，可用性不变。
 */
function curlDaemonSockets() {
  const explicit = String(process.env.SENTINEL_CURL_DAEMON_SOCKET || "").trim();
  if (explicit) {
    return explicit.split(",").map((s) => s.trim()).filter((s) => s && fs.existsSync(s));
  }
  const dir = String(process.env.SENTINEL_CURL_DAEMON_DIR || "/run/tilian").trim();
  if (!dir) return [];
  try {
    return fs.readdirSync(dir)
      .filter((name) => /^sentinel-curl-\d+\.sock$/.test(name))
      .sort()
      .map((name) => path.join(dir, name));
  } catch (_) {
    return [];
  }
}

let curlDaemonCursor = 0;

function residentCurlFetch(proxy, url, opts, impersonate) {
  const sockets = curlDaemonSockets();
  if (!sockets.length) return null;
  const net = require("net");
  const target = sockets[curlDaemonCursorSafe() % sockets.length];
  return new Promise((resolve, reject) => {
    let settled = false;
    const chunks = [];
    let sock;
    try {
      sock = net.connect(target);
    } catch (e) {
      reject(e);
      return;
    }
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try { sock.destroy(); } catch (_) {}
      reject(new Error("resident curl timeout"));
    }, 130000);
    if (timer.unref) timer.unref();
    sock.on("connect", () => {
      try {
        sock.end(JSON.stringify({
          url: String(url),
          method: opts.method || "GET",
          headers: opts.headers || {},
          body: opts.body == null ? null : String(opts.body),
          proxy,
          impersonate: impersonate || "chrome150",
          timeout: 120,
        }));
      } catch (e) {
        if (!settled) { settled = true; clearTimeout(timer); reject(e); }
      }
    });
    sock.on("data", (d) => chunks.push(d));
    sock.on("error", (e) => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      reject(e);
    });
    sock.on("close", () => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      try {
        const result = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        if (result && result.error) { reject(new Error(String(result.error))); return; }
        const status = Number((result && result.status) || 0);
        const body = String((result && result.body) || "");
        resolve({
          status,
          ok: status >= 200 && status < 300,
          headers: { get: () => null },
          text: async () => body,
          json: async () => JSON.parse(body),
        });
      } catch (e) {
        reject(e);
      }
    });
  });
}

function curlDaemonCursorSafe() {
  curlDaemonCursor = (curlDaemonCursor + 1) % 1000000;
  return curlDaemonCursor;
}

function makePythonCurlFetch(proxy, impersonate) {
  const { spawn } = require("child_process");
  const helper = require("path").join(__dirname, "sentinel_curl_fetch.py");
  const python = resolvePython();
  function spawnCurlFetch(url, opts) {
    opts = opts || {};
    return new Promise((resolve, reject) => {
      const child = spawn(python, [helper], { stdio: ["pipe", "pipe", "pipe"] });
      const stdout = [];
      const stderr = [];
      child.stdout.on("data", (chunk) => stdout.push(chunk));
      child.stderr.on("data", (chunk) => stderr.push(chunk));
      child.on("error", reject);
      child.on("close", (code) => {
        if (code !== 0) {
          reject(new Error(Buffer.concat(stderr).toString("utf8").slice(0, 4000) || `python curl exit ${code}`));
          return;
        }
        try {
          const result = JSON.parse(Buffer.concat(stdout).toString("utf8"));
          const status = Number(result.status || 0);
          const body = String(result.body || "");
          resolve({
            status,
            ok: status >= 200 && status < 300,
            headers: { get: () => null },
            text: async () => body,
            json: async () => JSON.parse(body),
          });
        } catch (error) {
          reject(error);
        }
      });
      child.stdin.end(JSON.stringify({
        url: String(url),
        method: opts.method || "GET",
        headers: opts.headers || {},
        body: opts.body == null ? null : String(opts.body),
        proxy,
        impersonate: impersonate || "chrome150",
        timeout: 120,
      }));
    });
  }

  return function pythonCurlFetch(url, opts) {
    opts = opts || {};
    const viaDaemon = residentCurlFetch(proxy, url, opts, impersonate);
    if (!viaDaemon) return spawnCurlFetch(url, opts);
    return viaDaemon.catch(() => spawnCurlFetch(url, opts));
  };
}

// 只把「读 stdin / 写 stdout」抽出来，mint 逻辑本身一字未动。
// 常驻模式下由 sentinel_daemon.js 传入 raw，返回值即原来的 stdout 内容。
async function handleRequest(raw) {
  let args;
  try {
    args = JSON.parse(raw || "{}");
  } catch (e) {
    return JSON.stringify({ error: "bad args json: " + e.message });
  }

  const ua = args.ua || "";
  // 与 Python 侧 sentinel_profile.cores 同源（此前桥接默认 16、调用方 8，PoW 与
  // sdk 沙箱在 cores 字段上会分裂）。
  const cores = args.cores || 8;
  const deviceId = args.deviceId || "";
  const flow = args.flow || "oauth_create_account";
  const proxy = args.proxy || "";
  const version = args.version || "20260219f9f6";
  const pageUrl = args.pageUrl || "https://auth.openai.com/about-you";
  const language = args.language || "en-US";
  const timezone = args.timezone || "America/Chicago";
  const cookieHeader = args.cookieHeader || "";
  const sentinelOrigin = args.sentinelOrigin || "https://chatgpt.com";
  const impersonate = String(args.impersonate || "").trim() || "chrome150";
  const secChUaRaw = String(args.secChUa || "").trim();
  const uaChromeMatch = String(ua || "").match(/(?:Chrome|CriOS)\/(\d+(?:\.\d+)*)/);
  const platformLabel = args.platformLabel || "Windows";
  // 调用方省略 secChUa 但 UA 为 Chromium/Chrome 时，用同一 Chromium GREASE 规则兜底，
  // 确保 /sentinel/req 请求头与沙箱 navigator.userAgentData.brands 永不分裂。
  const secChUa = secChUaRaw || (
    uaChromeMatch
      ? chromiumBrandEntries(uaChromeMatch[1].split(".")[0])
          .map((e) => `"${e.brand}";v="${e.version}"`)
          .join(", ")
      : ""
  );
  const rawPlat = String(args.secChUaPlatform || "").trim().replace(/^"+|"+$/g, "");
  const secChUaPlatform = rawPlat
    ? `"${rawPlat}"`
    : (secChUa && platformLabel ? `"${platformLabel}"` : "");
  // 高熵 Client Hints：参考 HAR 上 sec-ch-ua-arch/bitness/platform-version 与主站请求同源。
  // 调用方没给时按平台给默认值，避免 Windows 档案发出 macOS 的 arm/27.0.1。
  const defaultHints = platformLabel === "macOS"
    ? { arch: '"arm"', bitness: '"64"', platformVersion: '"14.6.1"' }
    : platformLabel === "Linux"
      ? { arch: '"x86"', bitness: '"64"', platformVersion: '""' }
      : { arch: '"x86"', bitness: '"64"', platformVersion: '"10.0.0"' };
  const clientHints = {
    "sec-ch-ua-arch": String(args.secChUaArch || "").trim() || defaultHints.arch,
    "sec-ch-ua-bitness": String(args.secChUaBitness || "").trim() || defaultHints.bitness,
    "sec-ch-ua-platform-version":
      String(args.secChUaPlatformVersion || "").trim() || defaultHints.platformVersion,
  };
  // 指纹档案驱动（与请求 UA/平台/屏幕一致，避免哨兵身份与请求身份打架）
  const platform = args.platform || "Win32";
  const screenW = Number(args.screenW) || 1920;
  const screenH = Number(args.screenH) || 1080;
  const availH = Number(args.availH) || (screenH - 48);
  const maxTouch = args.maxTouchPoints ? 1 : 0;
  const frameUrl = `${sentinelOrigin}/backend-api/sentinel/frame.html?sv=${version}`;

  // fetch + ProxyAgent 必须同源（同一 undici 版本）。优先用 npm undici；
  // 无代理且 undici 缺失时退回 Node 内置全局 fetch。
  let undici = null;
  try {
    undici = require("undici");
  } catch (_) {}

  // 默认所有代理（含 HTTP/HTTPS 与 SOCKS）均走项目 curl_cffi BoringSSL Chrome TLS，
  // 保证 /backend-api/sentinel/req 与 /api/auth/session、/payments/checkout 的 TLS 指纹一致，
  // 避免同一出口 IP 下出现 Node TLS vs Chrome TLS 分裂。
  // 仅当显式设置 SENTINEL_HTTP_TRANSPORT=undici 时退回 Node undici ProxyAgent。
  const forceUndici = String(process.env.SENTINEL_HTTP_TRANSPORT || "").trim().toLowerCase() === "undici";
  const curlHelperExists = fs.existsSync(path.join(__dirname, "sentinel_curl_fetch.py"));
  let dispatcher = null;
  let fetchImpl = undici && undici.fetch ? undici.fetch : fetch;
  if (proxy && forceUndici && undici && /^https?:/i.test(proxy)) {
    try {
      const pu = new URL(proxy);
      const opts = { uri: pu.origin }; // http://host:port（去掉 userinfo）
      if (pu.username || pu.password) {
        const auth = decodeURIComponent(pu.username) + ":" + decodeURIComponent(pu.password);
        opts.token = "Basic " + Buffer.from(auth, "utf8").toString("base64");
      }
      dispatcher = new undici.ProxyAgent(opts);
    } catch (e) {
      return JSON.stringify({ error: "ProxyAgent 构造失败：" + ((e && e.message) || e) });
    }
  } else if (!forceUndici && (proxy || curlHelperExists)) {
    fetchImpl = makePythonCurlFetch(proxy, impersonate);
  }

  const assetsDir = __dirname;
  const bootstrapSrc = fs.readFileSync(path.join(assetsDir, "sentinel_bootstrap.js"), "utf8");
  const sdkSrc = fs.readFileSync(path.join(assetsDir, "sentinel_sdk.js"), "utf8");

  // Node 的 crypto/navigator/performance/self 是只读全局，会和 bootstrap 的赋值冲突。
  // 用独立 vm 沙箱（全新 global），只注入 sdk 实际需要的 host 能力。
  const { performance: hostPerformance } = require("perf_hooks");
  const capture = { reqBody: "" };
  const sdkUrl = `${sentinelOrigin}/sentinel/${version}/sdk.js`;
  const dataBuild = String(args.dataBuild || "").trim();
  const sendHighEntropyHints = Boolean(args.sendHighEntropyHints);
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    queueMicrotask,
    __HOST_NOW__: () => hostPerformance.now(),
    fetch: makeFetch(
      ua,
      language,
      cookieHeader,
      dispatcher,
      fetchImpl,
      sentinelOrigin,
      frameUrl,
      secChUa,
      secChUaPlatform,
      capture,
      clientHints,
      sendHighEntropyHints
    ),
    TextEncoder,
    TextDecoder,
    btoa: (s) => Buffer.from(String(s), "latin1").toString("base64"),
    atob: (s) => Buffer.from(String(s), "base64").toString("latin1"),
    crypto: globalThis.crypto, // webcrypto：subtle + getRandomValues + randomUUID
    URL,
    URLSearchParams,
    __UA__: ua,
    __CORES__: cores,
    __FLOW__: flow,
    __SDK_URL__: sdkUrl,
    __DATA_BUILD__: dataBuild,
    __SEED_DID_KEY__: "oai-did",
    __SEED_DID_VAL__: deviceId,
    __PAGE_URL__: pageUrl,
    __LANGUAGE__: language,
    __LANGUAGES__: languageList(language),
    __TIMEZONE__: timezone,
    __COOKIE_HEADER__: cookieHeader,
    __PLATFORM__: platform,
    __PLATFORM_LABEL__: platformLabel,
    __SCREEN_W__: screenW,
    __SCREEN_H__: screenH,
    __AVAIL_H__: availH,
    __MAX_TOUCH__: maxTouch,
    // UA-CH 单一来源：JS 侧 navigator.userAgentData.brands / getHighEntropyValues
    // 必须与本次请求发出去的 sec-ch-ua* 头逐字一致（含 GREASE 品牌），否则
    // 同一份会话会同时呈现两套 Client Hints。
    __SEC_CH_UA__: secChUa,
    __SEC_CH_UA_FULL_LIST__: secChUa
      ? highEntropyClientHints(ua, clientHints, secChUa)["sec-ch-ua-full-version-list"]
      : "",
    __CH_ARCH__: String(clientHints["sec-ch-ua-arch"] || "").replace(/"/g, ""),
    __CH_BITNESS__: String(clientHints["sec-ch-ua-bitness"] || "").replace(/"/g, ""),
    __CH_PLATFORM_VERSION__: String(
      clientHints["sec-ch-ua-platform-version"] || ""
    ).replace(/"/g, ""),
  };
  vm.createContext(sandbox);

  try {
    vm.runInContext(bootstrapSrc, sandbox, { filename: "sentinel_bootstrap.js" });
  } catch (e) {
    return JSON.stringify({ error: "bootstrap: " + ((e && e.stack) || e) });
  }
  try {
    // 注入键必须不可枚举：sdk 的指纹数组会随机取 `Object.keys(window)` 的一个键上报，
    // 可枚举的话会把自己桥接层用的 __SENTINEL_SDK__ 暴露成「window 的某个键」。
    vm.runInContext(
      sdkSrc +
        "\n;Object.defineProperty(globalThis,'__SENTINEL_SDK__'," +
        "{value:SentinelSDK,enumerable:false,writable:true,configurable:true});",
      sandbox,
      { filename: "sentinel_sdk.js" }
    );
  } catch (e) {
    return JSON.stringify({ error: "sdk: " + ((e && e.stack) || e) });
  }

  const SDK = sandbox.__SENTINEL_SDK__;
  if (!SDK || typeof SDK.__proto2 !== "function") {
    return JSON.stringify({ error: "sdk.__proto2 未定义" });
  }

  let rawDriver;
  try {
    const p = SDK.__proto2(flow);
    rawDriver = await Promise.resolve(p);
  } catch (e) {
    return JSON.stringify({ error: "proto2: " + (e && e.stack || e) });
  }

  let rd;
  try {
    rd = JSON.parse(String(rawDriver));
  } catch (e) {
    return JSON.stringify({ error: "driver decode: " + e.message, raw: String(rawDriver).slice(0, 300) });
  }

  if (!rd.c) {
    return JSON.stringify({ error: "driver 无 c", soErr: rd.soErr || "", raw: String(rawDriver).slice(0, 300) });
  }

  const g = makeGenerator(ua, cores, language, timezone, screenW, screenH, {
    // bootstrap 在沙箱里生成的会话 sid / 启动基准 / 堆上限，PoW 直接复用。
    sid: sandbox.__SENTINEL_SID__,
    loadTs: sandbox.__LOAD_TS__,
    initialPerformance: sandbox.__SENTINEL_PERF__,
    heapLimit: sandbox.__SENTINEL_HEAP_LIMIT__,
    sdkUrl: sandbox.__SDK_URL__,
    dataBuild,
    pageUrl,
  });
  // sdk.js 自己发到 /sentinel/req 的指纹数组是同一 flow 的第一份自述；
  // 能解出来就直接复用，保证 p 与它逐字段一致。
  const captured = decodeReqConfig(capture.reqBody);
  let p;
  if (rd.powReq && rd.seed) {
    p = solvePoW(g, rd.seed, rd.diff, captured);
  } else {
    const data0 = powBaseConfig(g, captured);
    data0[3] = 1;
    p = "gAAAAAB" + b64Compact(data0) + "~S";
  }

  const mainTok = JSON.stringify({ p, t: rd.t || "", c: rd.c, id: deviceId, flow });
  let so = "";
  if (rd.so) {
    so = JSON.stringify({ so: rd.so, c: rd.c, id: deviceId, flow });
  }

  const out = JSON.stringify({
    main: mainTok,
    so,
    soErr: rd.soErr || "",
    powReq: !!rd.powReq,
    hasT: !!rd.t,
    hasSo: !!rd.so,
  });

  // token 已写完，优雅关闭 undici 连接后自然退出，避开 Windows libuv 退出断言
  // （该断言发生在 stdout 写出之后，不影响结果；但会让子进程 exit code 变脏）。
  // 只关闭本次请求构造的 ProxyAgent。常驻模式下不能碰全局 dispatcher，
  // 否则会连带废掉后续请求要复用的连接池。
  if (dispatcher && typeof dispatcher.close === "function") {
    try {
      await Promise.resolve(dispatcher.close()).catch(() => {});
    } catch (_) {}
  }
  return out;
}

/* ═══════════ 运行模式 ═══════════
 * 一次性模式（默认）：从 stdin 读一个请求、写到 stdout、退出 —— 与历史行为完全一致。
 * 常驻模式（SENTINEL_DAEMON_SOCKET=/path/to.sock）：进程复用，省掉每次 mint 冷启动
 *   一个 62MB Node 进程的开销。
 *
 * 隔离性保证：每次请求都会重新 makeGenerator()（随机 initialPerformance / sid /
 *   startedAt 全部重新生成）并 vm.createContext() 建全新沙箱，
 *   因此指纹状态与「全新进程」逐字段等价，不会产生跨账号相关性。
 */
async function runOnce() {
  const raw = await readStdin();
  const out = await handleRequest(raw);
  await new Promise((r) => process.stdout.write(out, r));
  try {
    const u = require("undici");
    await Promise.resolve(u.getGlobalDispatcher().close()).catch(() => {});
  } catch (_) {}
  // 兜底：若仍有 ref 句柄卡住事件循环，2s 后硬退出（unref 不阻塞正常退出）。
  setTimeout(() => process.exit(0), 2000).unref();
}

const daemonSocket = String(process.env.SENTINEL_DAEMON_SOCKET || "").trim();
if (daemonSocket) {
  require("./sentinel_daemon.js").serve(handleRequest, daemonSocket);
} else {
  runOnce().catch((e) => {
    process.stdout.write(JSON.stringify({ error: "fatal: " + ((e && e.stack) || e) }), () => process.exit(1));
  });
}
