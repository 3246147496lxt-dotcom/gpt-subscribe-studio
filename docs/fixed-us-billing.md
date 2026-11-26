# 固定美国免税账单 + 自由出口

目标：每一笔非 Team 订单都使用**同一份美国免税州账单地址**；出口代理可以按订单
自由选择、轮换 SID/IP，不需要和账单国家一致。

## 1. 生成固定账单文件

```bash
.venv/bin/python scripts/setup_fixed_us_billing.py \
  --name "Daniel Mercer" \
  --email billing@example.com \
  --line1 "1425 SW Morrison Street" \
  --city Portland --state OR --postal-code 97205
# 默认写入 ~/.gss/fixed-billing.json（0600），并在终端打印 export 行
export GSS_FIXED_BILLING_FILE="$HOME/.gss/fixed-billing.json"
```

约束：

- 文件必须在仓库之外，绝对路径，权限 `0600` / `0400`，不能是符号链接。
- 只接受 7 个字段：`name / email / line1 / city / state / postal_code / country`。
- `country` 必须为 `US`；`state` 必须是 `AK / DE / MT / NH / OR`。
- 其它字段不补齐、不猜测；不合法时启动后所有新结账直接失败（fail closed）。

`GSS_FIXED_BILLING_FILE` 一旦设置就优先于 `GSS_BILLING_PROFILES_FILE` /
`GSS_VERIFIED_BILLING_FILE`；移除该环境变量即可回到原来的多币种/单付款主体配置。

## 2. 出口与账单的分工

| 项目 | 来源 | 说明 |
| --- | --- | --- |
| 账单国家 | 固定资料 | 永远 `US`，不随机 |
| 街道/城市/州/邮编/姓名/邮箱 | 固定资料 | 每单完全一致 |
| 代理出口 | 订单 `exit_country` / Session 签发地 / 网关地区 | 任意支持国家，可轮换 SID |
| 币种 | 出口国家 | 如 PH→PHP、JP→JPY；不影响账单地址 |
| 语言 / 时区 / 设备档案 | 出口国家 | 与代理出口保持一致 |

建单请求示例（固定 US 账单 + 菲律宾出口）：

```json
{
  "plan": "plus",
  "billing_country": "US",
  "exit_country": "PH",
  "session": "<session JSON>"
}
```

非 Team 的 `billing_country` 只是兼容旧客户端的提示：即使传了别的国家，服务端也以
固定资料为准。要更换账单，只能更换管理员配置文件，不能用请求参数改。

建单请求里 `billing_details` 的实测形态（网页端 HAR，出口 JP）：

```json
{"billing_details": {"country": "JP", "currency": "JPY"}}
```

也就是说**建单那一步上报的是「当前选中地区」= 出口国 + 出口币种**，
固定美国免税地址不在这一步出现；紧接着的
`POST /backend-api/payments/checkout/taxes` 才带上真正的美国免税账单：

```json
{"billing_country": "US",
 "billing_address": {"line1": "…", "city": "…", "state": "OR", "country": "US", "postal_code": "…"},
 "currency": "jpy"}
```

口径分工：

| 阶段 | 上报的国家 | 说明 |
| --- | --- | --- |
| `POST /payments/checkout` | 出口国（如 `JP` / `PH`） | 网页端选择地区的回显，不是账单变更 |
| `POST /payments/checkout/taxes` | 固定 `US` + 免税州地址 | 零税重算，账单核验链看这里 |
| `POST /payments/checkout/confirm` | 不含国家 | 只提交 `checkout_session_id` / `confirm_token` |
| Stripe `payment_method_data[billing_details][address][country]` | 固定 `US` | 与固定资料完全一致 |

因此「建单 `billing_details.country` 必须等于 US」是错误写法：它应该等于出口国，
否则请求本身就与真实浏览器行为不符。`verified_payer` 的
`alternate_countries=(exit_country,)` 已原生接受这种出口回显。

## 3. 严格模式（可选）

默认允许出口与账单国不同。需要恢复旧约束时：

```bash
export GSS_REQUIRE_EXIT_BILLING_MATCH=1
```

此时 `exit_country != 账单国` 会在建单和预检阶段返回
`billing_country_mismatch`。

## 4. 上游回显校验

- 回显账单字段与固定地址不一致（姓名、街道、城市、州、邮编、邮箱）→
  `BILLING_COUNTRY_MISMATCH`，提交付款前停止。
- 回显国家等于**当前选中的出口国** → 允许（这是出口回显，不是账单变更）。
- 回显任何其它国家 → 仍然拦截。

## 5. 失败与排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `BILLING_PROFILE_UNAVAILABLE` | 未配置文件 / 权限不是 0600、0400 / 相对路径 / 符号链接 / 字段不合法 | 重新运行生成脚本并导出环境变量 |
| `BILLING_COUNTRY_MISMATCH` | 固定地址与上游回显或提交的 claim 冲突 | 检查是否被别的流程改写了地址；不要重试付款 |
| 预检 `billing_country_mismatch` | 开启了严格模式且出口 ≠ US | 关闭严格模式或把出口改成 US |
| 币种不是 USD | 正常：币种随出口 | 不需要修改账单 |

## 6. 相关测试

```bash
.venv/bin/python -m pytest tests/unit/test_fixed_us_billing.py \
  tests/unit/test_billing_profiles.py tests/unit/test_verified_payer.py \
  tests/integration/test_verified_order_billing.py \
  tests/integration/test_country_alignment.py
```
