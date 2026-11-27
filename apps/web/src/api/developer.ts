import { requestJson } from "./http";

export type ApiKeyItem = {
  id: string;
  name: string;
  key_prefix: string;
  status: "active" | "disabled" | "revoked" | string;
  rate_limit_rpm: number;
  allowed_plans: string[];
  note?: string | null;
  total_requests: number;
  total_orders: number;
  succeeded_orders: number;
  failed_orders: number;
  pending_orders: number;
  last_used_at?: string | null;
  created_at: string;
  updated_at: string;
  revoked_at?: string | null;
};

export type ApiKeyListResponse = {
  items: ApiKeyItem[];
  vault_retention_days: number;
};

export type ApiKeySecretResponse = {
  key: ApiKeyItem;
  api_key: string;
  warning: string;
};

export type ApiKeyCreateInput = {
  name: string;
  rate_limit_rpm: number;
  allowed_plans: string[];
  note?: string;
};

export type ApiKeyUpdateInput = {
  name?: string;
  status?: "active" | "disabled";
  rate_limit_rpm?: number;
  allowed_plans?: string[];
  note?: string;
};

export async function listApiKeys(signal?: AbortSignal): Promise<ApiKeyListResponse> {
  return requestJson<ApiKeyListResponse>("/api/developer/keys", { cache: "no-store", signal });
}

export async function createApiKey(input: ApiKeyCreateInput): Promise<ApiKeySecretResponse> {
  return requestJson<ApiKeySecretResponse>("/api/developer/keys", {
    method: "POST",
    body: input,
    fallbackError: "创建 API Key 失败",
  });
}

export async function updateApiKey(keyId: string, input: ApiKeyUpdateInput): Promise<ApiKeyItem> {
  return requestJson<ApiKeyItem>(`/api/developer/keys/${encodeURIComponent(keyId)}`, {
    method: "PATCH",
    body: input,
    fallbackError: "更新 API Key 失败",
  });
}

export async function rotateApiKey(keyId: string): Promise<ApiKeySecretResponse> {
  return requestJson<ApiKeySecretResponse>(`/api/developer/keys/${encodeURIComponent(keyId)}/rotate`, {
    method: "POST",
    fallbackError: "轮换 API Key 失败",
  });
}

export async function revokeApiKey(keyId: string): Promise<ApiKeyItem> {
  return requestJson<ApiKeyItem>(`/api/developer/keys/${encodeURIComponent(keyId)}`, {
    method: "DELETE",
    fallbackError: "吊销 API Key 失败",
  });
}
