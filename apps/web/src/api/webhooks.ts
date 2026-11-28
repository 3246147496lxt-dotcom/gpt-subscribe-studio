import { requestJson } from "./http";

export type WebhookConfig = {
  enabled: boolean;
  url_masked: string;
  has_url: boolean;
  has_secret: boolean;
  events: string[];
  supported_events: string[];
  telegram_enabled: boolean;
  telegram_has_token: boolean;
  telegram_token_masked: string;
  telegram_chat_id: string;
  telegram_thread_id?: number | null;
  telegram_api_base: string;
  telegram_silent: boolean;
  telegram_configured: boolean;
  telegram_bot_commands_enabled?: boolean;
  telegram_digest_enabled?: boolean;
  telegram_digest_hour?: number;
  telegram_streak_threshold?: number;
  telegram_disk_warn_mb?: number;
};

export type WebhookDelivery = {
  id: string;
  channel?: "webhook" | "telegram" | string;
  event: string;
  ok: boolean;
  status_code?: number | null;
  elapsed_ms: number;
  attempts: number;
  error?: string | null;
  created_at: string;
};

export type WebhookSettings = {
  config: WebhookConfig;
  deliveries: WebhookDelivery[];
};

export type WebhookTestResult = {
  ok: boolean;
  status_code?: number | null;
  elapsed_ms: number;
  error?: string | null;
  delivery?: WebhookDelivery | null;
  deliveries?: WebhookDelivery[];
};

export async function getWebhookSettings(signal?: AbortSignal): Promise<WebhookSettings> {
  return requestJson<WebhookSettings>("/api/settings/webhooks", { cache: "no-store", signal });
}

export async function saveWebhookSettings(input: {
  enabled?: boolean;
  url?: string;
  secret?: string;
  clear_secret?: boolean;
  events?: string[];
  telegram_enabled?: boolean;
  telegram_bot_token?: string;
  clear_telegram_token?: boolean;
  telegram_chat_id?: string;
  telegram_thread_id?: number | string | null;
  clear_telegram_thread_id?: boolean;
  telegram_api_base?: string;
  telegram_silent?: boolean;
  telegram_bot_commands_enabled?: boolean;
  telegram_digest_enabled?: boolean;
  telegram_digest_hour?: number;
  telegram_streak_threshold?: number;
  telegram_disk_warn_mb?: number;
}): Promise<WebhookConfig> {
  return requestJson<WebhookConfig>("/api/settings/webhooks", {
    method: "PUT",
    body: input,
  });
}

export async function testWebhook(channel: "all" | "webhook" | "telegram" = "all"): Promise<WebhookTestResult> {
  const query = channel && channel !== "all" ? `?channel=${encodeURIComponent(channel)}` : "";
  return requestJson<WebhookTestResult>(`/api/settings/webhooks/test${query}`, {
    method: "POST",
    timeoutMs: 20_000,
  });
}
