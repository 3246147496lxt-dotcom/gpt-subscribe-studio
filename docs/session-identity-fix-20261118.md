# 目标国家充值 + Session 重签发修复摘要（2026-11-18）

目标：允许在任意国家签发的 Session 上，按订单指定的 `billing_country`
（例如 PH 菲律宾）完成低价区充值；跨区时在目标国家出口重签发，并保证
「重签发 → 建单 → Stripe 扣款」全链路同一 IP、同一 UA。

## 1. 出口国家与账单国家解耦

- `exit_country` 决定代理出口、Session 重签发地和建单定价（默认等于 `billing_country`）
- `billing_country` 决定账单地址、税务国家和最终应付额
- 币种由 `currency_for(exit_country)` 推导（PH 出口锁定 PHP）
- 默认 US 地址使用 OR / DE 免税州（Portland 97201 / Wilmington 19801）
- 响应字段：`exit_country`、`session_reissued_for_country`
- 两个国家都必须在支持列表内

## 2. 跨区重签发（`ChatGPTClient.reauthorize_for_country`）

仅当 `statsigContext.country` 存在且与 `exit_country` 不同时触发：

1. 丢弃旧 `accessToken`
2. 只保留 `__Secure-next-auth.session-token`
3. 生成新的 `oai-did`（UUID4）和匹配的桌面 Chrome UA
4. 挂目标国家粘性代理，调用 `GET /api/auth/session`
5. 只接受新 `accessToken` / 新 Cookie，忽略响应里可能回带的旧 statsig 环境
6. 后续 `prepare_risk`（Node 哨兵）、`create_checkout`、Stripe 支付复用
   同一个 client / proxy / UA
7. 建单时用 `exit_country` 锁 PHP 定价，用 `billing_country` 调
   `/backend-api/payments/checkout/taxes` 重算税费（PH VAT 退掉）

跨区但缺少 sessionToken 时直接 401，提示重新导出完整 session。

## 3. 全链路粘性

- `create_order` 只生成一次 `exit_proxy`，重签发、建单、支付共用
- `CheckoutRequest.pin_proxy=True`：checkout adapter 不轮换 SID
- `_create_checkout_with_retry` 在 pin 模式下即使代理报错也不换 SID
- `pay-protocol` 优先复用 `OrderSecrets.proxy`，不再每次生成新 SID
- `pay` / Team prepare / quote 全部使用同一个 `secrets.proxy`
- UA 从 vault 的 `user_agent` 透传到 `StripeProtocol` / `PlusProtocol` /
  `PlusHarProtocol` / `captcha.verify_challenge`

## 4. 上游错误透传

所有关键请求 `>=400` 时记录：

- 原始状态码
- URL（query 值统一打码）
- 响应体原文（`redact_sensitive`，最多 4000 字符）

覆盖 `refresh_session`、账号补查、`create_checkout`、Stripe form/intent/
confirm、hosted checkout、tokenize、captcha verify、Plus/HAR 会话。

## 5. 验证

```bash
uv run python -m pytest tests
# 383 passed
```

新增/更新：

- `tests/integration/test_country_alignment.py`（跨区重签发 + 目标国家）
- `tests/integration/test_order_exit_country.py`（目标国家出口）
- `tests/unit/test_session_reauth.py`（丢弃旧身份、只用 sessionToken）
- `tests/unit/test_payment_exit_country.py`（支付目标国家）
- `tests/unit/test_checkout_proxy_pin.py`（pin 出口）
- `tests/unit/test_protocol_user_agent.py`（UA 透传）

## 6. 真实 Session 待验证

- 目标国家代理是否允许 `GET /api/auth/session`
- 重签发后的 accessToken 是否被目标国家 IP 上下文接受
- `curl_cffi` TLS impersonate 目前仍固定 `chrome150`
- 非 Chrome UA 的 client hints
