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
    const detail = (data as { detail?: { message?: string } } | null)?.detail;
    throw new Error(detail?.message || `查询失败（HTTP ${response.status}）`);
  }
  return data as AccountSubscription;
}
