import { requestJson } from "./http";

export type SystemCheck = {
  key: string;
  label: string;
  status: "pass" | "warn" | "fail";
  code: string;
  message: string;
};

export type SystemDiagnostics = {
  ok: boolean;
  python_version: string;
  node_version?: string | null;
  db_bytes: number;
  wal_bytes: number;
  table_counts: Record<string, number>;
  cached_orders: number;
  disk_free_mb: number;
  checks: SystemCheck[];
};

export type MaintenanceResult = {
  retention_days: number;
  pruned_events: number;
  db_bytes: number;
  wal_bytes: number;
  wal_checkpoint?: Record<string, number> | null;
  table_counts: Record<string, number>;
};

export async function getSystemDiagnostics(signal?: AbortSignal): Promise<SystemDiagnostics> {
  return requestJson<SystemDiagnostics>("/api/system/diagnostics", {
    cache: "no-store",
    signal,
    timeoutMs: 15_000,
  });
}

export async function runSystemMaintenance(retentionDays = 90): Promise<MaintenanceResult> {
  const query = new URLSearchParams({ retention_days: String(retentionDays) });
  return requestJson<MaintenanceResult>(`/api/system/maintenance?${query.toString()}`, {
    method: "POST",
    timeoutMs: 30_000,
  });
}
