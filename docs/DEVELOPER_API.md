# 开发者开放 API 对接文档（v1）

本文档面向接入本平台 ChatGPT 订阅代充协议的第三方开发者、发卡站及工作室系统。

- **协议版本**：`v1`（服务端版本 `0.2.0`）
- **基础地址（Base URL）**：`https://<你的服务域名>/api/v1`（例如 `https://luoxue-auto.top/api/v1`）
- **数据格式**：全部请求与响应均为 `application/json; charset=utf-8`（SSE 流式进度接口为 `text/event-stream`）
- **租户隔离**：每个 API Key 拥有完全独立的订单与任务视图，无法查询其他 Key 或 Web 门户的任务。

---

## 1. 鉴权方式

请联系平台管理员在管理后台「运营 → 开发者 API」签发独立 API Key（前缀为 `gss_live_`）。

所有 `/api/v1/*` 接口均支持以下任一请求头（推荐使用标准 Bearer 方式）：

```http
Authorization: Bearer gss_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

或：

```http
X-API-Key: gss_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> **安全提示**：API Key 仅在签发或轮换时展示一次，请勿硬编码在前端网页或客户端 App 中，仅限服务端调用。

---

## 2. 幂等性与资金安全规范（接入必读）

1. **必填 `Idempotency-Key`**：发起 `POST /api/v1/recharges` 时，**必须在请求头携带 1~200 字符的唯一 `Idempotency-Key`**（建议使用商户侧唯一订单号，如 `mch_20261127_0001`）。
2. **重试必须复用同一个 `Idempotency-Key`**：发生网络超时、502/503 或连接中断时，使用相同 `Idempotency-Key` 重试会安全返回原任务，不会重复扣款；若使用相同 `Idempotency-Key` 提交不同的套餐/账号/卡信息，服务端将拒绝并返回 `409 idempotency_conflict`。
3. **遇到以下状态严禁自动换卡或重刷**：
   - `unknown`（扣款结果待核对，已加账号防重锁）
   - `paid_pending_delivery`（已确认扣款成功，等待上游权益同步）
   - `requires_action`（需持卡人/本人在浏览器完成 3DS 或官方确认）
   
   以上状态必须等待平台后台自动对账或联系平台售后人工核查处理。

---

## 3. 支持套餐代码与地区

### 套餐代码对照

请求 `plan` 字段支持标准代码及常用别名（不区分大小写）：

| 标准代码 | 推荐别名 | 显示名称 | 官方美区基准价 |
| :--- | :--- | :--- | :--- |
| `go` | — | Go | \$8 / 月 |
| `plus` | — | Plus | \$20 / 月 |
| `pro_lite` | `pro100` | Pro100 | \$100 / 月 |
| `pro` | `pro200` | Pro200 | \$200 / 月 |
| `pro_max` | `pro500` | Pro500 | \$500 / 月 |

### 常用低价结算地区（`exit_country`）

- `PH`（菲律宾，PHP）
- `CL`（智利，CLP）
- `EG`（埃及，EGP）
- `JP`（日本，JPY）
- `US`（美国，USD）

> 留空 `exit_country` 时，自动沿用平台当前默认配置出口地区；实时参考价可通过 `GET /api/v1/plans` 动态获取。

---

## 4. 接口清单与详细契约

### 4.1 连通性与密钥自检：`GET /api/v1/ping`

验证 API Key 是否有效、查看开放套餐、限流配额（RPM）与当前排队深度。

**响应示例（`200 OK`）**：

```json
{
  "ok": true,
  "version": "0.2.0",
  "key_id": "key_a1b2c3d4e5f6",
  "key_name": "Partner-A",
  "key_prefix": "gss_live_7f3a9c1",
  "allowed_plans": ["go", "plus", "pro_lite", "pro", "pro_max"],
  "rate_limit_rpm": 30,
  "queue": {
    "queued": 0,
    "running": 1,
    "max_concurrency": 3,
    "draining": false
  },
  "server_time": "2026-11-27T08:30:00.000000+00:00"
}
```

---

### 4.2 查询套餐与实时参考价：`GET /api/v1/plans`

返回当前开放套餐列表、各低价区参考价格（USD / CNY / 当地币种）以及当前 API Key 的套餐权限标记 `allowed_for_key`。

**响应示例（`200 OK`）**：

```json
{
  "plans": [
    {
      "id": "pro_lite",
      "display_name": "Pro100",
      "aliases": ["pro100", "pro_100"],
      "default_region": "CL",
      "usd_price": 85.17,
      "cny_price": 570.04,
      "available": true,
      "allowed_for_key": true,
      "prices_by_country": {
        "CL": { "currency": "CLP", "local_amount": 86365, "usd": 85.17, "cny": 570.04 }
      }
    }
  ],
  "regions": [
    { "code": "PH", "label": "菲律宾", "currency": "PHP" },
    { "code": "CL", "label": "智利", "currency": "CLP" }
  ]
}
```

---

### 4.3 发起异步协议充值：`POST /api/v1/recharges`

提交客户 ChatGPT Session 与虚拟卡信息，服务端异步执行「前置体检 → 建单 → 协议支付 → 交付核验 → 自动关闭续费」全流程，立即返回 `202 Accepted` 与 `job_id`。

**必需请求头**：
- `Authorization: Bearer <API_KEY>`（或 `X-API-Key`）
- `Idempotency-Key: <1~200 字符唯一幂等键>`
- `Content-Type: application/json`

**请求体参数**：

| 字段 | 类型 | 必填 | 说明 |
| :--- | :--- | :---: | :--- |
| `plan` | string | 是 | 套餐代码：`go` / `plus` / `pro100` (`pro_lite`) / `pro200` (`pro`) / `pro500` (`pro_max`) |
| `exit_country` | string | 否 | 2 位国家码，如 `PH` / `CL` / `EG` / `JP` / `US` |
| `session` | string | 是 | 客户完整 ChatGPT Session JSON 字符串（至少包含有效 `accessToken`，推荐含完整 `cookies` / `sessionToken` / `oai-did`） |
| `card` | object | 是 | 虚拟卡对象：`{ "number": "16位卡号", "exp_month": "01~12", "exp_year": "YYYY或YY", "cvc": "3~4位安全码" }` |
| `billing` | object | 否 | 可选账单地址；不传时自动使用平台已核验美国免税账单 |
| `auto_cancel_renew` | boolean | 否 | 扣款成功后是否自动关闭下期连续订阅（保留当期权益、不退款），默认 `true` |
| `external_order_id` | string | 否 | 商户自有业务订单号（最长 128 字符），便于后续按单号检索与对账 |

**成功受理响应（`202 Accepted`）**：

```json
{
  "job_id": "9f1a2b3c4d5e6f708192a3b4c5d6e7f8",
  "order_id": null,
  "external_order_id": "MCH-20261127-0001",
  "status": "queued",
  "stage": "queued",
  "queue_position": 1,
  "plan": "pro_lite",
  "plan_display_name": "Pro100",
  "exit_country": "CL",
  "masked_email": "al***@gmail.com",
  "amount": null,
  "currency": null,
  "failure_code": null,
  "message": null,
  "auto_renew_status": "disabled",
  "auto_renew_canceled_at": null,
  "auto_renew_error": null,
  "checkout_outcome": null,
  "next_action": null,
  "events": [],
  "created_at": "2026-11-27T08:30:01.123456+00:00"
}
```

---

### 4.4 查询单笔充值状态：`GET /api/v1/recharges/{job_id}`

建议每 **2~3 秒** 轮询一次（或改用下方 SSE 流式接口），直至 `status` 进入终态或人工介入态。

**任务状态说明（`status`）**：

| 状态 | 类型 | 含义与建议处理 |
| :--- | :--- | :--- |
| `queued` | 进行中 | 排队等待工作槽位，继续轮询 |
| `running` | 进行中 | 正在建单 / 扣款 / 核验，继续轮询 |
| `succeeded` | **终态（成功）** | 扣款且权益已确认交付（关注 `auto_renew_status` 确认续费关闭结果） |
| `failed` | **终态（失败）** | 明确未成功；结合 `failure_code` 和 `checkout_outcome` 处理（若 `checkout_outcome == "rejected_before_payment"` 说明建单前已被拒，未扣款） |
| `paid_pending_delivery` | **待交付（禁重试）** | 已扣款成功，等待上游同步权益；平台对账器会自动跟进，**切勿重刷** |
| `requires_action` | **待验证（禁重试）** | 需要持卡人完成 3DS / 官方验证，读取 `next_action.redirect_url` 引导处理 |
| `unknown` | **待核对（禁重试）** | 支付结果暂未明确，已加账号锁防止重复扣款，请联系平台售后核实 |

**成功终态响应示例（`200 OK`）**：

```json
{
  "job_id": "9f1a2b3c4d5e6f708192a3b4c5d6e7f8",
  "order_id": "4f927f13-8f4d-4d62-97f9-5efad7d8d001",
  "external_order_id": "MCH-20261127-0001",
  "status": "succeeded",
  "stage": "auto_renew_canceled",
  "plan": "pro_lite",
  "plan_display_name": "Pro100",
  "exit_country": "CL",
  "masked_email": "al***@gmail.com",
  "amount": "86365",
  "currency": "CLP",
  "failure_code": null,
  "message": "订阅已交付",
  "auto_renew_status": "canceled",
  "auto_renew_canceled_at": "2026-11-27T08:32:10Z",
  "auto_renew_error": null,
  "events": [
    { "state": "running", "stage": "creating_checkout", "message": "正在创建结账单", "created_at": "..." },
    { "state": "succeeded", "stage": "auto_renew_canceled", "message": "下期自动续费已关闭", "created_at": "..." }
  ]
}
```

---

### 4.5 SSE 实时进度流：`GET /api/v1/recharges/{job_id}/stream`

建立 Server-Sent Events 长连接，实时推送与 `GET /api/v1/recharges/{job_id}` 同结构的最新任务快照，任务结束自动关闭流。

```bash
curl -N "https://<你的服务域名>/api/v1/recharges/<job_id>/stream" \
  -H "Authorization: Bearer gss_live_xxx"
```

---

### 4.6 分页查询历史充值任务：`GET /api/v1/recharges`

仅返回当前 API Key 发起的订单列表。

**查询参数**：
- `status`：`all`（默认）/ `queued` / `running` / `succeeded` / `failed` / `unknown` / `paid_pending_delivery` / `requires_action`
- `external_order_id`：精确匹配商户外部单号
- `q`：模糊搜索外部单号、脱敏邮箱、`job_id` 或 `order_id`
- `page`（默认 `1`）、`page_size`（默认 `20`，最大 `100`）

---

### 4.7 实时直查账号订阅状态：`POST /api/v1/account/subscription`

零缓存实时直查上游 ChatGPT 订阅周期、到期时间、自动续费状态及可选历史账单。

**请求体**：

```json
{
  "session": "<完整 Session JSON 字符串>",
  "includeInvoices": false
}
```

> 建议下单前调用时将 `includeInvoices` 设为 `false`（响应最快，直接返回当前套餐、是否有效订阅与到期日）。

---

### 4.8 主动关闭账号自动续费：`POST /api/v1/account/cancel-auto-renew`

若某笔订单开启了自动续费，或充值后 `auto_renew_status` 为 `manual_required`，可通过该接口直接调用一等官方接口关闭下期自动续费（保留当前周期全部权益，不会退款）。

**请求体**：

```json
{
  "session": "<完整 Session JSON 字符串>",
  "plan": "pro100"
}
```

**响应示例**：

```json
{
  "canceled": true,
  "already_canceled": false,
  "stage": "done",
  "message": "已关闭下期自动续费"
}
```

---

## 5. HTTP 状态码与常见业务错误码

### HTTP 状态码

| HTTP 状态码 | 常见 `detail.code` | 含义 |
| :--- | :--- | :--- |
| `400` | `unsupported_plan` / `idempotency_key_required` | 套餐不受支持或缺少 `Idempotency-Key` |
| `401` | `invalid_api_key` / `api_key_revoked` | 密钥无效或已被吊销 |
| `403` | `api_key_disabled` / `plan_not_allowed` | 密钥已停用或未开放对应套餐权限 |
| `404` | `not_found` | 任务不存在或不属于当前 API Key |
| `409` | `idempotency_conflict` / `payment_unknown_lock` | 幂等键冲突，或该账号存在未核销的 `unknown` 资金锁 |
| `422` | `INVALID_ORDER_INPUT` | 请求字段校验未通过（为保护隐私，响应不会回显原始卡密或 Session） |
| `429` | `rate_limit_exceeded` | 超出当前 API Key 每分钟调用配额，按 `Retry-After` 秒数退避 |
| `503` | `service_draining` / `db_unavailable` | 服务短暂重启排空或上游代理断连 |

### 常见业务失败码（`failure_code`）

| `failure_code` | 原因说明 | 建议动作 |
| :--- | :--- | :--- |
| `subscription_limit` | 该虚拟卡或浏览器设备（`oai-did`）关联的订阅数已达 OpenAI 上限 | 更换新虚拟卡，并确保客户账号使用独立设备指纹导出 Session |
| `card_declined` | 发卡行拒付（余额不足、风控拦截、未开境外线上扣款等） | 核对虚拟卡余额与风控记录，换卡重新下单（使用新 `Idempotency-Key`） |
| `insufficient_funds` | 卡片余额不足（注意含当地增值税 VAT/IVA） | 充值余额后换新 `Idempotency-Key` 重试 |
| `already_subscribed` | 目标账号当前已有有效订阅 | 无需重复充值 |
| `invalid_session` / `session_expired` | Session 无效或登录态过期 | 重新登录获取最新 Session |
| `unusual_activity` | 上游风控拦截本次结账请求 | 稍后重试或切换出口地区 |
| `payment_unknown_state` | 扣款结果未明确（已加账号锁） | **禁止重刷**，联系平台售后核对账单 |

---

## 6. 完整接入代码示例

### 6.1 Python（`httpx`）

```python
import time
import uuid
import httpx

BASE_URL = "https://luoxue-auto.top/api/v1"
API_KEY = "gss_live_your_api_key_here"


def submit_and_wait(session_json: str, card: dict, plan: str = "pro100", exit_country: str = "CL") -> dict:
    headers = {"Authorization": f"Bearer {API_KEY}"}
    external_id = f"MCH-{uuid.uuid4().hex[:12]}"

    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=30.0) as client:
        # 1. 提交异步充值任务
        res = client.post(
            "/recharges",
            headers={"Idempotency-Key": external_id},
            json={
                "plan": plan,
                "exit_country": exit_country,
                "external_order_id": external_id,
                "auto_cancel_renew": True,
                "session": session_json,
                "card": card,
            },
        )
        res.raise_for_status()
        job = res.json()
        job_id = job["job_id"]
        print(f"已受理 job_id={job_id}, queue_position={job.get('queue_position')}")

        # 2. 轮询直到非进行态
        terminal = {"succeeded", "failed", "unknown", "paid_pending_delivery", "requires_action"}
        while True:
            poll = client.get(f"/recharges/{job_id}")
            poll.raise_for_status()
            state = poll.json()
            if state["status"] in terminal:
                return state
            time.sleep(2.5)
```

### 6.2 Node.js（内置 `fetch`，Node 18+）

```javascript
const BASE_URL = "https://luoxue-auto.top/api/v1";
const API_KEY = "gss_live_your_api_key_here";

async function recharge({ session, card, plan = "plus", exitCountry = "PH", externalOrderId }) {
  const idemKey = externalOrderId || `mch-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const createRes = await fetch(`${BASE_URL}/recharges`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${API_KEY}`,
      "Idempotency-Key": idemKey,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      plan,
      exit_country: exitCountry,
      external_order_id: idemKey,
      auto_cancel_renew: true,
      session,
      card,
    }),
  });
  if (!createRes.ok) {
    throw new Error(`Create failed (${createRes.status}): ${await createRes.text()}`);
  }
  const { job_id } = await createRes.json();

  const terminal = new Set(["succeeded", "failed", "unknown", "paid_pending_delivery", "requires_action"]);
  while (true) {
    const pollRes = await fetch(`${BASE_URL}/recharges/${job_id}`, {
      headers: { Authorization: `Bearer ${API_KEY}` },
    });
    const data = await pollRes.json();
    if (terminal.has(data.status)) {
      return data;
    }
    await new Promise((r) => setTimeout(r, 2500));
  }
}
```

---

## 7. 凭证留存与售后说明

- 为便于处理异常订单核查、人工判定与售后补关续费，平台会将每笔订单使用的完整 Session 与虚拟卡信息经 **AES-256-GCM 独立加密** 存入售后凭证金库，默认保留 **30 天**，到期后自动销毁密文（仅保留脱敏后的卡头 BIN、后 4 位与不可逆卡指纹用于风控关联）。
- 任何 API 查询接口（`/api/v1/*`）均不会回传原始卡号、CVC 或完整 Session 明文；如需售后核查，请向平台提供 `job_id` 或 `order_id`。
