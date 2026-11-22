import type { Plan } from "./client";

export type QuickPayEvent = {
  state: string;
  stage: string;
  message: string;
  created_at: string;
};

export type QuickPayJob = {
  job_id: string;
  /** Server evidence, never inferred from a local timeout or an error string. */
  checkout_outcome?: "rejected_before_payment" | null;
  lock_id?: string | null;
  timings_ms?: Record<string, number>;
  events?: QuickPayEvent[];
  status: "queued" | "running" | "succeeded" | "failed" | "unknown" | "paid_pending_delivery";
  stage: string;
  order_id?: string | null;
  purchase_attempt_id?: string | null;
  amount?: string | null;
  currency?: string | null;
  failure_code?: string | null;
  message?: string | null;
  auto_renew_status?: string;
  auto_renew_canceled_at?: string | null;
  auto_renew_error?: string | null;
  proxy_rotations?: number;
  proxy_last_sid?: string | null;
  proxy_last_exit_ip?: string | null;
};

type CommonQuickPayInput = {
  plan: Plan;
  billing_country?: string;
  exit_country?: string;
  session: string;
  mode: { account_source: "customer_session"; card_source: "customer"; billing_mode: "standard"; risk_acknowledged: boolean };
  auto_cancel_renew: boolean;
};

export type QuickPayInput = CommonQuickPayInput & (
  | { debug_checkout_only: true; card?: never }
  | { debug_checkout_only: false; card: { number: string; cvc: string; exp_month: string; exp_year: string } }
);

async function parse<T>(response: Response): Promise<T> {
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    const message = typeof detail === "string" ? detail : detail?.message;
    throw new Error(message || `请求失败 HTTP ${response.status}`);
  }
  return body as T;
}

export async function startQuickPay(input: QuickPayInput, idempotencyKey: string): Promise<QuickPayJob> {
  if (input.debug_checkout_only === true && "card" in input) {
    throw new Error("仅建单请求不能包含银行卡资料");
  }
  if (input.debug_checkout_only !== true && !input.card) {
    throw new Error("付款请求必须提供银行卡资料");
  }
  const response = await fetch("/api/payments/quick-pay", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(input),
  });
  return parse<QuickPayJob>(response);
}

export async function getQuickPay(jobId: string): Promise<QuickPayJob> {
  const response = await fetch(`/api/payments/quick-pay/${encodeURIComponent(jobId)}`, { cache: "no-store" });
  return parse<QuickPayJob>(response);
}

export type PaymentLock = {
  id: string;
  lock_key: string;
  lock_type: string;
  order_id?: string | null;
  job_id?: string | null;
  idempotency_key?: string | null;
  payment_intent_id?: string | null;
  state: "held" | "released";
  reason: string;
  created_at: string;
  released_at?: string | null;
  released_by?: string | null;
  release_note?: string | null;
};

export async function releasePaymentLock(lockId: string, note: string): Promise<PaymentLock> {
  const response = await fetch(`/api/payments/locks/${encodeURIComponent(lockId)}/release`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm_no_charge: true, note, operator: "web-operator" }),
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(body?.detail?.message || `解锁失败 HTTP ${response.status}`);
  }
  return body.lock as PaymentLock;
}
