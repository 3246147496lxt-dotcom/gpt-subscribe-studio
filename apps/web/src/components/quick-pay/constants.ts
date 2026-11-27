import type { QuickPayJob } from "../../api/quickPay";

export const STAGE_LABEL: Record<string, string> = {
  queued: "排队等待执行",
  preflight: "正在自动体检",
  preflight_passed: "前置体检通过",
  creating_order: "正在生成订单",
  reissuing: "正在重签发 Session",
  reissued: "Session 重签发完成",
  session_refresh: "正在刷新 Session",
  session_refreshed: "Session 刷新完成",
  browser_decode: "正在初始化解码环境",
  browser_decoded: "解码环境完成",
  browser_decode_skipped: "已跳过浏览器解码",
  creating_checkout: "正在创建结账单",
  checkout_created: "结账单已生成",
  confirming_payment: "正在完成扣款确认",
  requires_action: "等待持卡人 / 本人验证",
  verifying_delivery: "正在核验订阅交付",
  paid_pending_delivery: "已付款，待交付",
  canceling_auto_renew: "正在关闭自动续费",
  auto_renew_canceled: "自动续费已关闭",
  draining: "服务正在排空重启",
  succeeded: "充值成功",
  failed: "任务未完成",
  unknown: "扣款结果待核对",
  manual_verdict: "人工判定",
};

export const FAILURE_LABEL: Record<string, string> = {
  card_declined: "发卡行拒绝付款；请核对银行记录",
  insufficient_funds: "卡片余额不足",
  subscription_limit: "该银行卡或设备关联的订阅数量已达上限；请更换卡片或隔离设备环境",
  already_subscribed: "当前账号已是有效订阅状态，无需重复开通",
  unusual_activity: "上游拒绝了本次请求",
  sentinel_challenge_failed: "安全验证失败；已熔断，未继续扣款",
  proxy_tunnel_failed: "代理连接失败；请检查出口节点是否可用",
  proxy_authentication_failed: "代理认证失败；请检查代理账号或权限",
  upstream_timeout: "上游超时；请勿自动重复扣款，先核对账单",
  browser_decode_unavailable: "浏览器解码不可用；请检查 Chromium/Playwright 环境",
  checkout_upstream_error: "建单未完成；请核对订单记录",
  invalid_session: "会话无效，本次请求已停止",
  session_expired: "会话已过期，本次请求已停止",
  payment_unknown_state: "扣款结果待核对；已锁定，请人工确认账单",
  payment_unknown_lock: "存在未核销的 unknown 订单，已锁定禁止再次扣款",
  BILLING_PROFILE_UNAVAILABLE: "固定核验账单资料不可用，未提交建单与付款",
  billing_country_mismatch: "出口地区可以自由选择；账单地址固定为美国免税州，请检查固定账单文件",
  service_draining: "服务正在重启排空，暂不接收新任务，请稍后重试",
  server_draining: "服务停机排空，未提交建单与付款，可安全重试",
  preflight_failed: "前置自动体检未通过，已停止提交",
  unsupported_country: "所选国家或地区暂不支持",
  invalid_request: "请求参数无效，未提交",
  manual_failed: "已人工判定为未扣款 / 失败",
  unknown_upstream: "未分类上游错误；请保留订单号和阶段日志排查",
};

export const TIMING_LABEL: Record<string, string> = {
  queue_ms: "排队等待",
  preflight_ms: "前置体检",
  create_order_ms: "建单总计",
  reissue_ms: "会话重签",
  browser_decode_ms: "浏览器解码",
  create_checkout_ms: "建单耗时",
  payment_ms: "支付耗时",
  cancel_renew_ms: "关闭续费",
  total_ms: "总耗时",
};

export function isQuickPayTerminal(job: QuickPayJob): boolean {
  return ["succeeded", "failed", "unknown", "paid_pending_delivery"].includes(job.status);
}

// The outcome flag is produced by the server. Block contradictory or legacy
// results rather than offering a new checkout while payment might be in flight.
export function isConfirmedRejection(job: QuickPayJob, locked: boolean): boolean {
  return job.checkout_outcome === "rejected_before_payment"
    && job.status === "failed"
    && job.stage === "failed"
    && Boolean(job.order_id)
    && !locked
    && !job.lock_id
    && [
      "checkout_upstream_error",
      "unusual_activity",
      "already_subscribed",
      "subscription_limit",
      "invalid_session",
      "session_expired",
    ].includes(job.failure_code ?? "")
    && !job.events?.some((e) =>
      [
        "checkout_created",
        "confirming_payment",
        "verifying_delivery",
        "unknown",
        "succeeded",
        "canceling_auto_renew",
        "auto_renew_canceled",
      ].includes(e.stage)
      || e.state === "unknown"
      || e.state === "succeeded",
    );
}
