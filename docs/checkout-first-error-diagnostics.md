# 建单首错诊断

按原作业 ID 查询 `GET /api/payments/quick-pay/{job_id}`。响应中的 `diagnostics` 是该作业的首份有效诊断，不跨请求补字段。它描述的是本地解析器的观察，而不是上游拒绝请求的原因。

- `unusual_activity_source`：只有实际判为异常流量时才出现。`structured_code` 表示接纳了明确的结构化错误码，`body_match` 表示在响应文本中命中分类短语。两者均命中时优先结构化码；由会话失效分支处理的响应不标记。
- `upstream_code_state` 和 `request_id_state`：`not_found` 表示在解析器识别的位置未见候选键；`accepted` 表示原值通过校验；`filtered` 表示存在候选，但没有被接纳的值。只检查原来支持的错误码位置和响应头，不把 type 或正文当成 code。
- 只新增枚举，不保存响应正文、完整响应头或被过滤值。旧记录、未收到 HTTP 响应及未经过该响应解析器的错误，均不反推标记。旧记录没有状态不等于 `not_found`。
