import { inspectSession } from "./sessionInspect";

export type ParsedBatchEntry = {
  label: string;
  session: string;
};

export type DeviceConflictGroup = {
  deviceFingerprint: string;
  deviceIdPrefix: string;
  indices: number[];
  labels: string[];
};

export type BatchDeviceAudit = {
  conflicts: DeviceConflictGroup[];
  byIndex: Record<number, { deviceIdPrefix?: string; duplicateLabels: string[] }>;
};

export function parseBatchSessions(raw: string): ParsedBatchEntry[] {
  const trimmed = raw.trim();
  if (!trimmed) return [];

  if (trimmed.startsWith("{") && trimmed.endsWith("}")) {
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return [{ label: "账号 #1", session: trimmed }];
      }
    } catch {
      // Fall through to multi-entry splitting
    }
  }

  const bySeparator = trimmed
    .split(/\r?\n\s*---+\s*\r?\n|\r?\n\s*\r?\n/)
    .map((chunk) => chunk.trim())
    .filter(Boolean);

  const chunks = bySeparator.length > 1
    ? bySeparator
    : trimmed.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);

  return chunks.map((chunk, idx) => {
    const pipeIdx = chunk.indexOf("|");
    if (pipeIdx > 0 && chunk.slice(pipeIdx + 1).trim().startsWith("{")) {
      const label = chunk.slice(0, pipeIdx).trim();
      const session = chunk.slice(pipeIdx + 1).trim();
      return { label: label || `账号 #${idx + 1}`, session };
    }
    return { label: `账号 #${idx + 1}`, session: chunk };
  });
}

/**
 * Detect shared `oai-did` / `deviceId` across multiple batch sessions in memory
 * so operators can catch multi-account device reuse before triggering `subscription_limit`.
 */
export function detectDuplicateDevices(entries: ParsedBatchEntry[]): BatchDeviceAudit {
  const groups = new Map<string, { prefix: string; indices: number[]; labels: string[] }>();
  const byIndex: BatchDeviceAudit["byIndex"] = {};

  entries.forEach((entry, idx) => {
    const info = inspectSession(entry.session);
    byIndex[idx] = { deviceIdPrefix: info.deviceIdPrefix, duplicateLabels: [] };
    if (info.deviceFingerprint && info.deviceIdPrefix) {
      const existing = groups.get(info.deviceFingerprint);
      if (existing) {
        existing.indices.push(idx);
        existing.labels.push(entry.label);
      } else {
        groups.set(info.deviceFingerprint, {
          prefix: info.deviceIdPrefix,
          indices: [idx],
          labels: [entry.label],
        });
      }
    }
  });

  const conflicts: DeviceConflictGroup[] = [];
  for (const [fp, grp] of groups.entries()) {
    if (grp.indices.length > 1) {
      conflicts.push({
        deviceFingerprint: fp,
        deviceIdPrefix: grp.prefix,
        indices: grp.indices,
        labels: grp.labels,
      });
      for (const idx of grp.indices) {
        byIndex[idx].duplicateLabels = grp.labels.filter((_, pos) => grp.indices[pos] !== idx);
      }
    }
  }

  return { conflicts, byIndex };
}
