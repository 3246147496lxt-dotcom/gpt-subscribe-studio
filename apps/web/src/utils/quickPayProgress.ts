import type { QuickPayJob } from "../api/quickPay";

export type StepStatus = "done" | "active" | "failed" | "paused" | "pending";
export type FlowStepId = "checkout" | "payment" | "cancel_renew";

export type FlowStepDef = {
  id: FlowStepId;
  title: string;
  shortDesc: string;
  startPct: number;
  endPct: number;
  typicalMs: number;
  stages: string[];
};

const CHECKOUT_STAGES = [
  "queued",
  "preflight",
  "preflight_passed",
  "creating_order",
  "running",
  "reissue",
  "reissuing",
  "reissued",
  "session_refresh",
  "session_refreshed",
  "browser_decode",
  "browser_decoded",
  "browser_decode_skipped",
  "checkout",
  "create_checkout",
  "creating_checkout",
  "checkout_retry_started",
  "checkout_created",
  "draining",
];

const PAYMENT_STAGES = [
  "confirming_payment",
  "requires_action",
  "unknown",
  "verifying_delivery",
  "paid_pending_delivery",
];

const CANCEL_RENEW_STAGES = [
  "canceling_auto_renew",
  "auto_renew_canceled",
];

/** Sub-stage relative progress (0..1) inside each business step. */
const CHECKOUT_SUB_RATIO: Record<string, number> = {
  queued: 0.05,
  draining: 0.05,
  preflight: 0.14,
  preflight_passed: 0.22,
  creating_order: 0.30,
  running: 0.30,
  reissue: 0.44,
  reissuing: 0.44,
  session_refresh: 0.46,
  reissued: 0.56,
  session_refreshed: 0.58,
  browser_decode: 0.65,
  browser_decoded: 0.72,
  browser_decode_skipped: 0.72,
  checkout: 0.78,
  create_checkout: 0.80,
  creating_checkout: 0.84,
  checkout_retry_started: 0.90,
  checkout_created: 1.0,
};

const PAYMENT_SUB_RATIO: Record<string, number> = {
  confirming_payment: 0.36,
  requires_action: 0.58,
  unknown: 0.62,
  verifying_delivery: 0.84,
  paid_pending_delivery: 0.92,
  succeeded: 1.0,
};

const CANCEL_RENEW_SUB_RATIO: Record<string, number> = {
  canceling_auto_renew: 0.50,
  auto_renew_canceled: 1.0,
  succeeded: 1.0,
};

export const SUBSTAGE_DETAIL: Record<string, { step: FlowStepId; label: string; action: string }> = {
  queued: { step: "checkout", label: "排队等待执行", action: "任务已入队，正在等待可用并发槽位" },
  draining: { step: "checkout", label: "服务停机排空", action: "后端正在排空重启，尚未建单" },
  preflight: { step: "checkout", label: "前置体检", action: "正在核验 Session、固定账单、国家与防重付锁" },
  preflight_passed: { step: "checkout", label: "前置体检通过", action: "前置安全检查全部通过，准备建单" },
  creating_order: { step: "checkout", label: "初始化订单", action: "正在初始化本地订单与幂等记录" },
  running: { step: "checkout", label: "开始执行建单", action: "正在启动建单流程" },
  reissue: { step: "checkout", label: "重签发 Session", action: "正在按目标出口地区重签发登录 Session" },
  reissuing: { step: "checkout", label: "重签发 Session", action: "正在按目标出口地区重签发登录 Session" },
  reissued: { step: "checkout", label: "Session 重签发完成", action: "地区 Session 已就绪" },
  session_refresh: { step: "checkout", label: "刷新 Session", action: "正在用刷新凭证换取最新 accessToken" },
  session_refreshed: { step: "checkout", label: "Session 刷新完成", action: "最新 accessToken 已就绪" },
  browser_decode: { step: "checkout", label: "初始化解码环境", action: "正在准备协议请求上下文" },
  browser_decoded: { step: "checkout", label: "解码环境就绪", action: "协议请求上下文已就绪" },
  browser_decode_skipped: { step: "checkout", label: "跳过浏览器解码", action: "采用纯协议直连模式" },
  checkout: { step: "checkout", label: "创建结账单", action: "正在向 ChatGPT 请求创建结账会话" },
  create_checkout: { step: "checkout", label: "创建结账单", action: "正在向 ChatGPT 请求创建结账会话" },
  creating_checkout: { step: "checkout", label: "创建结账单与报价", action: "正在向 ChatGPT 创建结账单并核算含税金额" },
  checkout_retry_started: { step: "checkout", label: "重试创建结账单", action: "出口波动，正在自动切换代理重试建单" },
  checkout_created: { step: "checkout", label: "结账单已生成", action: "结账单与应付报价已获取成功" },
  confirming_payment: { step: "payment", label: "提交卡片确认扣款", action: "正在向 Stripe 提交银行卡并等待发卡行扣款回执" },
  requires_action: { step: "payment", label: "等待 3DS / 本人验证", action: "发卡行或上游要求浏览器验证，已暂停等待完成" },
  unknown: { step: "payment", label: "扣款结果待核对", action: "支付结果未明朗，已加资金锁防止重复扣款" },
  verifying_delivery: { step: "payment", label: "核验订阅到账", action: "扣款已提交，正在查询 ChatGPT 确认订阅权益已生效" },
  paid_pending_delivery: { step: "payment", label: "已扣款待到账", action: "扣款已成功，正等待上游同步订阅权益（后台自动对账中）" },
  canceling_auto_renew: { step: "cancel_renew", label: "关闭连续订阅", action: "订阅已到账，正在向 ChatGPT 请求关闭下期自动续费" },
  auto_renew_canceled: { step: "cancel_renew", label: "连续订阅已关闭", action: "已成功关闭下期自动续费，保留当前周期权益" },
  succeeded: { step: "payment", label: "充值完成", action: "订阅已确认到账，全部流程完成" },
  failed: { step: "checkout", label: "流程中断", action: "当前步骤执行失败" },
};

const FAILURE_CODE_STAGE_FALLBACK: Record<string, string> = {
  preflight_failed: "preflight",
  BILLING_PROFILE_UNAVAILABLE: "preflight",
  billing_country_mismatch: "preflight",
  payment_unknown_lock: "preflight",
  unsupported_country: "preflight",
  invalid_request: "preflight",
  service_draining: "draining",
  server_draining: "draining",
  invalid_session: "creating_order",
  session_expired: "creating_order",
  token_revoked: "creating_order",
  browser_decode_unavailable: "browser_decode",
  proxy_tunnel_failed: "creating_checkout",
  proxy_authentication_failed: "creating_checkout",
  sentinel_challenge_failed: "creating_checkout",
  checkout_upstream_error: "creating_checkout",
  unusual_activity: "creating_checkout",
  already_subscribed: "creating_checkout",
  subscription_limit: "confirming_payment",
  payment_state_unavailable: "confirming_payment",
  card_declined: "confirming_payment",
  insufficient_funds: "confirming_payment",
  payment_unknown_state: "confirming_payment",
  upstream_timeout: "confirming_payment",
};

export type FailureDiagnosis = {
  stepNumber: number;
  totalSteps: number;
  stepTitle: string;
  substageCode: string;
  substageLabel: string;
  headline: string;
  reason: string;
  advice: string;
  failureCode?: string;
  upstreamStatus?: number;
  upstreamCode?: string;
  traceId?: string;
};

const FAILURE_CATALOG: Record<string, { headline: string; reason: string; advice: string }> = {
  card_declined: {
    headline: "发卡行拒绝了本次扣款",
    reason: "Stripe 已将扣款请求送达发卡行，但被发卡行直接拒付（本笔未扣款）。",
    advice: "请核对银行 App 拒付通知，确认已开启境外/线上/订阅扣款权限及账单地址一致；不要用同一张卡连续高频重试。",
  },
  insufficient_funds: {
    headline: "银行卡余额或可用额度不足",
    reason: "发卡行反馈当前卡片余额不足以覆盖含税订阅金额。",
    advice: "请先向卡片充值并预留少量税费/预授权余量后再发起新单，或更换其他卡片。",
  },
  already_subscribed: {
    headline: "当前账号已有生效中的订阅",
    reason: "上游检测到该 ChatGPT 账号已是有效订阅状态，拒绝重复开通。",
    advice: "可点击右上角「账号详情」查看当前套餐与到期日，无需重复支付。",
  },
  subscription_limit: {
    headline: "当前银行卡或设备关联的订阅数量已达上限",
    reason: "上游在提交卡片确认时返回 subscription_limit（OpenAI 判定该银行卡或浏览器设备 oai-did 已关联过多订阅，本笔未扣款）。",
    advice: "请更换一张新的银行卡；若多个账号在同一浏览器配置中导出 Session 且开启了直通模式，请清理 Cookie 或使用独立无痕/配置文件重新导出，或关闭直通模式后重试。",
  },
  unusual_activity: {
    headline: "触发了 ChatGPT 上游风控（unusual_activity）",
    reason: "在创建结账单时上游拒绝了本次请求，尚未进入银行卡扣款环节。",
    advice: "建议切换更干净的住宅代理出口 SID、确认代理国家与账单国家一致，或稍后由账号本人在官方页面检查。",
  },
  sentinel_challenge_failed: {
    headline: "上游安全校验未通过，已熔断停止",
    reason: "请求在结账安全校验阶段被拦截，系统已主动停止，未继续提交卡片。",
    advice: "请检查当前出口 IP 质量或本地 Node 运行环境，并确认网络稳定。",
  },
  proxy_tunnel_failed: {
    headline: "出口代理连接失败",
    reason: "无法通过当前代理节点与上游建立稳定 TLS 隧道。",
    advice: "请前往「代理设置」测试出口连通性，或更换代理地区/节点后重试。",
  },
  proxy_authentication_failed: {
    headline: "出口代理鉴权失败",
    reason: "代理网关返回认证错误（407 / 账号密码或白名单无效）。",
    advice: "请前往「代理设置」核对代理用户名、密码与套餐有效期。",
  },
  upstream_timeout: {
    headline: "上游接口响应超时",
    reason: "等待上游返回结果超时，网络或上游服务出现延迟。",
    advice: "若已进入扣款阶段，请切勿盲目重复提交；先在「开通记录」或「资金锁与对账」执行只读对账并核对银行账单。",
  },
  browser_decode_unavailable: {
    headline: "本地协议解码环境不可用",
    reason: "当前模式依赖的 Chromium / Playwright 解码组件未就绪或启动失败。",
    advice: "请检查本机浏览器解码依赖，或关闭浏览器解码开关后重试。",
  },
  checkout_upstream_error: {
    headline: "上游拒绝创建结账单",
    reason: "向 ChatGPT 请求生成结账会话时收到上游错误响应。",
    advice: "请结合下方的 HTTP 状态码与上游错误码排查（401 为会话失效，403 多为出口风控或地区限制，429 为频率受限）。",
  },
  invalid_session: {
    headline: "ChatGPT Session 无效",
    reason: "粘贴的 Session 格式无法解析或已被服务端判定无效。",
    advice: "请重新登录 ChatGPT 网页版并导出完整的 Session JSON（建议同时包含 accessToken 与 sessionToken/refreshToken）。",
  },
  session_expired: {
    headline: "ChatGPT Session 已过期",
    reason: "accessToken 已过期且缺少可用的刷新凭证，上游拒绝认证。",
    advice: "请刷新 ChatGPT 网页后重新复制最新 Session JSON 再试。",
  },
  token_revoked: {
    headline: "登录令牌已被吊销",
    reason: "该账号的登录令牌已在上游失效或被登出。",
    advice: "请重新登录目标账号获取全新 Session JSON。",
  },
  payment_unknown_state: {
    headline: "扣款结果尚未明朗（已自动锁定）",
    reason: "支付确认发出后未能获得明确终态，为防止重复扣款已自动锁定账号。",
    advice: "请勿再次提交！前往「资金锁与对账」点击「只读对账」，或核对银行扣款流水后再决定是否人工解锁。",
  },
  payment_unknown_lock: {
    headline: "账号存在未核销的防重付资金锁",
    reason: "该账号此前有一笔结果待核对的订单，系统自动拦截新单以防二次扣款。",
    advice: "请前往「资金锁与对账」标签页先执行只读对账，或在核实银行未扣款后留痕解锁。",
  },
  BILLING_PROFILE_UNAVAILABLE: {
    headline: "固定核验账单文件不可用",
    reason: "服务端未检测到合法的固定核验账单文件，或文件权限不满足 0400/0600。",
    advice: "请点击右上角「系统自检与维护」检查固定核验账单配置与文件权限。",
  },
  billing_country_mismatch: {
    headline: "出口地区与固定核验账单国家不一致",
    reason: "当前选择的出口国家与服务端固定核验账单的国家代码冲突，未提交上游。",
    advice: "请将表单中的「出口国家 / 币种」调整为与固定核验账单一致的国家（当前本地配置为 US）后重试。",
  },
  service_draining: {
    headline: "服务正在停机排空",
    reason: "后端正在优雅重启排空在途任务，暂不接收新订单。",
    advice: "请等待几秒后刷新队列状态重新提交（本单未扣款）。",
  },
  server_draining: {
    headline: "任务因服务重启排空而中止",
    reason: "任务排队期间服务进入停机排空，未提交建单与付款。",
    advice: "待服务恢复后可直接重新发起（完全未扣款）。",
  },
  preflight_failed: {
    headline: "前置自动体检未通过",
    reason: "在进入建单前被前置安全规则拦截，未消耗卡片风控额度。",
    advice: "点击表单上的「提交前体检」按钮查看逐项红灯原因并修复后再提交。",
  },
  payment_state_unavailable: {
    headline: "本地持久化存储暂时不可用",
    reason: "发起扣款确认前写入本地 SQLite 状态失败，为保证资金安全已中止扣款。",
    advice: "请打开右上角「系统自检与维护」检查磁盘空间与 SQLite 状态后重试。",
  },
  unsupported_country: {
    headline: "所选国家或地区暂不支持",
    reason: "指定的出口/账单国家不在系统支持的 25 个结算地区列表中。",
    advice: "请从下拉框中选择受支持的国家/地区。",
  },
  invalid_request: {
    headline: "请求参数校验未通过",
    reason: "提交的套餐、地区或卡片字段存在无效参数。",
    advice: "请核对表单各项输入后重新提交。",
  },
  quick_pay_failed: {
    headline: "执行过程中发生异常",
    reason: "任务在对应阶段遇到了未归类错误而中止。",
    advice: "请查看下方记录的失败步骤、HTTP 状态码与实时执行流水，确认是否需要切换代理或更新 Session。",
  },
};

const UPSTREAM_CODE_HINTS: Record<string, string> = {
  unusual_activity: "上游返回 unusual_activity（异常流量/风控拦截）",
  invalid_session: "上游返回 invalid_session（会话无效）",
  token_revoked: "上游返回 token_revoked（令牌已被吊销）",
  invalid_token: "上游返回 invalid_token（令牌无效）",
  expired_token: "上游返回 expired_token（令牌已过期）",
  token_expired: "上游返回 token_expired（令牌已过期）",
  session_expired: "上游返回 session_expired（会话已过期）",
  unauthorized: "上游返回 unauthorized（401 未授权）",
  authentication_error: "上游返回 authentication_error（认证失败）",
  access_denied: "上游返回 access_denied（拒绝访问）",
  already_subscribed: "上游返回 already_subscribed（账号已有订阅）",
  already_paid: "上游返回 already_paid（该结账单已支付）",
  subscription_limit: "上游返回 subscription_limit（该银行卡或设备关联的订阅数量已达上限）",
  billing_country_mismatch: "上游返回 billing_country_mismatch（账单国家不匹配）",
  rate_limit_exceeded: "上游返回 rate_limit_exceeded（请求过于频繁，触发限流）",
  rate_limit_error: "上游返回 rate_limit_error（触发限流）",
  too_many_requests: "上游返回 too_many_requests（429 频率受限）",
  card_declined: "上游返回 card_declined（发卡行拒卡）",
  account_deactivated: "上游返回 account_deactivated（账号已停用）",
  account_disabled: "上游返回 account_disabled（账号被禁用）",
  unsupported_country: "上游返回 unsupported_country（不支持该地区）",
  server_error: "上游返回 server_error（服务端内部错误）",
  service_unavailable: "上游返回 service_unavailable（上游服务暂不可用）",
};

export function inferCheckoutOnly(job: QuickPayJob, hintDebugOnly?: boolean): boolean {
  if (hintDebugOnly !== undefined) return hintDebugOnly;
  if (job.status === "succeeded" && job.stage === "checkout_created") return true;
  if (job.message && job.message.includes("仅建单")) return true;
  return false;
}

export function inferAutoCancelRenew(job: QuickPayJob, hintAutoCancel?: boolean): boolean {
  if (hintAutoCancel !== undefined) return hintAutoCancel;
  if (job.stage === "canceling_auto_renew" || job.stage === "auto_renew_canceled") return true;
  const status = job.auto_renew_status;
  if (status && status !== "disabled") return true;
  return Boolean(job.events?.some((ev) => ev.stage === "canceling_auto_renew" || ev.stage === "auto_renew_canceled"));
}

export function buildFlowSteps(checkoutOnly: boolean, includeCancelRenew: boolean): FlowStepDef[] {
  if (checkoutOnly) {
    return [
      {
        id: "checkout",
        title: "建单",
        shortDesc: "仅建单模式：校验会话、重签发并创建结账单",
        startPct: 0,
        endPct: 100,
        typicalMs: 8000,
        stages: [...CHECKOUT_STAGES, "succeeded"],
      },
    ];
  }
  if (includeCancelRenew) {
    return [
      {
        id: "checkout",
        title: "建单",
        shortDesc: "校验会话与账单、向 ChatGPT 创建结账单",
        startPct: 0,
        endPct: 40,
        typicalMs: 8000,
        stages: CHECKOUT_STAGES,
      },
      {
        id: "payment",
        title: "支付",
        shortDesc: "向 Stripe 提交卡片扣款并核验订阅到账",
        startPct: 40,
        endPct: 85,
        typicalMs: 8000,
        stages: PAYMENT_STAGES,
      },
      {
        id: "cancel_renew",
        title: "取消连续订阅",
        shortDesc: "订阅到账后自动关闭下期续费，保留当期权益",
        startPct: 85,
        endPct: 100,
        typicalMs: 2500,
        stages: [...CANCEL_RENEW_STAGES, "succeeded"],
      },
    ];
  }
  return [
    {
      id: "checkout",
      title: "建单",
      shortDesc: "校验会话与账单、向 ChatGPT 创建结账单",
      startPct: 0,
      endPct: 45,
      typicalMs: 8000,
      stages: CHECKOUT_STAGES,
    },
    {
      id: "payment",
      title: "支付",
      shortDesc: "向 Stripe 提交卡片扣款并核验订阅到账",
      startPct: 45,
      endPct: 100,
      typicalMs: 8000,
      stages: [...PAYMENT_STAGES, "succeeded"],
    },
  ];
}

export function resolveEffectiveStage(job: QuickPayJob): string {
  const diagStage = job.diagnostics?.stage;
  if (job.status === "failed") {
    if (diagStage && diagStage !== "failed" && diagStage !== "running") {
      return diagStage;
    }
    if (job.stage && job.stage !== "failed" && job.stage !== "running") {
      return job.stage;
    }
    const events = job.events || [];
    for (let i = events.length - 1; i >= 0; i -= 1) {
      const st = events[i]?.stage;
      if (st && st !== "failed" && st !== "running") {
        return st;
      }
    }
    if (job.failure_code && FAILURE_CODE_STAGE_FALLBACK[job.failure_code]) {
      return FAILURE_CODE_STAGE_FALLBACK[job.failure_code];
    }
    return "creating_order";
  }

  if (job.stage && job.stage !== "running") {
    return job.stage;
  }
  const events = job.events || [];
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const st = events[i]?.stage;
    if (st && st !== "running") return st;
  }
  return job.status || "queued";
}

export function stepIdForStage(stage: string, steps: FlowStepDef[]): FlowStepId {
  if (stage === "succeeded") {
    return steps[steps.length - 1]?.id ?? "checkout";
  }
  for (const step of steps) {
    if (step.stages.includes(stage)) return step.id;
  }
  const mapped = SUBSTAGE_DETAIL[stage]?.step;
  if (mapped && steps.some((s) => s.id === mapped)) return mapped;
  return steps[0]?.id ?? "checkout";
}

function pctForStage(stage: string, steps: FlowStepDef[]): number {
  const stepId = stepIdForStage(stage, steps);
  const step = steps.find((s) => s.id === stepId) ?? steps[0];
  if (!step) return 0;

  let ratio = 0.25;
  if (step.id === "checkout") {
    ratio = stage === "succeeded" ? 1 : (CHECKOUT_SUB_RATIO[stage] ?? 0.3);
  } else if (step.id === "payment") {
    ratio = PAYMENT_SUB_RATIO[stage] ?? 0.4;
  } else if (step.id === "cancel_renew") {
    ratio = CANCEL_RENEW_SUB_RATIO[stage] ?? 0.5;
  }
  return Math.round(step.startPct + (step.endPct - step.startPct) * ratio);
}

export type ComputedStep = {
  id: FlowStepId;
  index: number;
  title: string;
  shortDesc: string;
  status: StepStatus;
  substageLabel?: string;
  actionText?: string;
  elapsedMs?: number;
};

export type ComputedQuickPayFlow = {
  percent: number;
  color: "green" | "blue" | "orange" | "red";
  animated: boolean;
  currentStepIndex: number;
  totalSteps: number;
  currentStepId: FlowStepId;
  currentStepTitle: string;
  currentSubstageLabel: string;
  currentActionText: string;
  elapsedSeconds: number | null;
  etaText: string;
  steps: ComputedStep[];
};

function timingForStep(stepId: FlowStepId, timings?: Record<string, number>): number | undefined {
  if (!timings) return undefined;
  if (stepId === "checkout") {
    if (timings.create_order_ms !== undefined) return timings.create_order_ms;
    const parts = [timings.preflight_ms, timings.reissue_ms, timings.browser_decode_ms, timings.create_checkout_ms]
      .filter((v): v is number => v !== undefined);
    return parts.length > 0 ? parts.reduce((a, b) => a + b, 0) : undefined;
  }
  if (stepId === "payment") {
    return timings.payment_ms;
  }
  return undefined;
}

export function formatSecondsBrief(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0 秒";
  if (seconds < 10) return `${seconds.toFixed(1)} 秒`;
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const mins = Math.floor(seconds / 60);
  const rem = Math.round(seconds % 60);
  return rem > 0 ? `${mins} 分 ${rem} 秒` : `${mins} 分`;
}

export function computeQuickPayFlow(
  job: QuickPayJob,
  options: {
    debugOnly?: boolean;
    autoCancelRenew?: boolean;
    highWaterPct?: number;
    elapsedMs?: number | null;
    stageElapsedMs?: number;
  } = {},
): ComputedQuickPayFlow {
  const checkoutOnly = inferCheckoutOnly(job, options.debugOnly);
  const includeCancel = !checkoutOnly && inferAutoCancelRenew(job, options.autoCancelRenew);
  const defs = buildFlowSteps(checkoutOnly, includeCancel);

  const effectiveStage = resolveEffectiveStage(job);

  // Use furthest stage seen across current stage + events to prevent any backward jump.
  let bestStage = effectiveStage;
  let bestBasePct = pctForStage(effectiveStage, defs);
  if (job.status !== "failed") {
    for (const ev of job.events || []) {
      if (!ev.stage || ev.stage === "failed" || ev.stage === "running") continue;
      const p = pctForStage(ev.stage, defs);
      if (p > bestBasePct) {
        bestBasePct = p;
        bestStage = ev.stage;
      }
    }
  }

  const activeStepId = stepIdForStage(job.status === "failed" ? effectiveStage : bestStage, defs);
  const activeStepIdx = Math.max(0, defs.findIndex((s) => s.id === activeStepId));
  const activeStepDef = defs[activeStepIdx] ?? defs[0];

  let percent: number;
  let color: ComputedQuickPayFlow["color"] = "blue";
  let animated = false;

  if (job.status === "succeeded") {
    percent = 100;
    color = "green";
    animated = false;
  } else if (job.status === "failed") {
    const failPct = pctForStage(effectiveStage, defs);
    percent = Math.min(96, Math.max(5, failPct));
    color = "red";
    animated = false;
  } else if (job.status === "requires_action" || job.status === "paid_pending_delivery" || job.status === "unknown") {
    percent = Math.max(options.highWaterPct ?? 0, bestBasePct);
    color = "orange";
    animated = job.status === "paid_pending_delivery";
  } else {
    // Smooth incremental progress within the active business step, never exceeding step.endPct - 2.
    const stageElapsed = Math.max(0, options.stageElapsedMs ?? 0);
    const maxWithinStep = Math.max(bestBasePct, activeStepDef.endPct - 2);
    const span = Math.max(0, maxWithinStep - bestBasePct);
    const fraction = Math.min(0.88, stageElapsed / Math.max(2000, activeStepDef.typicalMs * 1.1));
    const smoothPct = Math.round(bestBasePct + span * fraction);
    percent = Math.min(98, Math.max(options.highWaterPct ?? 0, bestBasePct, smoothPct));
    color = "blue";
    animated = true;
  }

  const totalTimingMs = job.timings_ms?.total_ms;
  const effectiveElapsedMs = totalTimingMs != null ? totalTimingMs : (options.elapsedMs ?? null);
  const elapsedSeconds = effectiveElapsedMs != null && effectiveElapsedMs >= 0
    ? effectiveElapsedMs / 1000
    : null;

  const detailMeta = SUBSTAGE_DETAIL[effectiveStage];
  const currentSubstageLabel = detailMeta?.label || effectiveStage;
  let currentActionText = detailMeta?.action || `正在执行：${effectiveStage}`;
  if (job.status === "succeeded" && includeCancel && job.auto_renew_status === "manual_required") {
    currentActionText = "订阅已到账；自动关闭续费未成功，请手动前往 ChatGPT 账单页关闭";
  }

  let etaText = "";
  if (job.status === "succeeded") {
    if (checkoutOnly) etaText = "建单已完成（未提交扣款）";
    else if (includeCancel && job.auto_renew_status === "canceled") etaText = "充值已到账，连续订阅已关闭";
    else if (includeCancel && job.auto_renew_status === "manual_required") etaText = "充值已到账（续费需手动关闭）";
    else etaText = "充值已到账";
  } else if (job.status === "failed") {
    etaText = `在「${activeStepDef.title}」阶段中断（${currentSubstageLabel}）`;
  } else if (job.status === "requires_action") {
    etaText = "已暂停：等待你在浏览器完成 3DS / 官方验证";
  } else if (job.status === "paid_pending_delivery") {
    etaText = "已扣款：等待上游同步订阅权益（后台自动对账中）";
  } else if (job.status === "unknown") {
    etaText = "扣款结果待核对，已加资金锁防重付";
  } else if (job.status === "queued") {
    const pos = Math.max(1, job.queue_position ?? 1);
    const fullSec = defs.reduce((acc, s) => acc + s.typicalMs, 0) / 1000;
    const waitSec = (pos - 1) * (checkoutOnly ? 8 : 15);
    etaText = pos > 1
      ? `排队第 ${pos} 位 · 预计等待约 ${Math.round(waitSec)} 秒，开始后约 ${Math.round(fullSec)} 秒完成`
      : `即将开始建单 · 预计全程约 ${Math.round(fullSec)} 秒${checkoutOnly ? "完成建单" : "到账"}`;
  } else {
    const stepProgress = Math.max(0, Math.min(0.95, (percent - activeStepDef.startPct) / Math.max(1, activeStepDef.endPct - activeStepDef.startPct)));
    const remainingCurrentMs = Math.max(1000, activeStepDef.typicalMs * (1 - stepProgress));
    const remainingLaterMs = defs.slice(activeStepIdx + 1).reduce((acc, s) => acc + s.typicalMs, 0);
    const totalRemainingSec = (remainingCurrentMs + remainingLaterMs) / 1000;
    const overTypical = (options.stageElapsedMs ?? 0) > activeStepDef.typicalMs * 2.2;
    if (overTypical) {
      etaText = `「${activeStepDef.title}」耗时较长，正在安全等待上游回执（请勿重复提交）`;
    } else {
      etaText = `预计还需约 ${Math.max(2, Math.round(totalRemainingSec))} 秒${checkoutOnly ? "完成建单" : "完成"}`;
    }
  }

  const steps: ComputedStep[] = defs.map((def, idx) => {
    let status: StepStatus;
    if (job.status === "succeeded") {
      if (def.id === "cancel_renew" && job.auto_renew_status === "manual_required") {
        status = "paused";
      } else {
        status = "done";
      }
    } else if (idx < activeStepIdx) {
      status = "done";
    } else if (idx === activeStepIdx) {
      if (job.status === "failed") status = "failed";
      else if (job.status === "requires_action" || job.status === "unknown" || job.status === "paid_pending_delivery") {
        status = "paused";
      } else {
        status = "active";
      }
    } else {
      status = "pending";
    }

    let actionText: string | undefined;
    if (status === "active" || status === "paused" || status === "failed") {
      actionText = currentActionText;
    } else if (def.id === "cancel_renew" && job.status === "succeeded" && job.auto_renew_status === "manual_required") {
      actionText = "自动关闭失败，请手动到账单页关闭续费";
    }

    return {
      id: def.id,
      index: idx + 1,
      title: def.title,
      shortDesc: def.shortDesc,
      status,
      substageLabel: idx === activeStepIdx ? currentSubstageLabel : undefined,
      actionText,
      elapsedMs: timingForStep(def.id, job.timings_ms),
    };
  });

  return {
    percent,
    color,
    animated,
    currentStepIndex: activeStepIdx + 1,
    totalSteps: defs.length,
    currentStepId: activeStepDef.id,
    currentStepTitle: activeStepDef.title,
    currentSubstageLabel,
    currentActionText,
    elapsedSeconds,
    etaText,
    steps,
  };
}

/**
 * Produces structured, redacted, actionable failure diagnosis using only
 * server-allowlisted codes/diagnostics/stages — never raw upstream body prose.
 */
export function describeFailureDetail(
  job: QuickPayJob,
  debugOnly?: boolean,
  autoCancelRenew?: boolean,
): FailureDiagnosis {
  const checkoutOnly = inferCheckoutOnly(job, debugOnly);
  const includeCancel = !checkoutOnly && inferAutoCancelRenew(job, autoCancelRenew);
  const defs = buildFlowSteps(checkoutOnly, includeCancel);
  const substageCode = resolveEffectiveStage(job);
  const stepId = stepIdForStage(substageCode, defs);
  const stepIdx = Math.max(0, defs.findIndex((s) => s.id === stepId));
  const stepDef = defs[stepIdx] ?? defs[0];

  const code = job.failure_code || undefined;
  const diag = job.diagnostics || undefined;
  const upstreamStatus = typeof diag?.upstream_status === "number" ? diag.upstream_status : undefined;
  const upstreamCode = diag?.upstream_code || undefined;
  const traceId = diag?.request_id || diag?.cf_ray || undefined;

  const entry = (code && FAILURE_CATALOG[code]) || {
    headline: "任务未完成",
    reason: "流程在该步骤中断，未能继续推进。",
    advice: "请核对失败步骤与下方的安全诊断信息，并结合「开通记录」排查。",
  };

  let reason = entry.reason;
  if (upstreamCode && UPSTREAM_CODE_HINTS[upstreamCode]) {
    reason = `${UPSTREAM_CODE_HINTS[upstreamCode]}。${entry.reason}`;
  } else if (upstreamStatus) {
    const httpHint = upstreamStatus === 401
      ? "上游返回 HTTP 401（认证失败/会话失效）"
      : upstreamStatus === 402
        ? "上游返回 HTTP 402（发卡行拒付或付款未通过）"
        : upstreamStatus === 403
          ? "上游返回 HTTP 403（风控拦截或访问受限）"
          : upstreamStatus === 429
            ? "上游返回 HTTP 429（请求频率超限）"
            : upstreamStatus >= 500
              ? `上游返回 HTTP ${upstreamStatus}（上游服务暂时异常）`
              : `上游返回 HTTP ${upstreamStatus}`;
    reason = `${httpHint}。${entry.reason}`;
  }

  const subMeta = SUBSTAGE_DETAIL[substageCode];
  return {
    stepNumber: stepIdx + 1,
    totalSteps: defs.length,
    stepTitle: stepDef.title,
    substageCode,
    substageLabel: subMeta ? `${subMeta.label}（${subMeta.action}）` : substageCode,
    headline: entry.headline,
    reason,
    advice: entry.advice,
    failureCode: code,
    upstreamStatus,
    upstreamCode,
    traceId,
  };
}
