import { requestJson } from "./http";

export type FixedBillingAddress = {
  name: string;
  email: string;
  line1: string;
  city: string;
  state: string;
  postal_code: string;
  country: string;
};

export type FixedBillingStateOption = {
  value: string;
  label: string;
};

export type FixedBillingSource = "env_fixed" | "managed" | "legacy_env" | "none";

export type FixedBillingSettings = {
  configured: boolean;
  valid: boolean;
  source: FixedBillingSource;
  storage_label: string;
  can_delete: boolean;
  error?: string | null;
  address?: FixedBillingAddress | null;
  verification_id?: string | null;
  updated_at?: string | null;
  strict_exit_match: boolean;
  tax_free_states: FixedBillingStateOption[];
  default_sample: FixedBillingAddress;
};

export async function getFixedBillingSettings(signal?: AbortSignal): Promise<FixedBillingSettings> {
  return requestJson<FixedBillingSettings>("/api/settings/fixed-billing", {
    cache: "no-store",
    signal,
  });
}

export async function saveFixedBillingSettings(
  input: FixedBillingAddress,
): Promise<FixedBillingSettings> {
  return requestJson<FixedBillingSettings>("/api/settings/fixed-billing", {
    method: "PUT",
    body: input,
  });
}

export async function deleteFixedBillingSettings(): Promise<FixedBillingSettings> {
  return requestJson<FixedBillingSettings>("/api/settings/fixed-billing", {
    method: "DELETE",
  });
}
