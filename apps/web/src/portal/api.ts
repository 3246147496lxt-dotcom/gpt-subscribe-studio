import type { AccountSubscription, StripePortal } from "../api/account";
import type { QuickPayEvent, QuickPayNextAction } from "../api/quickPay";

export type PortalRegionPrice = {
  country: string;
  currency: string;
  local_price: number;
  usd: number;
  cny: number;
};

export type PortalPlanConfig = {
  id: string;
  title: string;
  title_en: string;
  full_name: string;
  default_region: string;
  usd_price: number | null;
  cny_price: number | null;
  local_price?: number | null;
  local_currency?: string | null;
  base_usd?: number | null;
  discount: string;
  discount_en: string;
  available: boolean;
  coming_soon?: boolean;
  region_prices?: Record<string, PortalRegionPrice>;
};

export type PortalReferencePriceStatus = {
  enabled: boolean;
  updated_at?: number;
  fx_updated_at?: number;
  fx_source?: string;
  stale: boolean;
};

export type PortalRegionConfig = {
  code: string;
  label: string;
  label_en: string;
  currency: string;
};

export type PortalAnnouncement = {
  id: string;
  text: string;
  text_en: string;
};

export type PortalSiteConfig = {
  brand_name: string;
  brand_name_en: string;
  footer_slogan: string;
  footer_slogan_en: string;
  hero_title: string;
  hero_title_en: string;
  hero_subtitle_1: string;
  hero_subtitle_1_en: string;
  hero_subtitle_2: string;
  hero_subtitle_2_en: string;
  card_shop_url: string;
  card_shop_label: string;
  card_shop_label_en: string;
  support_url: string;
  support_label: string;
  support_label_en: string;
  support_note: string;
  support_note_en: string;
  telegram_login_enabled: boolean;
  usd_cny_rate: number;
  price_updated_at: string;
  price_note: string;
  price_note_en: string;
  announcements: PortalAnnouncement[];
  plans: PortalPlanConfig[];
  regions: PortalRegionConfig[];
  reference_price_status?: PortalReferencePriceStatus;
  connected: boolean;
  guest_ttl_days: number;
};

export type PortalAuthState = {
  authenticated: boolean;
  username: string | null;
  user_id: string | null;
  has_guest_token?: boolean;
  guest_ttl_days?: number;
  claimed_tasks?: number;
};

export type PublicTask = {
  job_id: string;
  order_id?: string | null;
  status: "queued" | "running" | "succeeded" | "failed" | "unknown" | "paid_pending_delivery" | "requires_action";
  stage: string;
  queue_position?: number | null;
  amount?: string | null;
  currency?: string | null;
  failure_code?: string | null;
  message?: string | null;
  auto_renew_status?: string;
  auto_renew_canceled_at?: string | null;
  auto_renew_error?: string | null;
  checkout_outcome?: "rejected_before_payment" | null;
  next_action?: QuickPayNextAction | null;
  events?: QuickPayEvent[];
  plan?: string | null;
  exit_country?: string | null;
  masked_email?: string | null;
  created_at?: string | null;
  finished_at?: string | null;
  guest_token?: string;
};

export type PublicTasksPage = {
  items: PublicTask[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  authenticated: boolean;
  username: string | null;
};

export type CancelAutoRenewResult = {
  canceled: boolean;
  will_renew?: boolean;
  manual_required: boolean;
  portal_url?: string | null;
  end_time?: string | null;
  stage?: string | null;
  message: string;
};

const GUEST_STORAGE_KEY = "gss.portal.guest_token.v1";
const LAST_TASK_KEY = "gss.portal.last_task.v1";
const GUEST_TTL_MS = 7 * 24 * 60 * 60 * 1000;

export function readGuestToken(): string {
  try {
    const raw = window.localStorage.getItem(GUEST_STORAGE_KEY);
    if (!raw) return "";
    const parsed = JSON.parse(raw) as { token?: string; expiresAt?: number };
    if (!parsed.token || (parsed.expiresAt && parsed.expiresAt < Date.now())) {
      window.localStorage.removeItem(GUEST_STORAGE_KEY);
      return "";
    }
    return parsed.token;
  } catch {
    return "";
  }
}

export function saveGuestToken(token: string): void {
  if (!token) return;
  try {
    window.localStorage.setItem(
      GUEST_STORAGE_KEY,
      JSON.stringify({ token, expiresAt: Date.now() + GUEST_TTL_MS }),
    );
  } catch {
    // ignore storage errors
  }
}

export function readLastTaskId(): string {
  try {
    const raw = window.localStorage.getItem(LAST_TASK_KEY);
    if (!raw) return "";
    const parsed = JSON.parse(raw) as { jobId?: string; expiresAt?: number };
    if (!parsed.jobId || (parsed.expiresAt && parsed.expiresAt < Date.now())) {
      window.localStorage.removeItem(LAST_TASK_KEY);
      return "";
    }
    return parsed.jobId;
  } catch {
    return "";
  }
}

export function saveLastTaskId(jobId: string): void {
  try {
    if (!jobId) {
      window.localStorage.removeItem(LAST_TASK_KEY);
      return;
    }
    window.localStorage.setItem(
      LAST_TASK_KEY,
      JSON.stringify({ jobId, expiresAt: Date.now() + GUEST_TTL_MS }),
    );
  } catch {
    // ignore storage errors
  }
}

async function portalFetch<T>(
  path: string,
  options: {
    method?: string;
    body?: unknown;
    headers?: Record<string, string>;
    signal?: AbortSignal;
    timeoutMs?: number;
  } = {},
): Promise<T> {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? 25_000;
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const onParentAbort = () => controller.abort();
  options.signal?.addEventListener("abort", onParentAbort, { once: true });

  const headers: Record<string, string> = { ...(options.headers ?? {}) };
  const guest = readGuestToken();
  if (guest && !headers["X-Portal-Guest-Token"]) {
    headers["X-Portal-Guest-Token"] = guest;
  }
  if (options.body !== undefined && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  try {
    const res = await fetch(path, {
      method: options.method ?? "GET",
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    });
    const text = await res.text();
    let parsed: unknown = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      parsed = null;
    }
    if (!res.ok) {
      const detail = parsed && typeof parsed === "object" && "detail" in parsed
        ? (parsed as { detail?: unknown }).detail
        : parsed;
      const msg =
        (detail && typeof detail === "object" && "message" in detail && typeof (detail as { message?: unknown }).message === "string"
          ? (detail as { message: string }).message
          : typeof detail === "string"
            ? detail
            : null) || `请求失败（HTTP ${res.status}）`;
      throw new Error(msg);
    }
    return parsed as T;
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error("请求超时，请检查网络连接后重试。");
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
    options.signal?.removeEventListener("abort", onParentAbort);
  }
}

export function fetchSiteConfig(signal?: AbortSignal): Promise<PortalSiteConfig> {
  return portalFetch<PortalSiteConfig>("/api/public/site-config", { signal, timeoutMs: 10_000 });
}

export function fetchAuthMe(signal?: AbortSignal): Promise<PortalAuthState> {
  return portalFetch<PortalAuthState>("/api/public/auth/me", { signal, timeoutMs: 10_000 });
}

export function loginPortalUser(username: string, password: string): Promise<PortalAuthState> {
  return portalFetch<PortalAuthState>("/api/public/auth/login", {
    method: "POST",
    body: { username, password },
  });
}

export function registerPortalUser(username: string, password: string): Promise<PortalAuthState> {
  return portalFetch<PortalAuthState>("/api/public/auth/register", {
    method: "POST",
    body: { username, password },
  });
}

export function logoutPortalUser(): Promise<{ authenticated: boolean }> {
  return portalFetch<{ authenticated: boolean }>("/api/public/auth/logout", { method: "POST" });
}

export async function submitPublicRecharge(
  input: {
    plan: string;
    exit_country: string;
    session: string;
    card: { number: string; exp_month: string; exp_year: string; cvc: string };
    auto_cancel_renew?: boolean;
  },
  idempotencyKey: string,
): Promise<PublicTask> {
  const res = await portalFetch<PublicTask>("/api/public/quick-pay", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: { ...input, auto_cancel_renew: input.auto_cancel_renew ?? true },
    timeoutMs: 30_000,
  });
  if (res.guest_token) {
    saveGuestToken(res.guest_token);
  }
  saveLastTaskId(res.job_id);
  return res;
}

export function fetchPublicTasks(params: { q?: string; page?: number; pageSize?: number }): Promise<PublicTasksPage> {
  const sp = new URLSearchParams();
  if (params.q) sp.set("q", params.q);
  if (params.page) sp.set("page", String(params.page));
  if (params.pageSize) sp.set("page_size", String(params.pageSize));
  return portalFetch<PublicTasksPage>(`/api/public/tasks?${sp.toString()}`);
}

export function fetchPublicTask(jobId: string, signal?: AbortSignal): Promise<PublicTask> {
  return portalFetch<PublicTask>(`/api/public/tasks/${encodeURIComponent(jobId)}`, { signal });
}

export type PublicBillingHistoryResult = {
  invoices: AccountSubscription["invoices"];
  billingSource?: string | null;
  billingNote?: string | null;
  currency: string;
  retryable: boolean;
};

export function fetchPublicSubscriptionDetail(input: {
  session: string;
  forceRefresh?: boolean;
  includeInvoices?: boolean;
  signal?: AbortSignal;
}): Promise<AccountSubscription> {
  return portalFetch<AccountSubscription>("/api/public/account/subscription-detail", {
    method: "POST",
    body: {
      session: input.session,
      accountId: "",
      forceRefresh: Boolean(input.forceRefresh),
      includeInvoices: input.includeInvoices ?? false,
    },
    signal: input.signal,
    timeoutMs: 30_000,
  });
}

export function fetchPublicBillingHistory(input: {
  session: string;
  planName?: string;
  currency?: string;
  signal?: AbortSignal;
}): Promise<PublicBillingHistoryResult> {
  return portalFetch<PublicBillingHistoryResult>("/api/public/account/billing-history", {
    method: "POST",
    body: {
      session: input.session,
      accountId: "",
      planName: input.planName || "",
      currency: input.currency || "",
    },
    signal: input.signal,
    timeoutMs: 70_000,
  });
}

export function fetchPublicStripePortal(session: string): Promise<StripePortal> {
  return portalFetch<StripePortal>("/api/public/account/stripe-portal", {
    method: "POST",
    body: { session, accountId: "" },
    timeoutMs: 25_000,
  });
}

export function cancelAutoRenewStandalone(session: string, plan = ""): Promise<CancelAutoRenewResult> {
  return portalFetch<CancelAutoRenewResult>("/api/public/account/cancel-auto-renew", {
    method: "POST",
    body: { session, plan },
    timeoutMs: 75_000,
  });
}

export function retryTaskCancelAutoRenew(jobId: string, session: string): Promise<CancelAutoRenewResult> {
  return portalFetch<CancelAutoRenewResult>(`/api/public/tasks/${encodeURIComponent(jobId)}/cancel-auto-renew`, {
    method: "POST",
    body: { session },
    timeoutMs: 75_000,
  });
}

const TERMINAL_STATUSES = new Set(["succeeded", "failed", "unknown", "paid_pending_delivery"]);

export function subscribePublicTaskStream(
  jobId: string,
  handlers: {
    onUpdate: (task: PublicTask) => void;
    onDone?: (task: PublicTask) => void;
  },
): () => void {
  let closed = false;
  let pollTimer: number | null = null;
  let source: EventSource | null = null;

  const stop = () => {
    if (closed) return;
    closed = true;
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
    if (source) {
      source.close();
      source = null;
    }
  };

  const applyTask = (next: PublicTask) => {
    if (closed) return;
    handlers.onUpdate(next);
    if (TERMINAL_STATUSES.has(next.status)) {
      stop();
      handlers.onDone?.(next);
    }
  };

  const poll = async (delayMs: number) => {
    if (closed) return;
    try {
      const snap = await fetchPublicTask(jobId);
      applyTask(snap);
      if (!closed) {
        pollTimer = window.setTimeout(() => void poll(Math.min(delayMs + 500, 3000)), delayMs);
      }
    } catch {
      if (!closed) {
        pollTimer = window.setTimeout(() => void poll(3000), 3000);
      }
    }
  };

  if (typeof EventSource === "undefined") {
    pollTimer = window.setTimeout(() => void poll(1200), 1000);
    return stop;
  }

  source = new EventSource(`/api/public/tasks/${encodeURIComponent(jobId)}/stream`);
  source.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data) as { type?: string; job?: PublicTask };
      if (payload.type === "error") {
        source?.close();
        source = null;
        void poll(1500);
        return;
      }
      if (payload.job) {
        applyTask(payload.job);
      }
    } catch {
      // ignore malformed frame
    }
  };
  source.onerror = () => {
    if (closed) return;
    if (source) {
      source.close();
      source = null;
    }
    void poll(1500);
  };

  return stop;
}
