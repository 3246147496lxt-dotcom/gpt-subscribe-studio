import { requestJson } from "./http";

export type WebhookConfig = {
  enabled: boolean;
  url_masked: string;
  has_url: boolean;
  has_secret: boolean;
  events: string[];
  supported_events: string[];
};

export type WebhookDelivery = {
  id: string;
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
};

export async function getWebhookSettings(signal?: AbortSignal): Promise<WebhookSettings> {
  return requestJson<WebhookSettings>("/api/settings/webhooks", { cache: "no-store", signal });
}

export async function saveWebhookSettings(input: {
  enabled: boolean;
  url?: string;
  secret?: string;
  clear_secret?: boolean;
  events: string[];
}): Promise<WebhookConfig> {
  return requestJson<WebhookConfig>("/api/settings/webhooks", {
    method: "PUT",
    body: input,
  });
}

export async function testWebhook(): Promise<WebhookTestResult> {
  return requestJson<WebhookTestResult>("/api/settings/webhooks/test", {
    method: "POST",
    timeoutMs: 20_000,
  });
}
