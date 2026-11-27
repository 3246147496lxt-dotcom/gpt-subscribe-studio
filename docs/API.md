# GPT Subscribe Studio Developer API

面向外部系统对接的 HTTP API 文档。

```text
Base URL: http://127.0.0.1:8080
Content-Type: application/json
OpenAPI: docs/openapi.json
```

> 本服务默认只监听 `127.0.0.1`。没有内置公网鉴权，不要把 8080 直接暴露到公网。
> 生产环境应放在带鉴权、TLS、限流和审计的反向代理之后。

---

## 1. 认证与安全边界

当前版本没有 API Key / OAuth。调用方必须与服务在同一受信环境，或由网关完成认证。

敏感数据：

| 数据 | 处理方式 |
|---|---|
| ChatGPT Session JSON | 只在请求和内存 Vault 中流转，不写业务库 |
| 卡号 / CVC | 只用于当次支付，不持久化，不写日志 |
| 代理账号密码 | 只在代理配置和内存中使用 |
| `payment_intent_id` | 可安全用于对账 |
| `purchase_attempt_id` | 可安全用于排查与关联 |

不要记录或回传：

```text
accessToken
sessionToken
Cookie
卡号 / CVC
代理密码
```

---

## 2. 快速接入流程

### 2.1 健康检查

```http
GET /api/health
```

```json
{
  "status": "ok",
  "version": "0.2.0",
  "promo_enabled": false,
  "fake_adapters": false,
  "missing_upstreams": [],
  "proxy_sid_pinned": true
}
```

### 2.2 建单

```http
POST /api/orders
Idempotency-Key: <稳定的业务请求 ID>
```

```json
{
  "plan": "plus",
  "billing_country": "US",
  "exit_country": "PH",
  "session": "<SESSION_JSON>",
  "mode": {
    "account_source": "customer_session",
    "card_source": "customer",
    "billing_mode": "standard",
    "risk_acknowledged": false
  }
}
```

响应 `201`：

```json
{
  "id": "ORDER_ID",
  "status": "awaiting_payment",
  "purchase_attempt_id": "pa_...",
  "route_profile": "A",
  "failure_code": null,
  "plan": "plus",
  "billing_country": "US",
  "exit_country": "PH",
  "amount": "982.14",
  "currency": "PHP",
  "tax_delta": "117.86",
  "payment_status": "unpaid",
  "delivery_status": "pending"
}
```

非 Team 订单的账单地址由服务端固定账单资料提供；`billing` 不是调用方必须提交的字段。

### 2.3 查询订单

```http
GET /api/orders/{order_id}
```

建议轮询间隔：

```text
1s → 2s → 3s → 5s，最长 60s
```

不要根据单次 `unknown` 判断失败。

### 2.4 提交支付

全协议模式：

```http
POST /api/orders/{order_id}/bind-and-pay-protocol
```

```json
{
  "card": {
    "number": "CARD_NUMBER",
    "cvc": "CVC",
    "exp_month": "11",
    "exp_year": "2028"
  },
  "billing": {
    "email": "PAYER_EMAIL",
    "name": "PAYER_NAME",
    "line1": "PAYER_LINE1",
    "city": "PAYER_CITY",
    "state": "OR",
    "postal_code": "PAYER_ZIP",
    "country": "US"
  },
  "expected_amount": "982.14",
  "expected_currency": "PHP"
}
```

`expected_amount` / `expected_currency` 必须与建单结果一致；不一致返回 `409`。

### 2.5 查询支付结果

```http
GET /api/orders/{order_id}/bind-and-pay-protocol
```

不要重复提交同一订单的卡；重复提交会按幂等/原付款检查处理。

### 2.6 SSE 实时状态

```http
GET /api/events/orders/{order_id}/stream
Accept: text/event-stream
```

事件：

```text
data: {"type":"init","status":"awaiting_payment"}

data: {"order_id":"ORDER_ID","status":"active","timestamp":...}
```

每 60 秒发送心跳注释：

```text
: heartbeat <unix_ts>
```

---

## 3. 订单接口

### `POST /api/orders`

创建订单。

请求头：

| Header | 必填 | 说明 |
|---|---|---|
| `Idempotency-Key` | 建议 | 同一次业务请求必须复用；最长 255 |

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `plan` | string | 是 | `plus` / `go` / `pro_lite` / `pro` / `pro_max` / `team` |
| `billing_country` | string | 否 | 账单国家；非 Team 最终以服务端核验账单为准 |
| `exit_country` | string | 否 | 代理出口/定价/重签发国家；默认等于账单国家 |
| `session` | string | 是 | ChatGPT Session JSON；`customer_session` 模式必填 |
| `mode` | object | 否 | 模式开关 |
| `promo_code` | string | 否 | 促销码 |
| `promo_campaign_id` | string | 否 | 促销活动 ID |
| `workspace_name` | string | 否 | Team 工作区名称 |
| `seat_quantity` | int | 否 | Team 席位数，最少 2 |
| `price_interval` | string | 否 | `month` / `year` |

`mode`：

```json
{
  "account_source": "customer_session",
  "card_source": "customer",
  "billing_mode": "standard",
  "risk_acknowledged": false
}
```

`billing_mode=promo` 需要 `risk_acknowledged=true`。

响应字段补充：

| 字段 | 说明 |
|---|---|
| `purchase_attempt_id` | 本单稳定关联 ID，重签发/重试不变 |
| `route_profile` | `A` / `C`，当前线路 |
| `failure_code` | 标准化失败码 |
| `payment_status` | `unpaid` / `processing` / `succeeded` / `failed` |
| `delivery_status` | `pending` / `delivered` / `verify_delayed` |
| `receipt` | 结构化回执，可能为 null |

常见状态码：

| HTTP | code | 说明 |
|---|---|---|
| 201 | - | 订单创建成功 |
| 400 | `compliance_blocked` | 地区/套餐/模式不允许 |
| 401 | `invalid_session` | Session 无效 |
| 409 | `billing_country_mismatch` | 出口与固定账单不兼容 |
| 409 | `idempotency_conflict` | 幂等键绑定不同请求 |
| 503 | `browser_decode_unavailable` | Route C 解码不可用 |
| 503 | `BILLING_PROFILE_UNAVAILABLE` | 账单资料不可用 |

### `GET /api/orders/{order_id}`

返回当前订单完整视图。适合轮询。

### `POST /api/orders/{order_id}/verify?paid=true`

手工触发订阅状态核验。

```text
paid=true 表示调用方认为已提交付款，只用于重新查询/核验，不代表可以重复扣款。
```

一般对接不需要直接调用；优先用支付接口和订单查询。

---

## 4. 支付接口

### `POST /api/orders/{order_id}/bind-and-pay-protocol`

最常用的服务端全协议支付。

请求：

```json
{
  "card": {
    "number": "CARD_NUMBER",
    "cvc": "CVC",
    "exp_month": "11",
    "exp_year": "2028"
  },
  "billing": {
    "email": "PAYER_EMAIL",
    "name": "PAYER_NAME",
    "line1": "PAYER_LINE1",
    "city": "PAYER_CITY",
    "state": "OR",
    "postal_code": "PAYER_ZIP",
    "country": "US"
  },
  "expected_amount": "982.14",
  "expected_currency": "PHP"
}
```

成功/处理中响应：

```json
{
  "state": "paid",
  "payment_status": "succeeded",
  "delivery_status": "delivered",
  "payment_intent_id": "pi_...",
  "amount": "982.14",
  "currency": "PHP",
  "tax_delta": "117.86",
  "plan_type": "plus",
  "status_summary": "已支付并交付",
  "receipt": {
    "payment_intent_id": "pi_...",
    "amount": "982.14",
    "currency": "PHP",
    "tax_delta": "117.86",
    "plan_type": "plus",
    "payment_status": "succeeded",
    "delivery_status": "delivered",
    "summary": "已支付并交付"
  },
  "order": { "...": "OrderOut" }
}
```

状态值：

| state | 含义 |
|---|---|
| `paid` | 支付与交付均确认 |
| `pending` | 已扣款或已提交，等待订阅/交付确认 |
| `challenge_required` | 需要 3DS/银行验证 |
| `card_declined` | 卡被拒绝 |
| `unknown` | 结果不确定，禁止重复支付 |
| `no_payment_required` | 0 元订单 |

重要：

```text
state=unknown 或 message 含“扣款结果待核对”时，绝对不要重复提交卡。
先 GET /api/orders/{order_id} 查询，必要时人工对账。
```

### `GET /api/orders/{order_id}/bind-and-pay-protocol`

只读查询原支付尝试，不重复提交卡。

### `POST /api/orders/{order_id}/pay-protocol`

底层协议支付入口，支持：

```json
{
  "billing": { "...": "BillingIn" },
  "card": { "...": "CardIn" },
  "payment_method_id": "pm_...",
  "steps": {
    "fingerprint": false,
    "dwell": false,
    "elements": false,
    "tax_region": false
  }
}
```

二选一：

```text
card               服务端直接提交卡号
payment_method_id  浏览器 Stripe 输入框已经换好的 pm_
```

一般对接建议使用 `bind-and-pay-protocol`，不要直接调用此接口。

### `POST /api/orders/{order_id}/pay`

Team/旧版支付入口。非 Team 新接入不建议使用。

---

## 5. Team 接口

```http
POST /api/orders/{order_id}/team/quote
POST /api/orders/{order_id}/team/prepare
POST /api/orders/{order_id}/team/poll
```

用途：

| 接口 | 说明 |
|---|---|
| `team/quote` | 读取 Team 报价 |
| `team/prepare` | 创建/准备 Team 托管结账 |
| `team/poll` | 轮询 Team 支付/订阅结果 |

Team 请求包含：

```json
{
  "billing": { "...": "BillingIn" },
  "browser_locale": "en-US",
  "browser_timezone": "America/New_York"
}
```

Team 的 `checkout_url` 可能返回托管长链；非 Team 自定义结账通常返回 `null`，通过内部订单 ID 执行协议支付。

---

## 6. Saved Card 接口

```http
POST /api/orders/{order_id}/saved-card/prepare
GET  /api/orders/{order_id}/saved-card
POST /api/orders/{order_id}/saved-card/refresh-checkout
```

用于已保存支付方式的准备、状态查询和结账刷新。  
新接入优先使用 `bind-and-pay-protocol`，除非你的系统已经管理 `pm_`。

---

## 7. 促销接口

```http
POST /api/promo/eligibility
```

```json
{
  "session": "<SESSION_JSON>"
}
```

响应：

```json
{
  "account_id": "ACCOUNT_ID",
  "current_plan_type": "free",
  "subscription_plan": null,
  "is_free": true,
  "plus_eligible": true,
  "campaigns": [
    {
      "key": "PROMO_KEY",
      "campaign_id": "CAMPAIGN_ID",
      "discount_percentage": 100,
      "duration_num_periods": 1,
      "duration_period": "month",
      "title": "PROMO_TITLE",
      "summary": "PROMO_SUMMARY",
      "promotion_type": "PROMO_TYPE"
    }
  ]
}
```

只读；不会创建订单或付款。

---

## 8. 代理设置接口

管理建单/支付两个代理阶段。

```http
GET  /api/settings/proxies
PUT  /api/settings/proxies
POST /api/settings/proxies/parse
PUT  /api/settings/proxies/{role}
POST /api/settings/proxies/{role}/test
```

`role`：

```text
checkout
approve
```

`PUT /api/settings/proxies` 会把同一代理同时写入两个阶段：

```json
{
  "raw": "http://<USER>:<PASSWORD>@<HOST>:<PORT>",
  "enabled": true
}
```

探测：

```http
POST /api/settings/proxies/checkout/test
```

响应：

```json
{
  "role": "checkout",
  "region": "PH",
  "sid": "SID",
  "ip": "EXIT_IP",
  "elapsed_ms": 812
}
```

代理响应不会返回密码。

---

## 9. 错误模型

标准错误：

```json
{
  "detail": {
    "code": "unusual_activity",
    "message": "可读说明",
    "detail": {
      "session_reissued_for_country": true
    }
  }
}
```

上游失败码：

| code | 含义 | 建议 |
|---|---|---|
| `unusual_activity` | 上游判定异常流量 | 停止重试，换线路/账号 |
| `sentinel_challenge_failed` | Sentinel/安全验证失败 | 检查浏览器解码/线路 |
| `proxy_tunnel_failed` | 代理 CONNECT/TLS 失败 | 检查 SID/网关 |
| `proxy_authentication_failed` | 代理认证失败 | 检查用户名密码 |
| `upstream_timeout` | 上游超时 | 先查订单，不要重复扣款 |
| `billing_country_mismatch` | 出口与账单国家不兼容 | 使用匹配账单或允许的 split 配置 |
| `browser_decode_unavailable` | Route C 浏览器解码不可用 | 安装 Playwright/Chromium 或回退 A |
| `insufficient_funds` | 卡片余额不足 | 提示运营充值/换卡 |
| `card_declined` | 发卡行拒付或卡限额 | 换卡或等待冷却 |
| `unknown_upstream` | 未分类上游失败 | 记录 request id 后排查 |

业务错误：

```text
invalid_session
session_expired
idempotency_conflict
compliance_blocked
payment_declined
payment_unknown_state
subscription_not_active
```

---

## 10. 幂等与重试规则

### 建单

```text
同一次业务请求 → 同 Idempotency-Key
```

同一键绑定不同请求体会返回：

```text
409 idempotency_conflict
```

### 支付

```text
同一订单 → 不重复提交卡
```

已提交但结果未知时：

```text
GET /api/orders/{order_id}
```

不要用新卡、新金额或新请求体重试同一订单。

### 网络超时

建单 POST 超时属于“结果不确定”：

```text
不要立即重新建单
先用同一 Idempotency-Key 查询/重试
```

支付确认超时：

```text
不要重新提交卡
查询订单 / 原结账 / 订阅状态
```

---

## 11. 订单状态

```text
created
pending_checkout
awaiting_payment
paying
requires_action
unknown
verifying
active
delivered
rejected
failed
refunded
```

支付状态：

```text
unpaid
processing
succeeded
failed
```

交付状态：

```text
pending
delivered
verify_delayed
```

关键原则：

```text
payment_status=succeeded 后，订阅查询失败不能回退成 payment_status=failed。
```

---

## 12. Route C 与 purchase_attempt_id

Route C 是可选能力，默认 A：

```env
GSS_ROUTE_PROFILE=A
GSS_BROWSER_DECODE=0
```

启用 C：

```env
GSS_ROUTE_PROFILE=C
GSS_BROWSER_DECODE=1
GSS_BROWSER_HEADLESS=0
GSS_BROWSER_DECODE_AUTH=0
```

此时：

```text
Playwright 浏览器解码 → 直连
create_checkout / confirm → PH 订单代理
```

失败默认：

```text
browser_decode_unavailable
```

显式回退：

```env
GSS_BROWSER_DECODE_FALLBACK=A
```

`purchase_attempt_id`：

```text
pa_...
```

同一订单的重签发、线路切换、可恢复重试不变；新订单重新生成。

内部请求头：

```text
X-GSS-Purchase-Attempt: pa_...
```

---

## 13. 推荐对接代码骨架

```python
import time
import httpx

BASE = "http://127.0.0.1:8080"

def create_order(session: str, idem_key: str) -> dict:
    with httpx.Client(base_url=BASE, timeout=300) as client:
        response = client.post(
            "/api/orders",
            headers={"Idempotency-Key": idem_key},
            json={
                "plan": "plus",
                "billing_country": "US",
                "exit_country": "PH",
                "session": session,
                "mode": {
                    "account_source": "customer_session",
                    "card_source": "customer",
                    "billing_mode": "standard",
                    "risk_acknowledged": False,
                },
            },
        )
        response.raise_for_status()
        return response.json()


def pay_order(order_id: str, card: dict, billing: dict) -> dict:
    with httpx.Client(base_url=BASE, timeout=300) as client:
        response = client.post(
            f"/api/orders/{order_id}/bind-and-pay-protocol",
            json={
                "card": card,
                "billing": billing,
                "expected_amount": "982.14",
                "expected_currency": "PHP",
            },
        )
        response.raise_for_status()
        return response.json()
```

---

## 14. OpenAPI

机器可读定义：

```text
docs/openapi.json
```

运行中服务：

```text
GET /openapi.json
GET /docs
GET /redoc
```

---

## 15. 最小上线检查清单

- [ ] 8080 只在内网/回环可达
- [ ] 反向代理启用 TLS 与鉴权
- [ ] 不记录 Session、Cookie、卡号、CVC
- [ ] 建单强制使用 Idempotency-Key
- [ ] 支付未知状态禁止重复提交
- [ ] 订单查询与 SSE 至少实现一种
- [ ] 代理 SID 固定并健康检查
- [ ] 账单资料文件权限 0600
- [ ] Route C 启用前安装 Playwright Chromium
- [ ] 失败码进入告警/审计
