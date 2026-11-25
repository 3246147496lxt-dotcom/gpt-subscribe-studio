/**
 * Client-side local Session file extraction (JSON, TXT, RTF, DOCX, PDF fallback).
 * Strictly runs in browser memory; max 5 MB; never uploads raw files.
 */

const MAX_BYTES = 5 * 1024 * 1024;

function extractJsonObject(text: string): string | null {
  const start = text.indexOf("{");
  if (start < 0) return null;
  // Find balanced outermost JSON object containing accessToken / sessionToken / refreshToken.
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < text.length; i += 1) {
    const ch = text[i];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (ch === "\\") {
        escaped = true;
      } else if (ch === '"') {
        inString = false;
      }
      continue;
    }
    if (ch === '"') {
      inString = true;
    } else if (ch === "{") {
      depth += 1;
    } else if (ch === "}") {
      depth -= 1;
      if (depth === 0) {
        const candidate = text.slice(start, i + 1);
        try {
          const parsed = JSON.parse(candidate);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            return candidate;
          }
        } catch {
          // keep scanning
        }
        const nextStart = text.indexOf("{", i + 1);
        if (nextStart < 0) break;
        i = nextStart - 1;
      }
    }
  }
  return null;
}

function cleanRtf(raw: string): string {
  return raw
    .replace(/\\'[0-9a-fA-F]{2}/g, "")
    .replace(/\\par[d]?/g, "\n")
    .replace(/\\[a-zA-Z]+-?\d* ?/g, "")
    .replace(/\\([{}\\])/g, "$1")
    .trim();
}

async function extractDocxText(buffer: ArrayBuffer): Promise<string> {
  const bytes = new Uint8Array(buffer);
  const view = new DataView(buffer);
  // Locate ZIP local file headers (PK\x03\x04) and extract word/document.xml.
  let offset = 0;
  while (offset + 30 <= bytes.length) {
    if (view.getUint32(offset, true) !== 0x04034b50) {
      offset += 1;
      continue;
    }
    const method = view.getUint16(offset + 8, true);
    const compSize = view.getUint32(offset + 18, true);
    const nameLen = view.getUint16(offset + 26, true);
    const extraLen = view.getUint16(offset + 28, true);
    const nameStart = offset + 30;
    const name = new TextDecoder().decode(bytes.subarray(nameStart, nameStart + nameLen));
    const dataStart = nameStart + nameLen + extraLen;
    if (name === "word/document.xml" && compSize > 0 && dataStart + compSize <= bytes.length) {
      const slice = bytes.subarray(dataStart, dataStart + compSize);
      let xmlText = "";
      if (method === 0) {
        xmlText = new TextDecoder().decode(slice);
      } else if (method === 8 && typeof DecompressionStream !== "undefined") {
        const ds = new DecompressionStream("deflate-raw");
        const writer = ds.writable.getWriter();
        void writer.write(slice);
        void writer.close();
        xmlText = await new Response(ds.readable).text();
      }
      if (xmlText) {
        return xmlText
          .replace(/<w:p[^>]*>/g, "\n")
          .replace(/<[^>]+>/g, "")
          .replace(/&quot;/g, '"')
          .replace(/&amp;/g, "&")
          .replace(/&lt;/g, "<")
          .replace(/&gt;/g, ">")
          .trim();
      }
    }
    offset = dataStart + Math.max(1, compSize);
  }
  return "";
}

function extractSessionFromRawText(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "";
  const jsonHit = extractJsonObject(trimmed);
  if (jsonHit) return jsonHit;
  const stMatch = trimmed.match(/__Secure-next-auth\.session-token=[A-Za-z0-9._-]+/);
  if (stMatch) return stMatch[0];
  const jweMatch = trimmed.match(/eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){4}/);
  if (jweMatch) return jweMatch[0];
  const jwtMatch = trimmed.match(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/);
  if (jwtMatch) return jwtMatch[0];
  return trimmed;
}

export async function parseSessionFile(file: File): Promise<string> {
  if (file.size > MAX_BYTES) {
    throw new Error("文件超过 5 MB 限制，请选择较小的文件。");
  }
  const lower = file.name.toLowerCase();
  if (lower.endsWith(".docx")) {
    const buf = await file.arrayBuffer();
    const xmlText = await extractDocxText(buf);
    if (xmlText) {
      return extractSessionFromRawText(xmlText);
    }
  }
  const text = await file.text();
  if (lower.endsWith(".rtf") || text.startsWith("{\\rtf")) {
    return extractSessionFromRawText(cleanRtf(text));
  }
  return extractSessionFromRawText(text);
}
