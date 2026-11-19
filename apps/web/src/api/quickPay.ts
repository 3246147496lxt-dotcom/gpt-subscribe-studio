import type { Plan } from "./client";

export type QuickPayJob = {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "unknown";
  stage: string;
  order_id?: string | null;
  purchase_attempt_id?: string | null;
  amount?: string | null;
  currency?: string | null;
  failure_code?: string | null;
  message?: string | null;
};

export type QuickPayInput = {
  plan: Plan;
  billing_country?: string;
  exit_country?: string;
  session: string;
  card: { number: string; cvc: string; exp_month: string; exp_year: string };
  mode: { account_source: "customer_session"; card_source: "customer"; billing_mode: "standard"; risk_acknowledged: boolean };
  debug_checkout_only: boolean;
};

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
