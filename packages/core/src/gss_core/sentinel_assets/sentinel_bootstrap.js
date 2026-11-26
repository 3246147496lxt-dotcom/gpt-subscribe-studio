// sentinel_bootstrap.js — 在加载真·sdk.js 之前铺好浏览器环境 shim。
// 只提供 sdk.js 实际会读到的对象/方法，值对齐 Edge 150 / zh-CN。
(function () {
  "use strict";
  var g = globalThis;

  // ---- 可枚举面卫生 helper ----
  // sdk 的指纹数组第 11/12 位是 `T(Object.keys(document))` / `T(Object.keys(window))`
  // 的随机命中结果（T = 从数组里随机取一个）。凡是真浏览器上没有的键，只要可枚举，
  // 就有机会被当成「document / window 的某个键」上报给上游 —— 这是最直接的脚本指纹。
  // 因此：
  //   1) 桥接注入的构建参数（__UA__/__FLOW__/…）和本文件自造的运行时状态
  //      （__SENTINEL_*/__LOAD_TS__ 等）全部做成不可枚举；
  //   2) 模拟的 DOM 接口对象（document/navigator/screen/performance）按真 Chrome
  //      的形状收敛成「自有属性为空、方法挂在原型上」，只保留 WebIDL 上真正
  //      自有且可枚举的键（document.location 是唯一一个）。
  // 注意 __oai_so_* 必须保持可枚举：真页面的 collector 就是普通赋值写进去的，
  // 真 Chrome 的 window 上它们同样可枚举。
  function concealOwn(obj, key) {
    if (!obj) return;
    try {
      var desc = Object.getOwnPropertyDescriptor(obj, key);
      if (!desc || desc.enumerable === false) return;
      desc.enumerable = false;
      Object.defineProperty(obj, key, desc);
    } catch (e) {}
  }
  function concealAllOwn(obj) {
    if (!obj) return;
    var keys;
    try { keys = Object.keys(obj); } catch (e) { return; }
    for (var i = 0; i < keys.length; i++) concealOwn(obj, keys[i]);
  }

  // 桥接层用 __XXX__ 命名的构建参数：先记下值，再改成不可枚举。
  var _BRIDGE_INPUTS = [
    "__UA__", "__CORES__", "__FLOW__", "__SENTINEL_FLOW__", "__SDK_URL__",
    "__SEED_DID_KEY__", "__SEED_DID_VAL__", "__PAGE_URL__", "__LANGUAGE__",
    "__LANGUAGES__", "__TIMEZONE__", "__COOKIE_HEADER__", "__PLATFORM__",
    "__PLATFORM_LABEL__", "__SCREEN_W__", "__SCREEN_H__", "__AVAIL_H__", "__MAX_TOUCH__"
  ];
  for (var _bi = 0; _bi < _BRIDGE_INPUTS.length; _bi++) concealOwn(g, _BRIDGE_INPUTS[_bi]);

  // sdk 里的“自我防篡改”陷阱会对函数源码跑灾难性回溯正则 (((.+)+)+)+$。
  // 真浏览器 V8 上瞬间命中；goja/regexp2 会指数级卡死。这里对该固定模式短路，
  // 返回与真浏览器一致的结果（非空串必命中→返回 0），不影响 token 计算。
  var _search = String.prototype.search;
  String.prototype.search = function (re) {
    try {
      var s = (re && re.source) ? re.source : String(re);
      if (s.indexOf("(((.+)+)+)+") !== -1) return this.length ? 0 : -1;
    } catch (e) {}
    return _search.call(this, re);
  };

  // goja 抛 TypeError 用旧版措辞 "Cannot read property 'x' of undefined/null"，
  // V8/Chrome 用 "Cannot read properties of undefined/null (reading 'x')"。
  // collector 会把访问 undefined 属性时的错误串作为指纹熵记录（如 clientBootstrap），
  // 措辞不一致会暴露非 V8 引擎。覆写 Error.toString 把 goja 文案改写成 V8 文案。
  var _errToString = Error.prototype.toString;
  Error.prototype.toString = function () {
    var s = _errToString.call(this);
    try {
      s = s.replace(/Cannot read property '([^']*)' of undefined/g, "Cannot read properties of undefined (reading '$1')")
           .replace(/Cannot read property '([^']*)' of null/g, "Cannot read properties of null (reading '$1')");
    } catch (e) {}
    return s;
  };

  // sdk 里有基于 setInterval 的反调试轮询（每隔几秒检测 debugger）。
  // 在 eventloop 下这个循环定时器会让事件循环永不清空 → loop 卡死。
  // 一次性 token 计算不需要它，直接置空；setTimeout 保留（dx VM 会用）。
  g.setInterval = function () { return 0; };
  g.clearInterval = function () {};

  // crypto.getRandomValues（uuid v4 生成 sentinel deviceId 时需要）
  g.crypto = g.crypto || {};
  if (typeof g.crypto.getRandomValues !== "function") {
    g.crypto.getRandomValues = function (arr) {
      for (var i = 0; i < arr.length; i++) arr[i] = (Math.random() * 256) | 0;
      return arr;
    };
  }
  if (typeof g.crypto.randomUUID !== "function") {
    g.crypto.randomUUID = function () {
      var b = new Uint8Array(16);
      g.crypto.getRandomValues(b);
      b[6] = (b[6] & 0x0f) | 0x40;
      b[8] = (b[8] & 0x3f) | 0x80;
      var h = [];
      for (var i = 0; i < 16; i++) h.push((b[i] + 0x100).toString(16).slice(1));
      return h[0] + h[1] + h[2] + h[3] + "-" + h[4] + h[5] + "-" + h[6] + h[7] +
        "-" + h[8] + h[9] + "-" + h[10] + h[11] + h[12] + h[13] + h[14] + h[15];
    };
  }

  // window / self 自引用
  g.window = g;
  g.self = g;
  g.top = g;
  g.parent = g;

  // ---- Date 覆写：输出与当前出口国家一致的时区 ----
  // 真 Chrome 的 Date.prototype.toString() 里括号内是「本地化长时区名」
  // （如 Philippine Standard Time / Eastern Standard Time），不是 IANA 标识
  // （Asia/Manila）。输出 IANA 标识会与服务端记录的真实浏览器指纹直接矛盾。
  var _tzDisplayCache = "";
  function tzDisplayName(timezone) {
    if (_tzDisplayCache) return _tzDisplayCache;
    var name = "";
    try {
      var parts = new Intl.DateTimeFormat("en-US", {
        timeZone: timezone,
        timeZoneName: "long"
      }).formatToParts(new Date());
      for (var i = 0; i < parts.length; i++) {
        if (parts[i].type === "timeZoneName") { name = parts[i].value; break; }
      }
    } catch (e) {}
    // 极小 ICU 构建拿不到长名时退回 IANA 标识，保证不抛错。
    if (!name) name = timezone;
    _tzDisplayCache = name;
    return name;
  }
  // 暴露给 sentinel_bridge.js 的 PoW 生成器，两段必须写出同一个时区名。
  g.__TZ_DISPLAY_NAME__ = tzDisplayName(g.__TIMEZONE__ || "America/Chicago");

  var _DateToString = Date.prototype.toString;
  Date.prototype.toString = function () {
    try {
      var timezone = g.__TIMEZONE__ || "America/Chicago";
      var source = new Date(this.getTime());
      var parts = new Intl.DateTimeFormat("en-US", {
        timeZone: timezone,
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
        hourCycle: "h23"
      }).formatToParts(source).reduce(function (out, part) {
        if (part.type !== "literal") out[part.type] = Number(part.value);
        return out;
      }, {});
      var localAsUtc = Date.UTC(
        parts.year, parts.month - 1, parts.day,
        parts.hour, parts.minute, parts.second
      );
      var offsetMinutes = Math.round((localAsUtc - source.getTime()) / 60000);
      var d = new Date(source.getTime() + offsetMinutes * 60000);
      var wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()];
      var mo = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()];
      function p(n) { return (n < 10 ? "0" : "") + n; }
      var sign = offsetMinutes >= 0 ? "+" : "-";
      var offset = Math.abs(offsetMinutes);
      return wd + " " + mo + " " + p(d.getUTCDate()) + " " + d.getUTCFullYear() +
        " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds()) +
        " GMT" + sign + p(Math.floor(offset / 60)) + p(offset % 60) + " (" + tzDisplayName(timezone) + ")";
    } catch (e) {
      return _DateToString.call(this);
    }
  };

  // ---- 构造原生方法：toString() 返回 "function name() { [native code] }" ----
  function nativeFn(name) {
    var f = function () {};
    try { Object.defineProperty(f, "name", { value: name, configurable: true }); } catch (e) {}
    f.toString = function () { return "function " + name + "() { [native code] }"; };
    return f;
  }

  // Navigator 原型：一批 Chromium/Edge 150 上真实存在的方法 + 若干属性 getter。
  var navProtoMethods = [
    "clearOriginJoinedAdInterestGroups", "joinAdInterestGroup", "leaveAdInterestGroup",
    "runAdAuction", "createAuctionNonce", "deprecatedReplaceInURN", "deprecatedURNToURL",
    "updateAdInterestGroups", "canShare", "share", "requestMIDIAccess",
    "requestMediaKeySystemAccess", "getGamepads", "getBattery", "sendBeacon", "vibrate",
    "registerProtocolHandler", "unregisterProtocolHandler", "setAppBadge", "clearAppBadge",
    "getInstalledRelatedApps", "javaEnabled", "taintEnabled", "getUserMedia",
    "webkitGetUserMedia"
  ];
  var NavigatorProto = {};
  navProtoMethods.forEach(function (m) { NavigatorProto[m] = nativeFn(m); });
  // 少量属性(有些 Tt() 随机命中时返回其值的字符串形态)
  NavigatorProto.cookieEnabled = true;
  NavigatorProto.onLine = true;
  NavigatorProto.pdfViewerEnabled = true;
  NavigatorProto.webdriver = false;

  var navigator = Object.create(NavigatorProto);
  navigator.userAgent = g.__UA__;
  navigator.appVersion = g.__UA__.replace("Mozilla/", "");
  navigator.appName = "Netscape";
  navigator.appCodeName = "Mozilla";
  navigator.platform = (g.__PLATFORM__ || "Win32");
  navigator.product = "Gecko";
  navigator.productSub = "20030107";
  navigator.vendor = "Google Inc.";
  navigator.vendorSub = "";
  navigator.language = g.__LANGUAGE__ || "en-US";
  navigator.languages = g.__LANGUAGES__ || [navigator.language, "en-US", "en"];
  navigator.hardwareConcurrency = g.__CORES__;
  navigator.deviceMemory = 8;
  navigator.maxTouchPoints = 0;
  navigator.doNotTrack = null;
  // 常见 WebAPI 子对象 (collector_dx 会探测)
  var noop = function () {};
  var noopPromise = function () { return Promise.resolve(); };
  navigator.connection = { effectiveType: "4g", rtt: 50, downlink: 10, saveData: false, type: "wifi" };
  navigator.plugins = { length: 5, item: noop, namedItem: noop, refresh: noop };
  navigator.mimeTypes = { length: 4, item: noop, namedItem: noop };
  navigator.permissions = { query: function () { return Promise.resolve({ state: "prompt" }); } };
  navigator.mediaDevices = { enumerateDevices: function () { return Promise.resolve([]); }, getUserMedia: noopPromise };
  navigator.credentials = { get: noopPromise, store: noopPromise, create: noopPromise, preventSilentAccess: noopPromise };
  navigator.serviceWorker = { controller: null, ready: Promise.resolve({ active: null }), register: noopPromise, getRegistrations: function () { return Promise.resolve([]); } };
  navigator.storage = { estimate: function () { return Promise.resolve({ quota: 2e11, usage: 5e7 }); }, getDirectory: noopPromise, persisted: function () { return Promise.resolve(false); }, persist: function () { return Promise.resolve(false); } };
  navigator.clipboard = { read: noopPromise, readText: noopPromise, write: noopPromise, writeText: noopPromise };
  navigator.locks = { request: noopPromise, query: function () { return Promise.resolve({ held: [], pending: [] }); } };
  navigator.mediaCapabilities = { decodingInfo: function () { return Promise.resolve({ supported: true, smooth: true, powerEfficient: true }); } };
  (function () {
    var m = String(g.__UA__ || "").match(/Chrome\/(\d+(?:\.\d+)*)/);
    var fullVer = m ? m[1] : "150.0.7871.189";
    var ver = fullVer.split(".")[0] || "150";
    var plat = (g.__PLATFORM_LABEL__ || "Windows");
    navigator.userAgentData = { brands: [{ brand: "Google Chrome", version: ver }, { brand: "Chromium", version: ver }, { brand: "Not(A:Brand", version: "24" }], mobile: false, platform: plat, getHighEntropyValues: function () { return Promise.resolve({ platform: plat, platformVersion: (plat === "Windows" ? "10.0.0" : (plat === "macOS" ? "14.6.1" : "")), fullVersionList: [{ brand: "Google Chrome", version: fullVer }, { brand: "Chromium", version: fullVer }, { brand: "Not(A:Brand", version: "24.0.0.0" }] }); } };
  })();
  navigator.scheduling = { isInputPending: function () { return false; } };
  navigator.ink = { requestPresenter: noopPromise };
  navigator.gpu = { requestAdapter: noopPromise };
  navigator.hid = { getDevices: function () { return Promise.resolve([]); }, requestDevice: noopPromise };
  navigator.serial = { getPorts: function () { return Promise.resolve([]); }, requestPort: noopPromise };
  navigator.usb = { getDevices: function () { return Promise.resolve([]); }, requestDevice: noopPromise };
  navigator.bluetooth = { getAvailability: function () { return Promise.resolve(false); }, requestDevice: noopPromise };
  navigator.xr = { isSessionSupported: function () { return Promise.resolve(false); }, requestSession: noopPromise };
  navigator.wakeLock = { request: noopPromise };
  navigator.geolocation = { getCurrentPosition: noop, watchPosition: noop, clearWatch: noop };
  navigator.mediaSession = { metadata: null, playbackState: "none", setActionHandler: noop };
  g.navigator = navigator;

  // ---- screen ----
  var _sw = g.__SCREEN_W__ || 1920;
  var _sh = g.__SCREEN_H__ || 1080;
  var _ah = g.__AVAIL_H__ || (_sh - 48);
  g.screen = {
    width: _sw, height: _sh, availWidth: _sw, availHeight: _ah,
    colorDepth: 24, pixelDepth: 24, availLeft: 0, availTop: 0
  };
  g.innerWidth = _sw; g.innerHeight = _ah;
  g.outerWidth = _sw; g.outerHeight = _ah;
  g.devicePixelRatio = 1;
  g.navigator.maxTouchPoints = (g.__MAX_TOUCH__ || 0);

  // ---- history / location ----
  // collector 会记录当前页 URL 作为指纹熵。真实注册流在 /about-you 页触发 create_account
  // 的 sentinel；Go 侧可用 __PAGE_URL__ 按具体 flow 覆写，默认对齐 about-you。
  g.history = { length: 2, scrollRestoration: "auto", state: null };
  var _pageUrl = g.__PAGE_URL__ || "https://auth.openai.com/about-you";
  var _parsedPageUrl = new URL(_pageUrl);
  g.location = {
    href: _pageUrl, origin: _parsedPageUrl.origin,
    protocol: _parsedPageUrl.protocol, host: _parsedPageUrl.host, hostname: _parsedPageUrl.hostname,
    port: _parsedPageUrl.port, pathname: _parsedPageUrl.pathname,
    search: _parsedPageUrl.search, hash: _parsedPageUrl.hash,
    assign: function () {}, replace: function () {}, reload: function () {}, toString: function () { return _pageUrl; }
  };

  // ---- performance ----
  // collector 会读导航性能条目的 name（= 当前页 URL）作为指纹熵，真实页在此产出
  // "https://auth.openai.com/about-you"。缺 getEntriesByType 时该测点为空 → 少一项。
  //
  // 时基：真页面的 sentinel 是在「用户已经停留并交互了几秒」之后才被采集的
  //   · chatgpt_checkout：打开定价弹窗 → 浏览套餐 → 点「升级」，约 6.5s
  //   · oauth_create_account：填姓名 + 生日 → 提交，约 8s
  // 旧实现把 timeOrigin 钉在沙箱启动瞬间，`performance.now()` 从 0 开始，
  // 于是 p[13] 只有个位数毫秒（=页面刚打开 4ms 就提交建单），且按
  // `t0 = now - dwell` 反推的行为起点全部变成负数 —— 这是最硬的脚本指纹。
  // 现在时基由 flow 的停留时长反推：采集点 ≈ dwell + (180~900)ms。
  var _flowName = String((typeof g.__FLOW__ !== "undefined" && g.__FLOW__) ||
    (typeof g.__SENTINEL_FLOW__ !== "undefined" && g.__SENTINEL_FLOW__) || "oauth_create_account");
  var _isCheckoutFlow = /checkout|payment|approval|billing/i.test(_flowName);
  // 每次会话带自然随机抖动（不把 dwell 写死成同一个常量，避免跨订单聚类）
  var _dwell = _isCheckoutFlow
    ? Math.round((5200 + Math.random() * 2800) * 10) / 10
    : Math.round((6800 + Math.random() * 2800) * 10) / 10;
  var _t0 = Date.now();
  // 采集点 = 进页 + dwell；再留 180~900ms 给「点完按钮到 SDK 真正发出请求」。
  // 取整：timeOrigin / __LOAD_TS__ 是 epoch 毫秒，亚毫秒抖动只出现在 now() 上。
  var _perfBase = (typeof g.__SENTINEL_PERF__ === "number" && g.__SENTINEL_PERF__ > _dwell + 40)
    ? g.__SENTINEL_PERF__
    : Math.round(_dwell + 180 + Math.random() * 720);
  var _perfTick = 0;
  var _lastPerfNow = _perfBase;
  var _navEntry = {
    name: _pageUrl, entryType: "navigation", startTime: 0, duration: 420.5, type: "navigate",
    initiatorType: "navigation", nextHopProtocol: "h2", redirectCount: 0,
    domComplete: 410.2, domContentLoadedEventEnd: 300.1, domInteractive: 280.4,
    loadEventEnd: 418.9, responseEnd: 190.3, responseStart: 150.7, requestStart: 60.2,
    fetchStart: 5.1, connectEnd: 40.8, connectStart: 20.3, domainLookupEnd: 15.2,
    domainLookupStart: 10.1, secureConnectionStart: 25.6, transferSize: 18240, encodedBodySize: 17010, decodedBodySize: 61200
  };
  g.performance = {
    // timeOrigin = 导航起点（wall clock）：与 now() 严格互补，Date.now()-timeOrigin ≈ now()
    timeOrigin: _t0 - _perfBase,
    // 严格单调递增、亚毫秒分辨率（即使单毫秒内高频调用超过 1000 次也不会回绕倒退）
    now: function () {
      _perfTick += 1;
      var candidate = _perfBase + (Date.now() - _t0) + (_perfTick % 1000) / 1000;
      if (candidate <= _lastPerfNow) candidate = _lastPerfNow + 0.001;
      _lastPerfNow = candidate;
      return candidate;
    },
    memory: { jsHeapSizeLimit: 4294705152, totalJSHeapSize: 35000000, usedJSHeapSize: 22000000 },
    getEntries: function () { return [_navEntry]; },
    getEntriesByType: function (t) { return t === "navigation" ? [_navEntry] : []; },
    getEntriesByName: function (n) { return _navEntry.name === n ? [_navEntry] : []; },
    mark: function () {}, measure: function () {}, clearMarks: function () {}, clearMeasures: function () {},
    clearResourceTimings: function () {}, setResourceTimingBufferSize: function () {}
  };
  concealAllOwn(g.performance);

  // ---- 与 sentinel_bridge.js 的 PoW 生成器共享同一份会话标识与启动基准 ----
  // 若 sdk.js 与 PoW 各自新建 sid / loadTs，服务端会同时看到两条互相矛盾
  // 的指纹（同一 flow 两次上报不同设备），这正是 unusual_activity 的触发点。
  if (!g.__SENTINEL_SID__) {
    g.__SENTINEL_SID__ = (g.crypto && typeof g.crypto.randomUUID === "function")
      ? g.crypto.randomUUID()
      : "00000000-0000-4000-8000-000000000000";
  }
  if (typeof g.__SENTINEL_PERF__ !== "number" || g.__SENTINEL_PERF__ < _dwell + 40) {
    g.__SENTINEL_PERF__ = _perfBase;
  }
  if (typeof g.__LOAD_TS__ !== "number") {
    g.__LOAD_TS__ = _t0 - _perfBase;
  }
  g.__SENTINEL_HEAP_LIMIT__ = 4294705152;
  // 运行时状态同样不可枚举：真 Chrome 的 window 上没有这些键。
  concealOwn(g, "__SENTINEL_SID__");
  concealOwn(g, "__SENTINEL_PERF__");
  concealOwn(g, "__LOAD_TS__");
  concealOwn(g, "__SENTINEL_HEAP_LIMIT__");
  concealOwn(g, "__TZ_DISPLAY_NAME__");

  // ---- localStorage ----
  // collector_dx 会枚举 Object.keys(localStorage) 作为指纹熵。真实 auth 页在
  // statsig 客户端 SDK 初始化后会写入一批 statsig.* 键；goja 环境无该 SDK，
  // 需按真实结构预置这些键，且方法要不可枚举，否则 keys 会枚举出方法名。
  (function () {
    function digits(n) { var s = ""; for (var i = 0; i < n; i++) s += (Math.random() * 10) | 0; if (s[0] === "0") s = "1" + s.slice(1); return s; }
    function hex(n) { var h = "0123456789abcdef", s = ""; for (var i = 0; i < n; i++) s += h[(Math.random() * 16) | 0]; return s; }
    var stable = digits(9);
    var ls = {};
    var seed = [
      "statsig.cached.evaluations." + digits(8),
      "statsig.cached.evaluations." + digits(9),
      "statsig.cached.evaluations." + digits(10),
      "statsig.last_modified_time.evaluations",
      hex(16),
      "statsig.cached.evaluations." + digits(10),
      "statsig.session_id." + stable,
      "statsig.cached.evaluations." + digits(10),
      "statsig.cached.evaluations." + digits(10),
      "statsig.cached.evaluations." + digits(10),
      "statsig.stable_id." + stable
    ];
    seed.forEach(function (k) { ls[k] = "1"; });
    if (g.__SEED_DID_KEY__ && g.__SEED_DID_VAL__) ls[g.__SEED_DID_KEY__] = g.__SEED_DID_VAL__;
    function def(name, fn) { Object.defineProperty(ls, name, { value: fn, enumerable: false, writable: true, configurable: true }); }
    def("getItem", function (k) { return Object.prototype.hasOwnProperty.call(ls, k) ? ls[k] : null; });
    def("setItem", function (k, v) { ls[k] = "" + v; });
    def("removeItem", function (k) { delete ls[k]; });
    def("clear", function () { Object.keys(ls).forEach(function (k) { delete ls[k]; }); });
    def("key", function (i) { return Object.keys(ls)[i] || null; });
    Object.defineProperty(ls, "length", { get: function () { return Object.keys(ls).length; }, enumerable: false, configurable: true });
    g.localStorage = ls;
  })();

  // ---- document（sdk 读 scripts / documentElement / cookie / keys / body 文本量测指纹）----
  var sdkScript = { src: g.__SDK_URL__, getAttribute: function () { return null; }, type: "text/javascript" };
  var docElement = { getAttribute: function () { return null; }, tagName: "HTML", lang: g.__LANGUAGE__ || "en-US" };
  var _cookie = g.__COOKIE_HEADER__ || ((g.__SEED_DID_KEY__ && g.__SEED_DID_VAL__) ? (g.__SEED_DID_KEY__ + "=" + g.__SEED_DID_VAL__) : "");

  // 可写 style 壳：collector 的文本指纹会 Reflect.set(style, "fontFamily"/"fontSize"/... )
  function makeStyle() {
    var st = {};
    st.setProperty = function (k, v) { st[k] = "" + v; };
    st.getPropertyValue = function (k) { return Object.prototype.hasOwnProperty.call(st, k) ? st[k] : ""; };
    st.removeProperty = function (k) { var v = st[k]; delete st[k]; return v; };
    st.cssText = "";
    return st;
  }

  // DOM 元素壳：createElement("div") 后设 style/innerText，appendChild 到 body，
  // 再 getBoundingClientRect()（结果会被 JSON.stringify），最后 removeChild。
  function makeElement(tag) {
    var el = {
      tagName: ("" + tag).toUpperCase(),
      nodeName: ("" + tag).toUpperCase(),
      nodeType: 1,
      style: makeStyle(),
      innerText: "", textContent: "", innerHTML: "",
      className: "", id: "", ariaHidden: null, hidden: false,
      clientWidth: 0, clientHeight: 0, offsetWidth: 0, offsetHeight: 0,
      scrollWidth: 0, scrollHeight: 0, offsetLeft: 0, offsetTop: 0,
      children: [], childNodes: [], parentNode: null,
      setAttribute: function () {}, getAttribute: function () { return null; },
      removeAttribute: function () {}, hasAttribute: function () { return false; },
      addEventListener: function () {}, removeEventListener: function () {},
      appendChild: function (c) { this.children.push(c); this.childNodes.push(c); if (c) c.parentNode = this; return c; },
      removeChild: function (c) {
        var i = this.children.indexOf(c);
        if (i >= 0) { this.children.splice(i, 1); this.childNodes.splice(i, 1); }
        if (c) c.parentNode = null;
        return c;
      },
      insertBefore: function (c) { this.children.push(c); this.childNodes.push(c); return c; },
      remove: function () {},
      querySelector: function () { return null; },
      querySelectorAll: function () { return []; },
      getClientRects: function () { return [this.getBoundingClientRect()]; },
      getBoundingClientRect: function () {
        // 无真实排版：按文本长度给出稳定的亚像素宽度（Impact 15px 近似），
        // 结果仅作为指纹熵被服务端记录。
        var t = "" + (this.innerText || "");
        var w = t.length ? (t.length * 7.34 + 0.421875) : 0;
        var h = t.length ? 17.5 : 0;
        return { x: 8, y: 8, width: w, height: h, top: 8, right: 8 + w, bottom: 8 + h, left: 8 };
      }
    };
    return el;
  }

  var bodyEl = makeElement("body");
  var headEl = makeElement("head");
  g.document = {
    scripts: [sdkScript],
    documentElement: docElement,
    body: bodyEl,
    head: headEl,
    getElementsByTagName: function (t) {
      t = String(t).toLowerCase();
      if (t === "script") return [sdkScript];
      if (t === "body") return [bodyEl];
      if (t === "head") return [headEl];
      return [];
    },
    getElementById: function () { return null; },
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    createElement: function (tag) { return makeElement(tag || "div"); },
    createTextNode: function (t) { return { nodeType: 3, textContent: "" + t }; },
    referrer: "",
    location: g.location,
    URL: _pageUrl,
    documentURI: _pageUrl,
    baseURI: _pageUrl,
    title: "",
    readyState: "complete",
    hidden: false,
    visibilityState: "visible",
    characterSet: "UTF-8",
    contentType: "text/html",
    get cookie() { return _cookie; },
    set cookie(v) { _cookie = "" + v; }
  };
  // 真 Chrome 上 document 的方法/属性都挂在 HTMLDocument.prototype 上，
  // `Object.keys(document)` 只会剩下一个自有且可枚举的 `location`
  // （WebIDL [LegacyUnforgeable]），sdk 的 p[11] 因此恒为 "location"。
  // 旧实现把全部方法都做成可枚举自有属性，p[11] 会随机报出
  // "createTextNode" / "scripts" / "body" 这类真浏览器上不存在的键。
  (function () {
    var docOwn = [
      "scripts", "documentElement", "body", "head", "getElementsByTagName", "getElementById",
      "querySelector", "querySelectorAll", "createElement", "createTextNode", "referrer",
      "URL", "documentURI", "baseURI", "title", "readyState", "hidden", "visibilityState",
      "characterSet", "contentType", "cookie"
    ];
    for (var i = 0; i < docOwn.length; i++) concealOwn(g.document, docOwn[i]);
    try {
      Object.defineProperty(g.document, "location", {
        value: g.location, enumerable: true, writable: true, configurable: true
      });
    } catch (e) {}
  })();
  // `Object.keys(navigator)` / `Object.keys(screen)` 在真 Chrome 里同样是空的
  // （属性全在各自的原型链上）；这里只收掉可枚举性，按名读取不受影响。
  concealAllOwn(navigator);
  concealAllOwn(g.screen);

  // ---- __reactRouterContext ----
  // collector 会 try-读 ctx.state.loaderData.root.clientBootstrap 的地理字段。
  // 真实 auth 注册页此处 root 为 undefined，读其属性会连环抛错并被作为指纹熵记录。
  // 保持 root 缺失（与真实同形态、同测点数量），错误文案由上方 Error.toString
  // 覆写统一改成 V8 措辞，避免暴露非 V8 引擎。
  g.__reactRouterContext = { state: { loaderData: {} } };

  // ---- requestIdleCallback 保持 undefined → sdk 走 setTimeout 兜底 ----
  // ---- 常见空壳，避免 sdk 里偶发访问抛错 ----
  g.addEventListener = function () {};
  g.removeEventListener = function () {};
  g.dispatchEvent = function () { return true; };
  g.matchMedia = function () { return { matches: false, addListener: function () {}, removeListener: function () {}, addEventListener: function () {}, removeEventListener: function () {} }; };
  if (typeof g.console === "undefined") g.console = { log: function () {}, warn: function () {}, error: function () {} };

  // ---- 更多 collector_dx 常见探测对象 ----
  g.chrome = { runtime: { id: undefined, connect: function () {}, sendMessage: function () {} }, csi: function () { return {}; }, loadTimes: function () { return {}; } };
  g.caches = { open: function () { return Promise.resolve({ match: function () { return Promise.resolve(); } }); }, keys: function () { return Promise.resolve([]); }, has: function () { return Promise.resolve(false); }, match: function () { return Promise.resolve(); } };
  g.indexedDB = { open: function () { return { result: null, onerror: null, onsuccess: null }; }, databases: function () { return Promise.resolve([]); } };
  g.speechSynthesis = { getVoices: function () { return []; }, speak: function () {}, cancel: function () {}, pause: function () {}, resume: function () {} };
  g.visualViewport = { width: g.innerWidth, height: g.innerHeight, offsetLeft: 0, offsetTop: 0, scale: 1, pageLeft: 0, pageTop: 0, addEventListener: function () {} };
  g.origin = _parsedPageUrl.origin;
  g.isSecureContext = true;
  g.crossOriginIsolated = false;
  g.originAgentCluster = false;
  g.credentialless = false;
  g.scheduler = { postTask: function (fn) { return Promise.resolve(fn()); } };
  g.trustedTypes = { createPolicy: function () { return { createHTML: function (s) { return s; } }; } };
  g.cookieStore = { get: function () { return Promise.resolve(null); }, getAll: function () { return Promise.resolve([]); }, set: function () { return Promise.resolve(); } };
  g.AbortController = function () { this.signal = { aborted: false }; this.abort = function () { this.signal.aborted = true; }; };
  g.AbortSignal = { abort: function () { return { aborted: true }; }, timeout: function () { return { aborted: false }; } };
  g.URL = g.URL || function (u) { this.href = u; this.toString = function () { return u; }; };
  g.URLSearchParams = g.URLSearchParams || function () { this.get = function () { return null; }; this.set = function () {}; this.toString = function () { return ""; }; };
  g.Blob = g.Blob || function () { this.size = 0; this.type = ""; };
  g.File = g.File || function () { this.name = ""; this.size = 0; };
  g.FileReader = g.FileReader || function () { this.readAsText = function () {}; this.readAsArrayBuffer = function () {}; };
  g.FormData = g.FormData || function () { this.append = function () {}; this.get = function () { return null; }; };
  g.Headers = g.Headers || function () { this.get = function () { return null; }; this.set = function () {}; };
  g.Request = g.Request || function (u) { this.url = u; this.method = "GET"; };
  g.Response = g.Response || function () { this.ok = true; this.status = 200; this.json = function () { return Promise.resolve({}); }; };
  g.MutationObserver = g.MutationObserver || function () { this.observe = function () {}; this.disconnect = function () {}; this.takeRecords = function () { return []; }; };
  g.IntersectionObserver = g.IntersectionObserver || function () { this.observe = function () {}; this.disconnect = function () {}; this.unobserve = function () {}; };
  g.ResizeObserver = g.ResizeObserver || function () { this.observe = function () {}; this.disconnect = function () {}; this.unobserve = function () {}; };
  g.PerformanceObserver = g.PerformanceObserver || function () { this.observe = function () {}; this.disconnect = function () {}; };
  g.MessageChannel = g.MessageChannel || function () { this.port1 = { postMessage: function () {}, onmessage: null }; this.port2 = { postMessage: function () {}, onmessage: null }; };
  g.BroadcastChannel = g.BroadcastChannel || function () { this.postMessage = function () {}; this.close = function () {}; };
  g.Worker = g.Worker || function () { this.postMessage = function () {}; this.terminate = function () {}; };
  g.SharedWorker = g.SharedWorker || function () { this.port = { postMessage: function () {} }; };
  g.WebSocket = g.WebSocket || function () { this.send = function () {}; this.close = function () {}; this.readyState = 3; };
  g.XMLHttpRequest = g.XMLHttpRequest || function () { this.open = function () {}; this.send = function () {}; this.setRequestHeader = function () {}; this.status = 0; this.responseText = ""; };
  g.Image = g.Image || function () { this.src = ""; this.onload = null; this.onerror = null; };
  g.HTMLElement = g.HTMLElement || function () {};
  g.HTMLDocument = g.HTMLDocument || function () {};
  g.Event = g.Event || function (t) { this.type = t; this.bubbles = false; this.cancelable = false; };
  g.CustomEvent = g.CustomEvent || function (t) { this.type = t; this.detail = null; };
  g.DOMParser = g.DOMParser || function () { this.parseFromString = function () { return { documentElement: g.document.documentElement }; }; };
  g.getComputedStyle = g.getComputedStyle || function () { return { getPropertyValue: function () { return ""; } }; };
  g.requestAnimationFrame = g.requestAnimationFrame || function (fn) { return setTimeout(fn, 16); };
  g.cancelAnimationFrame = g.cancelAnimationFrame || function (id) { clearTimeout(id); };
  g.queueMicrotask = g.queueMicrotask || function (fn) { Promise.resolve().then(fn); };
  g.structuredClone = g.structuredClone || function (v) { return JSON.parse(JSON.stringify(v)); };

  // ---- __oai_so_*：会话行为遥测累加器 ----
  // create_account 需要第二个 sentinel 头 openai-sentinel-so-token，其内容由
  // /sentinel/req 里的 so.collector_dx + so.snapshot_dx 两段 DX 程序读取一批
  // window.__oai_so_* 累加器算出（真实页面通过 keydown/pointermove/click/scroll/
  // wheel 事件监听器实时累计）。协议模式无真实事件，这里按 flow 预置自洽的人类行为值：
  //   - oauth_create_account 等注册流：人在 /about-you 页停留 ~8 秒、输入姓名+生日；
  //   - chatgpt_checkout / checkout_session_approval 等结账流：浏览定价弹窗 →
  //     移动鼠标 → 点击升级按钮，全程没有任何键盘输入（keydown 必须为空）。
  // 结账流套用注册流的 12 次姓名按键会与真实结账页行为直接矛盾。
  (function () {
    var nowP = (g.performance && g.performance.now) ? g.performance.now() : _perfBase;
    var flow = _flowName;
    var isCheckout = _isCheckoutFlow;
    // 进页到采集的停留时间：注册流 ~8 秒；结账流（浏览定价 → 点击升级）~6.5 秒。
    // 与上面的 performance 时基同源，因此 t0 恒为正数。
    var dwell = _dwell;
    var t0 = nowP - dwell;
    // 采集点紧跟在最后一次交互之后（点完「升级」/「继续」SDK 立刻发请求）。
    var end = nowP - (40 + Math.random() * 160);

    var kd = [];
    var kdSpan = 0;
    if (!isCheckout) {
      // 注册流：输入姓名 "Anna Wilson2"（12 次按键）。
      var keys = ["A", "n", "n", "a", " ", "W", "i", "l", "s", "o", "n", "2"];
      var kdDeltas = [];
      for (var k = 0; k < keys.length; k++) {
        var kdGap = 140 + Math.random() * 90;
        kdDeltas.push(kdGap);
        kdSpan += kdGap;
      }
      // 打字结束在采集点前 ~200ms（提交按钮点击）。
      var kt = end - 200 - kdSpan;
      for (var i = 0; i < keys.length; i++) {
        kt += kdDeltas[i];
        kd.push({ ctrlKey: false, metaKey: false, altKey: false, shiftKey: (i === 0 || i === 5), key: keys[i], type: "keydown", t: kt });
      }
    }

    // pointermove 事件序列（鼠标移动，累计路径长度）。
    // 坐标按当前视口尺寸 (_sw × _ah) 等比生成并严格钳制在视口内，避免 1366/1440 小屏
    // 出现点击坐标越界；同时对步数、滚动量做自然随机扰动，避免跨订单常量聚类。
    var clamp = function (v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); };
    var pm = [];
    var sx0 = isCheckout
      ? Math.round(_sw * (0.48 + Math.random() * 0.14))
      : Math.round(_sw * (0.30 + Math.random() * 0.14));
    var sy0 = isCheckout
      ? Math.round(_ah * (0.18 + Math.random() * 0.10))
      : Math.round(_ah * (0.28 + Math.random() * 0.12));
    var tx = isCheckout
      ? Math.round(_sw * (0.62 + Math.random() * 0.14))
      : Math.round(_sw * (0.42 + Math.random() * 0.16));
    var ty = isCheckout
      ? Math.round(_ah * (0.42 + Math.random() * 0.18))
      : Math.round(_ah * (0.40 + Math.random() * 0.16));
    var px = sx0;
    var py = sy0;
    var htot = 0, pcnt = 0;
    var steps = isCheckout ? (18 + Math.floor(Math.random() * 10)) : (24 + Math.floor(Math.random() * 10));
    var gaps = [];
    var gapSum = 0;
    for (var a = 0; a < steps; a++) {
      // 第一步是「从静止到移动」的迟疑，后续是稳定的手部单步间隔。
      var gap = a === 0 ? 120 + Math.random() * 220 : 55 + Math.random() * 85;
      gaps.push(gap);
      gapSum += gap;
    }
    // 结账流：鼠标轨迹直接结束于点击；注册流：鼠标先落在姓名输入框，之后才打字。
    var pointerEnd = isCheckout ? end : (end - 200 - kdSpan - (300 + Math.random() * 300));
    var pt = pointerEnd - gapSum;
    var stepDx = (tx - sx0) / steps;
    var stepDy = (ty - sy0) / steps;
    for (var j = 0; j < steps; j++) {
      var prevX = px, prevY = py;
      var nx = isCheckout
        ? (px + stepDx + (Math.random() - 0.5) * 14)
        : (px + stepDx + (Math.random() - 0.5) * 32);
      var ny = isCheckout
        ? (py + stepDy + (Math.random() - 0.5) * 12)
        : (py + stepDy + (Math.random() - 0.5) * 24);
      px = clamp(nx, 32, _sw - 32);
      py = clamp(ny, 32, _ah - 32);
      pt += gaps[j];
      htot += Math.hypot(px - prevX, py - prevY); pcnt++;
      pm.push({ clientX: Math.round(px), clientY: Math.round(py), type: "pointermove", t: pt });
    }

    // scroll 序列：结账流滚动定价弹窗查看全部套餐（3~6 次随机），注册流为 0。
    var scrollCount = isCheckout ? (3 + Math.floor(Math.random() * 4)) : 0;
    var scrollSum = isCheckout ? Math.round(820 + Math.random() * 680) : 0;
    var scrollTop = isCheckout ? Math.round(420 + Math.random() * 340) : 0;
    // click 序列：结账流点击「升级」按钮 2 次（打开弹窗 + 确认套餐），注册流为提交按钮。
    var clickCount = 2;

    g.__FLOW_EFFECTIVE = flow;
    g.__oai_so_t0 = t0;
    g.__oai_so_h = kd;          // keydown 事件数组
    g.__oai_so_hi = kd.length;
    g.__oai_so_hp = 0;
    g.__oai_so_hw = 0;
    g.__oai_so_s = scrollSum;   // scroll 累加
    g.__oai_so_k = kd.length;   // 按键计数
    g.__oai_so_kp = 0;
    g.__oai_so_we = 0;          // wheel 事件数
    g.__oai_so_wb = 0;
    g.__oai_so_wl = 0;
    g.__oai_so_fs = 0;
    g.__oai_so_fs2 = 0;
    g.__oai_so_fn = 0;
    g.__oai_so_p = pm;          // pointer 事件数组
    g.__oai_so_pc = pcnt;       // pointer 计数
    g.__oai_so_i = [];          // input 事件数组（结账流无输入）
    g.__oai_so_m = pm.length;   // mousemove 计数
    g.__oai_so_ht = htot;       // 鼠标路径总长（hypot 累加）
    g.__oai_so_hc = pcnt;       // hypot 计数
    g.__oai_so_bc = clickCount; // click 计数
    g.__oai_so_bm = 0;
    g.__oai_so_ss = scrollSum;
    g.__oai_so_ss2 = scrollSum;
    g.__oai_so_sn = scrollCount; // scroll 计数
    g.__oai_so_cs = clickCount ? Math.round(180 + Math.random() * 160) : 0;  // click 累加
    g.__oai_so_cs2 = g.__oai_so_cs;
    g.__oai_so_cn = clickCount; // click 计数
    g.__oai_so_st = scrollTop;  // scrollTop
    // 视口宽度必须与 screen.width 同源：写死 1440 会和 1920 的屏幕档案直接矛盾。
    g.__oai_so_sw = _sw;
    g.__oai_so_sp = 0;
    g.__oai_so_spt = 0;
    g.__oai_so_sx0 = sx0;
    g.__oai_so_sy0 = sy0;
    g.__oai_so_lx = Math.round(px);
    g.__oai_so_ly = Math.round(py);

    // __FLOW_EFFECTIVE 是我们自造的键（真页面没有），不可枚举。
    concealOwn(g, "__FLOW_EFFECTIVE");
  })();

  // ---- window 可枚举面收尾 ----
  // 真 Chrome 的 window 上有一整批事件处理器属性（onbeforeunload/onmousemove/…），
  // sdk 的 p[12] 正是从 `Object.keys(window)` 里随机取一个上报。补齐这些键，
  // 让随机命中落回真浏览器也存在的名字上。
  (function () {
    var handlers = [
      "onbeforeunload", "onunload", "onload", "onblur", "onfocus", "onfocusin", "onfocusout",
      "onchange", "oninput", "onsubmit", "onreset", "onclick", "ondblclick", "onauxclick",
      "oncontextmenu", "onmousedown", "onmouseup", "onmousemove", "onmouseover", "onmouseout",
      "onmouseenter", "onmouseleave", "onpointerdown", "onpointerup", "onpointermove",
      "onpointerover", "onpointerout", "onkeydown", "onkeyup", "onkeypress", "onwheel",
      "onscroll", "onresize", "onerror", "onmessage", "onhashchange", "onpopstate",
      "onselect", "ontouchstart", "ontouchend", "ontouchmove", "ondragstart", "ondragend"
    ];
    for (var i = 0; i < handlers.length; i++) {
      if (!(handlers[i] in g)) {
        try { g[handlers[i]] = null; } catch (e) {}
      }
    }
  })();
})();

