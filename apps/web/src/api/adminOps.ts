import { requestJson } from "./http";
import type { ReconcilerStatus } from "./ledger";
import type { QueueStats } from "./quickPay";

export type ReferencePricingStatus = {
  enabled: boolean;
  running?: boolean;
  fx_source: string;
  fx_updated_at: number;
  fx_date?: string;
  updated_at: number;
  updated_label?: string;
  updated_full_label?: string;
  stale: boolean;
  usd_cny_rate?: number;
  last_error?: string | null;
};

export type HealthSnapshot = {
  status: string;
  version: string;
  promo_enabled: boolean;
  fake_adapters: boolean;
  missing_upstreams: string[];
  proxy_sid_pinned: boolean;
  queue: QueueStats;
  reconciler: ReconcilerStatus;
  reference_pricing: ReferencePricingStatus;
  webhooks: {
    enabled: boolean;
    configured: boolean;
    subscribed_events: number;
    recent_deliveries: number;
  };
};

export type PortalAnnouncement = {
  id: string;
  text: string;
  text_en: string;
};

export type RegionPriceCell = {
  country: string;
  currency: string;
  local_price: number;
  usd: number;
  cny: number;
};

export type ReferencePriceWinner = {
  country: string;
  region: string;
  region_name: string;
  region_name_en: string;
  currency: string;
  local_price: number;
  usd: number;
  cny: number;
  base: number;
  discount: string;
  discount_en: string;
  price_updated_at: number;
  updated_at: number;
  region_prices: Record<string, RegionPriceCell>;
};

export type PortalPlanItem = {
  id: string;
  title: string;
  title_en: string;
  full_name: string;
  default_region: string;
  usd_price?: number | null;
  cny_price?: number | null;
  local_price?: number | null;
  local_currency?: string | null;
  base_usd?: number | null;
  discount: string;
  discount_en: string;
  available: boolean;
  coming_soon?: boolean;
  region_prices?: Record<string, RegionPriceCell>;
};

export type PortalRegionItem = {
  code: string;
  label: string;
  label_en: string;
  currency: string;
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
  usd_cny_rate: number;
  fx_rates?: Record<string, number>;
  price_updated_at: string;
  price_note: string;
  price_note_en: string;
  announcements: PortalAnnouncement[];
  plans: PortalPlanItem[];
  regions: PortalRegionItem[];
  reference_prices?: Record<string, ReferencePriceWinner>;
  reference_price_status?: ReferencePricingStatus;
};

export type PortalSiteConfigPatch = Partial<
  Pick<
    PortalSiteConfig,
    | "brand_name"
    | "brand_name_en"
    | "footer_slogan"
    | "footer_slogan_en"
    | "hero_title"
    | "hero_title_en"
    | "hero_subtitle_1"
    | "hero_subtitle_1_en"
    | "hero_subtitle_2"
    | "hero_subtitle_2_en"
    | "card_shop_url"
    | "card_shop_label"
    | "card_shop_label_en"
    | "support_url"
    | "support_label"
    | "support_label_en"
    | "support_note"
    | "support_note_en"
    | "price_note"
    | "price_note_en"
    | "announcements"
  >
>;

export type ReferencePriceRefreshResult = {
  status: ReferencePricingStatus;
  reference_prices: Record<string, ReferencePriceWinner>;
  reference_price_status: ReferencePricingStatus;
};

export type CancelAutoRenewResult = {
  canceled: boolean;
  already_canceled?: boolean;
  message?: string;
  plan?: string;
};

export async function getHealth(signal?: AbortSignal): Promise<HealthSnapshot> {
  return requestJson<HealthSnapshot>("/api/health", {
    cache: "no-store",
    signal,
    timeoutMs: 12_000,
  });
}

export async function getAdminPortalSite(signal?: AbortSignal): Promise<PortalSiteConfig> {
  return requestJson<PortalSiteConfig>("/api/settings/portal-site", {
    cache: "no-store",
    signal,
    timeoutMs: 15_000,
  });
}

export async function updateAdminPortalSite(patch: PortalSiteConfigPatch): Promise<PortalSiteConfig> {
  return requestJson<PortalSiteConfig>("/api/settings/portal-site", {
    method: "PUT",
    body: patch,
    timeoutMs: 15_000,
    fallbackError: "保存门户配置失败",
  });
}

export async function refreshReferencePrices(): Promise<ReferencePriceRefreshResult> {
  return requestJson<ReferencePriceRefreshResult>("/api/settings/reference-prices/refresh", {
    method: "POST",
    timeoutMs: 25_000,
    fallbackError: "刷新实时汇率失败",
  });
}

export async function updateLocalReferencePrice(input: {
  plan: string;
  country: string;
  currency: string;
  amount: number;
}): Promise<{
  reference_prices: Record<string, ReferencePriceWinner>;
  reference_price_status: ReferencePricingStatus;
}> {
  return requestJson("/api/settings/reference-prices/local", {
    method: "PUT",
    body: input,
    timeoutMs: 15_000,
    fallbackError: "更新地区价格失败",
  });
}

export async function cancelAccountAutoRenew(input: {
  session: string;
  plan?: string;
  accountId?: string;
}): Promise<CancelAutoRenewResult> {
  return requestJson<CancelAutoRenewResult>("/api/public/account/cancel-auto-renew", {
    method: "POST",
    body: {
      session: input.session,
      plan: input.plan ?? "",
      accountId: input.accountId ?? "",
    },
    timeoutMs: 45_000,
    includeErrorCode: true,
    fallbackError: "关闭自动续费失败",
  });
}
