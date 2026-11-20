# 项目能力基线与竞品对齐审计

> 审计日期：2026-11-20  
> 审计范围：`packages/contracts`、`packages/core`、`services/api`、`apps/web`、`tests`  
> 审计方式：静态代码走查 + 现有测试 + 已部署实例实测记录  
> 竞品基准：`chong.gptnb.cc` 的公开业务描述（本次未能获取其内部接口/抓包，不对其未公开实现做事实断言）

## 0. 结论摘要

当前项目已经完成一条可运行的 **Route C 单一键建单/支付链路**，并且以下资金安全机制已有代码和测试：

```text
Session 跨区重签发
Route C 浏览器解码
pro_lite / Plus custom checkout
Sentinel fail-closed
PaymentIntent 字符串补全
卡号不落库
card_declined / insufficient_funds 分类
```

但距离“竞品级全自动运营”仍有明显差距，最关键的三个缺口是：

```text
P0-1  建单阶段没有自动 SID 轮换，GSS_PROXY_SID 仍是单点
P0-2  unknown 状态没有跨进程/跨请求的硬锁，前端仍可重新发起
P1-1  缺少 PH / CL / EG × Plus / pro_lite × US 免税账单的自动化矩阵证据
P1-2  可观测性只有日志与粗粒度 job stage，没有 browser-diagnostics / route_test 接口
```

因此当前基线应表述为：

```text
可用于受控测试和人工运营
尚未达到可无人值守的竞品级高可用
```

---

## 1. 核心业务铁律

以下要求是项目底线，任何迭代不得用“优化体验”或“提高成功率”为由绕过。

### 1.1 资金安全

```text
1. 卡号、CVC、有效期只允许存在于当前请求内存和支付协程局部变量。
2. 不写 Order、OrderView、Vault、SQLite、日志、SSE、前端 localStorage。
3. 未产生明确失败前，不允许对同一订单重复提交卡。
4. unknown / processing 状态不得自动换卡、自动重试、自动重新扣款。
5. Sentinel 失败必须 Fail Closed，不能继续 confirm。
6. subscription_limit 是终态业务拒绝，不得当作 unknown 重试。
7. 只有明确的 paid / payment_status=succeeded 且回执匹配，才允许交付。
```

### 1.2 目标业务模型

```text
低价区出口（当前验证过 PH；CL 为目标）
+ 固定美国免税账单地址（OR / Portland）
+ Plus 或 pro_lite（Pro 100）
```

允许跨区 Session，但必须满足：

```text
重签发只保留身份凭据
旧国家 Cookie / 旧设备 ID 不进入目标出口
所有建单/支付请求使用同一目标出口
```

### 1.3 运营安全

```text
不记录完整 Session
不记录完整卡号
不记录代理密码
不把上游原始错误体直接透传给前端
不把开发调试开关默认打开
```

---

## 2. 五维审计

### 2.1 输入无感与会话清洗

**结论：基本实现，跨区路径成立；同国路径按设计保留原环境。**

已实现：

- `packages/core/src/gss_core/session_token.py`
  - `parse_session_bundle()` 解析 `accessToken`、`sessionToken`、`statsigContext`、cookies。
  - `identity_cookies_for_reissue()` 只允许：
    ```text
    __Secure-next-auth.session-token
    ```
  - 旧 `__cf_bm`、`_cfuvid`、`oai-did`、`oai-sc` 不会进入重签发客户端。
- `packages/core/src/gss_core/chatgpt.py`
  - `reauthorize_for_country()` 只带 session token 到目标出口。
  - 重新生成 `device_id`、UA、`oai-session-id`。
  - `refresh_session(trust_statsig=False)` 禁止旧 statsig 覆盖新身份。
  - 刷新后强制 `oai-did = client.device_id`。
- `packages/core/src/gss_core/browser_context.py`
  - Route C 只透传 UA、`sec-ch-ua`、`sec-ch-ua-platform`、`Accept-Language`。
- 测试：
  - `tests/unit/test_session_reauth.py`
    - 覆盖旧 `__cf_bm`、`_cfuvid`、`oai-sc` 被丢弃。
    - 覆盖旧 deviceId / UA 不覆盖新身份。

隐藏缺口：

```text
GAP-S1  同国路径 from_raw_session() 仍保留原 cookies/statsig。
        对“同国”这是合理的，但如果用户以为所有请求都完全重生，
        需要在 UI/文档明确：只有跨区 reissue 会彻底清洗。

GAP-S2  没有真实 CF challenge 的端到端回放测试。
        当前只证明 Cookie 不进入内存客户端，未证明目标出口下
        __cf_bm / cf_clearance 会稳定重新生成。

GAP-S3  没有把“清洗后指纹”作为结构化诊断返回给前端。
```

### 2.2 多路由与全套餐矩阵

**结论：Plus/pro_lite 共用 custom checkout；参数结构一致，CL 失败不能仅归因于 payload。**

已实现：

- `RouteProfile` 只有 A / B / C：
  - `packages/contracts/src/gss_contracts/route.py`
  - A = 历史 curl_cffi
  - B/C = 浏览器解码 + proxy checkout/confirm
- `_PLAN_NAME`：
  - `plus -> chatgptplusplan`
  - `pro_lite -> chatgptprolite`
  - `pro -> chatgptpro`
  - `pro_max -> chatgptpromax`
- `create_checkout()`：
  - `billing_details.country = exit_country`
  - `billing_details.currency = currency_for(exit_country)`
  - `checkout_ui_mode = custom`
  - `checkout_source = chatgpt_web_merged`
  - `purchase_attempt_id` 每次新建
- 测试：
  - `test_pro_lite_cl_payload_uses_cl_price_but_keeps_us_tax_billing`
  - `test_create_checkout_maps_pro_max_to_chatgptpromax`

当前实际参数语义：

```text
pro_lite + CL 出口 + US 账单
→ 建单 body: plan_name=chatgptprolite,
             billing_details.country=CL,
             billing_details.currency=CLP
→ 后续 tax / confirm 使用 US 免税地址
```

隐藏缺口：

```text
GAP-M1  没有 Route D/E 的实现，只有 A/B/C。
GAP-M2  没有 PH/CL/EG × Plus/pro_lite × US 的自动化矩阵测试。
GAP-M3  没有竞品真实 create_checkout 抓包，
        不能声称 billing_details 字段与竞品 100% 一致。
GAP-M4  未证明 CL + pro_lite 的失败是 payload 还是 IP；
        已观测到同一 Session 在 US 出口可建单，CL 出口失败，
        更支持“IP/风控”而非“plan_name 错误”。
GAP-M5  前端固定 billing_country=US，无法直接测试 CL/EG 账单闭环。
```

### 2.3 建单阶段代理轮换与高可用

**结论：未实现自动轮换，当前是单 SID 单点。**

现状：

- `packages/core/src/gss_core/proxies.py`
  - `new_proxy_sid()` 在 `GSS_PROXY_SID` 存在时**始终返回固定值**。
  - `checkout_proxy_for()` 只重写地区和固定 SID，不负责失败重试。
- 部署配置：
  - `GSS_PROXY_SID=5510901`（本次新 CL SID）
  - `proxy-gateways.json` 的 checkout/approve 使用同一 SID。
- 没有 `proxy_rotations` 状态机、没有失败计数、没有冷却时间。

隐藏缺口：

```text
GAP-P1  建单遇到 proxy_tunnel_failed / unusual_activity 不会自动换 SID。
GAP-P2  没有“仅建单阶段、未扣款”安全重试边界。
GAP-P3  没有代理健康检查结果持久化，无法知道哪个 SID 已暴露。
GAP-P4  没有多供应商/多地区池，CL 住宅 IP 供给仍是单点。
```

建议优先级：**P0**。  
建议实现：

```text
create_checkout only
→ 失败码 in {proxy_tunnel_failed, unusual_activity}
→ 记录 failed_sid
→ 从 CL 池取下一个未使用 SID
→ 重建客户端（保留 session token，不复制旧 Cookie）
→ 最多重试 1 次
→ 第二次失败立即停止，不进入支付
```

### 2.4 单页一键充值前端与资金安全闭环

**结论：单页一键建单/支付链路已存在，但前端是 React，不是 Vue；unknown 硬锁不足。**

已实现：

- `apps/web/src/pages/QuickPay.tsx`
  - Session、套餐、出口国家、卡号/有效期/CVC 同页。
  - 提交后按钮禁用，轮询 `GET /api/payments/quick-pay/{job_id}`。
  - 阶段文案、金额/币种、failure_code。
  - 仅建单调试开关。
- `apps/web/src/App.tsx`
  - 默认页签为 `quick`。
- `services/api/src/gss_api/services/order_flow.py`
  - 异步 job：订单 → reissue → decode → checkout → pay。
  - 卡只在 `payload.card` 与局部变量，不写入 job。
- `services/api/src/gss_api/routers/quick_pay.py`
  - `POST /api/payments/quick-pay` 202。
  - `GET /api/payments/quick-pay/{job_id}`。
  - 必须 `Idempotency-Key`。
- 资金安全已有：
  - Sentinel fail-closed。
  - `subscription_limit` 终态。
  - PaymentIntent 字符串补全。
  - 卡号不落盘。
  - `unknown` 不自动支付。

隐藏缺口：

```text
GAP-F1  QuickPay 在 status=unknown 后按钮恢复可用，用户可用新 key 再发一单。
        当前只有提示，没有前端硬锁。
GAP-F2  quick-pay job 和 order store 都是进程内存，容器重启即丢失；
        unknown 订单无法跨重启恢复。
GAP-F3  一键接口没有持久化 job 状态，无法多副本部署。
GAP-F4  前端没有把“同卡/同 session 已 unknown”作为浏览器侧幂等锁。
GAP-F5  卡字段是普通 input，不是 Stripe iframe；卡数据进入本站 JS 内存，
        虽不落盘，但 PCI 暴露面高于竞品的 hosted 输入框模式。
```

### 2.5 可观测性与诊断

**结论：错误码和阶段日志已有，但缺少结构化诊断 API。**

已实现：

- `packages/contracts/src/gss_contracts/errors.py`
  - `unusual_activity`
  - `subscription_limit`
  - `sentinel_challenge_failed`
  - `proxy_tunnel_failed`
  - `proxy_authentication_failed`
  - `upstream_timeout`
  - `browser_decode_unavailable`
  - `insufficient_funds`
  - `card_declined`
  - `unknown_upstream`
- `packages/core/src/gss_core/upstream_errors.py`
  - 状态码缺失时仍可识别 `unusual_activity`。
  - 卡侧 `insufficient_funds` / `card_declined` 分类。
- `services/api/src/gss_api/routers/orders.py`
  - `stage_timing stage=reissue/browser_decode/create_checkout`。
  - `checkout_payload` 只记录字段名，不记录值。
  - 重签发失败不再用通用文案掩盖原因。
- `QuickPayJob`：
  - `stage`、`status`、`failure_code`、`message`。

隐藏缺口：

```text
GAP-O1  前端 FAILURE_LABEL 缺少 checkout_upstream_error / invalid_session /
        proxy_authentication_failed / unknown_upstream，部分错误只显示通用“充值失败”。
GAP-O2  stage_timing 只在日志，不在 quick-pay 响应中返回毫秒耗时。
GAP-O3  没有 browser-diagnostics / route_test 接口。
GAP-O4  没有把 upstream_status / upstream_url / sentinel flow 作为结构化字段返回。
GAP-O5  没有代理 SID、出口 IP、ASN 的诊断快照（当前只写日志）。
GAP-O6  checkout_payload 日志是本次新增，部署后需要用真实失败单验证字段。
```

---

## 3. 当前已 100% 完成并验证通过

以下项目有代码 + 自动测试或实测证据：

```text
[x] Session JSON 解析（accessToken / sessionToken / statsig）
[x] 跨区 reissue 丢弃旧环境 Cookie（__cf_bm/_cfuvid/oai-did/oai-sc）
[x] 目标出口下生成新 device_id / oai-session-id
[x] Route C 浏览器解码
[x] Plus / pro_lite custom checkout
[x] pro_lite 使用 chatgptprolite
[x] Sentinel 失败 fail-closed
[x] subscription_limit 终态拒绝
[x] 卡号不进入 Order/Vault/DB
[x] PaymentIntent 字符串自动补全
[x] card_declined / insufficient_funds 分类
[x] QuickPay 单页表单 + 轮询阶段
[x] 仅建单调试模式
[x] 一键接口 Idempotency-Key
[x] 前端 Session 输入框固定高度
[x] 默认进入一键充值页
[x] 测试套件通过：710 passed（2026-11-20）
```

---

## 4. Gap List 与优先级

| 优先级 | ID | 差异 | 影响 | 建议 |
|---|---|---|---|---|
| P0 | GAP-P1/P2 | 建单失败不自动换 SID | 单 IP 被风控即整单失败 | 建单专用 SID 轮换，最多 1 次，绝不支付重试 |
| P0 | GAP-F1 | unknown 后前端可重新发起 | 可能重复建单/重复扣款风险 | unknown 硬锁，需人工解锁 |
| P0 | GAP-F2/F3 | job/order 仅内存 | 重启丢单、无法多副本 | 持久化 job/order 状态与幂等键 |
| P1 | GAP-M2/M4 | 缺矩阵测试 | 无法判断地区/套餐差异 | PH/CL/EG × Plus/pro_lite × US 自动化矩阵 |
| P1 | GAP-M3 | 无竞品抓包 | 不能声称载荷完全一致 | 抓取竞品 create_checkout 网络请求并脱敏比对 |
| P1 | GAP-S1/S2 | 同国路径保留旧 Cookie | 同国请求可能携带旧环境 | 明确策略；跨区已清洗，同国按设计 |
| P1 | GAP-O1 | 前端错误码映射不全 | 运营无法定位 | 补全 UI 映射表 |
| P1 | GAP-O2 | 阶段耗时只在日志 | 无法量化瓶颈 | quick-pay 响应增加 `timings_ms` |
| P2 | GAP-M1 | 只有 A/B/C | 没有 D/E 备份路线 | 评估 Route D/E，不盲目增加 |
| P2 | GAP-O3/O4/O5 | 无诊断接口 | 排障靠日志 | 增加只读 `/api/diagnostics` |
| P2 | GAP-F5 | 卡输入在本站 JS | PCI 暴露面高 | 评估 Stripe iframe / hosted 输入 |
| P2 | GAP-M5 | 前端固定 US 账单 | 无法测 CL/EG 账单 | 增加账单国家选项，但默认 US |

---

## 5. 竞品对齐判断

基于当前证据，只能做出以下保守判断：

```text
已对齐：
- 低价区出口 + 美国免税账单的业务方向
- 单页输入 Session/套餐/卡
- 建单阶段跨区 reissue
- 卡号不落盘
- 错误码可读

未对齐：
- 竞品级的自动 SID 轮换与高可用
- unknown 的跨进程硬锁
- 多地区/多套餐矩阵证据
- browser-diagnostics / route_test 级别的可观测性
- 竞品真实 checkout 载荷的抓包比对
```

### 关于 `CL + pro_lite` 被拦的结论

本次日志显示：

```text
session_country=SG
exit_country=CL
billing_country=US
plan=pro_lite
create_checkout 400 unusual_activity
```

同一新 Session 在 US 出口可以建单，因此不能把失败归因于：

```text
plan_name 写错
pro_lite 不存在
billing_country 参数缺失
```

更可能是：

```text
CL 出口 IP 被风控
或 SG → CL 跨洲重签发的会话仍被关联
或 OpenAI 对 pro_lite + 跨国组合的风控更严
```

需要矩阵实验才能区分，不应继续盲目改 payload。

---

## 6. 下一次迭代前的固定流程

每次开始新开发前：

```text
1. 先读 docs/PROJECT_BASELINE.md
2. 选择 Gap List 中一个 P0/P1 项
3. 先写可复现测试或抓包证据
4. 再改代码
5. 改动必须说明影响哪个铁律
6. 全量测试通过 + 部署验证后再更新本文件
```

禁止：

```text
无证据连续改 create_checkout payload
在 unknown 订单上自动重试
在支付阶段自动换 SID
把卡号/完整 Session 写进日志或文档
把竞品未公开实现当成已验证事实
```

---

## 7. 证据索引

```text
packages/contracts/src/gss_contracts/errors.py
packages/contracts/src/gss_contracts/route.py
packages/core/src/gss_core/session_token.py
packages/core/src/gss_core/chatgpt.py
packages/core/src/gss_core/proxies.py
packages/core/src/gss_core/upstream_errors.py
packages/core/src/gss_core/browser_context.py
services/api/src/gss_api/services/order_flow.py
services/api/src/gss_api/routers/quick_pay.py
services/api/src/gss_api/routers/orders.py
services/api/src/gss_api/routers/protocol_cards.py
apps/web/src/pages/QuickPay.tsx
apps/web/src/App.tsx
tests/unit/test_session_reauth.py
tests/unit/test_chatgpt_protocol.py
tests/unit/test_upstream_errors.py
tests/unit/test_quick_pay_flow.py
docs/QUICK_PAY_API.md
```
