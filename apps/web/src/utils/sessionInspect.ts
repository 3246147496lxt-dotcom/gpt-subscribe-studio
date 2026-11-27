export type SessionInspection = {
  state: "empty" | "valid" | "expiring" | "expired" | "refresh_only" | "opaque" | "invalid";
  summary: string;
  maskedEmail?: string;
  accountPrefix?: string;
  detectedOs?: "macOS" | "Windows" | "Linux";
  deviceIdPrefix?: string;
  deviceFingerprint?: string;
  hasRefreshCredential: boolean;
  hasCookies: boolean;
  expiresInSeconds?: number;
};

function fnv1aHex(input: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < input.length; i += 1) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, "0");
}

function parseStatsigObj(raw: unknown): Record<string, unknown> | null {
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  if (typeof raw === "string" && raw.trim().startsWith("{")) {
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      return null;
    }
  }
  return null;
}

function extractDeviceIdFromJson(obj: Record<string, unknown>): {
  deviceIdPrefix?: string;
  deviceFingerprint?: string;
} {
  const statsig = parseStatsigObj(obj.statsigContext ?? obj.statsig_context ?? obj.statsig);
  const directCandidates: unknown[] = [
    obj.deviceId,
    obj.device_id,
    obj["oai-did"],
    obj.oai_did,
    statsig?.deviceId,
    statsig?.device_id,
    statsig?.["oai-did"],
  ];

  let found = "";
  for (const item of directCandidates) {
    if (typeof item === "string" && item.trim().length >= 8) {
      found = item.trim();
      break;
    }
  }

  if (!found && obj.cookies) {
    if (typeof obj.cookies === "object" && !Array.isArray(obj.cookies)) {
      const val = (obj.cookies as Record<string, unknown>)["oai-did"];
      if (typeof val === "string" && val.trim().length >= 8) {
        found = val.trim();
      }
    } else if (Array.isArray(obj.cookies)) {
      for (const c of obj.cookies) {
        if (
          c && typeof c === "object"
          && (c as Record<string, unknown>).name === "oai-did"
          && typeof (c as Record<string, unknown>).value === "string"
        ) {
          const val = String((c as Record<string, unknown>).value).trim();
          if (val.length >= 8) {
            found = val;
            break;
          }
        }
      }
    } else if (typeof obj.cookies === "string") {
      const m = obj.cookies.match(/(?:^|;\s*)oai-did=([^;\s]+)/i);
      if (m && m[1] && m[1].length >= 8) found = m[1].trim();
    }
  }

  if (!found && typeof obj.cookie === "string") {
    const m = obj.cookie.match(/(?:^|;\s*)oai-did=([^;\s]+)/i);
    if (m && m[1] && m[1].length >= 8) found = m[1].trim();
  }

  if (!found) return {};
  const normalized = found.toLowerCase();
  return {
    deviceIdPrefix: `${found.slice(0, 8)}…`,
    deviceFingerprint: `${fnv1aHex(normalized)}-${fnv1aHex(`${normalized}:${normalized.length}`)}`,
  };
}

function detectOsFromJson(obj: Record<string, unknown>): "macOS" | "Windows" | "Linux" | undefined {
  const statsig = parseStatsigObj(obj.statsigContext ?? obj.statsig_context ?? obj.statsig);
  const candidates = [
    statsig?.userAgent, statsig?.user_agent, statsig?.ua,
    obj.userAgent, obj.user_agent, obj.ua,
    statsig?.["sec-ch-ua-platform"], statsig?.sec_ch_ua_platform, statsig?.platform, statsig?.os,
    obj["sec-ch-ua-platform"], obj.sec_ch_ua_platform, obj.platform, obj.os,
  ];
  for (const item of candidates) {
    if (typeof item !== "string" || !item.trim()) continue;
    const low = item.replace(/^"+|"+$/g, "").trim().toLowerCase();
    if (low.includes("macintosh") || low.includes("mac os") || low.includes("macos") || low.includes("darwin") || low === "mac") {
      return "macOS";
    }
    if (low.includes("windows") || low.includes("win32") || low === "win") {
      return "Windows";
    }
    if (low.includes("linux") || low.includes("x11")) {
      return "Linux";
    }
  }
  return undefined;
}

function decodeBase64Url(segment: string): string | null {
  try {
    const normalized = segment.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
    if (typeof globalThis.atob !== "function") return null;
    const binary = globalThis.atob(padded);
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    return new TextDecoder().decode(bytes);
  } catch {
    return null;
  }
}

function decodeJwtClaims(token: string): Record<string, unknown> | null {
  const parts = token.trim().split(".");
  if (parts.length < 2) return null;
  const decoded = decodeBase64Url(parts[1]);
  if (!decoded) return null;
  try {
    const parsed = JSON.parse(decoded);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

export function maskEmail(email: string): string {
  const trimmed = email.trim();
  const at = trimmed.indexOf("@");
  if (at <= 0) return trimmed;
  const local = trimmed.slice(0, at);
  const domain = trimmed.slice(at + 1);
  if (local.length <= 2) return `${local[0]}***@${domain}`;
  return `${local.slice(0, 2)}***@${domain}`;
}

export function formatRemainingDuration(seconds: number): string {
  const abs = Math.abs(Math.round(seconds));
  if (abs < 60) return `${abs} 秒`;
  if (abs < 3600) return `${Math.floor(abs / 60)} 分钟`;
  if (abs < 86400) return `${Math.floor(abs / 3600)} 小时`;
  return `${Math.floor(abs / 86400)} 天`;
}

/**
 * Pure in-memory transient session inspection for instant UI feedback.
 * Never persists, logs, or echoes raw tokens/cookies.
 */
export function inspectSession(raw: string, nowMs: number = Date.now()): SessionInspection {
  const text = raw.trim();
  if (!text) {
    return {
      state: "empty",
      summary: "",
      hasRefreshCredential: false,
      hasCookies: false,
    };
  }

  let accessToken = "";
  let email = "";
  let detectedOs: "macOS" | "Windows" | "Linux" | undefined;
  let deviceIdPrefix: string | undefined;
  let deviceFingerprint: string | undefined;
  let hasRefreshCredential = false;
  let hasCookies = false;

  if (text.startsWith("{")) {
    let obj: Record<string, unknown>;
    try {
      const parsed = JSON.parse(text);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return {
          state: "invalid",
          summary: "Session JSON 顶层必须是对象（{...}）",
          hasRefreshCredential: false,
          hasCookies: false,
        };
      }
      obj = parsed as Record<string, unknown>;
    } catch {
      return {
        state: "invalid",
        summary: "JSON 格式不完整或语法有误，请检查是否粘贴完整",
        hasRefreshCredential: false,
        hasCookies: false,
      };
    }

    const tokenCandidate = obj.accessToken ?? obj.access_token ?? obj.token;
    if (typeof tokenCandidate === "string") {
      accessToken = tokenCandidate.trim();
    }
    detectedOs = detectOsFromJson(obj);
    const dev = extractDeviceIdFromJson(obj);
    deviceIdPrefix = dev.deviceIdPrefix;
    deviceFingerprint = dev.deviceFingerprint;
    const userObj = obj.user && typeof obj.user === "object" ? (obj.user as Record<string, unknown>) : null;
    if (userObj && typeof userObj.email === "string") {
      email = userObj.email.trim();
    } else if (typeof obj.email === "string") {
      email = obj.email.trim();
    }

    const cookieSessionToken = Boolean(
      (obj.cookies && typeof obj.cookies === "object" && !Array.isArray(obj.cookies)
        && typeof (obj.cookies as Record<string, unknown>)["__Secure-next-auth.session-token"] === "string"
        && String((obj.cookies as Record<string, unknown>)["__Secure-next-auth.session-token"]).trim())
      || (Array.isArray(obj.cookies) && obj.cookies.some(
        (item) => Boolean(
          item && typeof item === "object"
          && (item as Record<string, unknown>).name === "__Secure-next-auth.session-token"
          && typeof (item as Record<string, unknown>).value === "string"
          && String((item as Record<string, unknown>).value).trim(),
        ),
      ))
      || (typeof obj.cookies === "string" && obj.cookies.includes("__Secure-next-auth.session-token="))
      || (typeof obj.cookie === "string" && obj.cookie.includes("__Secure-next-auth.session-token=")),
    );
    hasRefreshCredential = Boolean(
      (typeof obj.refreshToken === "string" && obj.refreshToken.trim())
      || (typeof obj.refresh_token === "string" && obj.refresh_token.trim())
      || (typeof obj.sessionToken === "string" && obj.sessionToken.trim())
      || (typeof obj.session_token === "string" && obj.session_token.trim())
      || (typeof obj["__Secure-next-auth.session-token"] === "string"
        && String(obj["__Secure-next-auth.session-token"]).trim())
      || cookieSessionToken,
    );
    hasCookies = Boolean(
      (typeof obj.cookies === "string" && obj.cookies.trim())
      || (Array.isArray(obj.cookies) && obj.cookies.length > 0)
      || (obj.cookies && typeof obj.cookies === "object" && Object.keys(obj.cookies).length > 0)
      || (typeof obj.cookie === "string" && obj.cookie.trim()),
    );

    if (!accessToken) {
      if (hasRefreshCredential) {
        return {
          state: "refresh_only",
          summary: "未包含 accessToken，但检测到刷新凭证，建单时将尝试自动刷新",
          maskedEmail: email ? maskEmail(email) : undefined,
          detectedOs,
          deviceIdPrefix,
          deviceFingerprint,
          hasRefreshCredential,
          hasCookies,
        };
      }
      return {
        state: "invalid",
        summary: "JSON 中缺少 accessToken（也未找到刷新凭证），请重新导出完整 Session",
        maskedEmail: email ? maskEmail(email) : undefined,
        detectedOs,
        deviceIdPrefix,
        deviceFingerprint,
        hasRefreshCredential: false,
        hasCookies,
      };
    }
  } else if (text.startsWith("__Secure-next-auth.session-token=") && text.length > 36 && !/\s/.test(text)) {
    return {
      state: "refresh_only",
      summary: "已识别 sessionToken（ST），建单时将在目标出口自动签发新 accessToken",
      hasRefreshCredential: true,
      hasCookies: false,
    };
  } else if (text.startsWith("eyJ") && text.split(".").length === 5 && !/\s/.test(text)) {
    return {
      state: "refresh_only",
      summary: "已识别 5 段 JWE sessionToken（ST），建单时将在目标出口自动签发新 accessToken",
      hasRefreshCredential: true,
      hasCookies: false,
    };
  } else if (text.split(".").length === 3 && !/\s/.test(text)) {
    accessToken = text;
  } else {
    return {
      state: "invalid",
      summary: "无法识别为有效的 Session JSON 或 JWT Token",
      hasRefreshCredential: false,
      hasCookies: false,
    };
  }

  const claims = decodeJwtClaims(accessToken);
  if (!claims) {
    return {
      state: "opaque",
      summary: "已识别 accessToken（非标准 JWT 结构，将在提交时由服务端核验）",
      maskedEmail: email ? maskEmail(email) : undefined,
      detectedOs,
      deviceIdPrefix,
      deviceFingerprint,
      hasRefreshCredential,
      hasCookies,
    };
  }

  const profile = claims["https://api.openai.com/profile"] as Record<string, unknown> | undefined;
  const auth = claims["https://api.openai.com/auth"] as Record<string, unknown> | undefined;
  if (!email && profile && typeof profile.email === "string") {
    email = profile.email.trim();
  }
  const accountId = (auth && typeof auth.chatgpt_account_id === "string" && auth.chatgpt_account_id.trim())
    || (typeof claims.sub === "string" ? claims.sub.trim() : "");
  const accountPrefix = accountId ? `${accountId.slice(0, 10)}…` : undefined;
  const maskedEmail = email ? maskEmail(email) : undefined;

  const exp = typeof claims.exp === "number" ? claims.exp : undefined;
  if (exp !== undefined) {
    const remaining = exp - nowMs / 1000;
    if (remaining <= 0) {
      return {
        state: "expired",
        summary: hasRefreshCredential
          ? `accessToken 已过期 ${formatRemainingDuration(remaining)}（含刷新凭证，建单时将尝试刷新）`
          : `accessToken 已过期 ${formatRemainingDuration(remaining)} 且无刷新凭证，请重新导出`,
        maskedEmail,
        accountPrefix,
        detectedOs,
        deviceIdPrefix,
        deviceFingerprint,
        hasRefreshCredential,
        hasCookies,
        expiresInSeconds: Math.round(remaining),
      };
    }
    if (remaining < 300) {
      return {
        state: "expiring",
        summary: `accessToken 即将过期（剩余 ${formatRemainingDuration(remaining)}）`,
        maskedEmail,
        accountPrefix,
        detectedOs,
        deviceIdPrefix,
        deviceFingerprint,
        hasRefreshCredential,
        hasCookies,
        expiresInSeconds: Math.round(remaining),
      };
    }
    return {
      state: "valid",
      summary: `凭证有效，剩余约 ${formatRemainingDuration(remaining)}`,
      maskedEmail,
      accountPrefix,
      detectedOs,
      deviceIdPrefix,
      deviceFingerprint,
      hasRefreshCredential,
      hasCookies,
      expiresInSeconds: Math.round(remaining),
    };
  }

  return {
    state: "valid",
    summary: "已解析 Session JWT 结构",
    maskedEmail,
    accountPrefix,
    detectedOs,
    deviceIdPrefix,
    deviceFingerprint,
    hasRefreshCredential,
    hasCookies,
  };
}
