# 竞品 HAR 对照分析

> 输入：用户提供的 `chong.gptnb.cc` 浏览器 HAR  
> 处理：`scripts/sanitize_har.py` 脱敏后得到 39 条业务 API 记录  
> 限制：HAR 只抓浏览器到竞品后端的请求，**不包含竞品后端到 OpenAI/Stripe 的上游请求**。因此本文只能还原竞品的编排/API，不可声称还原其上游协议。

## 1. 结论先行

竞品前端不是直接请求 ChatGPT/Stripe，而是：

```text
浏览器
  → chong.gptnb.cc /api/payments/*
  → 竞品后端异步队列
  → 竞品后端自行访问 OpenAI/Stripe
```

所以本次 HAR 能确定的是：

```text
竞品的订单 API
竞品的状态机
竞品的轮询节奏
竞品的字段设计
竞品的卡/币种/账单展示方式
```

不能确定的是：

```text
竞品 create_checkout 的真实上游 body
竞品如何清洗 Session
竞品如何选择代理 SID
竞品的 Sentinel / confirm 协议细节
```

## 2. 本次 HAR 的实际流程

39 条业务 API 中，核心为：

```text
POST /api/payments/session/check
POST /api/payments/manage          # 订阅/交易历史
POST /api/payments/manage
POST /api/payments/orders         # 创建异步订单
GET  /api/payments/orders/{id}    # 高频轮询
```

### 2.1 创建订单请求

脱敏后的关键字段：

```json
{
  "plan": "pro5",
  "currency": "clp",
  "billing": {
    "state": "DE",
    "country": "US"
  },
  "auto": true,
  "card": "***REDACTED***"
}
```

结论：

```text
竞品前端使用 plan=pro5
竞品前端用 currency=clp
竞品账单是 US + DE（免税州）
竞品把卡直接提交给其后端
竞品 auto=true 表示建单后自动进入支付队列
```

### 2.2 创建订单响应

脱敏后关键字段：

```json
{
  "plan": "pro5",
  "state": "queued",
  "amount": null,
  "currency": "clp",
  "payment_status": "not_submitted",
  "proxy_rotations": 0,
  "reconcile_attempts": 0,
  "reconciliation_limit": 3,
  "execution": {
    "queue_ms": 43,
    "total_ms": 43,
    "running": false,
    "estimated": true
  },
  "events": []
}
```

结论：

```text
POST 只负责入队，不同步建 upstream checkout
真正金额、checkout_id、intent_id 在轮询 ready 后才出现
```

### 2.3 轮询状态机

从轮询响应中观察到：

```text
queued
  ↓
ready
  ↓
submitting
  ↓
succeeded
```

实际事件：

```text
queued       amount=null
ready        amount=86365, currency=clp
submitting   intent/checkout 已存在
succeeded    payment_status=succeeded
```

`execution` 最终：

```json
{
  "queue_ms": 153,
  "elapsed_ms": 62487,
  "total_ms": 62639,
  "running": false
}
```

事件时间线：

```text
queued       ~4.8s 后出现
ready        ~15.8s 后出现
submitting   ~28.5s 后出现
succeeded    ~38.4s 后出现
最终完成     ~62.6s
```

### 2.4 轮询频率

HAR 中订单创建后出现约 25 次 `GET /orders/{id}`，间隔大约：

```text
0.4s – 1.2s
```

竞品前端是高频短轮询，不依赖 SSE。

## 3. 字段对照

### 3.1 竞品订单响应字段

```text
id
account_id
account_email
plan
state
flow
amount
currency
settlement_country
proxy_id
checkout_id
intent_id
processor_entity
platform_completed
payment_status
message
created_at / updated_at
submitted
card_brand / card_last4
card_number / card_exp_month / card_exp_year
proxy_rotations
proxy_session_id / proxy_session_ip / proxy_name / proxy_country
execution {queue_ms, elapsed_ms, total_ms, running, estimated}
state_label
needs_session
events [{state, message, created_at}]
reconciliation_limit
can_resolve_uncharged
can_force_release
automatic_confirmation
queue_position
billing
customer_session_client_secret
guest_security
```

### 3.2 与我们的对照

| 维度 | 竞品 | 我们 | 判断 |
|---|---|---|---|
| 创建订单 | `/api/payments/orders` | `/api/payments/quick-pay` | 都是异步入口 |
| 创建返回 | 201 + `queued` | 202 + `job_id/queued` | 语义接近 |
| 轮询 | `/orders/{id}` | `/quick-pay/{job_id}` | 语义接近 |
| 状态 | `queued/ready/submitting/succeeded` | `queued/creating_order/checkout_created/confirming_payment/succeeded` | 需映射 |
| 金额 | `amount` 在 ready 后出现，整数最小单位 | `amount` 字符串，金额在 order 后出现 | 需统一展示 |
| 税额 | 本次响应无独立 `tax` 字段 | `tax_delta` | 竞品可能把税并入 total |
| 套餐名 | `pro5` | `pro_lite` | 需兼容别名 |
| 账单州 | `DE` | 固定 `OR` | 两者都免税，但字段不同 |
| 执行耗时 | `execution.queue_ms/elapsed_ms/total_ms` | 只有日志 `stage_timing` | GAP-O2 |
| 事件 | `events[]` 完整状态事件 | 无结构化事件 | 需补 |
| 代理轮换 | `proxy_rotations` | 无 | GAP-P |
| 失败字段 | `decline_code/error_code/diagnostics/reconcile_*` | `failure_code/message` | GAP-O1 |
| 卡字段 | 返回 `card_number/card_exp_*` | 不返回 | 我们的安全更好，不要复制 |
| Session 预检 | `/session/check` | 无 | 可补 |
| 订阅/交易历史 | `/manage` | 无 | 可补 |

## 4. 安全观察

竞品订单响应包含：

```text
card_number
card_exp_month
card_exp_year
```

这是**不应复制**的行为。即使前端脱敏，也意味着后端把卡数据回传给了浏览器。

我们的优势：

```text
OrderOut 不返回卡字段
QuickPayRequest.card 不写入 Order/Vault/DB
```

必须保持。

## 5. 具体改进建议

### P0：不要继续盲目改上游 payload

本次 HAR 没有上游请求，不能据此修改 `create_checkout` 的 `billing_details` 或税字段。

### P1：补前端可观测性（GAP-O1）

`QuickPay.tsx` 的 `FAILURE_LABEL` 建议补：

```text
checkout_upstream_error
invalid_session
session_expired
proxy_authentication_failed
browser_decode_unavailable
payment_unknown_state
unknown_upstream
```

### P1：补阶段耗时（GAP-O2）

建议 `QuickPayJobOut` 增加：

```json
{
  "timings_ms": {
    "queue_ms": 43,
    "create_order_ms": 3210,
    "browser_decode_ms": 2156,
    "create_checkout_ms": 15635,
    "payment_ms": 62487,
    "total_ms": 62639
  }
}
```

前端展示：

```text
排队
建单
解码
创建结账单
支付确认
总耗时
```

### P1：状态映射

竞品状态与我们：

```text
queued       → queued
ready        → checkout_created
submitting   → confirming_payment
succeeded    → succeeded
```

建议在 `QuickPayJobOut` 增加 `state` 或统一 `stage`，避免前端猜。

### P1：兼容 `pro5`

竞品前端使用：

```text
plan=pro5
```

我们的后端使用：

```text
plan=pro_lite
```

建议在 API 层增加别名：

```text
pro5 → pro_lite
```

仅在服务端映射，不改变内部 `Plan` 枚举。

### P2：Session 预检与交易历史

竞品有：

```text
POST /api/payments/session/check
POST /api/payments/manage
```

可以评估增加只读接口，但要确保不返回原始 token、卡或完整账单地址。

### P2：账单州可配置

竞品使用：

```text
country=US, state=DE
```

我们固定：

```text
country=US, state=OR
```

两者都免税，不建议为了对齐而改默认；可在配置中允许 DE/OR 选择。

## 6. 对 `PROJECT_BASELINE.md` 的增量结论

```text
已确认：
- 竞品也是异步订单 + 高频轮询
- 竞品有显式状态事件和 execution 耗时
- 竞品有 proxy_rotations 字段
- 竞品 plan=pro5，currency=clp
- 竞品把 US/DE 免税账单作为固定地址

仍无法确认：
- 竞品上游 create_checkout 真实字段
- 竞品 Session 清洗方式
- 竞品 SID 轮换策略
- 竞品 tax 拆分是否独立

应新增 Gap：
- GAP-O2+M: timings/state/events 结构化
- GAP-M6: pro5 别名
- GAP-O7: 失败字段 reconcile/decline 诊断
```

## 7. 下一步

```text
1. 先把本报告中的 P1 可观测性项落到 quick_pay_jobs / QuickPayJobOut
2. 再评估 pro5 别名
3. 不要根据这份 HAR 修改上游 create_checkout
4. 若要看竞品上游协议，需要竞品服务端流量或授权侧抓包；
   浏览器 HAR 无法提供
```
