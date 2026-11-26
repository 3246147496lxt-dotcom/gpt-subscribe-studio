// sentinel_bootstrap.js — 在加载真·sdk.js 之前铺好浏览器环境 shim。
(function () {
  "use strict";
  var g = globalThis;

  var _hostNow = typeof g.__HOST_NOW__ === "function" ? g.__HOST_NOW__ : null;
  try { delete g.__HOST_NOW__; } catch (e) {}

  function concealOwn(obj, key) {
    if (!obj) return;
    try {
      var desc = Object.getOwnPropertyDescriptor(obj, key);
      if (!desc || desc.enumerable === false) return;
      desc.enumerable = false;
      Object.defineProperty(obj, key, desc);
    } catch (e) {}
  }

  var _BRIDGE_INPUTS = [
    "__UA__", "__CORES__", "__FLOW__", "__SENTINEL_FLOW__", "__SDK_URL__",
    "__SEED_DID_KEY__", "__SEED_DID_VAL__", "__PAGE_URL__", "__LANGUAGE__",
    "__LANGUAGES__", "__TIMEZONE__", "__COOKIE_HEADER__", "__PLATFORM__",
    "__PLATFORM_LABEL__", "__SCREEN_W__", "__SCREEN_H__", "__AVAIL_H__", "__MAX_TOUCH__",
    "__SEC_CH_UA__", "__SEC_CH_UA_FULL_LIST__", "__CH_ARCH__", "__CH_BITNESS__",
    "__CH_PLATFORM_VERSION__", "__HOST_NOW__"
  ];
  for (var _bi = 0; _bi < _BRIDGE_INPUTS.length; _bi++) concealOwn(g, _BRIDGE_INPUTS[_bi]);

  // WeakMap 标记型 Function.prototype.toString：替身方法与覆盖方法均返回 [native code]，
  // 实例无多余自有 toString 键，且 sdk.js 自身函数的源码自检保持原样。
  var _fnToString = Function.prototype.toString;
  var _nativeFnNames = new WeakMap();
  function markNative(fn, name) {
    if (typeof fn !== "function") return fn;
    var label = name !== undefined ? String(name) : String(fn.name || "");
    try { Object.defineProperty(fn, "name", { value: label, configurable: true }); } catch (e) {}
    _nativeFnNames.set(fn, "function " + label + "() { [native code] }");
    return fn;
  }
  // WebIDL 原生方法不可用 new 构造且无自有 .prototype（concise method 语法自动满足）。
  function makeNonCtor(impl, name) {
    var fn = ({ m() { return impl.apply(this, arguments); } }).m;
    return markNative(fn, name);
  }
  Function.prototype.toString = makeNonCtor(function () {
    if (_nativeFnNames.has(this)) return _nativeFnNames.get(this);
    return _fnToString.call(this);
  }, "toString");
  function nativeFn(name) {
    return makeNonCtor(function () {}, name);
  }
  function nativePromiseFn(name, value) {
    return makeNonCtor(function () { return Promise.resolve(value); }, name);
  }
  function tagObj(tag, obj) {
    Object.defineProperty(obj, Symbol.toStringTag, {
      value: tag,
      writable: false,
      enumerable: false,
      configurable: true
    });
    return obj;
  }

  // 能力探测：仅在非 V8 引擎（如 goja）下补齐 Error 文案与灾难性回溯正则短路，
  // V8/Node 环境不覆盖原生 Error.prototype.toString / String.prototype.search。
  var _errSample = "";
  try {
    var _undef;
    _undef.x;
  } catch (err) {
    _errSample = String(err);
  }
  if (_errSample.indexOf("Cannot read property ") !== -1) {
    var _search = String.prototype.search;
    String.prototype.search = markNative(function (re) {
      try {
        var s = (re && re.source) ? re.source : String(re);
        if (s.indexOf("(((.+)+)+)+") !== -1) return this.length ? 0 : -1;
      } catch (e) {}
      return _search.call(this, re);
    }, "search");

    var _errToString = Error.prototype.toString;
    Error.prototype.toString = markNative(function () {
      var s = _errToString.call(this);
      try {
        s = s.replace(/Cannot read property '([^']*)' of undefined/g, "Cannot read properties of undefined (reading '$1')")
             .replace(/Cannot read property '([^']*)' of null/g, "Cannot read properties of null (reading '$1')");
      } catch (e) {}
      return s;
    }, "toString");
  }

  g.setInterval = makeNonCtor(function () { return 0; }, "setInterval");
  g.clearInterval = makeNonCtor(function () {}, "clearInterval");

  g.crypto = g.crypto || {};
  if (typeof g.crypto.getRandomValues !== "function") {
    g.crypto.getRandomValues = makeNonCtor(function (arr) {
      for (var i = 0; i < arr.length; i++) arr[i] = (Math.random() * 256) | 0;
      return arr;
    }, "getRandomValues");
  }
  if (typeof g.crypto.randomUUID !== "function") {
    g.crypto.randomUUID = makeNonCtor(function () {
      var b = new Uint8Array(16);
      g.crypto.getRandomValues(b);
      b[6] = (b[6] & 0x0f) | 0x40;
      b[8] = (b[8] & 0x3f) | 0x80;
      var h = [];
      for (var i = 0; i < 16; i++) h.push((b[i] + 0x100).toString(16).slice(1));
      return h[0] + h[1] + h[2] + h[3] + "-" + h[4] + h[5] + "-" + h[6] + h[7] +
        "-" + h[8] + h[9] + "-" + h[10] + h[11] + h[12] + h[13] + h[14] + h[15];
    }, "randomUUID");
  }

  try {
    Object.defineProperty(g, Symbol.toStringTag, {
      value: "Window",
      writable: false,
      enumerable: false,
      configurable: true
    });
  } catch (e) {}

  g.window = g;
  g.self = g;
  g.top = g;
  g.parent = g;

  // ---- WebIDL 原型与访问器辅助 ----
  function defineAccessor(proto, name, getter, setter, isValidReceiver) {
    var getFn = makeNonCtor(function () {
      if (!isValidReceiver(this)) throw new TypeError("Illegal invocation");
      return getter.call(this);
    }, "get " + name);
    var desc = { get: getFn, enumerable: true, configurable: true };
    if (setter) {
      desc.set = makeNonCtor(function (v) {
        if (!isValidReceiver(this)) throw new TypeError("Illegal invocation");
        return setter.call(this, v);
      }, "set " + name);
    }
    Object.defineProperty(proto, name, desc);
  }
  function defineStaticGetter(proto, name, value, isValidReceiver) {
    defineAccessor(proto, name, function () { return value; }, null, isValidReceiver);
  }
  function defineMethod(proto, name, fn, isValidReceiver) {
    var wrapped = isValidReceiver
      ? makeNonCtor(function () {
          if (!isValidReceiver(this)) throw new TypeError("Illegal invocation");
          return fn.apply(this, arguments);
        }, name)
      : makeNonCtor(fn, name);
    Object.defineProperty(proto, name, {
      value: wrapped,
      writable: true,
      enumerable: true,
      configurable: true
    });
  }
  function defineTag(proto, tag) {
    Object.defineProperty(proto, Symbol.toStringTag, {
      value: tag,
      writable: false,
      enumerable: false,
      configurable: true
    });
  }
  function defineInterface(name, proto, parentProto) {
    if (parentProto) Object.setPrototypeOf(proto, parentProto);
    defineTag(proto, name);
    var Ctor = markNative(function () {
      throw new TypeError("Illegal constructor");
    }, name);
    Ctor.prototype = proto;
    Object.defineProperty(proto, "constructor", {
      value: Ctor,
      writable: true,
      enumerable: false,
      configurable: true
    });
    Object.defineProperty(g, name, {
      value: Ctor,
      writable: true,
      enumerable: false,
      configurable: true
    });
    return Ctor;
  }

  var EventTargetProto = {};
  var isEventTarget = function (self) { return EventTargetProto.isPrototypeOf(self); };
  defineMethod(EventTargetProto, "addEventListener", function () {}, isEventTarget);
  defineMethod(EventTargetProto, "removeEventListener", function () {}, isEventTarget);
  defineMethod(EventTargetProto, "dispatchEvent", function () { return true; }, isEventTarget);
  defineInterface("EventTarget", EventTargetProto, null);

  // ---- Date 覆写：本地化长时区名 ----
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
    if (!name) name = timezone;
    _tzDisplayCache = name;
    return name;
  }
  g.__TZ_DISPLAY_NAME__ = tzDisplayName(g.__TIMEZONE__ || "America/Chicago");

  var _DateToString = Date.prototype.toString;
  Date.prototype.toString = makeNonCtor(function () {
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
  }, "toString");

  // ---- Chromium GenerateBrandVersionList ----
  var _GREASEY_CHARS = " (:-./);=?_";
  var _GREASE_VERSIONS = ["8", "99", "24"];
  var _BRAND_ORDERS = [
    [0, 1, 2], [0, 2, 1], [1, 0, 2], [1, 2, 0], [2, 0, 1], [2, 1, 0]
  ];
  function chromiumBrands(major, useFullBuild) {
    var seed = parseInt(String(major || "150"), 10) || 150;
    var m = String(major || seed).split(".")[0] || String(seed);
    var n = _GREASEY_CHARS.length;
    var greaseBrand = "Not" + _GREASEY_CHARS[seed % n] + "A" + _GREASEY_CHARS[(seed + 1) % n] + "Brand";
    var greaseVer = _GREASE_VERSIONS[seed % _GREASE_VERSIONS.length];
    var mainVer = useFullBuild || m;
    var items = [
      { brand: greaseBrand, version: useFullBuild ? (greaseVer + ".0.0.0") : greaseVer },
      { brand: "Chromium", version: mainVer },
      { brand: "Google Chrome", version: mainVer }
    ];
    var order = _BRAND_ORDERS[seed % _BRAND_ORDERS.length];
    var out = [];
    for (var pos = 0; pos < 3; pos++) {
      out.push(items[order.indexOf(pos)]);
    }
    return out;
  }
  function parseBrandList(raw) {
    var out = [];
    String(raw || "").replace(/"([^"]*)"\s*;\s*v="([^"]*)"/g, function (_all, brand, version) {
      if (brand) out.push({ brand: brand, version: version });
      return "";
    });
    return out;
  }

  // ---- Navigator（WebIDL accessor + 原型方法，实例零自有属性）----
  var NavigatorProto = {};
  defineInterface("Navigator", NavigatorProto, null);
  var isNavigator = function (self) { return NavigatorProto.isPrototypeOf(self); };

  var _uaStr = String(g.__UA__ || "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.7871.189 Safari/537.36");
  var _uaMatch = _uaStr.match(/(?:Chrome|CriOS)\/(\d+(?:\.\d+)*)/);
  var _fullVer = _uaMatch ? _uaMatch[1] : "150.0.7871.189";
  var _majorVer = _fullVer.split(".")[0] || "150";
  var _platLabel = String(g.__PLATFORM_LABEL__ || "Windows");
  var _brands = parseBrandList(g.__SEC_CH_UA__);
  if (!_brands.length) _brands = chromiumBrands(_majorVer, null);
  var _fullList = parseBrandList(g.__SEC_CH_UA_FULL_LIST__);
  if (!_fullList.length) _fullList = chromiumBrands(_majorVer, _fullVer);
  var _platVersion = String(g.__CH_PLATFORM_VERSION__ ||
    (_platLabel === "Windows" ? "10.0.0" : (_platLabel === "macOS" ? "14.6.1" : "")));
  var _arch = String(g.__CH_ARCH__ || (_platLabel === "macOS" ? "arm" : "x86"));
  var _bitness = String(g.__CH_BITNESS__ || "64");

  var _navLanguage = g.__LANGUAGE__ || "en-US";
  function defaultLanguages(loc) {
    var s = String(loc || "en-US").trim() || "en-US";
    var primary = s.split("-")[0];
    var out = [s];
    if (primary && out.indexOf(primary) === -1) out.push(primary);
    if (primary !== "en" && out.indexOf("en") === -1) out.push("en");
    return out;
  }
  var _navLanguages = Object.freeze(
    (Array.isArray(g.__LANGUAGES__) && g.__LANGUAGES__.length
      ? g.__LANGUAGES__
      : defaultLanguages(_navLanguage)).slice()
  );
  var _navMaxTouch = g.__MAX_TOUCH__ || 0;
  var _navSubObjects = {
    scheduling: tagObj("Scheduling", { isInputPending: makeNonCtor(function () { return false; }, "isInputPending") }),
    userActivation: tagObj("UserActivation", { hasBeenActive: true, isActive: true }),
    geolocation: tagObj("Geolocation", { getCurrentPosition: nativeFn("getCurrentPosition"), watchPosition: nativeFn("watchPosition"), clearWatch: nativeFn("clearWatch") }),
    connection: tagObj("NetworkInformation", { effectiveType: "4g", rtt: 50, downlink: 10, saveData: false, type: "wifi" }),
    plugins: tagObj("PluginArray", { length: 5, item: nativeFn("item"), namedItem: nativeFn("namedItem"), refresh: nativeFn("refresh") }),
    mimeTypes: tagObj("MimeTypeArray", { length: 4, item: nativeFn("item"), namedItem: nativeFn("namedItem") }),
    webkitTemporaryStorage: tagObj("DeprecatedStorageQuota", { queryUsageAndQuota: nativeFn("queryUsageAndQuota"), requestQuota: nativeFn("requestQuota") }),
    webkitPersistentStorage: tagObj("DeprecatedStorageQuota", { queryUsageAndQuota: nativeFn("queryUsageAndQuota"), requestQuota: nativeFn("requestQuota") }),
    clipboard: tagObj("Clipboard", { read: nativePromiseFn("read"), readText: nativePromiseFn("readText", ""), write: nativePromiseFn("write"), writeText: nativePromiseFn("writeText") }),
    credentials: tagObj("CredentialsContainer", { get: nativePromiseFn("get", null), store: nativePromiseFn("store"), create: nativePromiseFn("create", null), preventSilentAccess: nativePromiseFn("preventSilentAccess") }),
    keyboard: tagObj("Keyboard", { getLayoutMap: makeNonCtor(function () { return Promise.resolve(new Map()); }, "getLayoutMap"), lock: nativePromiseFn("lock"), unlock: nativeFn("unlock") }),
    locks: tagObj("LockManager", { request: nativePromiseFn("request"), query: makeNonCtor(function () { return Promise.resolve({ held: [], pending: [] }); }, "query") }),
    mediaCapabilities: tagObj("MediaCapabilities", { decodingInfo: makeNonCtor(function () { return Promise.resolve({ supported: true, smooth: true, powerEfficient: true }); }, "decodingInfo") }),
    mediaDevices: tagObj("MediaDevices", { enumerateDevices: makeNonCtor(function () { return Promise.resolve([]); }, "enumerateDevices"), getUserMedia: nativePromiseFn("getUserMedia") }),
    mediaSession: tagObj("MediaSession", { metadata: null, playbackState: "none", setActionHandler: nativeFn("setActionHandler") }),
    permissions: tagObj("Permissions", { query: makeNonCtor(function () { return Promise.resolve({ state: "prompt" }); }, "query") }),
    serviceWorker: tagObj("ServiceWorkerContainer", { controller: null, ready: Promise.resolve({ active: null }), register: nativePromiseFn("register"), getRegistrations: makeNonCtor(function () { return Promise.resolve([]); }, "getRegistrations") }),
    storage: tagObj("StorageManager", { estimate: makeNonCtor(function () { return Promise.resolve({ quota: 2e11, usage: 5e7 }); }, "estimate"), getDirectory: nativePromiseFn("getDirectory"), persisted: makeNonCtor(function () { return Promise.resolve(false); }, "persisted"), persist: makeNonCtor(function () { return Promise.resolve(false); }, "persist") }),
    ink: tagObj("Ink", { requestPresenter: nativePromiseFn("requestPresenter") }),
    gpu: tagObj("GPU", { requestAdapter: nativePromiseFn("requestAdapter", null) }),
    hid: tagObj("HID", { getDevices: makeNonCtor(function () { return Promise.resolve([]); }, "getDevices"), requestDevice: nativePromiseFn("requestDevice", []) }),
    serial: tagObj("Serial", { getPorts: makeNonCtor(function () { return Promise.resolve([]); }, "getPorts"), requestPort: nativePromiseFn("requestPort") }),
    usb: tagObj("USB", { getDevices: makeNonCtor(function () { return Promise.resolve([]); }, "getDevices"), requestDevice: nativePromiseFn("requestDevice") }),
    bluetooth: tagObj("Bluetooth", { getAvailability: makeNonCtor(function () { return Promise.resolve(false); }, "getAvailability"), requestDevice: nativePromiseFn("requestDevice") }),
    xr: tagObj("XRSystem", { isSessionSupported: makeNonCtor(function () { return Promise.resolve(false); }, "isSessionSupported"), requestSession: nativePromiseFn("requestSession") }),
    wakeLock: tagObj("WakeLock", { request: nativePromiseFn("request") }),
    userAgentData: tagObj("NavigatorUAData", {
      brands: _brands.map(function (b) { return { brand: b.brand, version: b.version }; }),
      mobile: false,
      platform: _platLabel,
      getHighEntropyValues: makeNonCtor(function (hints) {
        var all = {
          brands: _brands.map(function (b) { return { brand: b.brand, version: b.version }; }),
          fullVersionList: _fullList.map(function (b) { return { brand: b.brand, version: b.version }; }),
          fullVersion: _fullVer,
          uaFullVersion: _fullVer,
          platform: _platLabel,
          platformVersion: _platVersion,
          architecture: _arch,
          bitness: _bitness,
          model: "",
          mobile: false,
          wow64: false
        };
        if (Object.prototype.toString.call(hints) !== "[object Array]") return Promise.resolve(all);
        var picked = { brands: all.brands, mobile: all.mobile, platform: all.platform };
        for (var i = 0; i < hints.length; i++) {
          if (Object.prototype.hasOwnProperty.call(all, hints[i])) picked[hints[i]] = all[hints[i]];
        }
        return Promise.resolve(picked);
      }, "getHighEntropyValues"),
      toJSON: makeNonCtor(function () { return { brands: _brands, mobile: false, platform: _platLabel }; }, "toJSON")
    })
  };

  defineStaticGetter(NavigatorProto, "vendorSub", "", isNavigator);
  defineStaticGetter(NavigatorProto, "productSub", "20030107", isNavigator);
  defineStaticGetter(NavigatorProto, "vendor", "Google Inc.", isNavigator);
  defineAccessor(NavigatorProto, "maxTouchPoints", function () { return _navMaxTouch; }, null, isNavigator);
  defineStaticGetter(NavigatorProto, "scheduling", _navSubObjects.scheduling, isNavigator);
  defineStaticGetter(NavigatorProto, "userActivation", _navSubObjects.userActivation, isNavigator);
  defineStaticGetter(NavigatorProto, "doNotTrack", null, isNavigator);
  defineStaticGetter(NavigatorProto, "geolocation", _navSubObjects.geolocation, isNavigator);
  defineStaticGetter(NavigatorProto, "connection", _navSubObjects.connection, isNavigator);
  defineStaticGetter(NavigatorProto, "plugins", _navSubObjects.plugins, isNavigator);
  defineStaticGetter(NavigatorProto, "mimeTypes", _navSubObjects.mimeTypes, isNavigator);
  defineStaticGetter(NavigatorProto, "pdfViewerEnabled", true, isNavigator);
  defineStaticGetter(NavigatorProto, "webkitTemporaryStorage", _navSubObjects.webkitTemporaryStorage, isNavigator);
  defineStaticGetter(NavigatorProto, "webkitPersistentStorage", _navSubObjects.webkitPersistentStorage, isNavigator);
  defineStaticGetter(NavigatorProto, "hardwareConcurrency", g.__CORES__ || 8, isNavigator);
  defineStaticGetter(NavigatorProto, "cookieEnabled", true, isNavigator);
  defineStaticGetter(NavigatorProto, "appCodeName", "Mozilla", isNavigator);
  defineStaticGetter(NavigatorProto, "appName", "Netscape", isNavigator);
  defineStaticGetter(NavigatorProto, "appVersion", _uaStr.replace("Mozilla/", ""), isNavigator);
  defineStaticGetter(NavigatorProto, "platform", g.__PLATFORM__ || "Win32", isNavigator);
  defineStaticGetter(NavigatorProto, "product", "Gecko", isNavigator);
  defineStaticGetter(NavigatorProto, "userAgent", _uaStr, isNavigator);
  defineStaticGetter(NavigatorProto, "language", _navLanguage, isNavigator);
  defineStaticGetter(NavigatorProto, "languages", _navLanguages, isNavigator);
  defineStaticGetter(NavigatorProto, "onLine", true, isNavigator);
  defineStaticGetter(NavigatorProto, "webdriver", false, isNavigator);
  defineStaticGetter(NavigatorProto, "deviceMemory", 8, isNavigator);
  defineStaticGetter(NavigatorProto, "clipboard", _navSubObjects.clipboard, isNavigator);
  defineStaticGetter(NavigatorProto, "credentials", _navSubObjects.credentials, isNavigator);
  defineStaticGetter(NavigatorProto, "keyboard", _navSubObjects.keyboard, isNavigator);
  defineStaticGetter(NavigatorProto, "locks", _navSubObjects.locks, isNavigator);
  defineStaticGetter(NavigatorProto, "mediaCapabilities", _navSubObjects.mediaCapabilities, isNavigator);
  defineStaticGetter(NavigatorProto, "mediaDevices", _navSubObjects.mediaDevices, isNavigator);
  defineStaticGetter(NavigatorProto, "mediaSession", _navSubObjects.mediaSession, isNavigator);
  defineStaticGetter(NavigatorProto, "permissions", _navSubObjects.permissions, isNavigator);
  defineStaticGetter(NavigatorProto, "serviceWorker", _navSubObjects.serviceWorker, isNavigator);
  defineStaticGetter(NavigatorProto, "storage", _navSubObjects.storage, isNavigator);
  defineStaticGetter(NavigatorProto, "userAgentData", _navSubObjects.userAgentData, isNavigator);
  defineStaticGetter(NavigatorProto, "ink", _navSubObjects.ink, isNavigator);
  defineStaticGetter(NavigatorProto, "gpu", _navSubObjects.gpu, isNavigator);
  defineStaticGetter(NavigatorProto, "hid", _navSubObjects.hid, isNavigator);
  defineStaticGetter(NavigatorProto, "serial", _navSubObjects.serial, isNavigator);
  defineStaticGetter(NavigatorProto, "usb", _navSubObjects.usb, isNavigator);
  defineStaticGetter(NavigatorProto, "bluetooth", _navSubObjects.bluetooth, isNavigator);
  defineStaticGetter(NavigatorProto, "xr", _navSubObjects.xr, isNavigator);
  defineStaticGetter(NavigatorProto, "wakeLock", _navSubObjects.wakeLock, isNavigator);

  var _navMethodImpls = {
    canShare: function () { return false; },
    share: function () { return Promise.resolve(); },
    getBattery: function () {
      return Promise.resolve(tagObj("BatteryManager", {
        charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1,
        onchargingchange: null, onchargingtimechange: null, ondischargingtimechange: null, onlevelchange: null
      }));
    },
    getGamepads: function () { return [null, null, null, null]; },
    requestMIDIAccess: function () { return Promise.resolve(); },
    requestMediaKeySystemAccess: function () { return Promise.resolve(); },
    sendBeacon: function () { return true; },
    vibrate: function () { return true; },
    setAppBadge: function () { return Promise.resolve(); },
    clearAppBadge: function () { return Promise.resolve(); },
    getInstalledRelatedApps: function () { return Promise.resolve([]); },
    javaEnabled: function () { return false; },
    taintEnabled: function () { return false; }
  };
  var navProtoMethods = [
    "canShare", "share", "getBattery", "getGamepads", "requestMIDIAccess",
    "requestMediaKeySystemAccess", "sendBeacon", "vibrate",
    "registerProtocolHandler", "unregisterProtocolHandler", "setAppBadge", "clearAppBadge",
    "getInstalledRelatedApps", "javaEnabled", "taintEnabled", "getUserMedia",
    "webkitGetUserMedia", "clearOriginJoinedAdInterestGroups", "joinAdInterestGroup",
    "leaveAdInterestGroup", "runAdAuction", "createAuctionNonce",
    "deprecatedReplaceInURN", "deprecatedURNToURL", "updateAdInterestGroups"
  ];
  for (var _nmi = 0; _nmi < navProtoMethods.length; _nmi++) {
    var _nm = navProtoMethods[_nmi];
    defineMethod(NavigatorProto, _nm, _navMethodImpls[_nm] || function () {}, isNavigator);
  }
  var navigator = Object.create(NavigatorProto);
  g.navigator = navigator;

  // ---- screen / window geometry ----
  var _isMacProfile = _platLabel === "macOS";
  var _isLinuxProfile = _platLabel === "Linux";
  var _sw = g.__SCREEN_W__ || 1920;
  var _sh = g.__SCREEN_H__ || 1080;
  var _ah = g.__AVAIL_H__ || (_sh - (_isMacProfile ? 25 : (_isLinuxProfile ? 24 : 48)));
  var _dpr = _isMacProfile ? 2 : 1;
  var _chromeH = 88 + Math.round(Math.random() * 20);

  var _availTop, _screenX, _screenY, _innerW, _outerW, _outerH, _innerH;
  if (!_isMacProfile && !_isLinuxProfile) {
    // Windows 最大化窗口：DWM 8px 隐形边框 -> screenX/Y = -8，outer = avail + 16。
    _availTop = 0;
    _screenX = -8;
    _screenY = -8;
    _innerW = _sw;
    _outerW = _sw + 16;
    _outerH = _ah + 16;
    _innerH = _ah - _chromeH;
  } else {
    // macOS / Linux 单屏：无 DWM +16 边框，outerWidth === innerWidth <= availWidth，outerHeight <= availHeight。
    _availTop = _isMacProfile ? Math.max(0, Math.min(25, _sh - _ah)) : 0;
    _screenX = 0;
    _screenY = _availTop;
    _innerW = _sw;
    _outerW = _sw;
    _outerH = _ah - 4 - Math.round(Math.random() * 16);
    _innerH = _outerH - _chromeH;
  }

  var ScreenProto = {};
  defineInterface("Screen", ScreenProto, EventTargetProto);
  var isScreen = function (self) { return ScreenProto.isPrototypeOf(self); };
  var _orientationObj = tagObj("ScreenOrientation", {
    angle: 0,
    type: "landscape-primary",
    onchange: null,
    addEventListener: nativeFn("addEventListener"),
    removeEventListener: nativeFn("removeEventListener"),
    dispatchEvent: makeNonCtor(function () { return true; }, "dispatchEvent")
  });
  defineStaticGetter(ScreenProto, "availWidth", _sw, isScreen);
  defineStaticGetter(ScreenProto, "availHeight", _ah, isScreen);
  defineStaticGetter(ScreenProto, "width", _sw, isScreen);
  defineStaticGetter(ScreenProto, "height", _sh, isScreen);
  defineStaticGetter(ScreenProto, "colorDepth", 24, isScreen);
  defineStaticGetter(ScreenProto, "pixelDepth", 24, isScreen);
  defineStaticGetter(ScreenProto, "availLeft", 0, isScreen);
  defineStaticGetter(ScreenProto, "availTop", _availTop, isScreen);
  defineStaticGetter(ScreenProto, "orientation", _orientationObj, isScreen);
  defineStaticGetter(ScreenProto, "isExtended", false, isScreen);
  var _screenOnChange = null;
  defineAccessor(ScreenProto, "onchange", function () { return _screenOnChange; }, function (v) { _screenOnChange = v; }, isScreen);

  g.screen = Object.create(ScreenProto);
  g.innerWidth = _innerW;
  g.innerHeight = _innerH;
  g.outerWidth = _outerW;
  g.outerHeight = _outerH;
  g.devicePixelRatio = _dpr;
  g.screenX = _screenX;
  g.screenY = _screenY;
  g.scrollX = 0;
  g.scrollY = 0;

  // ---- history / reactive location ----
  g.history = tagObj("History", {
    length: 2,
    scrollRestoration: "auto",
    state: null,
    back: nativeFn("back"),
    forward: nativeFn("forward"),
    go: nativeFn("go"),
    pushState: nativeFn("pushState"),
    replaceState: nativeFn("replaceState")
  });
  var _pageUrl = g.__PAGE_URL__ || "https://auth.openai.com/about-you";
  var _locUrl = new URL(_pageUrl);
  var LocationProto = {};
  defineInterface("Location", LocationProto, null);
  var locObj = Object.create(LocationProto);
  function _setLocHref(v) {
    _locUrl = new URL(String(v), _locUrl.href);
  }
  function defLocProp(name, getter, setter) {
    var desc = {
      get: makeNonCtor(getter, "get " + name),
      enumerable: true,
      configurable: false
    };
    if (setter) desc.set = makeNonCtor(setter, "set " + name);
    Object.defineProperty(locObj, name, desc);
  }
  defLocProp("href", function () { return _locUrl.href; }, _setLocHref);
  defLocProp("origin", function () { return _locUrl.origin; }, null);
  defLocProp("protocol", function () { return _locUrl.protocol; }, function (v) { _locUrl.protocol = String(v); });
  defLocProp("host", function () { return _locUrl.host; }, function (v) { _locUrl.host = String(v); });
  defLocProp("hostname", function () { return _locUrl.hostname; }, function (v) { _locUrl.hostname = String(v); });
  defLocProp("port", function () { return _locUrl.port; }, function (v) { _locUrl.port = String(v); });
  defLocProp("pathname", function () { return _locUrl.pathname; }, function (v) { _locUrl.pathname = String(v); });
  defLocProp("search", function () { return _locUrl.search; }, function (v) { _locUrl.search = String(v); });
  defLocProp("hash", function () { return _locUrl.hash; }, function (v) { _locUrl.hash = String(v); });
  Object.defineProperty(locObj, "assign", { value: makeNonCtor(_setLocHref, "assign"), enumerable: true, writable: false, configurable: false });
  Object.defineProperty(locObj, "replace", { value: makeNonCtor(_setLocHref, "replace"), enumerable: true, writable: false, configurable: false });
  Object.defineProperty(locObj, "reload", { value: nativeFn("reload"), enumerable: true, writable: false, configurable: false });
  Object.defineProperty(locObj, "toString", { value: makeNonCtor(function () { return _locUrl.href; }, "toString"), enumerable: true, writable: false, configurable: false });
  g.location = locObj;

  // ---- performance（W3C Navigation + Resource Timing + 0.1ms 量化单调时钟）----
  var _flowName = String((typeof g.__FLOW__ !== "undefined" && g.__FLOW__) ||
    (typeof g.__SENTINEL_FLOW__ !== "undefined" && g.__SENTINEL_FLOW__) || "oauth_create_account");
  var _isCheckoutFlow = /checkout|payment|approval|billing/i.test(_flowName);

  function skewedBetween(lo, hi, centerFrac) {
    var u = 0;
    for (var i = 0; i < 3; i++) u += Math.random();
    u /= 3;
    var warped = Math.pow(u, centerFrac);
    var val = lo + warped * (hi - lo);
    if (val < lo) val = lo;
    if (val > hi) val = hi;
    return Math.round(val * 10) / 10;
  }

  var _dwell = _isCheckoutFlow
    ? skewedBetween(5200, 7800, 1.35)
    : skewedBetween(6800, 9800, 1.35);
  var _t0 = Date.now();
  var _hostT0 = _hostNow ? _hostNow() : _t0;
  var _perfBase = (typeof g.__SENTINEL_PERF__ === "number" && g.__SENTINEL_PERF__ > _dwell + 640)
    ? g.__SENTINEL_PERF__
    : Math.round(_dwell + 720 + Math.random() * 880);
  var _lastPerfNow = _perfBase;

  var _fetchS = 2.5 + Math.random() * 4.5,
      _dnsS = _fetchS + Math.random() * 3.0,
      _dnsE = _dnsS + 1.0 + Math.random() * 10.0,
      _connS = _dnsE + Math.random() * 2.0,
      _secS = _connS + 2.0 + Math.random() * 10.0,
      _connE = _secS + 8.0 + Math.random() * 18.0,
      _rS = _connE + 1.0 + Math.random() * 8.0,
      _resSt = _rS + 45.0 + Math.random() * 55.0,
      _resE = _resSt + 18.0 + Math.random() * 35.0,
      _domI = _resE + 40.0 + Math.random() * 55.0,
      _dclS = _domI + 0.5 + Math.random() * 3.0,
      _dclE = _dclS + 7.5 + Math.random() * 23.0,
      _domC = _dclE + 80.0 + Math.random() * 65.0,
      _loadS = _domC + 0.1 + Math.random() * 0.5,
      _loadE = _loadS + 0.3 + Math.random() * 3.0,
      _navDur = _loadE + 0.1 + Math.random() * 0.4;
  var _encSize = 12500 + Math.round(Math.random() * 13000);
  var _xferSize = _encSize + 420 + Math.round(Math.random() * 580);
  var _decSize = Math.round(_encSize * (2.6 + Math.random() * 1.4));

  var _navEntry = tagObj("PerformanceNavigationTiming", {
    name: _pageUrl, entryType: "navigation", startTime: 0,
    duration: _navDur, type: "navigate",
    initiatorType: "navigation", nextHopProtocol: "h2", redirectCount: 0,
    unloadEventStart: 0, unloadEventEnd: 0,
    redirectStart: 0, redirectEnd: 0,
    workerStart: 0,
    fetchStart: _fetchS,
    domainLookupStart: _dnsS, domainLookupEnd: _dnsE,
    connectStart: _connS, secureConnectionStart: _secS, connectEnd: _connE,
    requestStart: _rS, responseStart: _resSt, responseEnd: _resE,
    responseStatus: 200,
    domInteractive: _domI,
    domContentLoadedEventStart: _dclS, domContentLoadedEventEnd: _dclE,
    domComplete: _domC,
    loadEventStart: _loadS, loadEventEnd: _loadE,
    transferSize: _xferSize, encodedBodySize: _encSize, decodedBodySize: _decSize,
    serverTiming: []
  });

  var _sdkUrlStr = String(g.__SDK_URL__ || "https://chatgpt.com/sentinel/20260810913b/sdk.js");
  var _sdkResStart = _resSt + 12 + Math.random() * 18;
  var _sdkReqStart = _sdkResStart + 1 + Math.random() * 4;
  var _sdkRespStart = _sdkReqStart + 14 + Math.random() * 20;
  var _sdkResEnd = Math.max(_sdkRespStart + 2, Math.min(_domC - 4, _sdkRespStart + 10 + Math.random() * 18));
  var _sdkEncSize = 9800 + Math.round(Math.random() * 1800);
  var _sdkXferSize = _sdkEncSize + 360 + Math.round(Math.random() * 300);
  var _sdkDecSize = Math.round(_sdkEncSize * (3.1 + Math.random() * 0.5));
  var _sdkResEntry = tagObj("PerformanceResourceTiming", {
    name: _sdkUrlStr, entryType: "resource", startTime: _sdkResStart,
    duration: _sdkResEnd - _sdkResStart,
    initiatorType: "script", nextHopProtocol: "h2",
    workerStart: 0, redirectStart: 0, redirectEnd: 0,
    fetchStart: _sdkResStart,
    domainLookupStart: _sdkResStart, domainLookupEnd: _sdkResStart,
    connectStart: _sdkResStart, connectEnd: _sdkResStart,
    secureConnectionStart: _sdkResStart,
    requestStart: _sdkReqStart, responseStart: _sdkRespStart, responseEnd: _sdkResEnd,
    responseStatus: 200,
    transferSize: _sdkXferSize, encodedBodySize: _sdkEncSize, decodedBodySize: _sdkDecSize,
    serverTiming: []
  });

  var _heapTotal = 26000000 + Math.round(Math.random() * 14000000);
  var _heapUsed = Math.round(_heapTotal * (0.55 + Math.random() * 0.3));
  var _perfMemory = tagObj("MemoryInfo", {
    jsHeapSizeLimit: 4294705152,
    totalJSHeapSize: _heapTotal,
    usedJSHeapSize: _heapUsed
  });
  var _perfEventCounts = new Map();

  var PerformanceProto = {};
  defineInterface("Performance", PerformanceProto, EventTargetProto);
  var isPerformance = function (self) { return PerformanceProto.isPrototypeOf(self); };
  var _perfTimeOrigin = _t0 - _perfBase;
  var _perfTiming = tagObj("PerformanceTiming", {
    navigationStart: Math.round(_perfTimeOrigin),
    unloadEventStart: 0,
    unloadEventEnd: 0,
    redirectStart: 0,
    redirectEnd: 0,
    fetchStart: Math.round(_perfTimeOrigin + _fetchS),
    domainLookupStart: Math.round(_perfTimeOrigin + _dnsS),
    domainLookupEnd: Math.round(_perfTimeOrigin + _dnsE),
    connectStart: Math.round(_perfTimeOrigin + _connS),
    secureConnectionStart: Math.round(_perfTimeOrigin + _secS),
    connectEnd: Math.round(_perfTimeOrigin + _connE),
    requestStart: Math.round(_perfTimeOrigin + _rS),
    responseStart: Math.round(_perfTimeOrigin + _resSt),
    responseEnd: Math.round(_perfTimeOrigin + _resE),
    domLoading: Math.round(_perfTimeOrigin + _resSt + (_resE - _resSt) * 0.5),
    domInteractive: Math.round(_perfTimeOrigin + _domI),
    domContentLoadedEventStart: Math.round(_perfTimeOrigin + _dclS),
    domContentLoadedEventEnd: Math.round(_perfTimeOrigin + _dclE),
    domComplete: Math.round(_perfTimeOrigin + _domC),
    loadEventStart: Math.round(_perfTimeOrigin + _loadS),
    loadEventEnd: Math.round(_perfTimeOrigin + _loadE)
  });
  defineStaticGetter(PerformanceProto, "timeOrigin", _perfTimeOrigin, isPerformance);
  defineStaticGetter(PerformanceProto, "timing", _perfTiming, isPerformance);
  defineStaticGetter(PerformanceProto, "navigation", tagObj("PerformanceNavigation", { type: 0, redirectCount: 0 }), isPerformance);
  defineStaticGetter(PerformanceProto, "memory", _perfMemory, isPerformance);
  defineStaticGetter(PerformanceProto, "eventCounts", _perfEventCounts, isPerformance);
  defineStaticGetter(PerformanceProto, "interactionCount", 0, isPerformance);
  var _perfBufHandler = null;
  defineAccessor(PerformanceProto, "onresourcetimingbufferfull",
    function () { return _perfBufHandler; },
    function (v) { _perfBufHandler = v; },
    isPerformance);

  defineMethod(PerformanceProto, "now", function () {
    var elapsed = _hostNow ? Math.max(0, _hostNow() - _hostT0) : Math.max(0, Date.now() - _t0);
    var candidate = Math.floor((_perfBase + elapsed) * 10) / 10;
    if (candidate < _lastPerfNow) candidate = _lastPerfNow;
    _lastPerfNow = candidate;
    return candidate;
  }, isPerformance);
  defineMethod(PerformanceProto, "getEntries", function () { return [_navEntry, _sdkResEntry]; }, isPerformance);
  defineMethod(PerformanceProto, "getEntriesByType", function (t) {
    if (t === "navigation") return [_navEntry];
    if (t === "resource") return [_sdkResEntry];
    return [];
  }, isPerformance);
  defineMethod(PerformanceProto, "getEntriesByName", function (n) {
    var out = [];
    if (_navEntry.name === n) out.push(_navEntry);
    if (_sdkResEntry.name === n) out.push(_sdkResEntry);
    return out;
  }, isPerformance);
  var _perfVoid = ["mark", "measure", "clearMarks", "clearMeasures", "clearResourceTimings", "setResourceTimingBufferSize"];
  for (var _pvi = 0; _pvi < _perfVoid.length; _pvi++) {
    defineMethod(PerformanceProto, _perfVoid[_pvi], function () {}, isPerformance);
  }
  defineMethod(PerformanceProto, "toJSON", function () {
    return { timeOrigin: _perfTimeOrigin };
  }, isPerformance);
  g.performance = Object.create(PerformanceProto);

  // ---- 与 sentinel_bridge.js 的 PoW 生成器共享同一份会话标识与启动基准 ----
  if (!g.__SENTINEL_SID__) {
    g.__SENTINEL_SID__ = (g.crypto && typeof g.crypto.randomUUID === "function")
      ? g.crypto.randomUUID()
      : "00000000-0000-4000-8000-000000000000";
  }
  if (typeof g.__SENTINEL_PERF__ !== "number" || g.__SENTINEL_PERF__ < _dwell + 640) {
    g.__SENTINEL_PERF__ = _perfBase;
  }
  if (typeof g.__LOAD_TS__ !== "number") {
    g.__LOAD_TS__ = _perfTimeOrigin;
  }
  g.__SENTINEL_HEAP_LIMIT__ = 4294705152;
  concealOwn(g, "__SENTINEL_SID__");
  concealOwn(g, "__SENTINEL_PERF__");
  concealOwn(g, "__LOAD_TS__");
  concealOwn(g, "__SENTINEL_HEAP_LIMIT__");
  concealOwn(g, "__TZ_DISPLAY_NAME__");

  // ---- Storage（localStorage / sessionStorage）----
  function makeStorage(initial) {
    var st = tagObj("Storage", {});
    if (initial) {
      var ks = Object.keys(initial);
      for (var i = 0; i < ks.length; i++) st[ks[i]] = String(initial[ks[i]]);
    }
    function def(name, fn) {
      Object.defineProperty(st, name, {
        value: makeNonCtor(fn, name),
        enumerable: false,
        writable: true,
        configurable: true
      });
    }
    def("getItem", function (k) { return Object.prototype.hasOwnProperty.call(st, k) ? st[k] : null; });
    def("setItem", function (k, v) { st[k] = "" + v; });
    def("removeItem", function (k) { delete st[k]; });
    def("clear", function () { Object.keys(st).forEach(function (k) { delete st[k]; }); });
    def("key", function (i) { return Object.keys(st)[i] || null; });
    Object.defineProperty(st, "length", {
      get: makeNonCtor(function () { return Object.keys(st).length; }, "get length"),
      enumerable: false,
      configurable: true
    });
    return st;
  }
  (function () {
    function digits(n) { var s = ""; for (var i = 0; i < n; i++) s += (Math.random() * 10) | 0; if (s[0] === "0") s = "1" + s.slice(1); return s; }
    function hex(n) { var h = "0123456789abcdef", s = ""; for (var i = 0; i < n; i++) s += h[(Math.random() * 16) | 0]; return s; }
    var stable = digits(9);
    var seed = {};
    var modTs = _t0 - Math.round(120000 + Math.random() * 86400000);
    var evalBlob = function (t) {
      return JSON.stringify({ feature_gates: {}, dynamic_configs: {}, layer_configs: {}, time: t, has_updates: true });
    };
    seed["statsig.cached.evaluations." + digits(8)] = evalBlob(modTs);
    seed["statsig.cached.evaluations." + digits(9)] = evalBlob(modTs + 1400);
    seed["statsig.cached.evaluations." + digits(10)] = evalBlob(modTs + 3200);
    seed["statsig.last_modified_time.evaluations"] = String(modTs);
    seed[hex(16)] = g.crypto.randomUUID();
    seed["statsig.cached.evaluations." + digits(10)] = evalBlob(modTs + 5100);
    seed["statsig.session_id." + stable] = g.crypto.randomUUID();
    seed["statsig.cached.evaluations." + digits(10)] = evalBlob(modTs + 7400);
    seed["statsig.cached.evaluations." + digits(10)] = evalBlob(modTs + 9100);
    seed["statsig.cached.evaluations." + digits(10)] = evalBlob(modTs + 11200);
    seed["statsig.stable_id." + stable] = g.crypto.randomUUID();
    if (g.__SEED_DID_KEY__ && g.__SEED_DID_VAL__) seed[g.__SEED_DID_KEY__] = g.__SEED_DID_VAL__;
    g.localStorage = makeStorage(seed);
    g.sessionStorage = makeStorage(null);
  })();

  // ---- Node / Document / HTMLDocument ----
  var NodeProto = {};
  defineInterface("Node", NodeProto, EventTargetProto);
  var DocumentProto = {};
  defineInterface("Document", DocumentProto, NodeProto);
  var HTMLDocumentProto = {};
  defineInterface("HTMLDocument", HTMLDocumentProto, DocumentProto);
  var isDocument = function (self) { return HTMLDocumentProto.isPrototypeOf(self); };

  var sdkScript = tagObj("HTMLScriptElement", {
    src: _sdkUrlStr,
    getAttribute: makeNonCtor(function () { return null; }, "getAttribute"),
    type: "text/javascript"
  });
  var docElement = tagObj("HTMLHtmlElement", {
    getAttribute: makeNonCtor(function () { return null; }, "getAttribute"),
    tagName: "HTML",
    lang: _navLanguage
  });
  var _cookie = g.__COOKIE_HEADER__ || ((g.__SEED_DID_KEY__ && g.__SEED_DID_VAL__) ? (g.__SEED_DID_KEY__ + "=" + g.__SEED_DID_VAL__) : "");

  function makeStyle() {
    var st = tagObj("CSSStyleDeclaration", {});
    st.setProperty = makeNonCtor(function (k, v) { st[k] = "" + v; }, "setProperty");
    st.getPropertyValue = makeNonCtor(function (k) { return Object.prototype.hasOwnProperty.call(st, k) ? st[k] : ""; }, "getPropertyValue");
    st.removeProperty = makeNonCtor(function (k) { var v = st[k]; delete st[k]; return v; }, "removeProperty");
    st.cssText = "";
    return st;
  }

  function fontScaleFor(style) {
    var fam = String((style && (style.fontFamily || style["font-family"])) || "sans-serif").toLowerCase();
    var primary = fam.split(",")[0].replace(/['"]/g, "").trim();
    var known = {
      "arial": 0.548, "helvetica": 0.546, "helvetica neue": 0.551, "times new roman": 0.492,
      "times": 0.491, "courier new": 0.600, "courier": 0.600, "georgia": 0.531,
      "verdana": 0.618, "tahoma": 0.542, "trebuchet ms": 0.534, "impact": 0.489,
      "comic sans ms": 0.584, "segoe ui": 0.526, "consolas": 0.550,
      "monospace": 0.600, "serif": 0.495, "sans-serif": 0.544
    };
    var factor = Object.prototype.hasOwnProperty.call(known, primary) ? known[primary] : 0;
    if (!factor) {
      var fallback = fam.split(",").slice(-1)[0].replace(/['"]/g, "").trim();
      factor = Object.prototype.hasOwnProperty.call(known, fallback) ? known[fallback] : 0.544;
    }
    var sizePx = parseFloat(String((style && (style.fontSize || style["font-size"])) || "16"));
    if (!isFinite(sizePx) || sizePx <= 0) sizePx = 16;
    return { factor: factor, sizePx: sizePx };
  }

  var _TAG_TO_IFACE = {
    BODY: "HTMLBodyElement", HEAD: "HTMLHeadElement", HTML: "HTMLHtmlElement",
    DIV: "HTMLDivElement", SPAN: "HTMLSpanElement", SCRIPT: "HTMLScriptElement",
    INPUT: "HTMLInputElement", CANVAS: "HTMLCanvasElement", IFRAME: "HTMLIFrameElement",
    A: "HTMLAnchorElement", P: "HTMLParagraphElement", FORM: "HTMLFormElement",
    BUTTON: "HTMLButtonElement", IMG: "HTMLImageElement", STYLE: "HTMLStyleElement",
    LINK: "HTMLLinkElement", META: "HTMLMetaElement"
  };

  function makeElement(tag) {
    var upper = ("" + tag).toUpperCase();
    var el = tagObj(_TAG_TO_IFACE[upper] || "HTMLElement", {
      tagName: upper,
      nodeName: upper,
      nodeType: 1,
      style: makeStyle(),
      innerText: "", textContent: "", innerHTML: "",
      className: "", id: "", ariaHidden: null, hidden: false,
      clientWidth: 0, clientHeight: 0, offsetWidth: 0, offsetHeight: 0,
      scrollWidth: 0, scrollHeight: 0, offsetLeft: 0, offsetTop: 0,
      children: [], childNodes: [], parentNode: null,
      setAttribute: nativeFn("setAttribute"),
      getAttribute: makeNonCtor(function () { return null; }, "getAttribute"),
      removeAttribute: nativeFn("removeAttribute"),
      hasAttribute: makeNonCtor(function () { return false; }, "hasAttribute"),
      addEventListener: nativeFn("addEventListener"),
      removeEventListener: nativeFn("removeEventListener"),
      appendChild: makeNonCtor(function (c) { this.children.push(c); this.childNodes.push(c); if (c) c.parentNode = this; return c; }, "appendChild"),
      removeChild: makeNonCtor(function (c) {
        var i = this.children.indexOf(c);
        if (i >= 0) { this.children.splice(i, 1); this.childNodes.splice(i, 1); }
        if (c) c.parentNode = null;
        return c;
      }, "removeChild"),
      insertBefore: makeNonCtor(function (c) { this.children.push(c); this.childNodes.push(c); return c; }, "insertBefore"),
      remove: nativeFn("remove"),
      querySelector: makeNonCtor(function () { return null; }, "querySelector"),
      querySelectorAll: makeNonCtor(function () { return []; }, "querySelectorAll"),
      getClientRects: makeNonCtor(function () { return [this.getBoundingClientRect()]; }, "getClientRects"),
      getBoundingClientRect: makeNonCtor(function () {
        var t = "" + (this.innerText || this.textContent || "");
        if (!t.length) return { x: 8, y: 8, width: 0, height: 0, top: 8, right: 8, bottom: 8, left: 8 };
        var fm = fontScaleFor(this.style);
        var w = Math.round((t.length * fm.sizePx * fm.factor + 0.421875) * 1000000) / 1000000;
        var h = Math.round((fm.sizePx * 1.15625) * 1000) / 1000;
        return { x: 8, y: 8, width: w, height: h, top: 8, right: 8 + w, bottom: 8 + h, left: 8 };
      }, "getBoundingClientRect")
    });
    return el;
  }

  var bodyEl = makeElement("body");
  var headEl = makeElement("head");
  var _docTitle = "";
  var _docDir = "";
  var _docDesignMode = "off";
  var _docDomain = _locUrl.hostname;
  var _docImpl = tagObj("DOMImplementation", {
    createHTMLDocument: nativeFn("createHTMLDocument"),
    createDocument: nativeFn("createDocument"),
    createDocumentType: nativeFn("createDocumentType"),
    hasFeature: makeNonCtor(function () { return true; }, "hasFeature")
  });
  var _docTimeline = tagObj("DocumentTimeline", { currentTime: _perfBase });
  var _emptyList = Object.freeze([]);

  defineStaticGetter(NodeProto, "nodeType", 9, isDocument);
  defineStaticGetter(NodeProto, "nodeName", "#document", isDocument);
  defineStaticGetter(NodeProto, "nodeValue", null, isDocument);
  defineStaticGetter(NodeProto, "childNodes", [docElement], isDocument);
  defineStaticGetter(NodeProto, "firstChild", docElement, isDocument);
  defineStaticGetter(NodeProto, "lastChild", docElement, isDocument);
  defineStaticGetter(NodeProto, "parentNode", null, isDocument);
  defineStaticGetter(NodeProto, "parentElement", null, isDocument);
  defineStaticGetter(NodeProto, "previousSibling", null, isDocument);
  defineStaticGetter(NodeProto, "nextSibling", null, isDocument);
  defineStaticGetter(NodeProto, "ownerDocument", null, isDocument);
  defineStaticGetter(NodeProto, "isConnected", true, isDocument);
  defineAccessor(NodeProto, "baseURI", function () { return _locUrl.href; }, null, isDocument);
  defineAccessor(NodeProto, "textContent", function () { return null; }, function () {}, isDocument);
  var _nodeMethods = [
    "appendChild", "cloneNode", "compareDocumentPosition", "contains", "getRootNode",
    "hasChildNodes", "insertBefore", "isDefaultNamespace", "isEqualNode", "isSameNode",
    "lookupNamespaceURI", "lookupPrefix", "normalize", "removeChild", "replaceChild"
  ];
  for (var _nmi2 = 0; _nmi2 < _nodeMethods.length; _nmi2++) {
    defineMethod(NodeProto, _nodeMethods[_nmi2], function () {}, isDocument);
  }

  defineStaticGetter(DocumentProto, "implementation", _docImpl, isDocument);
  defineAccessor(DocumentProto, "URL", function () { return _locUrl.href; }, null, isDocument);
  defineAccessor(DocumentProto, "documentURI", function () { return _locUrl.href; }, null, isDocument);
  defineStaticGetter(DocumentProto, "compatMode", "CSS1Compat", isDocument);
  defineStaticGetter(DocumentProto, "characterSet", "UTF-8", isDocument);
  defineStaticGetter(DocumentProto, "charset", "UTF-8", isDocument);
  defineStaticGetter(DocumentProto, "inputEncoding", "UTF-8", isDocument);
  defineStaticGetter(DocumentProto, "contentType", "text/html", isDocument);
  defineStaticGetter(DocumentProto, "doctype", tagObj("DocumentType", { name: "html", publicId: "", systemId: "" }), isDocument);
  defineStaticGetter(DocumentProto, "documentElement", docElement, isDocument);
  defineStaticGetter(DocumentProto, "head", headEl, isDocument);
  defineStaticGetter(DocumentProto, "body", bodyEl, isDocument);
  defineStaticGetter(DocumentProto, "scripts", [sdkScript], isDocument);
  defineStaticGetter(DocumentProto, "currentScript", null, isDocument);
  defineStaticGetter(DocumentProto, "defaultView", g, isDocument);
  defineStaticGetter(DocumentProto, "referrer", "", isDocument);
  defineAccessor(DocumentProto, "domain", function () { return _docDomain; }, function (v) { _docDomain = String(v); }, isDocument);
  defineAccessor(DocumentProto, "title", function () { return _docTitle; }, function (v) { _docTitle = "" + v; }, isDocument);
  defineAccessor(DocumentProto, "dir", function () { return _docDir; }, function (v) { _docDir = "" + v; }, isDocument);
  defineAccessor(DocumentProto, "designMode", function () { return _docDesignMode; }, function (v) { _docDesignMode = "" + v; }, isDocument);
  defineStaticGetter(DocumentProto, "readyState", "complete", isDocument);
  defineStaticGetter(DocumentProto, "hidden", false, isDocument);
  defineStaticGetter(DocumentProto, "visibilityState", "visible", isDocument);
  defineStaticGetter(DocumentProto, "wasDiscarded", false, isDocument);
  defineStaticGetter(DocumentProto, "activeElement", bodyEl, isDocument);
  defineStaticGetter(DocumentProto, "scrollingElement", docElement, isDocument);
  defineStaticGetter(DocumentProto, "children", [docElement], isDocument);
  defineStaticGetter(DocumentProto, "firstElementChild", docElement, isDocument);
  defineStaticGetter(DocumentProto, "lastElementChild", docElement, isDocument);
  defineStaticGetter(DocumentProto, "childElementCount", 1, isDocument);
  defineStaticGetter(DocumentProto, "all", _emptyList, isDocument);
  defineStaticGetter(DocumentProto, "anchors", _emptyList, isDocument);
  defineStaticGetter(DocumentProto, "applets", _emptyList, isDocument);
  defineStaticGetter(DocumentProto, "embeds", _emptyList, isDocument);
  defineStaticGetter(DocumentProto, "forms", _emptyList, isDocument);
  defineStaticGetter(DocumentProto, "images", _emptyList, isDocument);
  defineStaticGetter(DocumentProto, "links", _emptyList, isDocument);
  defineStaticGetter(DocumentProto, "plugins", _emptyList, isDocument);
  defineStaticGetter(DocumentProto, "styleSheets", _emptyList, isDocument);
  defineStaticGetter(DocumentProto, "timeline", _docTimeline, isDocument);
  defineStaticGetter(DocumentProto, "lastModified", "10/29/2026 09:12:00", isDocument);
  defineStaticGetter(DocumentProto, "fullscreen", false, isDocument);
  defineStaticGetter(DocumentProto, "fullscreenEnabled", true, isDocument);
  defineStaticGetter(DocumentProto, "pictureInPictureElement", null, isDocument);
  defineStaticGetter(DocumentProto, "pictureInPictureEnabled", true, isDocument);
  defineStaticGetter(DocumentProto, "pointerLockElement", null, isDocument);
  defineStaticGetter(DocumentProto, "webkitCurrentFullScreenElement", null, isDocument);
  defineStaticGetter(DocumentProto, "webkitFullscreenElement", null, isDocument);
  defineStaticGetter(DocumentProto, "webkitFullscreenEnabled", true, isDocument);
  defineStaticGetter(DocumentProto, "webkitHidden", false, isDocument);
  defineStaticGetter(DocumentProto, "webkitIsFullScreen", false, isDocument);
  defineStaticGetter(DocumentProto, "webkitVisibilityState", "visible", isDocument);
  defineAccessor(DocumentProto, "cookie", function () { return _cookie; }, function (v) { _cookie = "" + v; }, isDocument);

  defineMethod(DocumentProto, "getElementsByTagName", function (t) {
    t = String(t).toLowerCase();
    if (t === "script") return [sdkScript];
    if (t === "body") return [bodyEl];
    if (t === "head") return [headEl];
    return [];
  }, isDocument);
  defineMethod(DocumentProto, "getElementById", function () { return null; }, isDocument);
  defineMethod(DocumentProto, "querySelector", function () { return null; }, isDocument);
  defineMethod(DocumentProto, "querySelectorAll", function () { return []; }, isDocument);
  defineMethod(DocumentProto, "createElement", function (tag) { return makeElement(tag || "div"); }, isDocument);
  defineMethod(DocumentProto, "createTextNode", function (t) { return tagObj("Text", { nodeType: 3, textContent: "" + t }); }, isDocument);

  var _docExtraImpls = {
    hasFocus: function () { return true; },
    getElementsByClassName: function () { return []; },
    getElementsByName: function () { return []; },
    getElementsByTagNameNS: function () { return []; },
    elementFromPoint: function () { return bodyEl; },
    elementsFromPoint: function () { return [bodyEl, docElement]; },
    getAnimations: function () { return []; },
    hasStorageAccess: function () { return Promise.resolve(true); },
    requestStorageAccess: function () { return Promise.resolve(); }
  };
  var _docExtraMethods = [
    "open", "close", "write", "writeln", "hasFocus", "getElementsByClassName",
    "getElementsByName", "getElementsByTagNameNS", "getSelection", "elementFromPoint",
    "elementsFromPoint", "adoptNode", "importNode", "createAttribute", "createComment",
    "createDocumentFragment", "createElementNS", "createEvent", "createRange",
    "createTreeWalker", "createNodeIterator", "append", "prepend", "replaceChildren",
    "execCommand", "queryCommandEnabled", "queryCommandState", "queryCommandSupported",
    "queryCommandValue", "exitFullscreen", "exitPictureInPicture", "exitPointerLock",
    "getAnimations", "hasStorageAccess", "requestStorageAccess"
  ];
  for (var _dmi = 0; _dmi < _docExtraMethods.length; _dmi++) {
    var _dm = _docExtraMethods[_dmi];
    defineMethod(DocumentProto, _dm, _docExtraImpls[_dm] || function () {}, isDocument);
  }

  var _eventHandlers = [
    "onabort", "onafterprint", "onanimationcancel", "onanimationend", "onanimationiteration",
    "onanimationstart", "onappinstalled", "onauxclick", "onbeforeinput", "onbeforeinstallprompt",
    "onbeforematch", "onbeforeprint", "onbeforetoggle", "onbeforeunload", "onbeforexrselect",
    "onblur", "oncancel", "oncanplay", "oncanplaythrough", "onchange", "onclick", "onclose",
    "oncommand", "oncontentvisibilityautostatechange", "oncontextlost", "oncontextmenu",
    "oncontextrestored", "oncopy", "oncuechange", "oncut", "ondblclick", "ondevicemotion",
    "ondeviceorientation", "ondeviceorientationabsolute", "ondrag", "ondragend", "ondragenter",
    "ondragleave", "ondragover", "ondragstart", "ondrop", "ondurationchange", "onemptied",
    "onended", "onerror", "onfocus", "onformdata", "ongamepadconnected", "ongamepaddisconnected",
    "ongotpointercapture", "onhashchange", "oninput", "oninvalid", "onkeydown", "onkeypress",
    "onkeyup", "onlanguagechange", "onload", "onloadeddata", "onloadedmetadata", "onloadstart",
    "onlostpointercapture", "onmessage", "onmessageerror", "onmousedown", "onmouseenter",
    "onmouseleave", "onmousemove", "onmouseout", "onmouseover", "onmouseup", "onmousewheel",
    "onoffline", "ononline", "onpagehide", "onpagereveal", "onpageshow", "onpageswap",
    "onpaste", "onpause", "onplay", "onplaying", "onpointercancel", "onpointerdown",
    "onpointerenter", "onpointerleave", "onpointermove", "onpointerout", "onpointerover",
    "onpointerrawupdate", "onpointerup", "onpopstate", "onprogress", "onratechange",
    "onrejectionhandled", "onreset", "onresize", "onscroll", "onscrollend",
    "onscrollsnapchange", "onscrollsnapchanging", "onsearch", "onsecuritypolicyviolation",
    "onseeked", "onseeking", "onselect", "onselectionchange", "onselectstart", "onslotchange",
    "onstalled", "onstorage", "onsubmit", "onsuspend", "ontimeupdate", "ontoggle",
    "ontransitioncancel", "ontransitionend", "ontransitionrun", "ontransitionstart",
    "onunhandledrejection", "onunload", "onvolumechange", "onwaiting", "onwebkitanimationend",
    "onwebkitanimationiteration", "onwebkitanimationstart", "onwebkittransitionend", "onwheel"
  ];
  var _docHandlerStore = Object.create(null);
  _eventHandlers.forEach(function (hname) {
    _docHandlerStore[hname] = null;
    defineAccessor(DocumentProto, hname,
      function () { return _docHandlerStore[hname]; },
      function (v) { _docHandlerStore[hname] = v; },
      isDocument);
  });

  var docInstance = Object.create(HTMLDocumentProto);
  try {
    Object.defineProperty(docInstance, "location", {
      get: makeNonCtor(function () { return g.location; }, "get location"),
      set: makeNonCtor(function (v) { _setLocHref(v); }, "set location"),
      enumerable: true,
      configurable: false
    });
  } catch (e) {
    docInstance.location = g.location;
  }
  g.document = docInstance;

  g.__reactRouterContext = { state: { loaderData: {} } };

  g.addEventListener = nativeFn("addEventListener");
  g.removeEventListener = nativeFn("removeEventListener");
  g.dispatchEvent = makeNonCtor(function () { return true; }, "dispatchEvent");
  g.matchMedia = makeNonCtor(function () {
    return tagObj("MediaQueryList", {
      matches: false,
      addListener: nativeFn("addListener"),
      removeListener: nativeFn("removeListener"),
      addEventListener: nativeFn("addEventListener"),
      removeEventListener: nativeFn("removeEventListener")
    });
  }, "matchMedia");
  if (typeof g.console === "undefined") {
    g.console = { log: nativeFn("log"), warn: nativeFn("warn"), error: nativeFn("error") };
  }

  g.chrome = { runtime: { id: undefined, connect: nativeFn("connect"), sendMessage: nativeFn("sendMessage") }, csi: makeNonCtor(function () { return {}; }, "csi"), loadTimes: makeNonCtor(function () { return {}; }, "loadTimes") };
  g.caches = tagObj("CacheStorage", { open: makeNonCtor(function () { return Promise.resolve({ match: nativePromiseFn("match") }); }, "open"), keys: makeNonCtor(function () { return Promise.resolve([]); }, "keys"), has: makeNonCtor(function () { return Promise.resolve(false); }, "has"), match: nativePromiseFn("match") });
  g.indexedDB = tagObj("IDBFactory", { open: makeNonCtor(function () { return { result: null, onerror: null, onsuccess: null }; }, "open"), databases: makeNonCtor(function () { return Promise.resolve([]); }, "databases") });
  g.speechSynthesis = tagObj("SpeechSynthesis", { getVoices: makeNonCtor(function () { return []; }, "getVoices"), speak: nativeFn("speak"), cancel: nativeFn("cancel"), pause: nativeFn("pause"), resume: nativeFn("resume") });
  g.visualViewport = tagObj("VisualViewport", { width: _innerW, height: _innerH, offsetLeft: 0, offsetTop: 0, scale: 1, pageLeft: 0, pageTop: 0, addEventListener: nativeFn("addEventListener") });
  Object.defineProperty(g, "origin", {
    get: makeNonCtor(function () { return _locUrl.origin; }, "get origin"),
    enumerable: true,
    configurable: true
  });
  g.isSecureContext = true;
  g.crossOriginIsolated = false;
  g.originAgentCluster = false;
  g.credentialless = false;
  g.scheduler = tagObj("Scheduler", { postTask: makeNonCtor(function (fn) { return Promise.resolve(fn()); }, "postTask") });
  g.trustedTypes = tagObj("TrustedTypePolicyFactory", { createPolicy: makeNonCtor(function () { return { createHTML: makeNonCtor(function (s) { return s; }, "createHTML") }; }, "createPolicy") });
  g.cookieStore = tagObj("CookieStore", { get: makeNonCtor(function () { return Promise.resolve(null); }, "get"), getAll: makeNonCtor(function () { return Promise.resolve([]); }, "getAll"), set: nativePromiseFn("set") });

  // WebIDL 接口构造器在真 Chrome 中均为 enumerable: false，不进入 Object.keys(window) / p[12]。
  function defGlobalCtor(name, fallback) {
    var val = g[name] || markNative(fallback, name);
    try {
      Object.defineProperty(g, name, {
        value: val,
        writable: true,
        enumerable: false,
        configurable: true
      });
    } catch (e) {
      g[name] = val;
      concealOwn(g, name);
    }
  }
  concealOwn(g, "TextEncoder");
  concealOwn(g, "TextDecoder");
  defGlobalCtor("AbortController", function () { this.signal = { aborted: false }; this.abort = nativeFn("abort"); });
  var _AbortSignalCtor = markNative(function () { throw new TypeError("Illegal constructor"); }, "AbortSignal");
  _AbortSignalCtor.abort = makeNonCtor(function () { return { aborted: true }; }, "abort");
  _AbortSignalCtor.timeout = makeNonCtor(function () { return { aborted: false }; }, "timeout");
  Object.defineProperty(g, "AbortSignal", { value: _AbortSignalCtor, writable: true, enumerable: false, configurable: true });
  defGlobalCtor("URL", function (u) { this.href = u; this.toString = makeNonCtor(function () { return u; }, "toString"); });
  defGlobalCtor("URLSearchParams", function () { this.get = makeNonCtor(function () { return null; }, "get"); this.set = nativeFn("set"); this.toString = makeNonCtor(function () { return ""; }, "toString"); });
  defGlobalCtor("Blob", function () { this.size = 0; this.type = ""; });
  defGlobalCtor("File", function () { this.name = ""; this.size = 0; });
  defGlobalCtor("FileReader", function () { this.readAsText = nativeFn("readAsText"); this.readAsArrayBuffer = nativeFn("readAsArrayBuffer"); });
  defGlobalCtor("FormData", function () { this.append = nativeFn("append"); this.get = makeNonCtor(function () { return null; }, "get"); });
  defGlobalCtor("Headers", function () { this.get = makeNonCtor(function () { return null; }, "get"); this.set = nativeFn("set"); });
  defGlobalCtor("Request", function (u) { this.url = u; this.method = "GET"; });
  defGlobalCtor("Response", function () { this.ok = true; this.status = 200; this.json = makeNonCtor(function () { return Promise.resolve({}); }, "json"); });
  defGlobalCtor("MutationObserver", function () { this.observe = nativeFn("observe"); this.disconnect = nativeFn("disconnect"); this.takeRecords = makeNonCtor(function () { return []; }, "takeRecords"); });
  defGlobalCtor("IntersectionObserver", function () { this.observe = nativeFn("observe"); this.disconnect = nativeFn("disconnect"); this.unobserve = nativeFn("unobserve"); });
  defGlobalCtor("ResizeObserver", function () { this.observe = nativeFn("observe"); this.disconnect = nativeFn("disconnect"); this.unobserve = nativeFn("unobserve"); });
  defGlobalCtor("PerformanceObserver", function () { this.observe = nativeFn("observe"); this.disconnect = nativeFn("disconnect"); });
  defGlobalCtor("MessageChannel", function () { this.port1 = { postMessage: nativeFn("postMessage"), onmessage: null }; this.port2 = { postMessage: nativeFn("postMessage"), onmessage: null }; });
  defGlobalCtor("BroadcastChannel", function () { this.postMessage = nativeFn("postMessage"); this.close = nativeFn("close"); });
  defGlobalCtor("Worker", function () { this.postMessage = nativeFn("postMessage"); this.terminate = nativeFn("terminate"); });
  defGlobalCtor("SharedWorker", function () { this.port = { postMessage: nativeFn("postMessage") }; });
  defGlobalCtor("WebSocket", function () { this.send = nativeFn("send"); this.close = nativeFn("close"); this.readyState = 3; });
  defGlobalCtor("XMLHttpRequest", function () { this.open = nativeFn("open"); this.send = nativeFn("send"); this.setRequestHeader = nativeFn("setRequestHeader"); this.status = 0; this.responseText = ""; });
  defGlobalCtor("Image", function () { this.src = ""; this.onload = null; this.onerror = null; });
  defGlobalCtor("HTMLElement", function () { throw new TypeError("Illegal constructor"); });
  defGlobalCtor("Event", function (t) { this.type = t; this.bubbles = false; this.cancelable = false; });
  defGlobalCtor("CustomEvent", function (t) { this.type = t; this.detail = null; });
  defGlobalCtor("DOMParser", function () { this.parseFromString = makeNonCtor(function () { return { documentElement: g.document.documentElement }; }, "parseFromString"); });
  g.getComputedStyle = g.getComputedStyle || makeNonCtor(function () { return tagObj("CSSStyleDeclaration", { getPropertyValue: makeNonCtor(function () { return ""; }, "getPropertyValue") }); }, "getComputedStyle");
  g.requestAnimationFrame = g.requestAnimationFrame || makeNonCtor(function (fn) { return setTimeout(fn, 16); }, "requestAnimationFrame");
  g.cancelAnimationFrame = g.cancelAnimationFrame || makeNonCtor(function (id) { clearTimeout(id); }, "cancelAnimationFrame");
  g.queueMicrotask = g.queueMicrotask || makeNonCtor(function (fn) { Promise.resolve().then(fn); }, "queueMicrotask");
  g.structuredClone = g.structuredClone || makeNonCtor(function (v) { return JSON.parse(JSON.stringify(v)); }, "structuredClone");

  // 补齐核心 Window WebIDL 全局属性，确保 Object.keys(window).slice(0, N) 对齐真 Chrome。
  g.name = "";
  g.customElements = tagObj("CustomElementRegistry", {
    define: nativeFn("define"),
    get: makeNonCtor(function () { return undefined; }, "get"),
    getName: makeNonCtor(function () { return null; }, "getName"),
    upgrade: nativeFn("upgrade"),
    whenDefined: nativePromiseFn("whenDefined")
  });
  g.navigation = tagObj("Navigation", {
    currentEntry: tagObj("NavigationHistoryEntry", { url: _pageUrl, key: "", id: "", index: 0, sameDocument: true }),
    canGoBack: false,
    canGoForward: false,
    entries: makeNonCtor(function () { return []; }, "entries"),
    addEventListener: nativeFn("addEventListener"),
    removeEventListener: nativeFn("removeEventListener")
  });
  g.locationbar = tagObj("BarProp", { visible: true });
  g.menubar = tagObj("BarProp", { visible: true });
  g.personalbar = tagObj("BarProp", { visible: true });
  g.scrollbars = tagObj("BarProp", { visible: true });
  g.statusbar = tagObj("BarProp", { visible: true });
  g.toolbar = tagObj("BarProp", { visible: true });
  g.status = "";
  g.closed = false;
  g.frames = g;
  g.length = 0;
  g.opener = null;
  g.frameElement = null;
  g.external = tagObj("External", {
    AddSearchProvider: nativeFn("AddSearchProvider"),
    IsSearchProviderInstalled: makeNonCtor(function () { return 0; }, "IsSearchProviderInstalled")
  });

  // ---- __oai_so_*：会话行为遥测累加器 ----
  (function () {
    var nowP = (g.performance && g.performance.now) ? g.performance.now() : _perfBase;
    var flow = _flowName;
    var isCheckout = _isCheckoutFlow;
    var dwell = _dwell;
    var t0 = nowP - dwell;
    var end = nowP - (40 + Math.random() * 160);

    var kd = [];
    var inputEvents = [];
    var kdSpan = 0;
    if (!isCheckout) {
      var firstNames = [
        "Anna", "Laura", "Mark", "Nina", "Peter", "Sofia", "Daniel", "Elena",
        "Marco", "Julia", "Thomas", "Clara", "Victor", "Iris", "Max", "Amy",
        "Leo", "Eva", "Lucas", "Maya", "Owen", "Chloe", "Ethan", "Nora",
        "Liam", "Zoe", "Noah", "Sara", "Adam", "Emma", "David", "Grace",
        "Henry", "Alice", "James", "Olivia", "Arthur", "Hannah", "Samuel", "Katherine"
      ];
      var lastNames = [
        "Wilson", "Bennett", "Costa", "Novak", "Silva", "Meyer", "Klein", "Duarte",
        "Fischer", "Moreau", "Keller", "Rossi", "Wu", "Lee", "Ng", "Park",
        "Chen", "Kim", "Cruz", "Reyes", "Santos", "Tan", "Lim", "Lopez",
        "Weber", "Schmidt", "Wagner", "Bauer", "Hoffman", "Richter", "Laurent", "Dubois",
        "Bernard", "Martin", "Garcia", "Martinez", "Alvarez", "Morales", "Kowalski", "Montgomery"
      ];
      var pick = function (arr) { return arr[Math.floor(Math.random() * arr.length)]; };
      var fullName = pick(firstNames) + " " + pick(lastNames);
      var bMonth = String(1 + Math.floor(Math.random() * 12)).padStart(2, "0");
      var bDay = String(1 + Math.floor(Math.random() * 28)).padStart(2, "0");
      var bYear = String(1982 + Math.floor(Math.random() * 22));
      var birthText = bMonth + "/" + bDay + "/" + bYear;

      var segments = [
        { text: fullName, pauseBefore: 0 },
        { text: birthText, pauseBefore: 160 + Math.random() * 180, tabBefore: true }
      ];
      var eventsPlan = [];
      for (var si = 0; si < segments.length; si++) {
        var seg = segments[si];
        if (seg.tabBefore) {
          eventsPlan.push({ type: "tab", gap: 110 + Math.random() * 110 });
        }
        var chars = seg.text.split("");
        var running = "";
        for (var ci = 0; ci < chars.length; ci++) {
          var ch = chars[ci];
          var isUpper = /[A-Z]/.test(ch);
          var preGap = (ci === 0 ? seg.pauseBefore : 0) + 65 + Math.random() * 85;
          if (isUpper) {
            eventsPlan.push({ type: "shift", gap: preGap });
            eventsPlan.push({ type: "char", key: ch, shiftKey: true, gap: 22 + Math.random() * 48, value: (running += ch) });
          } else {
            eventsPlan.push({ type: "char", key: ch, shiftKey: false, gap: preGap, value: (running += ch) });
          }
        }
      }
      for (var ep = 0; ep < eventsPlan.length; ep++) kdSpan += eventsPlan[ep].gap;
      var maxTypingBudget = Math.max(800, end - t0 - 1800);
      if (kdSpan > maxTypingBudget) {
        var kScale = maxTypingBudget / kdSpan;
        kdSpan = 0;
        for (var ks = 0; ks < eventsPlan.length; ks++) {
          eventsPlan[ks].gap *= kScale;
          kdSpan += eventsPlan[ks].gap;
        }
      }
      var kt = end - 200 - kdSpan;
      for (var ei = 0; ei < eventsPlan.length; ei++) {
        var ev = eventsPlan[ei];
        kt += ev.gap;
        if (ev.type === "tab") {
          kd.push({
            ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
            key: "Tab", type: "keydown", t: kt
          });
        } else if (ev.type === "shift") {
          kd.push({
            ctrlKey: false, metaKey: false, altKey: false, shiftKey: true,
            key: "Shift", type: "keydown", t: kt
          });
        } else {
          kd.push({
            ctrlKey: false, metaKey: false, altKey: false, shiftKey: ev.shiftKey,
            key: ev.key, type: "keydown", t: kt
          });
          inputEvents.push({
            type: "input",
            value: ev.value,
            inputType: "insertText",
            t: kt + 6 + Math.random() * 10
          });
        }
      }
    }

    // pointermove 事件序列：严格按视口 (_innerW × _innerH) 生成并钳制，绝不晚于 kd[0].t。
    var clamp = function (v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); };
    var pm = [];
    var marginY = Math.min(24, Math.floor(_innerH * 0.1));
    var marginX = Math.min(24, Math.floor(_innerW * 0.1));
    var sx0 = isCheckout
      ? Math.round(_innerW * (0.48 + Math.random() * 0.14))
      : Math.round(_innerW * (0.30 + Math.random() * 0.14));
    var sy0 = isCheckout
      ? Math.round(_innerH * (0.18 + Math.random() * 0.10))
      : Math.round(_innerH * (0.28 + Math.random() * 0.12));
    var tx = isCheckout
      ? Math.round(_innerW * (0.62 + Math.random() * 0.14))
      : Math.round(_innerW * (0.42 + Math.random() * 0.16));
    var ty = isCheckout
      ? Math.round(_innerH * (0.42 + Math.random() * 0.18))
      : Math.round(_innerH * (0.40 + Math.random() * 0.16));
    var px = clamp(sx0, marginX, _innerW - marginX);
    var py = clamp(sy0, marginY, _innerH - marginY);
    sx0 = px;
    sy0 = py;
    var htot = 0, pcnt = 0;
    var steps = isCheckout ? (18 + Math.floor(Math.random() * 10)) : (24 + Math.floor(Math.random() * 10));
    var gaps = [];
    var gapSum = 0;
    for (var a = 0; a < steps; a++) {
      var gap = a === 0 ? 120 + Math.random() * 220 : 55 + Math.random() * 85;
      gaps.push(gap);
      gapSum += gap;
    }
    var pointerEnd = isCheckout
      ? end
      : (kd[0].t - (260 + Math.random() * 240));
    var availPointerWindow = pointerEnd - (t0 + 160);
    if (availPointerWindow <= 0) {
      pointerEnd = t0 + 160 + Math.min(gapSum, isCheckout ? 600 : Math.max(80, kd[0].t - t0 - 280));
      availPointerWindow = pointerEnd - (t0 + 160);
    }
    if (gapSum > availPointerWindow && availPointerWindow > 0) {
      var pScale = (availPointerWindow * 0.92) / gapSum;
      gapSum = 0;
      for (var q = 0; q < gaps.length; q++) {
        gaps[q] = gaps[q] * pScale;
        gapSum += gaps[q];
      }
    }
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
      px = clamp(nx, marginX, _innerW - marginX);
      py = clamp(ny, marginY, _innerH - marginY);
      pt += gaps[j];
      htot += Math.hypot(px - prevX, py - prevY);
      pcnt++;
      pm.push({ clientX: Math.round(px), clientY: Math.round(py), type: "pointermove", t: pt });
    }

    // scroll / wheel / click / focus 样本累加：二阶和 sum2 >= sum^2 / n 恒成立。
    var scrollCount = 0, scrollSum = 0, scrollSum2 = 0, scrollTop = 0;
    var wheelCount = 0, wheelSum = 0;
    if (isCheckout) {
      scrollCount = 8 + Math.floor(Math.random() * 7);
      for (var sc = 0; sc < scrollCount; sc++) {
        var sDelta = Math.round(36 + Math.random() * 52);
        scrollSum += sDelta;
        scrollSum2 += sDelta * sDelta;
      }
      scrollTop = Math.round(scrollSum * (0.72 + Math.random() * 0.25));
      wheelCount = Math.max(4, Math.round(scrollCount * (0.55 + Math.random() * 0.45)));
      var wheelBase = scrollSum;
      for (var wc = 0; wc < wheelCount; wc++) {
        var wDelta = wc === wheelCount - 1
          ? Math.max(20, wheelBase)
          : Math.max(20, Math.round((scrollSum / wheelCount) * (0.8 + Math.random() * 0.4)));
        wheelSum += wDelta;
        wheelBase -= wDelta;
      }
    }

    var clickCount = 2;
    var clickSum = 0, clickSum2 = 0;
    for (var cc = 0; cc < clickCount; cc++) {
      var cDur = Math.round(85 + Math.random() * 145);
      clickSum += cDur;
      clickSum2 += cDur * cDur;
    }

    var focusCount = isCheckout ? 1 : 2;
    var focusSum = 0, focusSum2 = 0;
    for (var fc = 0; fc < focusCount; fc++) {
      var fDur = Math.round(140 + Math.random() * 340);
      focusSum += fDur;
      focusSum2 += fDur * fDur;
    }

    g.__FLOW_EFFECTIVE = flow;
    g.__oai_so_t0 = t0;
    g.__oai_so_h = kd;
    g.__oai_so_hi = kd.length;
    g.__oai_so_hp = 0;
    g.__oai_so_hw = 0;
    g.__oai_so_s = scrollSum;
    g.__oai_so_k = kd.length;
    g.__oai_so_kp = 0;
    g.__oai_so_we = wheelCount;
    g.__oai_so_wb = wheelSum;
    g.__oai_so_wl = 0;
    g.__oai_so_fs = focusSum;
    g.__oai_so_fs2 = focusSum2;
    g.__oai_so_fn = focusCount;
    g.__oai_so_p = pm;
    g.__oai_so_pc = pcnt;
    g.__oai_so_i = inputEvents;
    g.__oai_so_m = pm.length;
    g.__oai_so_ht = htot;
    g.__oai_so_hc = pcnt;
    g.__oai_so_bc = clickCount;
    g.__oai_so_bm = 0;
    g.__oai_so_ss = scrollSum;
    g.__oai_so_ss2 = scrollSum2;
    g.__oai_so_sn = scrollCount;
    g.__oai_so_cs = clickSum;
    g.__oai_so_cs2 = clickSum2;
    g.__oai_so_cn = clickCount;
    g.__oai_so_st = scrollTop;
    g.__oai_so_sw = _innerW;
    g.__oai_so_sp = 0;
    g.__oai_so_spt = 0;
    g.__oai_so_sx0 = sx0;
    g.__oai_so_sy0 = sy0;
    g.__oai_so_lx = Math.round(px);
    g.__oai_so_ly = Math.round(py);

    concealOwn(g, "__FLOW_EFFECTIVE");
  })();

  // ---- window 事件处理器属性（对齐真 Chrome ~128 项 on*）----
  (function () {
    for (var i = 0; i < _eventHandlers.length; i++) {
      if (!(_eventHandlers[i] in g)) {
        try { g[_eventHandlers[i]] = null; } catch (e) {}
      }
    }
  })();

  // ---- window 键序 ----
  (function () {
    var canonical = [
      "window", "self", "document", "name", "location", "customElements", "history",
      "navigation", "locationbar", "menubar", "personalbar", "scrollbars", "statusbar",
      "toolbar", "status", "closed", "frames", "length", "top", "opener", "parent",
      "frameElement", "external", "screen", "innerWidth", "innerHeight", "outerWidth",
      "outerHeight", "scrollX", "scrollY", "screenX", "screenY", "devicePixelRatio",
      "performance", "navigator", "localStorage", "sessionStorage", "indexedDB",
      "caches", "crypto", "origin", "isSecureContext", "crossOriginIsolated",
      "visualViewport", "console"
    ];
    var existing = Object.keys(g);
    var pending = {};
    for (var i = 0; i < existing.length; i++) pending[existing[i]] = true;
    var order = [];
    for (var c = 0; c < canonical.length; c++) {
      if (pending[canonical[c]]) { order.push(canonical[c]); pending[canonical[c]] = false; }
    }
    for (var e = 0; e < existing.length; e++) {
      if (pending[existing[e]]) order.push(existing[e]);
    }
    var stash = [];
    for (var s = 0; s < order.length; s++) {
      var desc = Object.getOwnPropertyDescriptor(g, order[s]);
      if (!desc || !desc.configurable) continue;
      try { delete g[order[s]]; stash.push([order[s], desc]); } catch (err) {}
    }
    for (var r = 0; r < stash.length; r++) {
      try { Object.defineProperty(g, stash[r][0], stash[r][1]); } catch (err) {}
    }
  })();
})();
