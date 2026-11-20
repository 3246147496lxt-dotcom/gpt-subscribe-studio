# HAR 对照分析清单

用途：用一次真实浏览器全链路 HAR，对照 `docs/PROJECT_BASELINE.md` 中的 `GAP-O1`、`GAP-O2`、多币种与 Pro 100 展示差异。

## 安全前置

HAR 原始文件可能包含：

```text
Cookie
Authorization
完整卡号
CVC
有效期
Session Token
邮箱 / 地址
```

不要上传原始 HAR，不要提交到 Git，不要发到聊天窗口。

只运行脱敏脚本，并只发送：

```text
tmp/sanitized_flow.json
```

原始 HAR 分析完成后删除。

## 运行命令

```bash
cd /Users/pu/Documents/Codex/2026-11-03/x/gpt-subscribe-studio-1

python3 scripts/sanitize_har.py \
  --input ~/Downloads/chatgpt.har \
  --output tmp/sanitized_flow.json

python3 -m json.tool tmp/sanitized_flow.json | head -80
```

脚本行为：

```text
过滤 image / JS / CSS / font / media / wasm
只保留 JSON / event-stream / /backend-api / /api / /v1 / /payments / /checkout
去除 URL query 值
去掉 Cookie / Authorization / 卡 / CVC / 有效期 / token / 邮箱 / 地址
保留 method / path / status / time_ms
保留请求与响应 JSON 的字段骨架
保留 status / state / payment_status / currency / amount / tax / plan / code 等枚举和业务值
```

输出：

```text
tmp/sanitized_flow.json
```

## (a) 结账单字段对照

逐项记录网页 HAR 中的实际字段，与我们的实现比对：

| 维度 | HAR 字段 | 我们的位置 | 判断 |
|---|---|---|---|
| 结账单创建路径 | `/backend-api/payments/checkout` | `chatgpt.py:create_checkout` | 路径是否一致 |
| 套餐名 | `plan_name` | `_PLAN_NAME` | `pro_lite -> chatgptprolite` 是否一致 |
| 出口国家 | `billing_details.country` | `_checkout_body` | 是 CL 还是 US |
| 币种 | `billing_details.currency` | `_checkout_body` | CLP / USD |
| 初始金额 | `amount` / `amount_total` / `minorUnitsAmount` | `OrderOut.amount` | 是否与前端显示一致 |
| 税额 | `tax` / `tax_amount` / `total.tax` | `view.tax_delta` | 是否需要新增税额字段 |
| 免税地址 | `billing_address` / `tax_region` | `checkout_address()` | 建单阶段是否出现 |
| 结账 UI 模式 | `checkout_ui_mode` | `_checkout_body` | `custom` 是否一致 |
| 结账来源 | `checkout_source` | `_checkout_body` | `chatgpt_web_merged` 是否一致 |
| 采购尝试 ID | `purchase_attempt_id` | `Order.purchase_attempt_id` | 是否需要透传 |
| 优惠活动 | `promo_campaign` | `_checkout_body` | 是否存在 |
| 处理器 | `processor_entity` | `CheckoutSession.processor_entity` | 是否一致 |

重点判断：

```text
1. 网页端在 CL 出口 + US 账单时，billing_details.country 到底是 CL 还是 US
2. 税额是在 create_checkout 返回，还是后续 taxes / elements 返回
3. amount、tax、total 是否为整数最小单位，字段名是什么
4. 网页端是否传 billing_address；如果传，传在哪个阶段
```

## (b) 异步任务 / 轮询阶段对照

从 HAR 中提取所有业务 API 的：

```text
method
path
status
time_ms
response_json.status / state / payment_status
```

与我们的 `quick_pay_jobs` 对照：

| 网页阶段 | HAR path | 我们的 stage | 差异 |
|---|---|---|---|
| 建单 | `/backend-api/payments/checkout` | `creating_checkout` / `checkout_created` | 是否需要拆细 |
| 税重算 | `/backend-api/payments/checkout/taxes` | `checkout_created` 内部 | 是否应独立阶段 |
| 元素会话 | `/v1/elements/sessions` | `confirming_payment` 前 | 是否应显示 |
| 确认 | `/backend-api/payments/checkout/confirm` | `confirming_payment` | 是否需独立 `confirm` 阶段 |
| Intent 确认 | `/v1/payment_intents/*/confirm` | `confirming_payment` | 是否需独立 `intent_confirm` |
| 结果轮询 | `/backend-api/payments/checkout/*` | `verifying_delivery` | 轮询间隔/次数 |

需要输出：

```text
真实阶段顺序
每一步 time_ms
哪些步骤可以合并
哪些步骤需要新增 quick_pay_jobs.stage
```

## (c) 前端 FAILURE_LABEL 与耗时展示

当前 `apps/web/src/pages/QuickPay.tsx` 的 `FAILURE_LABEL` 需要对照 HAR 中出现的错误码补全。

重点检查：

```text
checkout_upstream_error
invalid_session
session_expired
unusual_activity
subscription_limit
sentinel_challenge_failed
proxy_tunnel_failed
proxy_authentication_failed
browser_decode_unavailable
insufficient_funds
card_declined
payment_unknown_state
unknown_upstream
```

建议前端展示：

```text
阶段名
该阶段 time_ms
失败码
失败阶段
是否可重试
是否触发 unknown 锁
```

`GAP-O2` 目标：

```text
QuickPayJobOut 增加 timings_ms
按阶段展示：reissue / browser_decode / create_checkout / payment
```

## 输出结论模板

分析后按以下格式给出：

```text
1. 结账单字段差异
   - 缺失字段：
   - 命名差异：
   - 币种/税额差异：

2. 阶段差异
   - 网页实际阶段：
   - 我们缺少的阶段：
   - 可合并阶段：

3. 前端可观测性改进
   - 需要补的 FAILURE_LABEL：
   - 需要展示的耗时：
   - 需要展示的诊断字段：

4. 对 PROJECT_BASELINE.md 的更新
   - 已对齐：
   - 仍 Gap：
   - 下一步优先级：
```
