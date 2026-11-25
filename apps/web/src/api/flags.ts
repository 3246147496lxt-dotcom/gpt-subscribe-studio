import { requestJson } from "./http";

export type RuntimeFlagOption = {
  value: string;
  label: string;
};

export type RuntimeFlagValue = boolean | number | string;

export type RuntimeFlagItem = {
  key: string;
  category: "session" | "protocol" | "runtime";
  label: string;
  description: string;
  kind: "bool" | "enum" | "int";
  env_var: string;
  value: RuntimeFlagValue;
  default: RuntimeFlagValue;
  env_value?: RuntimeFlagValue | null;
  source: "override" | "env" | "default";
  options: RuntimeFlagOption[];
  min_value?: number | null;
  max_value?: number | null;
};

export type RuntimeFlagsSnapshot = {
  items: RuntimeFlagItem[];
  override_count: number;
  updated_at?: string | null;
  fake_mode: boolean;
};

export async function getRuntimeFlags(signal?: AbortSignal): Promise<RuntimeFlagsSnapshot> {
  return requestJson<RuntimeFlagsSnapshot>("/api/settings/flags", { cache: "no-store", signal });
}

export async function updateRuntimeFlags(
  updates: Record<string, RuntimeFlagValue | null>,
): Promise<RuntimeFlagsSnapshot> {
  return requestJson<RuntimeFlagsSnapshot>("/api/settings/flags", {
    method: "PUT",
    body: { updates },
  });
}

export async function resetRuntimeFlags(keys?: string[]): Promise<RuntimeFlagsSnapshot> {
  return requestJson<RuntimeFlagsSnapshot>("/api/settings/flags/reset", {
    method: "POST",
    body: keys ? { keys } : {},
  });
}
