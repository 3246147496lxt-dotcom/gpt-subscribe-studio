export type ParsedBatchEntry = {
  label: string;
  session: string;
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
