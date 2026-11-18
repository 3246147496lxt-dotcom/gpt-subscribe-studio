# Session 身份对齐修复摘要（2026-11-18）

目标：让订单国家、代理出口、账单地址、币种、设备指纹与 Session 签发时的
`statsigContext` 完全一致，并让上游 4xx/5xx 可观测。

## 1. 国家三位一体

有 `statsigContext.country` 时：

- `Order.billing_country` 直接使用签发地，忽略请求里的 `billing_country`
- 币种由 `currency_for(签发地)` 推导
- 默认账单地址由 `address_for(签发地)` 生成，并写入 `OrderSecrets.billing`
- 代理出口使用同一国家
- 响应新增 `country_aligned_to_session=true`，日志记录
  `country_aligned_to_session source=session requested_billing_country=... session_country=...`
- 签发地不在支持列表时返回 400 `COMPLIANCE_BLOCKED`

没有 `statsigContext.country` 时保持原行为：使用请求的 `billing_country`。

## 2. vault 保留 Session 身份

`OrderSecrets` 新增：

- `user_agent`：`statsigContext.userAgent`
- `device_id`：`statsigContext.deviceId`（同时写 `oai-device-id` / Cookie `oai-did`）
- `session_country`：Session 签发地
- `exit_country`：本单实际出口国家

`profile` 不再由 `_pick_profile()` 随机决定，而是由 Session UA 推导
（`profile_from_session_ua`）；仅当 UA 完全缺失时才回退到 device_id 派生。

## 3. 全链路 UA 透传

以下入口新增/使用 `user_agent`：

- `StripeProtocol`
- `PlusProtocol`
- `PlusHarProtocol`（通过 `apply_user_agent` 覆盖 `DeviceProfile` 的
  UA / chrome_build / chrome_major / platform / client hints）
- `captcha.verify_challenge`
- `captcha.solve_hcaptcha`（支付 adapter 现在会传 proxy + UA）

硬编码 UA 只作为完全缺失时的 fallback，不再在正常链路生效。

## 4. 上游错误透传

所有关键请求在 `>=400` 时记录：

- 原始 HTTP 状态码
- 请求 URL（query 值统一打码为 `***`）
- 响应体原文（`redact_sensitive`，最多 4000 字符）

覆盖：`refresh_session`、`/backend-api/me`、accounts check、优惠资格、
`create_checkout`、subscriptions、Stripe form/intent/confirm/setup_intent/
confirmation_tokens、hosted checkout、tokenize、captcha verify、
StripeProtocol 的 `_post` / `_get`、PlusProtocol 的 Stripe 提交、
PlusHarProtocol 的 web/stripe/m 会话。

## 5. 验证

```bash
uv run python -m pytest tests
# 377 passed
```

新增回归测试：

- `tests/integration/test_country_alignment.py`
- `tests/integration/test_order_exit_country.py`
- `tests/unit/test_exit_country.py`
- `tests/unit/test_payment_exit_country.py`
- `tests/unit/test_protocol_user_agent.py`
- `tests/unit/test_chatgpt_protocol.py`（statsig 优先级 / 上游日志）
- `tests/unit/test_session_refresh.py`（刷新日志脱敏）

## 6. 仍需真实 Session 验证

- `/api/auth/session` 是否稳定回带 `statsigContext`
- 非 Chrome UA（Safari/Firefox/Edge）的 client hints
- `curl_cffi` TLS impersonate 目前仍固定 `chrome150`
- 支付阶段 Stripe 是否接受「Session 签发地 = 账单国」的地址模板
