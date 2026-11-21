# AI 代码审查交接说明

> 用途：把这个项目交给另一个 AI/工程师做独立审查。  
> 本文不包含 Session、卡号、Cookie、代理密码或任何生产凭证。

## 1. 项目一句话

自托管的 ChatGPT 订阅充值控制台：

```text
FastAPI 后端
+ React 19 / Mantine 前端
+ Route C（Playwright 浏览器解码 + 固定出口代理）
+ SQLite 持久化
+ QuickPay 一键建单/支付
+ unknown 资金锁
+ 自动关闭连续订阅
+ 建单阶段 SID 轮换（当前默认关闭）
```

仓库：

```text
https://github.com/3246147496lxt-dotcom/gpt-subscribe-studio
```

本地路径示例：

```text
/Users/pu/Documents/Codex/2026-11-03/x/gpt-subscribe-studio-1
```

---

## 2. 审查目标

请优先审查：

```text
1. 资金安全：是否可能重复扣款、误交付、unknown 误释放
2. 持久化：订单/任务/锁/幂等键是否会在重启后丢失或错乱
3. 并发与幂等：同一账号、同一 Idempotency-Key、多 worker 的行为
4. Session 清洗：跨区 reissue 是否真的只带身份凭据
5. Route C：浏览器解码与代理出口是否可能错配
6. 代理轮换：是否严格限制在建单阶段
7. 自动关闭续费：是否可能调用退款、误伤当期权益或误解锁
8. 前端：unknown 硬锁、localStorage、错误码展示是否完整
9. 测试盲区：哪些结论只有单元测试，没有真实上游验证
10. 部署安全：SQLite、环境变量、登录会话、容器边界
```

不要默认“测试全绿 = 没有问题”。  
请主动找反例、竞态、异常路径和测试未覆盖的边界。

---

## 3. 必须先读的文档

```text
docs/PROJECT_BASELINE.md
docs/QUICK_PAY_API.md
docs/COMPETITOR_HAR_ANALYSIS.md
docs/HAR_REVIEW_CHECKLIST.md
```

`PROJECT_BASELINE.md` 是当前能力基线和 Gap List，任何审查结论都应对照它。

---

## 4. 核心业务铁律

```text
1. 卡号/CVC/有效期只允许存在于请求内存和支付协程局部变量
2. 不写 Order、OrderView、Vault、SQLite、日志、SSE、localStorage
3. 未明确失败前，不允许同一订单重复提交卡
4. unknown / processing 不得自动换卡、自动重试、自动重新扣款
5. Sentinel 失败必须 Fail Closed，不能继续 confirm
6. subscription_limit 是终态业务拒绝，不得当 unknown 重试
7. 只有 paid + 回执匹配才允许交付
8. 自动关闭续费只能设置到期不续费，严禁调用退款
9. 代理 SID 只允许在建单阶段、未进入支付前轮换
10. 不把上游原始响应体、Cookie、token、卡数据透传给前端
```

---

## 5. 关键文件

### 契约

```text
packages/contracts/src/gss_contracts/errors.py
packages/contracts/src/gss_contracts/state.py
packages/contracts/src/gss_contracts/route.py
packages/contracts/src/gss_contracts/models.py
```

### 核心

```text
packages/core/src/gss_core/chatgpt.py
packages/core/src/gss_core/session_token.py
packages/core/src/gss_core/proxies.py
packages/core/src/gss_core/sentinel.py
packages/core/src/gss_core/browser_decode.py
packages/core/src/gss_core/browser_context.py
packages/core/src/gss_core/upstream_errors.py
packages/core/src/gss_core/vault.py
```

### API / 编排

```text
services/api/src/gss_api/main.py
services/api/src/gss_api/store.py
services/api/src/gss_api/persistence.py
services/api/src/gss_api/locks.py
services/api/src/gss_api/services/order_flow.py
services/api/src/gss_api/routers/orders.py
services/api/src/gss_api/routers/quick_pay.py
services/api/src/gss_api/routers/protocol_cards.py
services/api/src/gss_api/routers/locks.py
services/api/src/gss_api/schemas.py
```

### 前端

```text
apps/web/src/pages/QuickPay.tsx
apps/web/src/pages/OrderNew.tsx
apps/web/src/api/quickPay.ts
apps/web/src/App.tsx
```

### 测试

```text
tests/unit/test_session_reauth.py
tests/unit/test_quick_pay_flow.py
tests/unit/test_persistence_and_locks.py
tests/unit/test_cancel_auto_renew.py
tests/unit/test_checkout_proxy_rotation.py
tests/unit/test_upstream_errors.py
tests/integration/test_country_alignment.py
```

---

## 6. 当前状态与已知限制

```text
测试：723 passed（以仓库当前运行为准）
部署：单节点 Docker，SQLite /data/gss.db
Route C：启用
自动关闭续费：框架已实现，真实 cancel endpoint 未配置，默认 manual_required
建单 SID 轮换：已实现，但生产默认 GSS_CHECKOUT_ROTATE=0
unknown 锁：后端 + 前端已实现
```

已知风险，请重点复核：

```text
1. SQLite 单节点，不是多副本强一致方案
2. quick-pay job lease 不是完整分布式租约
3. 自动取消续费的真实 ChatGPT 接口尚未验证
4. 代理轮换使用随机 SID，可能选中不可用节点
5. 部分结论来自 HAR/单元测试，不是生产全链路
6. 前端卡输入仍是本站普通 input，不是 Stripe iframe
```

---

## 7. 请重点回答的问题

### 资金安全

```text
- 有没有路径能在 unknown 后再次提交卡？
- PaymentIntent 字符串补全后是否可能重复 confirm？
- 自动取消续费失败是否可能误回滚 paid？
- 人工解锁是否可能在已扣款时放行？
```

### 持久化与并发

```text
- 重启后订单/任务/锁是否会不一致？
- 同一 Idempotency-Key 并发请求是否可能创建两单？
- SQLite 唯一索引是否覆盖所有竞态？
- quick-pay 任务崩溃后是否会错误重放支付？
```

### Session / 代理

```text
- 跨区 reissue 是否可能带入旧 __cf_bm/cf_clearance/oai-did？
- browser_context 与代理出口是否可能错配？
- SID 轮换是否可能影响 confirm？
- 代理失败是否可能被误判为支付失败？
```

### 自动取消续费

```text
- 是否可能调用退款接口？
- 是否可能取消当期权益？
- 取消失败是否会触发资金锁或回滚订单？
- 手动重试接口是否接受新 Session？
```

### 前端

```text
- unknown 后是否真的无法重复提交？
- localStorage 被清空后后端是否仍能拦截？
- 错误码和阶段耗时是否完整？
```

---

## 8. 审查输出格式

请按以下格式输出，不要直接改代码：

```text
## 结论摘要
- 是否可上线：
- 最高风险：

## Findings
### F-01 [severity]
- 文件/行号：
- 问题：
- 攻击/失败路径：
- 证据：
- 建议修复：
- 建议测试：

## 测试缺口
...

## 与 PROJECT_BASELINE 的差异
...

## 建议优先级
P0 / P1 / P2
```

没有证据的猜测请标为：

```text
hypothesis
```

不要把推测写成已验证事实。

---

## 9. 运行方式

```bash
cd <repo>
uv run python -m pytest tests -p no:warnings
cd apps/web && npm run build
```

不要运行真实支付。  
不要使用生产 Session 或真实卡。  
只使用 fake adapters、单元测试和脱敏 HAR。

---

## 10. 给审查 AI 的边界

```text
- 只做代码审查、威胁建模、测试设计
- 不主动扫描任何第三方生产系统
- 不请求或输出 Session/Cookie/卡号/代理密码
- 不根据未验证竞品实现直接改上游 payload
- 不把“竞品这样做”当成事实，除非有抓包或文档证据
```
