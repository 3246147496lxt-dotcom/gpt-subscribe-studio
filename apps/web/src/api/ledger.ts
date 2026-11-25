import { requestJson } from "./http";
import type { QuickPayEvent } from "./quickPay";

export type OrderLedgerStats = {
  total: number;
  delivered: number;
  paid_pending_delivery: number;
  unknown: number;
  failed: number;
  active_locks: number;
};

export type OrderLedgerItem = {
  id: string;
  plan: string;
  billing_country: string;
  exit_country?: string | null;
  status: string;
  payment_status: string;
  delivery_status: string;
  amount?: string | null;
  currency?: string | null;
  failure_code?: string | null;
  last_error?: string | null;
  purchase_attempt_id?: string | null;
  payment_intent_id?: string | null;
  auto_cancel_renew: boolean;
  auto_renew_status: string;
  auto_renew_canceled_at?: string | null;
  auto_renew_error?: string | null;
  job_id?: string | null;
  reconcile_attempts: number;
  active_lock_id?: string | null;
  created_at: string;
  updated_at: string;
  unknown_since?: string | null;
  finished_at?: string | null;
};

export type OrderLedgerPage = {
  stats: OrderLedgerStats;
  total: number;
  limit: number;
  offset: number;
  items: OrderLedgerItem[];
};

export type PaymentLockSummary = {
  id: string;
  lock_key_prefix: string;
  lock_type: string;
  order_id?: string | null;
  job_id?: string | null;
  payment_intent_id?: string | null;
  state: "held" | "released" | "resolved" | string;
  reason: string;
  created_at: string;
  released_at?: string | null;
  released_by?: string | null;
  release_note?: string | null;
  order_plan?: string | null;
  order_status?: string | null;
  order_payment_status?: string | null;
  order_delivery_status?: string | null;
  order_amount?: string | null;
  order_currency?: string | null;
  order_billing_country?: string | null;
};

export type PaymentLocksPage = {
  counts: Record<string, number>;
  total: number;
  limit: number;
  offset: number;
  items: PaymentLockSummary[];
};

export type ReconcileResult = {
  order_id?: string | null;
  job_id?: string | null;
  outcome: string;
  changed: boolean;
  order_status?: string | null;
  payment_status?: string | null;
  delivery_status?: string | null;
  job_status?: string | null;
  lock_state?: string | null;
  reconcile_attempts: number;
  message: string;
};

export type ReconcilerStatus = {
  enabled: boolean;
  running: boolean;
  interval_s: number;
  max_attempts: number;
  pending_candidates: number;
  last_run_at?: string | null;
  total_runs: number;
  resolved_count: number;
};

export type OrderLedgerFilter = {
  status?: string;
  plan?: string;
  search?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
};

function buildLedgerQuery(params: OrderLedgerFilter): URLSearchParams {
  const query = new URLSearchParams();
  if (params.status && params.status !== "all") query.set("status", params.status);
  if (params.plan && params.plan !== "all") query.set("plan", params.plan);
  if (params.search) query.set("search", params.search);
  if (params.date_from) query.set("date_from", params.date_from);
  if (params.date_to) query.set("date_to", params.date_to);
  if (params.limit != null) query.set("limit", String(params.limit));
  if (params.offset != null) query.set("offset", String(params.offset));
  return query;
}

export async function getOrderLedger(params: OrderLedgerFilter = {}, signal?: AbortSignal): Promise<OrderLedgerPage> {
  const query = buildLedgerQuery(params);
  return requestJson<OrderLedgerPage>(`/api/orders?${query.toString()}`, { cache: "no-store", signal });
}

export function buildOrderLedgerExportUrl(params: Omit<OrderLedgerFilter, "limit" | "offset"> = {}): string {
  const query = buildLedgerQuery(params);
  const qs = query.toString();
  return qs ? `/api/orders/export.csv?${qs}` : "/api/orders/export.csv";
}

export async function getOrderEvents(orderId: string, signal?: AbortSignal): Promise<QuickPayEvent[]> {
  return requestJson<QuickPayEvent[]>(`/api/orders/${encodeURIComponent(orderId)}/events`, {
    cache: "no-store",
    signal,
  });
}

export async function reconcileOrder(orderId: string): Promise<ReconcileResult> {
  return requestJson<ReconcileResult>(`/api/orders/${encodeURIComponent(orderId)}/reconcile`, {
    method: "POST",
    timeoutMs: 45_000,
  });
}

export async function getPaymentLocks(
  params: { state?: string; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<PaymentLocksPage> {
  const query = new URLSearchParams();
  if (params.state) query.set("state", params.state);
  if (params.limit != null) query.set("limit", String(params.limit));
  if (params.offset != null) query.set("offset", String(params.offset));
  return requestJson<PaymentLocksPage>(`/api/payments/locks?${query.toString()}`, {
    cache: "no-store",
    signal,
  });
}

export async function reconcilePaymentLock(lockId: string): Promise<ReconcileResult> {
  return requestJson<ReconcileResult>(`/api/payments/locks/${encodeURIComponent(lockId)}/reconcile`, {
    method: "POST",
    timeoutMs: 45_000,
  });
}

export async function releasePaymentLockManual(lockId: string, note: string, operator: string): Promise<void> {
  await requestJson<unknown>(`/api/payments/locks/${encodeURIComponent(lockId)}/release`, {
    method: "POST",
    body: { confirm_no_charge: true, note, operator: operator.trim() || "web-operator" },
  });
}

export async function getReconcilerStatus(signal?: AbortSignal): Promise<ReconcilerStatus> {
  return requestJson<ReconcilerStatus>("/api/payments/reconciler", { cache: "no-store", signal });
}
