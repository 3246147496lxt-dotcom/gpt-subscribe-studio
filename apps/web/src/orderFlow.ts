import { formatAmount, type Order } from "./api/client";
import { FAILED_STATUSES, SUCCESS_STATUSES } from "./api/history";

type StepId = "account" | "register" | "checkout" | "quote" | "payment" | "subscription";
type StepState = "idle" | "waiting" | "active" | "complete" | "attention" | "failed";

interface StepResult {
  state: StepState;
  status: string;
  evidence?: string;
}

interface FlowStep extends StepResult {
  id: StepId;
  title: string;
  description: string;
}

export interface OrderFlow {
  tone: "current" | "attention" | "failed" | "complete";
  label: string;
  detail?: string;
  focus?: StepId;
  steps: FlowStep[];
}

export function getOrderFlow(order: Order | null, createRequested = false, error: string | null = null, selectedPlan?: string): OrderFlow {
  const team = (order?.plan ?? selectedPlan) === "team";
  const definitions: Array<Pick<FlowStep, "id" | "title" | "description">> = [
    { id: "account", title: "核对账号与套餐", description: team
      ? "检查登录信息、结算地区、团队席位和计费周期。"
      : "检查所选套餐和地区，读取账号登录信息，按需校验登录状态。" },
    { id: "register", title: "登记订单", description: "记录套餐和结算地区，分配订单号，后续回执用它关联到本单。" },
    { id: "checkout", title: "创建结账", description: team
      ? "按团队席位和计费周期请求托管结账，保存返回的结账信息。"
      : "向订阅服务请求所选套餐的结账，保存返回的结账信息。" },
    { id: "quote", title: "读取应付金额", description: team
      ? "建单后单独请求团队报价，读取金额和币种。结算时金额可能更新。"
      : "从结账响应中读取可用报价，记录应付金额和币种。" },
    { id: "payment", title: "确认支付结果", description: "付款发起后，读取支付服务的处理结果，记录扣款状态和回执。" },
    { id: "subscription", title: "核验订阅权益", description: "查询账号的订阅是否生效，记录交付结果及返回的到期时间。" },
  ];
  const results: Partial<Record<StepId, StepResult>> = {};
  function mark(id: StepId, state: StepState, status: string, evidence?: string) {
    results[id] = { state, status, evidence };
  }
  function report(tone: OrderFlow["tone"], label: string, detail?: string | null, focus?: StepId): OrderFlow {
    return { tone, label, detail: detail || undefined, focus,
      steps: definitions.map(step => ({ ...step, ...(results[step.id] ?? { state: "idle", status: "未开始" }) })),
    };
  }

  if (!order) {
    if (!createRequested) return error
      ? report("attention", "表单校验未通过，建单请求尚未发送", error)
      : report("current", "等待提交建单请求");

    // This specific server rejection precedes order registration. A generic request
    // error carries no such guarantee (for example, a response may have been lost).
    if (error && /无法在(?:目标|出口)国家\s*\S+\s*重签发\s*Session/i.test(error)) {
      mark("account", "failed", "登录状态校验失败");
      return report("failed", "账号校验失败，流程在登记订单前中止", error, "account");
    }

    for (const id of ["account", "register", "checkout"] as const) {
      mark(id, "waiting", error ? "结果未确认" : "等待回执");
    }
    if (!team) mark("quote", "waiting", error ? "结果未确认" : "等待回执");
    if (error) {
      mark("payment", "idle", "尚未提交付款");
      return report("failed", "未取得完整建单回执，具体中断环节待确认", error);
    }
    return report("current", "建单请求已发出，等待后端返回结果",
      "账号检查、订单登记和结账共用一次响应。收到结果后，这里会更新。");
  }

  const payment = order.payment_status ?? order.receipt?.payment_status;
  const paid = payment === "succeeded" || order.receipt?.payment_status === "succeeded";
  const paidAmount = order.paid_currency ? formatAmount(order.paid_amount, order.paid_currency) : null;
  const failed = FAILED_STATUSES.includes(order.status);
  const paymentFailed = payment === "failed" && !paid;
  const delivered = SUCCESS_STATUSES.includes(order.status) || order.delivery_status === "delivered"
    || order.receipt?.delivery_status === "delivered" || (!failed && !paymentFailed && order.subscription?.active === true);
  const delayed = order.delivery_status === "verify_delayed" || order.receipt?.delivery_status === "verify_delayed";
  const quoted = formatAmount(order.amount, order.currency);
  const checkoutReturned = !!(order.checkout_url || order.checkout_kind || order.publishable_key
    || ["awaiting_payment", "paying", "requires_action", "verifying", "active", "delivered"].includes(order.status) || paid);

  mark("account", "complete", "建单参数已受理");
  mark("register", "complete", "已登记", `订单号 ${order.id}`);
  if (checkoutReturned) mark("checkout", "complete", "结账已创建");
  else if (order.status === "pending_checkout") mark("checkout", "active", "正在创建结账");
  else mark("checkout", "waiting", "等待结账回执");
  if (order.quote_error) mark("quote", "attention", "报价读取异常", order.quote_error);
  else if (quoted && order.currency) mark("quote", "complete", "已取得报价", `应付 ${quoted} · ${order.currency.toUpperCase()}`);
  else mark("quote", "waiting", "待取得完整报价");

  if (paid) mark("payment", "complete", "扣款已确认", paidAmount ? `已付 ${paidAmount}` : undefined);
  else if (paymentFailed) mark("payment", "failed", "支付失败", order.last_error || undefined);
  else if (order.status === "requires_action") mark("payment", "attention", "等待支付验证");
  else if (order.status === "unknown" || failed || delivered || order.status === "verifying") mark("payment", "waiting", "扣款结果未确认");
  else if (order.status === "paying" || payment === "processing") mark("payment", "active", "支付处理中");
  else if (checkoutReturned) mark("payment", "waiting", "等待付款");

  if (delivered) {
    const expires = order.subscription?.expires_at;
    const date = expires ? new Date(expires) : null;
    const expiryText = date && !Number.isNaN(date.valueOf()) ? `到期日 ${date.toLocaleDateString("zh-CN")}` : undefined;
    const label = order.subscription?.active || order.status === "active" ? "订阅已生效" : "服务端已确认交付";
    mark("subscription", "complete", label, expiryText);
    return report("complete", label);
  }
  if (paid && failed) {
    mark("subscription", "attention", "交付未确认");
    return report("attention", "扣款已确认，订单未完成交付", order.last_error || error, "subscription");
  }
  if (failed || paymentFailed) return report("failed", paymentFailed ? "付款未完成" : "订单未完成", order.last_error || error, paymentFailed ? "payment" : undefined);
  if (paid || delayed || order.status === "verifying") {
    if (delayed) mark("subscription", "attention", "订阅核验延迟");
    else if (order.status === "verifying") mark("subscription", "active", "正在核验订阅");
    else mark("subscription", "waiting", "等待核验结果");
    return report(delayed || error ? "attention" : "current",
      paid ? "扣款已确认，等待订阅核验结果" : "等待订阅核验结果", error, "subscription");
  }
  if (order.status === "awaiting_payment" && order.quote_error) return report("attention", "结账已创建，报价读取异常", error, "quote");
  if (order.status === "unknown") return report("attention", "支付结果待确认", error || order.last_error, "payment");
  if (order.status === "requires_action") return report("attention", "等待支付验证", error || order.last_error, "payment");
  if (order.status === "paying" || payment === "processing") return report("current", "支付服务正在处理付款", error, "payment");
  if (order.status === "awaiting_payment" && error) return report("attention", "结账已创建，当前操作未完成", error);
  if (order.status === "awaiting_payment") return report("current", "结账已创建，等待付款", null, "payment");
  return report("current", checkoutReturned ? "等待订单状态更新" : "已登记订单，等待结账回执", error, checkoutReturned ? undefined : "checkout");
}
