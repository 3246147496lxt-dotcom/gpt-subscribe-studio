import { requestJson } from "./http";

export interface AccountInvoice {
  plan: string;
  paidAt: string;
  status: string;
  amount: string;
  invoiceId?: string | null;
  invoiceUrl?: string | null;
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
  billingSource?: string | null;
  billingNote?: string | null;
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
  return requestJson<AccountSubscription>("/api/account/subscription-detail", {
    method: "POST",
    body: {
      session: input.session,
      accountId: input.accountId ?? "",
      forceRefresh: Boolean(input.forceRefresh),
    },
    timeoutMs: 60_000,
    includeErrorCode: true,
    fallbackError: "查询失败",
  });
}

export interface StripePortal {
  url: string;
  source: string;
}

export async function fetchStripePortal(input: { session: string; accountId?: string }): Promise<StripePortal> {
  return requestJson<StripePortal>("/api/account/stripe-portal", {
    method: "POST",
    body: { session: input.session, accountId: input.accountId ?? "" },
    timeoutMs: 30_000,
    includeErrorCode: true,
    fallbackError: "无法打开账单门户",
  });
}
