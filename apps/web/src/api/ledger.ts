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

export type OrderLedgerDiagnostics = {
  stage?: string;
  upstream_status?: number;
  upstream_code?: string;
  cf_ray?: string;
  request_id?: string;
  [key: string]: unknown;
};

export type OrderLedgerItem = {
  id: string;
  source?: "portal" | "api" | "quick_pay" | "step" | string;
  account_email?: string | null;
  masked_email?: string | null;
  portal_username?: string | null;
  api_key_id?: string | null;
  api_key_name?: string | null;
  external_order_id?: string | null;
  account_tag?: string | null;
  card_brand?: string | null;
  vault_has_session?: boolean;
  vault_has_card?: boolean;
  vault_card_last4?: string | null;
  plan: string;
  billing_country: string;
  exit_country?: string | null;
  status: string;
  payment_status: string;
  delivery_status: string;
  amount?: string | null;
  currency?: string | null;
  tax_delta?: string | null;
  failure_code?: string | null;
  last_error?: string | null;
  purchase_attempt_id?: string | null;
  payment_intent_id?: string | null;
  checkout_session_id?: string | null;
  checkout_kind?: string | null;
  route_profile?: string | null;
  billing_mode?: string | null;
  billing_verified?: boolean;
  session_reissued?: boolean;
  auto_cancel_renew: boolean;
  auto_renew_status: string;
  auto_renew_attempts?: number;
  auto_renew_canceled_at?: string | null;
  auto_renew_error?: string | null;
  auto_renew_http_status?: number | null;
  proxy_rotations?: number;
  proxy_last_sid?: string | null;
  proxy_last_exit_ip?: string | null;
  job_id?: string | null;
  job_status?: string | null;
  job_stage?: string | null;
  job_attempts?: number;
  reconcile_attempts: number;
  last_reconciled_at?: string | null;
  active_lock_id?: string | null;
  total_ms?: number | null;
  timings_ms?: Record<string, number>;
  diagnostics?: OrderLedgerDiagnostics;
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
  source?: string;
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
  if (params.source && params.source !== "all") query.set("source", params.source);
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

export type OrderFailureExchange = {
  title: string;
  method?: string | null;
  url?: string | null;
  http_status?: number | null;
  request?: unknown;
  response?: unknown;
  captured?: boolean;
};

export type OrderFailureDetail = {
  order_id: string;
  title: string;
  summary: string;
  error_code?: string | null;
  stage?: string | null;
  upstream_status?: number | null;
  upstream_code?: string | null;
  cf_ray?: string | null;
  request_id?: string | null;
  exchange?: OrderFailureExchange | null;
  events: QuickPayEvent[];
};

export async function getOrderEvents(orderId: string, signal?: AbortSignal): Promise<QuickPayEvent[]> {
  return requestJson<QuickPayEvent[]>(`/api/orders/${encodeURIComponent(orderId)}/events`, {
    cache: "no-store",
    signal,
  });
}

export async function getOrderFailureDetail(orderId: string, signal?: AbortSignal): Promise<OrderFailureDetail> {
  return requestJson<OrderFailureDetail>(`/api/orders/${encodeURIComponent(orderId)}/failure-detail`, {
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

export type OrderManualVerdictValue = "delivered" | "failed" | "refunded";

export type OrderManualVerdictInput = {
  verdict: OrderManualVerdictValue;
  note: string;
  operator?: string;
  confirm_no_charge?: boolean;
  mark_auto_renew_canceled?: boolean;
};

export type OrderManualVerdictResult = {
  order_id: string;
  previous_status: string;
  order_status: string;
  payment_status: string;
  delivery_status: string;
  auto_renew_status: string;
  job_status?: string | null;
  lock_state?: string | null;
  operator: string;
  note: string;
  message: string;
};

export async function submitOrderManualVerdict(
  orderId: string,
  input: OrderManualVerdictInput,
): Promise<OrderManualVerdictResult> {
  return requestJson<OrderManualVerdictResult>(
    `/api/orders/${encodeURIComponent(orderId)}/manual-verdict`,
    {
      method: "POST",
      body: {
        verdict: input.verdict,
        note: input.note.trim(),
        operator: (input.operator || "web-operator").trim() || "web-operator",
        confirm_no_charge: Boolean(input.confirm_no_charge),
        mark_auto_renew_canceled: Boolean(input.mark_auto_renew_canceled),
      },
      timeoutMs: 20_000,
      fallbackError: "提交人工判定失败",
    },
  );
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

export type OrderVaultMeta = {
  order_id: string;
  available: boolean;
  has_session: boolean;
  has_card: boolean;
  card_brand?: string | null;
  card_bin?: string | null;
  card_last4?: string | null;
  card_exp?: string | null;
  card_fingerprint?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  expires_at?: string | null;
  remaining_days?: number | null;
  expired: boolean;
  purged_at?: string | null;
  purge_reason?: string | null;
  retention_days: number;
};

export type OrderVaultCard = {
  number: string;
  exp_month: string;
  exp_year: string;
  cvc: string;
  billing?: Record<string, string> | null;
};

export type OrderVaultReveal = {
  meta: OrderVaultMeta;
  session?: string | null;
  card?: OrderVaultCard | null;
};

export async function getOrderVaultMeta(orderId: string, signal?: AbortSignal): Promise<OrderVaultMeta> {
  return requestJson<OrderVaultMeta>(`/api/orders/${encodeURIComponent(orderId)}/vault`, {
    cache: "no-store",
    signal,
  });
}

export async function revealOrderVault(
  orderId: string,
  input: { operator?: string; reason?: string } = {},
): Promise<OrderVaultReveal> {
  return requestJson<OrderVaultReveal>(`/api/orders/${encodeURIComponent(orderId)}/vault/reveal`, {
    method: "POST",
    body: {
      operator: input.operator || "web-operator",
      reason: input.reason || "售后核查",
    },
    cache: "no-store",
    fallbackError: "解密读取留存凭证失败",
  });
}

export async function purgeOrderVault(
  orderId: string,
  input: { operator?: string; reason?: string } = {},
): Promise<OrderVaultMeta> {
  return requestJson<OrderVaultMeta>(`/api/orders/${encodeURIComponent(orderId)}/vault`, {
    method: "DELETE",
    body: {
      operator: input.operator || "web-operator",
      reason: input.reason || "人工销毁凭证",
    },
    fallbackError: "销毁订单留存凭证失败",
  });
}
