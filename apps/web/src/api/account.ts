export interface AccountInvoice {
  plan: string;
  paidAt: string;
  status: string;
  amount: string;
}

export interface AccountSubscription {
  email: string;
  accountId: string;
  status: string;
  planName: string;
  startTime?: string | null;
  endTime?: string | null;
  remainingDays?: number | null;
  autoRenew: string;
  paymentChannel: string;
  currency: string;
  invoices: AccountInvoice[];
  queriedAt: string;
  cached: boolean;
  stale: boolean;
  warning?: string | null;
}

export interface AccountSubscriptionInput {
  session: string;
  accountId?: string;
  forceRefresh?: boolean;
}

export async function fetchAccountSubscription(input: AccountSubscriptionInput): Promise<AccountSubscription> {
  const response = await fetch("/api/account/subscription-detail", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session: input.session,
      accountId: input.accountId ?? "",
      forceRefresh: Boolean(input.forceRefresh),
    }),
  });
  const data = await response.json().catch(() => null) as
    | { detail?: { code?: string; message?: string } }
    | AccountSubscription
    | null;
  if (!response.ok) {
    const detail = (data as { detail?: { code?: string; message?: string } } | null)?.detail;
    const message = detail?.message || `查询失败（HTTP ${response.status}）`;
    throw new Error(detail?.code ? `${message}（${detail.code}）` : message);
  }
  return data as AccountSubscription;
}

export interface StripePortal {
  url: string;
  source: string;
}

export async function fetchStripePortal(input: { session: string; accountId?: string }): Promise<StripePortal> {
  const response = await fetch("/api/account/stripe-portal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session: input.session, accountId: input.accountId ?? "" }),
  });
  const data = await response.json().catch(() => null) as
    | { detail?: { code?: string; message?: string } }
    | StripePortal
    | null;
  if (!response.ok) {
    const detail = (data as { detail?: { code?: string; message?: string } } | null)?.detail;
    const message = detail?.message || `无法打开账单门户（HTTP ${response.status}）`;
    throw new Error(detail?.code ? `${message}（${detail.code}）` : message);
  }
  return data as StripePortal;
}
