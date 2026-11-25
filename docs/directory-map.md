# 目录职责（Directory Map）

**更新时间**: 2026-11-25

```text
gpt-subscribe-studio/
├── .github/workflows/           # GitHub Actions CI（后端 pytest/release_check + 前端 tsc/单测/build）
├── apps/
│   └── web/                     # React + Vite + Mantine 前端工作台（代码拆包 + 面板懒加载）
│       ├── src/api/             # API 封装：client / quickPay / ledger / system / webhooks / proxy / accountDetail
│       ├── src/pages/           # QuickPay（首屏同步）与 OrderNew（懒加载）
│       └── src/components/      # BatchPreflightPanel / ServerLedgerPanel / LocksPanel / SystemDiagnosticsModal 等
├── services/
│   ├── api/src/gss_api/         # 主 FastAPI 服务
│   │   ├── main.py              # 应用入口、lifespan（队列恢复/对账器/优雅停机）与隐私 422 拦截
│   │   ├── persistence.py       # 主 SQLite WAL 存储（订单、队列、事件、资金锁、Webhook 投递与维护）
│   │   ├── store.py             # 有界 LRU 订单缓存 + SQLite 按需回查
│   │   ├── routers/             # orders / quick_pay / locks / events(SSE) / webhooks / system / proxy / account_detail
│   │   ├── services/            # order_flow / preflight / reconciler / webhooks / diagnostics / account_detail
│   │   ├── db.py                # [Legacy] 早期 SQLAlchemy 模型（保留供旧集成测试兼容）
│   │   └── repository.py        # [Legacy] 早期 SQLAlchemy 仓储（保留供旧集成测试兼容）
│   ├── orchestrator/            # 流水线状态机与 [Legacy] 独立 worker
│   ├── checkout/                # 结账适配器（CheckoutAdapter）
│   ├── payment/                 # 支付适配器（PaymentAdapter）
│   └── account/                 # 账号与订阅校验适配器（AccountAdapter）
├── packages/
│   ├── contracts/               # 共享领域契约：状态机枚举、模型、错误码与 adapter 协议
│   └── core/src/gss_core/       # 共享底座：脱敏、配置、chatgpt 协议、chatgpt_parsers 解析器、固定美国免税账单（billing / billing_profiles / fixed_billing）
├── deploy/                      # docker-compose、.env.example、Dockerfile
├── docs/                        # 架构、上游集成、合规、目录职责、ADR
├── specs/                       # SDD 工件
├── scripts/                     # release_check.py、setup_fixed_us_billing.py 等发布/运维脚本
└── tests/                       # 单元 / 契约 / 集成测试（conftest.py 全局隔离临时数据与配置目录）
```

## 职责与边界速查

| 路径 | 拥有 | 禁止 |
|---|---|---|
| `apps/web` | UI、SSE 订阅、批量前置体检、系统自检与账本导出 | 禁止将卡号、CVC 或 Session 持久化到浏览器存储 |
| `services/api/routers` | HTTP / SSE 契约、幂等键校验、422 脱敏、CSV 防注入导出 | 禁止直接写原始 SQL 或泄漏敏感入参 |
| `services/api/services` | 持久化队列调度、只读对账、前置体检、Webhook 与诊断逻辑 | 禁止在付款确认后自动重放扣款 |
| `services/api/persistence.py` | SQLite WAL 事务、索引迁移、安全清理与维护 | 禁止落盘 Session / Token / 卡号 / CVC / 明文口令 |
| `packages/contracts` | 跨服务模型、错误码、状态机 | 禁止依赖任何服务实现 |
| `packages/core` | 脱敏、配置、ChatGPT/Stripe 协议与纯解析器 (`chatgpt_parsers.py`) | 禁止含业务编排逻辑 |
| `deploy` | 容器与环境编排模板 | 禁止提交真实凭据或账单文件 |

## 依赖方向（单向）

```text
apps/web ──► services/api ──► services/{checkout,payment,account,orchestrator}
                  │                                   │
                  └──────────► packages/contracts ◄───┘
                                       ▲
                                 packages/core
```

`packages/*` 不得反向依赖 `services/*`。
