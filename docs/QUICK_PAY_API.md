# Quick Pay API 合同

前端使用 React 19 + Mantine；后端提供异步任务 + 轮询接口。

```text
POST /api/payments/quick-pay
GET  /api/payments/quick-pay/{job_id}
```

`Idempotency-Key` 必填。

## POST /api/payments/quick-pay

```json
{
  "plan": "plus",
  "billing_country": "US",
  "exit_country": "PH",
  "session": "SESSION_JSON",
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
  "mode": {
    "account_source": "customer_session",
    "card_source": "customer",
    "billing_mode": "standard",
    "risk_acknowledged": false
  },
  "debug_checkout_only": false
}
```

响应 `202`：

```json
{
  "job_id": "JOB_ID",
  "status": "queued",
  "stage": "queued",
  "order_id": null,
  "purchase_attempt_id": null,
  "amount": null,
  "currency": null,
  "failure_code": null,
  "message": null
}
```

同一 `Idempotency-Key` 再次请求返回同一个 job。

## GET /api/payments/quick-pay/{job_id}

```json
{
  "job_id": "JOB_ID",
  "status": "running",
  "stage": "browser_decode",
  "order_id": "ORDER_ID",
  "purchase_attempt_id": "pa_...",
  "amount": "982.14",
  "currency": "PHP",
  "failure_code": null,
  "message": null
}
```

阶段：

```text
queued
creating_order
reissuing
reissued
session_refresh
session_refreshed
browser_decode
browser_decoded
browser_decode_skipped
creating_checkout
checkout_created
confirming_payment
verifying_delivery
succeeded
failed
unknown
```

终态：

```text
succeeded | failed | unknown
```

失败时 `failure_code` 示例：

```text
card_declined
insufficient_funds
subscription_limit
unusual_activity
sentinel_challenge_failed
proxy_tunnel_failed
upstream_timeout
browser_decode_unavailable
```

## 轮询

```text
0–10 秒：每 1 秒
10–60 秒：每 2 秒
60 秒后：每 5 秒
终态停止
```

## debug_checkout_only

```json
{
  "debug_checkout_only": true
}
```

只建单，不提交卡；返回：

```text
status=succeeded
stage=checkout_created
message=仅建单完成，未提交卡
```

## 安全约束

- `card` 只在请求内存和后台协程局部变量中存在；
- 不写入 Order、OrderView、Vault、DB、SSE、日志；
- 前端不要打印请求体；
- 终态后不得用同一订单重复提交卡；
- `unknown` 状态禁止自动换卡重试。
