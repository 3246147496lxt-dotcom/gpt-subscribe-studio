# 项目状态总结

**更新时间**: 2026-11-25  
**项目名称**: gpt-subscribe-studio  
**当前阶段**: 四阶段自动化运维与工程稳定性优化完成

## 架构与已上线能力总览

### 1. 核心协议与安全红线 (`packages/contracts` & `packages/core`)
- **领域契约与状态机**：显式订单状态、支付状态与交付门槛校验。
- **ChatGPT / Stripe 协议模块拆分**：核心传输逻辑位于 `gss_core/chatgpt.py`，无状态响应解析、拒绝码建议与脱敏日志辅助已解耦至 `gss_core/chatgpt_parsers.py`（并在 `chatgpt.py` 全量 Re-export 保持向后兼容）。
- **安全铁律**：
  - 凭证（Session / Token / 卡号 / CVC / 账单原文 / 代理口令 / Webhook Secret）严禁持久化或明文打印。
  - 已触发付款确认的订单绝不自动重放扣款；通过 `payment_locks` 防止重复扣款。
  - 内置 `scripts/release_check.py` 与 GitHub Actions CI（`.github/workflows/ci.yml`）双重扫描。

### 2. 持久化队列、有界缓存与数据库维护 (`services/api/src/gss_api/`)
- **原生 SQLite WAL 持久化 (`persistence.py`)**：
  - 持久化 `orders`、`quick_pay_jobs`、`order_events`、`payment_locks`、`webhook_deliveries`。
  - 启动自动平滑迁移旧库列并建队列索引；支持超期终态审计事件安全清理与 `PRAGMA wal_checkpoint(TRUNCATE)`。
  - *注：`db.py`、`repository.py` 及 `services/orchestrator/src/gss_orchestrator/worker.py` 为早期 SQLAlchemy 双轨模块，已标注 Legacy 保留供旧测试兼容。*
- **有界缓存 + 按需回查 (`store.py`)**：
  - 启动仅预加载最近热订单（默认上限 `GSS_STORE_MAX_REHYDRATE=500`），冷订单按需回查 SQLite；内存缓存超过 `GSS_STORE_MAX_CACHED=1000` 自动 LRU 淘汰最老终态订单。
- **并发队列与优雅停机 (`services/order_flow.py`)**：
  - 持久化队列支持受控并发（`GSS_QUICK_PAY_MAX_CONCURRENCY`）、排队位次追踪与服务重启/停机时的在途任务排空。

### 3. 前置体检、只读对账与可观测性
- **单条与批量前置体检 (`POST /api/payments/preflight`、`POST /api/payments/preflight/batch`)**：
  - 一键核验 Session 有效性、固定核验账单文件权限与国家一致性、活跃资金锁、队列负载以及可选代理/上游连通性（批量支持最多 20 条并发核验，绝不接收卡号）。
- **只读自动对账补偿器 (`services/reconciler.py`)**：
  - 针对 `paid_pending_delivery` 与 `unknown` 订单按阶梯退避执行纯只读状态核对；支持 `GET /api/payments/reconciler` 与按订单/作业/资金锁手动触发只读对账。
- **SSE 实时推送与 `requires_action` 断点闭环 (`routers/events.py`)**：
  - `GET /api/events/quick-pay/{job_id}/stream` 推送任务全生命周期实时进度，客户端断流自动退回快照轮询。
- **脱敏 Webhook 告警 (`services/webhooks.py`、`routers/webhooks.py`)**：
  - 支持 HTTPS URL 校验（拒绝内网/环回 SSRF 地址）、可选 HMAC-SHA256 签名、指数退避重试与投递历史审计。
- **服务端账本、资金锁与脱敏 CSV 导出 (`routers/orders.py`、`routers/locks.py`)**：
  - `GET /api/orders` 支持状态、套餐、关键词及日期范围分页检索；`GET /api/orders/export.csv` 流式导出带 UTF-8 BOM 且具备 CSV 公式注入防护的脱敏账本。
- **只读系统自检与一键维护 (`routers/system.py`、`services/diagnostics.py`)**：
  - `GET /api/system/diagnostics` 返回 Python/Node 版本、SQLite 主库/WAL 体积、内存缓存单数、磁盘余量、账单文件权限与后台服务状态；`POST /api/system/maintenance` 执行安全清理与 WAL 截断。

### 4. 固定美国免税账单 + 自由出口
- **固定账单资料**：新增 `GSS_FIXED_BILLING_FILE`（单个美国免税州地址，0600/0400、绝对路径、非符号链接），并提供 `scripts/setup_fixed_us_billing.py` 一键生成；`GSS_BILLING_PROFILES_FILE` / `GSS_VERIFIED_BILLING_FILE` 优先级更高，向后兼容。
- **出口与账单解耦**：账单国家固定 US，出口可按订单选择任意地区并轮换 SID；建单 `billing_details.country` 使用账单国、`currency` 随出口（与网页端 HAR 一致）。默认允许分离，`GSS_REQUIRE_EXIT_BILLING_MATCH=1` 可恢复严格模式。
- **上游回显校验**：仅允许回显当前出口国；其它国家或地址字段冲突仍在提交付款前拦截。订单、付费、税费重算与 payment method 全部使用同一份固定地址。
- **地址兜底**：`address_for("US")` 改为确定性的固定免税地址，不再随机生成。

### 5. Web 前端工作台 (`apps/web`)
- **按需懒加载与代码拆包**：非首屏面板与弹窗采用 `React.lazy` + 首次访问挂载，Vite `manualChunks` 将 React、Mantine 与图标拆分为独立分包，零大包警告。
- **功能标签页**：一键充值（含前置体检、SSE 实时进度与队列监控）、批量体检与安全入队、分步调试、服务端开通记录（含套餐/日期筛选与 CSV 导出）、资金锁与对账台、代理设置、Webhook 通知设置，以及顶栏账号详情与系统自检维护弹窗。

## 验证与运行命令

```bash
# 1. 全量后端测试（测试数据与配置已通过 tests/conftest.py 全自动隔离）
.venv/bin/python -m pytest

# 2. 发布前隐私与安全扫描
.venv/bin/python scripts/release_check.py

# 3. 前端类型检查、单测与生产拆包构建
cd apps/web \
  && ./node_modules/.bin/tsc --noEmit -p tsconfig.json \
  && node scripts/test-quick-pay.mjs \
  && ./node_modules/.bin/vite build
```
