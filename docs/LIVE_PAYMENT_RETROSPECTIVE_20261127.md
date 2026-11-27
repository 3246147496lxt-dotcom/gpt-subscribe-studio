# 实单支付全链路复盘与运维手册（2026-11-27 · v0.2.0）

> **归档日期**：2026-11-27  
> **对应版本**：`v0.2.0`  
> **脱敏声明**：本文仅记录脱敏后的 8 位订单前缀、套餐/地区/币种、HTTP 接口路径、字段结构、状态码与耗时统计；**不包含**任何真实邮箱、Session/JWT、Cookie、卡号/CVC、代理账密或个人身份信息。

---

## 1. 里程碑结论

2026-11-27 完成了基于真实浏览器抓包（`sample4` / `sample5`）与真卡实单的全链路打通。以 `4f927f13`（`pro_lite` · 出口 `CL` · 固定美国免税州账单 · `86365 CLP`）为基准，**纯协议一键开通 → 含税重算 → Stripe 扣款 → 订阅到账核验 → 自动关闭下期续费**已实现单次端到端全绿闭环：

| 指标 | 实测结果（基准单 `4f927f13`） |
|---|---|
| 套餐 / 出口 / 账单国 | `pro_lite` / `CL` (`CLP`) / `US`（免税州） |
| 税前 → 税后金额 | `102774 CLP` → `86365 CLP`（免税重算减免 `tax_delta = 16409 CLP`） |
| 建单耗时 (`create_checkout_ms`) | `24.97 s` |
| 支付 + 核验耗时 (`payment_ms`) | `95.23 s` |
| 自动关闭续费 (`cancel_renew_ms`) | `10.85 s`（直调一等接口 `POST /backend-api/subscriptions/cancel` -> `200 OK`） |
| 总耗时 (`total_ms`) | `131.08 s` |
| 最终状态 | `status=delivered` · `payment=succeeded` · `delivery=delivered` · `auto_renew=canceled` |

---

## 2. 实测样本统计（2026-11-27 本地 12 笔实单轨迹）

本轮排障共执行 **12 笔**真实上游实单（`plus / PH` 6 笔、`pro_lite / CL` 6 笔），覆盖建单调试、协议对齐、核验修复、退订接口迁移与卡风控验证全阶段：

### 2.1 分阶段结果分布

| 阶段分类 | 笔数 | 订单前缀 | 核心现象与结论 |
|---|---:|---|---|
| **全链路闭环成功** | 1 | `4f927f13` | 换新卡 + 全部修复生效后，一杆跑通 `paid -> delivered -> auto_renew=canceled` |
| **扣款成功，暴露核验/退订旧缺陷** | 2 | `07916c2e`, `e14f4dcc` | 真实扣款并开通成功，但触发了 `prolite` 别名缺失、升级后旧 AT 401 刷新失败与 Portal 退订关闭（修复后实单已验证） |
| **支付确认期卡/设备风控拒单** | 1 | `e0173eb0` | 建单/算税/Stripe 指纹/`ctoken` 全绿，`checkout/confirm` 返回 `200 {"status": "subscription_limit"}`（未扣款）；换卡后下一单 `4f927f13` 即刻通过 |
| **早期协议未对齐期 `unknown`** | 3 | `ea6fb61a`, `c537b1e6`, `a16203e2` | `sample5` 抓包修复前的过渡单（含 publishable-key 确认响应缺 `amount_received` 等协议差异） |
| **建单前置拦截（零扣款）** | 4 | `5b7b0d40`, `61984a13`, `7760be04`, `ddbb36e8` | 2 笔 `unusual_activity`（400）、1 笔 `invalid_session`（401）、1 笔 `reissue` 超时/上游异常，均安全止损于建单前 |
| **仅建单调试（未提交支付）** | 1 | `d8763a41` | 验证 `Plus / PH` 建单 + 美国免税州重算（`982.14 PHP`, `tax_delta=117.86`） |

### 2.2 逐单脱敏明细（按时间顺序）

| 订单前缀 | 套餐 | 出口/币种 | 税后金额 | `tax_delta` | 最终状态 | 支付/交付/续费 | 失败码 | 总耗时 |
|---|---|---|---|---|---|---|---|---:|
| `5b7b0d40` | `plus` | `PH` | — | — | `rejected` | `unpaid / pending / disabled` | `unusual_activity` | `22.5 s` |
| `d8763a41` | `plus` | `PH / PHP` | `982.14` | `117.86` | `awaiting_payment` | `unpaid / pending / pending` | — | `27.7 s` |
| `ea6fb61a` | `pro_lite` | `CL / CLP` | `86365` | `16409` | `unknown` | `processing / pending / pending` | — | `40.7 s` |
| `61984a13` | `pro_lite` | `CL` | — | — | `rejected` | `unpaid / pending / disabled` | `unusual_activity` | `19.4 s` |
| `c537b1e6` | `plus` | `PH / PHP` | `982.14` | `117.86` | `unknown` | `processing / pending / pending` | — | `162.3 s` |
| `7760be04` | `plus` | `PH` | — | — | `rejected` | `unpaid / pending / disabled` | `unknown_upstream` | `55.8 s` |
| `ddbb36e8` | `pro_lite` | `CL` | — | — | `rejected` | `unpaid / pending / disabled` | `invalid_session` | `4.4 s` |
| `a16203e2` | `plus` | `PH / PHP` | `982.14` | `117.86` | `unknown` | `processing / pending / pending` | `checkout_upstream_error` | `84.0 s` |
| `07916c2e` | `plus` | `PH / PHP` | `982.14` | `117.86` | `verifying` | `succeeded / verify_delayed / manual_required` | — | `112.8 s` |
| `e14f4dcc` | `pro_lite` | `CL / CLP` | `86365` | `16409` | `verifying` | `succeeded / verify_delayed / manual_required` | — | `163.4 s` |
| `e0173eb0` | `pro_lite` | `CL / CLP` | `86365` | `16409` | `rejected` | `failed / pending / pending` | `subscription_limit` | `85.3 s` |
| `4f927f13` | `pro_lite` | `CL / CLP` | `86365` | `16409` | `delivered` | `succeeded / delivered / canceled` | — | `131.1 s` |

> **耗时基线参考（当前住宅代理环境）**：建单 `22–28 s`；含完整 Stripe 遥测的支付确认 `63–122 s`；一等接口关闭自动续费 `6–11 s`。

---

## 3. 本轮实锤的 9 项上游变化与代码修复

### 3.1 直通建单模式与浏览器预热对齐
- **背景**：冷调 `/api/auth/session` 重颁 AT 在部分出口易触发风控或会话漂移。
- **修复**：
  - 新增 `direct_session_checkout` 运行开关（`packages/core/src/gss_core/runtime_flags.py`、`plus_har_protocol.py`、`services/api/src/gss_api/routers/orders.py`）：当 Session 含有未过期 `accessToken` 时，保留原 `oai-did` 与分片 Cookie，跳过冷重颁，经 `_soft_checkout_prelude()` 完成首页、`/backend-api/me`、`/backend-api/accounts/check/v4-2023-04-27`、`/backend-api/checkout_pricing_config/configs/{country}` 预热后直接建单。
  - 新增 `client_os_profile`（`auto / macos / windows / linux`）与 `expand_reduced_chrome_build()`（`packages/core/src/gss_core/chatgpt.py`），统一 UA、Client Hints、Accept-CH 动态捕获与 Sentinel 沙箱系统画像。

### 3.2 `PlusHarProtocol` 对齐 `sample5` 完整真卡支付抓包
- **根因**：
  1. Stripe 客户端 publishable-key 调 `POST /v1/payment_intents/{pi}/confirm` 成功时返回 `status: "succeeded"` 与 `amount`，**不返回** `amount_received`（该字段仅 secret-key 响应提供）；旧校验只认 `amount_received`，导致真卡扣款成功后被误判为 `unknown`。
  2. `save_payment_method_for_reuse` 需与结账页实际支持项一致。
- **修复**：`packages/core/src/gss_core/plus_har_protocol.py` 新增 `_intent_settled_amount()` 兼容客户端确认回执与服务端回执，新增 `_sync_save_choice()` 对齐保存卡选项，并将 `checkout_session_approval` + `chatgpt_checkout` 双 Sentinel 与 `m.stripe.com/6` 服务端 `guid/muid/sid` 采纳作为发卡前硬门禁。

### 3.3 `pro_lite` / `pro_max` 核验字段无下划线差异
- **现象**：`e14f4dcc` 实单已扣款并开通 Pro Lite，但持续停在 `verify_delayed`。
- **根因**：建单请求使用 `pro_lite` / `chatgptproliteplan`，但上游 `/backend-api/subscriptions` 与 `/backend-api/accounts/check` 返回的是**无下划线**的 `plan_type: "prolite"`（Pro Max 同理为 `"promax"`）。
- **修复**：在 `plus_har_protocol.py`、`services/account/src/gss_account/adapter.py`、`services/api/src/gss_api/routers/orders.py`、`services/api/src/gss_api/account_detail.py` 统一补齐 `prolite` / `chatgptproliteplan` / `promax` / `chatgptpromaxplan` 别名，并增加 1 次 2 秒 webhook 延迟短重试与观测日志。

### 3.4 升级后旧 `accessToken` 立即失效与分片 Cookie 轮转
- **现象**：支付完成后立即查订阅返回 `401 token_expired`，刷新 `/api/auth/session` 仍拿到旧状态或无效 token。
- **根因**：
  1. 套餐升级会令旧 JWT 立刻失效；真实浏览器刷新 `/api/auth/session` 时会附带 `X-OpenAI-Failed-Access-Token-Iat`、`X-OpenAI-Target-Path`、`X-OpenAI-Target-Route`（实证见 `sample5 [2835]`）。
  2. 浏览器导出 Session 常包含 `.0/.1` 分片 NextAuth Cookie；若轮转后只写回主 Cookie 而不清除旧分片，发包时旧分片会覆盖新 `sessionToken`。
- **修复**：`packages/core/src/gss_core/chatgpt.py` 实现 `refresh_session(..., force_refresh=True)` 附带上述失败令牌时间戳头，并在 `_sync_rotated_session_token()`、`_merge_cookies()`、`_cookie_header()` 中先清除旧分片 Cookie 再写入轮转后的会话令牌。

### 3.5 自动关闭续费迁至 ChatGPT 一等接口（替代已禁用的 Stripe Portal）
- **现象**：进入 Stripe Customer Portal 取消订阅始终返回 `stage=portal_cancel_disabled`。
- **逆向实证**：抓取并分析 ChatGPT 生产前端脚本（`ConsumerBillingSettings` -> `CancellationDialog` -> `332236.d2b98fcd55.js` / `663090.157fff978f.js`）确认：
  - OpenAI 已在 Stripe Customer Portal 配置中将 `features.subscription_cancel.enabled` 设为 `false`；
  - 真实取消入口已迁移至 ChatGPT 一等接口：**`POST https://chatgpt.com/backend-api/subscriptions/cancel`**，请求体 `{"account_id": "<account_id>"}`，请求头携带 `chatgpt-account-id`。
- **修复**：
  - `packages/core/src/gss_core/chatgpt.py` 新增 `ChatGPTClient.cancel_subscription_direct()`（内置 401 强制刷新重试）；
  - `services/api/src/gss_api/auto_renew.py` 将其设为主路径，保留 Stripe Portal + Playwright 兜底（仅当一等接口返回 404/405/3xx 时降级），并增加 3 出口 SID 代理轮换重试；
  - 修正 `apps/web/src/components/AccountDetailModal.tsx` 在退订失败时误显绿色提示条的 UI 问题。
  - **实测验证**：手动退订与 `4f927f13` 自动退订均直出 `POST /backend-api/subscriptions/cancel -> 200 OK`。

### 3.6 住宅代理 TLS 断连（`curl (35)`）多出口 SID 自愈
- **现象**：部分住宅代理节点在 TLS 握手或请求途中断开（`curl: (35) SSLError`），导致查号、核验或关闭续费单点失败。
- **修复**：在 `services/api/src/gss_api/account_detail.py`（`_fetch_serial`）、`services/api/src/gss_api/auto_renew.py`、`services/account/src/gss_account/adapter.py` 中为只读查号与幂等取消操作加入最多 3 个新鲜出口 SID 的自动切换重试，并在全部耗尽时返回干净的 `DomainError`（不再抛 500）。

### 3.7 手动账号查询全面去缓存
- **现象**：账号详情查询命中旧快照缓存或失败时回落脏缓存，导致刚开通/刚取消续费的状态显示滞后或误报。
- **修复**：移除 `services/api/src/gss_api/routers/account.py` 与 `services/api/src/gss_api/routers/public_portal.py` 的缓存命中返回与异常降级读旧缓存逻辑；`AccountDetailModal.tsx` 与 `PortalModals.tsx` 统一传 `forceRefresh: true` 并移除「缓存」标签，每次手动查询均实时直查上游。

### 3.8 固定美国免税州账单后台托管闭环
- 新增 `services/api/src/gss_api/routers/fixed_billing.py`、`apps/web/src/api/fixedBilling.ts`、`apps/web/src/components/FixedBillingSettings.tsx` 及 `packages/core/src/gss_core/fixed_billing.py` 托管落盘能力，支持在管理后台直接配置并校验 5 个美国免税州（`OR / DE / MT / NH / AK`），自动以 `0600` 权限落盘到 `config/fixed-billing.json`。

### 3.9 `subscription_limit` 真实语义与阶段纠偏
- **实证**：从 ChatGPT 线上前端包 `13455.adbcf21f0b.js` 查得官方原始文案：
  ```text
  CheckoutForm.bannerMessageSubscriptionLimitReached:
  "Too many subscriptions associated with this card or device. We can’t complete this purchase."
  ```
- **纠偏**：`subscription_limit` 发生在 `POST /backend-api/payments/checkout/confirm`（而非建单阶段），含义是**银行卡指纹或浏览器设备 `oai-did` 关联订阅数超限**（而非“当前账号订阅上限”）。已同步修正 `plus_har_protocol.py`、`checkout_protocol.py`、`orders.py`、`quickPayProgress.ts`、`constants.ts`。

---

## 4. 错误码与风控实战速查表（Ops Runbook）

| 错误码 / 现象 | 发生阶段 | 真实根因 | 是否可能已扣款 | 标准处置动作 |
|---|---|---|---|---|
| `subscription_limit` | `POST /backend-api/payments/checkout/confirm` (`200`) | 同一张银行卡（`card.fingerprint`）或同一设备（`oai-did`）近期关联订阅过多 | **否（未进 Stripe 扣款）** | **换一张新卡**；同时确保不同账号导出 Session 时浏览器 Cookie / `oai-did` 相互隔离 |
| `unusual_activity` | `POST /backend-api/payments/checkout` (`400`) | 建单风控（IP 声誉、会话与环境异常） | **否** | 保持 `checkout_rotate=true` 自动换 SID 重试；多次出现时更换代理池/地区或新导出 Session |
| 浏览器「无法加载付款表单。请重试。」 | 浏览器填卡前（建单或加载 `js.stripe.com` iframe） | 浏览器前端加载 Stripe Elements 脚本/iframe 超时断连，或建单 `400 unusual activity` | **否** | **仅浏览器前端出现**，纯协议模式不加载前端 JS/iframe，天然免疫 |
| `card_declined` / `insufficient_funds` | `POST /v1/confirmation_tokens` 或 `payment_intents/{id}/confirm` (`402`) | 发卡行拒付 / Stripe Radar 拦截 / 余额不足 | **否** | 核对银行 App 拒付原因、境外订阅权限与含税余额；不要用同一张卡高频重试 |
| `proxy_tunnel_failed` / `curl (35)` | 任意上游请求连接阶段 | 住宅代理节点 TLS 断连或隧道超时 | 建单前：**否**；确认阶段：**待核对** | 查号/建单/关续费已自动换 SID 重试；确认阶段超时须先执行只读对账，切勿盲目重付 |
| `invalid_session` / `session_expired` | 鉴权或升级后核验 (`401`) | `accessToken` 过期/被吊销且缺少可用刷新凭证 | 建单前：**否** | 重新登录 ChatGPT 导出完整 Session JSON（务必包含 `sessionToken` 或 `refresh_token`） |
| `payment_unknown_state` | `confirm` 发出后无明确终态 | 网络中断或上游超时，结果未明朗 | **可能已扣款** | 系统自动加资金锁；先到「资金锁与对账」执行**只读对账**并核对银行账单，再人工解锁 |

---

## 5. 运营与批量实操避坑清单

1. **单卡订阅上限控制**：同一张银行卡在 OpenAI 侧通常 2–3 单后即触发 `subscription_limit`（本轮实测 `sample5` + `07916c2e` + `e14f4dcc` 后，第 4 单 `e0173eb0` 直接命中，换卡后 `4f927f13` 立刻成功）。批量运行需提前规划卡池轮换上限。
2. **导出 Session 必须做设备隔离（`oai-did`）**：
   - 开启 `direct_session_checkout=true` 时，系统原样保留导出 Session 里的 `oai-did`；
   - 若多个账号在**同一 Chrome Profile 且不清理 Cookie** 下连续登录导出，所有账号共享同一个 `oai-did`，会被 OpenAI 归入同一台设备配额而触发 `subscription_limit`；
   - 建议每个账号用**独立无痕窗口或独立 Chrome Profile** 导出，或者关闭 `direct_session_checkout` 走重签发隔离设备。
3. **导出完整 Session JSON**：确保同时包含 `accessToken` 与 `sessionToken`（必要时含分片 Cookie）。支付升级后旧 AT 会立即失效，缺少 `sessionToken` 将无法自动刷新完成到账核验与自动关闭续费。
4. **推荐运行开关组合（`config/runtime-flags.json`）**：
   - `checkout_rotate: true`（建单代理超时自动换 SID 重试）
   - `direct_session_checkout: true`（配合独立浏览器环境导出 Session，避免冷重颁触发风控）
   - `plus_engine: "har"`、`har_telemetry: "full"`
5. **后续可选增强（未在 v0.2.0 强行改动，按需开启）**：
   - 目前 `config/fixed-billing.json` 若未手动替换，会发送默认占位邮箱 `billing@example.com` 至 Stripe `confirmation_tokens`；而 `sample5` 真实浏览器抓包显示官方前端发送的是**该 ChatGPT 账号自己的注册邮箱**。后续如规模化跑单，可评估将 Stripe `billing_details[email]` 改为默认跟随账号邮箱（同时保留固定免税州地址）。
