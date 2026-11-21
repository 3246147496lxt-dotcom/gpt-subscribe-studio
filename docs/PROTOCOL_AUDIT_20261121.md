# ChatGPT Checkout / Stripe 协议链路深度审计

> 审计范围：`packages/core/src/gss_core/chatgpt.py`、`sentinel.py`、`browser_decode.py`、`browser_context.py`、`plus_har_protocol.py`、`har_device.py`、`har_telemetry.py`、`stripe_protocol.py`、`protocol_cards.py`、`proxies.py`
>
> 结论基于当前仓库代码和已有测试；未修改任何代码。
> 未使用真实卡、真实 Session 或生产代理做主动探测。

## 0. 结论摘要

当前链路不是“没有浏览器指纹”，而是存在三处更严重的**身份上下文分裂**：

```text
1. Route C 浏览器解码得到的 UA / Client Hints / 视口
   没有应用到 PlusHarProtocol 的 ChatGPTClient
2. PlusHarProtocol 的 DeviceProfile 是独立随机生成的
   Sentinel、ChatGPT Web、Stripe 可能各自看到不同平台和屏幕
3. pro_lite / pro 的 create_checkout plan_name 直接拼接错误
```

其中第 3 条是确定性代码错误，不是风控玄学：

```text
pro_lite -> chatgptpro_liteplan
pro      -> chatgptproplan
pro_max  -> chatgptpro_maxplan
```

真实网页对应的是：

```text
pro_lite -> chatgptprolite
pro      -> chatgptpro
pro_max  -> chatgptpromax
```

这会直接导致 Pro 100 建单载荷不符合网页端格式。

---

# 高危必改

## H-01 `PlusHarProtocol` 没有应用 Route C 的 BrowserContext

### 证据

`packages/core/src/gss_core/plus_har_protocol.py:419-435`

```python
def _prepare(self) -> None:
    if self.client is None:
        self.client = ChatGPTClient.from_raw_session(self.session, proxy=self.proxy)
        self.client.timeout = self.timeout
        self.client.language = self.device.locale
        self.client.timezone = self.device.timezone
        ...
```

这里没有调用：

```python
self.client.apply_browser_context(self.browser_context)
```

而 `ChatGPTClient.apply_browser_context` 已存在：

`packages/core/src/gss_core/chatgpt.py:660-679`

```python
def apply_browser_context(self, context):
    self.browser_context = context
    self.user_agent = context.user_agent
    self._sec_ch_ua_override = context.sec_ch_ua
    self.language = context.locale
    self.timezone = context.timezone_id
    self.extra_cookies.update(context.cookies)
```

但 `PlusHarProtocol` 只在 HTTP Web 请求时使用了：

`packages/core/src/gss_core/plus_har_protocol.py:330-333`

```python
web_headers = self.device.web_headers(referer="https://chatgpt.com/")
if self.browser_context is not None:
    web_headers.update(self.browser_context.headers())
```

因此同一次支付里可能出现：

```text
ChatGPT Web 请求：
  UA = Playwright 浏览器 UA
  sec-ch-ua = 浏览器真实值

Sentinel mint：
  UA = ChatGPTClient.profile 的旧 UA
  platform = _PROFILES 的硬编码平台
  screen = 1920x1080 / 1032
```

Sentinel 使用的 `sentinel_profile` 来自：

`packages/core/src/gss_core/chatgpt.py:653-658`

```python
@property
def sentinel_profile(self):
    p = self.profile
    return {
        "platform": p["platform"],
        "platform_label": p["label"],
        "screen_w": p["screen"][0],
        "screen_h": p["screen"][1],
        "avail_h": p["screen"][2],
        "cores": 8,
    }
```

### 影响

同一个账号、同一个出口 IP，但：

```text
Web 请求看到 macOS / Chrome 157 / 1440x900
Sentinel 看到 Windows / 1920x1080 / cores=8
Stripe DeviceProfile 看到 Linux / 另一组 screen
```

这类跨请求身份矛盾会直接触发 hCaptcha/Radar。

### 修复方案

`PlusHarProtocol._prepare` 创建 client 后立即：

```python
self.client.apply_browser_context(self.browser_context)
```

并让 `sentinel_profile` 从 BrowserContext 派生，而不是从 `ChatGPTClient.profile` 的 UA 猜测：

```python
def sentinel_profile_from_context(context):
    return {
        "platform": context.sec_ch_ua_platform or ...,
        "screen_w": context.viewport_width,
        "screen_h": context.viewport_height,
        "avail_h": context.viewport_height - 80,
        "cores": context.hardware_concurrency or 8,
    }
```

同时不要把 `BrowserContext.sentinel_token` 直接塞进已登录 client，匿名浏览器的 token 不能代表当前账号。

---

## H-02 `PlusHarProtocol` 的 plan_name 拼接错误

### 证据

`packages/core/src/gss_core/plus_har_protocol.py:489-500`

```python
body = {
    "plan_name": "chatgptplusplan" if self.plan == "plus" else f"chatgpt{self.plan}plan",
    ...
    "purchase_attempt_id": str(uuid.uuid4()),
}
```

对于：

```text
plus      -> chatgptplusplan       正确
pro_lite  -> chatgptpro_liteplan   错误
pro       -> chatgptproplan        错误，真实值是 chatgptpro
pro_max   -> chatgptpro_maxplan    错误，真实值是 chatgptpromax
```

而 `chatgpt.py` 已经有正确映射：

`packages/core/src/gss_core/chatgpt.py:178-185`

```python
_PLAN_NAME = {
    "plus": "chatgptplusplan",
    "go": "chatgptgoplan",
    "pro_lite": "chatgptprolite",
    "pro": "chatgptpro",
    "pro_max": "chatgptpromax",
    ...
}
```

### 影响

```text
Pro 100 建单 body 的 plan_name 不是网页端值
上游可能直接 400，或进入不同套餐分支
即使使用了正确 CL/PH 出口，也会被当成异常请求
```

这是当前 Pro 100 失败的头号确定性代码问题。

### 修复方案

```python
from gss_core.chatgpt import _PLAN_NAME

"plan_name": _PLAN_NAME.get(self.plan, "chatgptplusplan"),
"purchase_attempt_id": self.purchase_attempt_id or str(uuid.uuid4()),
```

不要继续用 f-string 拼套餐名。

---

## H-03 浏览器解码 Cookie 被直接并入代理会话

### 证据

Playwright 解码是 direct：

`packages/core/src/gss_core/browser_decode.py:95-137`

```python
page.goto(url, ...)
cookies = {c["name"]: c["value"] for c in await context.cookies() ...}
```

`PlusHarProtocol._web_session` 会把浏览器上下文的全部 Cookie 合并：

`packages/core/src/gss_core/plus_har_protocol.py:330-340`

```python
if self.browser_context is not None:
    cookies.update({str(k): str(v) for k, v in (self.browser_context.cookies or {}).items()})
```

`ChatGPTClient.apply_browser_context` 也做同样的事：

`packages/core/src/gss_core/chatgpt.py:673-675`

### 影响

浏览器解码是 direct（服务器直连），而 checkout 走 PH 代理。  
把 direct 产生的：

```text
__cf_bm
cf_clearance
_ cfuvid
```

带到 PH 代理会话，等于把两个出口的 Cloudflare 身份混在一起。

即使 UA 完全一致，CF/OpenAI 仍可能看到：

```text
cookie 绑定 IP ≠ 当前请求 IP
```

### 修复方案

只合并**非地理绑定**的浏览器字段：

```python
BROWSER_COOKIE_DENYLIST = {
    "__cf_bm", "cf_clearance", "_cfuvid", "cf_chl_2",
    "oai-did", "oai-sc", "__Secure-next-auth.session-token",
}

cookies.update({
    k: v for k, v in browser_context.cookies.items()
    if k not in BROWSER_COOKIE_DENYLIST
})
```

`__cf_bm`/`cf_clearance` 应该由目标代理会话自己获取，而不是从 direct 浏览器复制。

---

## H-04 Sentinel 环境参数是硬编码/半硬编码

### 证据

`sentinel.py:71-87` 默认：

```python
platform="Linux x86_64",
platform_label="Linux",
screen_w=1920,
screen_h=1080,
avail_h=1032,
cores=8,
```

`chatgpt.py:653-658` 的 `sentinel_profile` 又固定：

```python
"cores": 8
```

但 BrowserContext 实际是：

```text
Playwright viewport 1440x900
真实 UA 的平台可能 macOS / Windows
实际 screen 由浏览器决定
```

### 影响

Sentinel 证明的环境与 Web/Stripe 设备环境不一致。  
尤其 `cores=8`、`1920x1080` 与浏览器视口不一致时，属于明显脚本特征。

### 修复方案

```text
- platform/platform_label 从 BrowserContext 的 navigator.userAgentData 派生
- screen_w/screen_h 使用浏览器 screen，不是 viewport
- avail_h 使用 screen.availHeight
- cores 使用 navigator.hardwareConcurrency
- 不要用 _PROFILES 的静态 screen 覆盖浏览器真实值
```

---

## H-05 Stripe 链路存在 httpx 降级路径

### 证据

`stripe_protocol.py:29-41`

```python
import httpx
try:
    from curl_cffi import requests as curl_requests
    _HAS_CURL_CFFI = True
except Exception:
    _HAS_CURL_CFFI = False
```

`stripe_protocol.py:238-257`

```python
if _HAS_CURL_CFFI and curl_requests is not None:
    self._client = curl_requests.Session(impersonate="chrome150", ...)
else:
    self._client = httpx.Client(...)
```

### 影响

一旦 curl_cffi 在某些环境不可用，Stripe 请求会静默变成 Python `httpx` TLS 指纹。  
这比直接失败更危险，因为：

```text
ChatGPT Web 仍是 curl_cffi
Stripe 变成 httpx
同一订单跨域 TLS 指纹不一致
```

### 修复方案

Stripe 域必须 fail-closed：

```python
if not _HAS_CURL_CFFI:
    raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                      "Stripe 支付需要 curl_cffi，当前环境不可用，已停止")
```

不要在支付链路使用 `httpx` 作为静默 fallback。

---

## H-06 Stripe guid/muid/sid 可能回退为本地随机值

### 证据

`har_device.py:262-269`

```python
guid=_stripe_id(),
muid=_stripe_id(),
sid=_stripe_id(),
stripe_js_id=_uuid(),
elements_session_id=...
```

`har_telemetry.py:198-215`

```python
self.register()
...
def _adopt_server_ids(self):
    self.device.guid = self.guid
    self.device.muid = self.muid
    self.device.sid = self.sid
```

只有 `/m.stripe.com/6` 成功返回并解析出 `guid/muid/sid` 时才采用服务端值。

`plus_har_protocol.py:785-811` 会把：

```text
payment_method_data[guid]
payment_method_data[muid]
payment_method_data[sid]
```

直接写入 confirmation token。

### 影响

如果：

```text
m.stripe.com 超时
代理返回 403
telemetry mode=off
```

则 confirm 携带的是本地随机 `guid/muid/sid`，而不是 Stripe 实际签发的设备 ID。  
这是典型的 Radar 设备不一致。

### 修复方案

```text
confirm 前要求 telemetry.register() 成功且已 adopt server IDs
否则停止在 confirm 之前，返回可诊断的 telemetry_unavailable
不要把本地随机 ID 当成真实 Stripe ID
```

---

# 中危优化

## M-01 `purchase_attempt_id` 没有复用订单值

`plus_har_protocol.py:499`

```python
"purchase_attempt_id": str(uuid.uuid4()),
```

构造函数已经接收 `purchase_attempt_id`：

`plus_har_protocol.py:194`

```python
purchase_attempt_id: str = "",
```

但建单时没有使用。

### 影响

```text
订单 pa_ 与 create_checkout purchase_attempt_id 不一致
日志/对账无法关联同一购买尝试
```

### 修复

```python
"purchase_attempt_id": self.purchase_attempt_id or str(uuid.uuid4()),
```

---

## M-02 Sentinel Token 没有 TTL / 单次使用约束

`chatgpt.py:894-916` 只保存：

```text
self.sentinel_token
self.sentinel_so
```

`_route_headers` 对所有付款请求无条件附加：

`chatgpt.py:725-730`

```python
if self.sentinel_token:
    headers["openai-sentinel-token"] = self.sentinel_token
```

没有：

```text
minted_at
expires_at
flow
```

也没有在 token 被使用后清空。

### 影响

```text
长链路中复用过期 token
不同 flow 复用错 token
重试时复用一个已被上游消费的 token
```

### 修复

```python
self._sentinel_meta = {"token": ..., "flow": ..., "minted_at": ...}
```

每次 `_route_headers` 校验 flow 和 TTL；不匹配时重新 mint。

---

## M-03 create_checkout 错误被压缩成通用信息

`plus_har_protocol.py:506-515`

```python
response = self._app("POST", "/backend-api/payments/checkout", ...)
payload = _json(response)
status = ...
if status == 401:
    raise DomainError(ErrorCode.INVALID_SESSION, ...)
if status >= 400:
    raise DomainError(ErrorCode.CHECKOUT_UPSTREAM_ERROR,
                      f"创建结账失败 HTTP {status}")
```

这里没有：

```text
响应体 detail
upstream error code
cf-ray
request-id
openai-processing-ms
```

`_read_checkout` 同样：

`plus_har_protocol.py:528-534`

### 影响

只能知道“HTTP 400”，无法区分：

```text
unusual_activity
invalid plan_name
billing mismatch
proxy/CF challenge
```

### 修复

在抛错前调用统一的 `_log_upstream_failure`，并把安全错误码放入 `DomainError.detail`：

```python
{"upstream_status": status, "upstream_code": code,
 "cf_ray": headers.get("cf-ray"), "request_id": ...}
```

---

## M-04 `ChatGPTClient._raise` 丢失上游 code

`chatgpt.py:991-1023`

对 `unusual activity` 直接转换成固定中文，不保留上游 `code`。  
对 403 也只写“被拦截”，没有 `cf-ray`/`challenge` 类型。

### 影响

运营无法区分：

```text
Cloudflare 403
OpenAI unusual_activity
Sentinel challenge
Billing country mismatch
```

### 修复

```text
DomainError.detail 增加：
  upstream_status
  upstream_code
  upstream_type
  cf_ray
  request_id
```

---

## M-05 Stripe 协议 UA 与 DeviceProfile 可能不一致

`stripe_protocol.py:50-78`

硬编码：

```python
UA = "Mozilla/5.0 (X11; Linux x86_64) ... Chrome/150..."
STRIPE_HEADERS = {"User-Agent": UA, ...}
```

但 `PlusHarProtocol` 使用的是 `DeviceProfile.ua`（可能 Windows/macOS）。  
如果走到 `StripeProtocol` 旧路径，Stripe 会看到 Linux UA，而 ChatGPT 看到另一个 UA。

### 修复

统一使用 `DeviceProfile.ua` / `device.stripe_headers()`，不要在模块级写死 UA。

---

## M-06 缺少 Client Hints 完整组

`chatgpt._identity_headers`：

`chatgpt.py:681-712`

包含：

```text
sec-ch-ua
sec-ch-ua-mobile
sec-ch-ua-platform
```

但缺少或不一致的常见字段：

```text
sec-ch-ua-platform-version
sec-ch-ua-arch
sec-ch-ua-bitness
sec-ch-ua-full-version
sec-ch-ua-full-version-list
sec-ch-ua-model
```

`plus_har_protocol.device.client_hint_headers()` 有这些字段，说明项目知道它们重要，但 `ChatGPTClient` 的普通路径没有统一使用。

### 修复

让 `ChatGPTClient` 使用与 `DeviceProfile`/BrowserContext 相同的 client hint 生成器，不要维护第二套。

---

## M-07 日志保存不足

`chatgpt._log_upstream_failure`：

`chatgpt.py:2191-2212`

只记录：

```text
where
status
url
body
```

没有记录：

```text
response headers
cf-ray
request-id
openai-processing-ms
www-authenticate
content-type
```

### 修复

增加安全 header allowlist：

```text
cf-ray
x-request-id
openai-processing-ms
content-type
www-authenticate
```

绝不记录 `set-cookie`、`authorization`、`cookie`。

---

# 建议修复顺序

## P0

```text
1. H-02 plan_name 映射
2. H-01 PlusHar 应用 BrowserContext
3. H-03 禁止 direct 浏览器 Cookie 进入代理会话
4. H-04 Sentinel 使用浏览器真实环境
5. H-05 Stripe 禁止 httpx fallback
6. H-06 Stripe 设备 ID 不可用则停止 confirm
```

## P1

```text
7. M-01 purchase_attempt_id 复用
8. M-02 Sentinel TTL
9. M-03/M-04 结构化上游错误
10. M-05/M-06 UA/Client Hints 统一
11. M-07 日志安全 header
```

---

# 不要修改的安全红线

```text
卡号/CVC/有效期仍只在内存
不写 Order/Vault/SQLite/日志
unknown 资金锁逻辑不变
美国 OR 免税地址不变
confirm 阶段不换 IP、不重试
```

---

# 验证建议

修复后不要直接跑真实支付。先做：

```text
1. 同一测试账号 + 同一出口，分别跑：
   - 浏览器手工支付
   - 协议支付
2. 对比 HAR：
   - plan_name
   - purchase_attempt_id
   - UA / sec-ch-ua / sec-ch-ua-platform
   - oai-did / oai-device-id
   - guid/muid/sid
   - time_on_page
3. 只在建单阶段做一次 A/B
4. 确认 create_checkout 返回 200 后，再测试 confirm
```

当前测试全绿不代表这些线上身份问题已被覆盖；现有测试主要验证逻辑分支，不验证真实浏览器指纹一致性。
