# Quick Pay API 合同

前端使用 React 19 + Mantine；后端提供异步任务 + 轮询接口。

```text
POST /api/payments/quick-pay
GET  /api/payments/quick-pay/{job_id}
```

`Idempotency-Key` 必填。`billing` 可省略，后端会使用本单已核验的固定美国免税账单。

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

同一合法请求使用同一 `Idempotency-Key` 时返回原 job。HTTP 202 仅表示任务已受理，不等于上游已建单、付款或开通。

## GET /api/payments/quick-pay/{job_id}

```json
{
  "job_id": "JOB_ID",
  "lock_id": null,
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

在正常建单字段中设置上面的开关。**必须省略整个 `card` 字段**（包括 `null` 与旧版占位卡）；携带时返回脱敏 422，不入队。普通付款请求（开关为 false 或省略）仍须提供有效的 `card`。

仅建单成功时返回：

```text
status=succeeded
stage=checkout_created
message=仅建单完成，未提交卡
```

### 建单失败后由本人办理

状态 API 的可选字段 `checkout_outcome` 不是上游字段。只有数据库能关联到同一账户、作业及被拒的订单，确认首次响应是明确的 HTTP 拒绝，且没有结账成功、付款尝试、未决状态、现存锁或冲突订单时，才可能为 `rejected_before_payment`。缺证据时为空；不靠超时、错误文字或前端推测赋值。

界面仅对证据明确的拒单显示“未创建结账单，未提交付款”。本人办理是独立前往官方入口，不传递卡、Session、订单号或返回令牌，不重放原作业，也不会因用户点击而把原单改为成功。已有订阅、后续付款失败和 `unknown` 不使用该入口。旧客户端请随新版后端同步更新：仅建单请求不再接受占位卡。

## 安全约束

- `card` 只在请求内存和后台协程局部变量中存在；
- 不写入 Order、OrderView、Vault、DB、SSE、日志；
- 前端不要打印请求体；
- 终态后不得用同一订单重复提交卡；
- `unknown` 状态禁止自动换卡重试。

## unknown 锁

当 `status=unknown` 时，`lock_id` 非空：

```json
{
  "status": "unknown",
  "stage": "unknown",
  "lock_id": "LOCK_ID",
  "order_id": "ORDER_ID"
}
```

同一账号再次提交会返回：

```text
HTTP 409
code=payment_unknown_lock
```

解锁：

```http
POST /api/payments/locks/{lock_id}/release
```

```json
{
  "confirm_no_charge": true,
  "note": "已核对银行账单，无扣款",
  "operator": "web-operator"
}
```

## 自动关闭续费

一键充值请求可传：

```json
{"auto_cancel_renew": true}
```

结果返回：

```text
auto_renew_status: disabled | pending | canceling_auto_renew | canceled | manual_required
auto_renew_canceled_at
auto_renew_error
```

手动重试：

```http
POST /api/orders/{order_id}/auto-renew/cancel
```

不接收新 Session；vault 过期返回：

```text
409 vault_expired
```

## 建单 SID 轮换

明确拒绝（包括 `unusual_activity` 和会话失效）是终态，不二发。仅既有开关已启用且能确认未获上游响应的隧道连接失败时：

```text
最多自动重试 1 次
confirm 阶段绝不换 SID
```

返回：

```text
proxy_rotations
proxy_last_sid（截断脱敏）
proxy_last_exit_ip
```
