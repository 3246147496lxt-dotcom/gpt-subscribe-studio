import { IconReceipt } from "@tabler/icons-react";

import { formatAmount, isHostedOrder, type Order, type Plan } from "../api/client";
import { FAILED_STATUSES, SUCCESS_STATUSES } from "../api/history";
import { getOrderFlow, type OrderFlow } from "../orderFlow";
import { planLabel, regionLabel } from "../plans";
import "./order-summary.css";

export const STATUS_LABELS: Record<string, string> = {
  created: "已创建",
  pending_checkout: "正在生成结账",
  awaiting_payment: "等待支付",
  paying: "支付中",
  requires_action: "需要支付验证",
  unknown: "状态待确认",
  verifying: "正在确认订阅",
  active: "订阅已生效",
  delivered: "已开通",
  rejected: "已拒绝",
  failed: "失败",
};

export function statusColor(status: string): string {
  if (SUCCESS_STATUSES.includes(status)) return "green";
  if (FAILED_STATUSES.includes(status)) return "red";
  return "yellow";
}

interface Props {
  plan: Plan;
  country: string;
  order: Order | null;
  seats?: number;
  progress?: OrderFlow;
}

export function OrderSummary({ plan, country, order, seats, progress = getOrderFlow(order) }: Props) {
  const currentPlan = order?.plan ?? plan;
  const due = order ? formatAmount(order.amount, order.currency) : null;
  const region = order?.billing_country || country;
  const stamp = progress.tone === "complete" ? "已开通"
    : progress.tone === "failed" ? "未完成"
    : progress.tone === "attention" ? "待核对"
    : progress.steps.every(step => step.state === "idle") ? "待提交" : "待确认";
  const amountPlaceholder = !order ? "待获取金额"
    : isHostedOrder(order) ? "尚未取得 Stripe 报价" : "未从结账拿到金额";

  return (
    <div className="order-summary">
      <section className="order-summary-card" aria-label="本次订阅">
        <h2>本次订阅</h2>
        <dl className="order-summary-details">
          <dt>套餐</dt><dd>ChatGPT {planLabel(currentPlan)}</dd>
          <dt>{currentPlan === "team" ? "创建结账地区" : "地区"}</dt>
          <dd>{region ? regionLabel(region) : "未选择"}</dd>
          {currentPlan === "team" && <><dt>席位</dt><dd>{order?.seat_quantity ?? seats ?? "—"} 人</dd></>}
          <dt>订单号</dt><dd>{order?.id ?? "待创建"}</dd>
        </dl>
      </section>

      <section className="order-summary-card order-receipt" aria-label="订阅回执">
        <header className="order-receipt-head">
          <IconReceipt size={24} stroke={1.7} aria-hidden="true" />
          <h2>订阅回执</h2>
          <span className="order-receipt-stamp" data-tone={progress.tone}>{stamp}</span>
        </header>
        <p className="order-receipt-caption">本次应付</p>
        <p className="order-receipt-amount" data-placeholder={!due || undefined}>{due ?? amountPlaceholder}</p>
        {order?.quote_error && <p className="order-receipt-note order-receipt-error">{order.quote_error}</p>}
        {currentPlan === "team" && due && <p className="order-receipt-note">Stripe 当前报价，确认前会按账单地址重算税费。</p>}

        <p className="order-receipt-status">{progress.label}</p>
      </section>
    </div>
  );
}
