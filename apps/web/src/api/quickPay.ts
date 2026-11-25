import type { Plan } from "./client";
import { requestJson } from "./http";

export type QuickPayEvent = {
  state: string;
  stage: string;
  message: string;
  created_at: string;
};

export type QuickPayNextAction = {
  type?: string;
  redirect_url?: string;
  message?: string;
};

export type QuickPayDiagnostics = {
  stage?: string;
  upstream_status?: number;
  upstream_code?: string;
  cf_ray?: string;
  request_id?: string;
  unusual_activity_source?: string;
};

export type QuickPayJob = {
  job_id: string;
  /** Server evidence, never inferred from a local timeout or an error string. */
  checkout_outcome?: "rejected_before_payment" | null;
  lock_id?: string | null;
  timings_ms?: Record<string, number>;
  diagnostics?: QuickPayDiagnostics | null;
  next_action?: QuickPayNextAction | null;
  events?: QuickPayEvent[];
  status: "queued" | "running" | "succeeded" | "failed" | "unknown" | "paid_pending_delivery" | "requires_action";
  stage: string;
  queue_position?: number | null;
  attempts?: number;
  reconcile_attempts?: number;
  last_reconciled_at?: string | null;
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

export type PreflightCheck = {
  key: string;
  label: string;
  status: "pass" | "warn" | "fail";
  code: string;
  message: string;
};

export type QueueStats = {
  queued: number;
  running: number;
  max_concurrency: number;
  draining: boolean;
};

export type QueueJobSummary = {
  job_id: string;
  status: QuickPayJob["status"];
  stage: string;
  queue_position?: number | null;
  attempts: number;
  order_id?: string | null;
  amount?: string | null;
  currency?: string | null;
  failure_code?: string | null;
  message?: string | null;
  timings_ms?: Record<string, number>;
  created_at: string;
  finished_at?: string | null;
};

export type QueueStatus = {
  stats: QueueStats;
  jobs: QueueJobSummary[];
};

export type PreflightInput = {
  plan: Plan;
  billing_country?: string;
  exit_country?: string;
  session: string;
  debug_checkout_only?: boolean;
  check_proxy_connectivity?: boolean;
  check_account_upstream?: boolean;
};

export type PreflightResult = {
  ok: boolean;
  blocking_code?: string | null;
  summary: string;
  checks: PreflightCheck[];
  queue: QueueStats;
  elapsed_ms: number;
};

export type BatchPreflightItemInput = {
  label?: string;
  plan: Plan;
  billing_country?: string;
  exit_country?: string;
  session: string;
  check_proxy_connectivity?: boolean;
  check_account_upstream?: boolean;
};

export type BatchPreflightItemResult = {
  index: number;
  label: string;
  ok: boolean;
  blocking_code?: string | null;
  summary: string;
  checks: PreflightCheck[];
  elapsed_ms: number;
};

export type BatchPreflightResult = {
  total: number;
  passed: number;
  failed: number;
  elapsed_ms: number;
  items: BatchPreflightItemResult[];
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

export async function startQuickPay(input: QuickPayInput, idempotencyKey: string): Promise<QuickPayJob> {
  if (input.debug_checkout_only === true && "card" in input) {
    throw new Error("仅建单请求不能包含银行卡资料");
  }
  if (input.debug_checkout_only !== true && !input.card) {
    throw new Error("付款请求必须提供银行卡资料");
  }
  return requestJson<QuickPayJob>("/api/payments/quick-pay", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: input,
    timeoutMs: 30_000,
  });
}

export async function getQuickPay(jobId: string, signal?: AbortSignal): Promise<QuickPayJob> {
  return requestJson<QuickPayJob>(`/api/payments/quick-pay/${encodeURIComponent(jobId)}`, {
    cache: "no-store",
    signal,
  });
}

export async function getQuickPayQueue(limit = 15, signal?: AbortSignal): Promise<QueueStatus> {
  return requestJson<QueueStatus>(`/api/payments/queue?limit=${encodeURIComponent(String(limit))}`, {
    cache: "no-store",
    signal,
  });
}

export async function runQuickPayPreflight(input: PreflightInput): Promise<PreflightResult> {
  return requestJson<PreflightResult>("/api/payments/preflight", {
    method: "POST",
    body: input,
    timeoutMs: 45_000,
  });
}

export async function runBatchPreflight(items: BatchPreflightItemInput[]): Promise<BatchPreflightResult> {
  return requestJson<BatchPreflightResult>("/api/payments/preflight/batch", {
    method: "POST",
    body: { items },
    timeoutMs: 60_000,
  });
}

export async function reconcileQuickPay(jobId: string): Promise<{
  outcome: string;
  changed: boolean;
  message: string;
}> {
  return requestJson(`/api/payments/quick-pay/${encodeURIComponent(jobId)}/reconcile`, {
    method: "POST",
    timeoutMs: 45_000,
  });
}

const STREAM_DONE_STATUSES = new Set(["succeeded", "failed", "unknown", "paid_pending_delivery"]);

export function subscribeQuickPayStream(
  jobId: string,
  handlers: {
    onUpdate: (job: QuickPayJob) => void;
    onDone?: (job: QuickPayJob) => void;
    onFallback?: () => void;
  },
): () => void {
  let closed = false;
  let fallbackTimer: number | null = null;
  let source: EventSource | null = null;

  const stop = () => {
    if (closed) return;
    closed = true;
    if (fallbackTimer !== null) {
      window.clearTimeout(fallbackTimer);
      fallbackTimer = null;
    }
    if (source) {
      source.close();
      source = null;
    }
  };

  const handleJob = (next: QuickPayJob) => {
    if (closed) return;
    handlers.onUpdate(next);
    if (STREAM_DONE_STATUSES.has(next.status)) {
      stop();
      handlers.onDone?.(next);
    }
  };

  const pollSnapshot = async (delayMs: number) => {
    if (closed) return;
    try {
      const snap = await getQuickPay(jobId);
      handleJob(snap);
      if (!closed) {
        fallbackTimer = window.setTimeout(() => void pollSnapshot(Math.min(delayMs + 500, 3000)), delayMs);
      }
    } catch {
      if (!closed) {
        fallbackTimer = window.setTimeout(() => void pollSnapshot(3000), 3000);
      }
    }
  };

  if (typeof EventSource === "undefined") {
    handlers.onFallback?.();
    fallbackTimer = window.setTimeout(() => void pollSnapshot(1000), 1000);
    return stop;
  }

  source = new EventSource(`/api/events/quick-pay/${encodeURIComponent(jobId)}/stream`);
  source.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data) as { type?: string; job?: QuickPayJob };
      if (payload.job) {
        handleJob(payload.job);
      }
    } catch {
      // Ignore malformed SSE frame; reconnect or snapshot fallback will recover.
    }
  };
  source.onerror = () => {
    if (closed) return;
    if (source) {
      source.close();
      source = null;
    }
    handlers.onFallback?.();
    void pollSnapshot(1500);
  };

  return stop;
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
  const body = await requestJson<{ lock: PaymentLock }>(`/api/payments/locks/${encodeURIComponent(lockId)}/release`, {
    method: "POST",
    body: { confirm_no_charge: true, note, operator: "web-operator" },
    fallbackError: "解锁失败",
  });
  return body.lock;
}
