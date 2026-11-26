export type HttpRequestOptions = {
  method?: string;
  headers?: Record<string, string>;
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
  cache?: RequestCache;
  includeErrorCode?: boolean;
  fallbackError?: string;
};

export function isAbortError(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  const name = (err as { name?: string }).name;
  return name === "AbortError";
}

let redirectingAdminLogin = false;

export function handleAdminAuthRedirect(url: string, status: number, rawMessage?: string): boolean {
  if (status !== 401 || typeof window === "undefined") return false;
  const currentPath = window.location.pathname || "";
  if (!currentPath.startsWith("/admin")) return false;
  let apiPath = url;
  try {
    apiPath = new URL(url, window.location.origin).pathname;
  } catch {
    // keep raw url fallback
  }
  if (apiPath.startsWith("/api/public/")) return false;
  // Gateway TestAccessMiddleware emits 401 {"detail": "请先输入访问密码"}.
  // Upstream ChatGPT session 401s on /api/account/* or /api/promo/* must not log out the admin.
  const isUpstreamSessionEndpoint =
    apiPath.startsWith("/api/account/") || apiPath.startsWith("/api/promo/");
  const isGatewayAuthError = rawMessage === "请先输入访问密码";
  if (isUpstreamSessionEndpoint && !isGatewayAuthError) return false;
  if (!redirectingAdminLogin) {
    redirectingAdminLogin = true;
    window.location.assign("/login");
  }
  return true;
}

export async function requestJson<T>(url: string, options: HttpRequestOptions = {}): Promise<T> {
  const {
    method = "GET",
    headers = {},
    body,
    signal,
    timeoutMs = 30_000,
    cache,
    includeErrorCode = false,
    fallbackError,
  } = options;

  const controller = new AbortController();
  let timedOut = false;

  const onExternalAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) {
      controller.abort();
    } else {
      signal.addEventListener("abort", onExternalAbort, { once: true });
    }
  }

  const timer = timeoutMs > 0
    ? setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeoutMs)
    : null;

  const finalHeaders: Record<string, string> = { ...headers };
  let encodedBody: BodyInit | undefined;
  if (body !== undefined) {
    if (typeof body === "string") {
      encodedBody = body;
    } else {
      if (!finalHeaders["Content-Type"]) {
        finalHeaders["Content-Type"] = "application/json";
      }
      encodedBody = JSON.stringify(body);
    }
  }

  try {
    const response = await fetch(url, {
      method,
      headers: finalHeaders,
      body: encodedBody,
      cache,
      signal: controller.signal,
    });
    const parsed = await response.json().catch(() => null) as
      | { detail?: string | { code?: string; message?: string } }
      | T
      | null;

    if (!response.ok) {
      const detail = (parsed as { detail?: string | { code?: string; message?: string } } | null)?.detail;
      const rawMessage = typeof detail === "string" ? detail : detail?.message;
      handleAdminAuthRedirect(response.url || url, response.status, rawMessage);
      const code = typeof detail === "object" && detail ? detail.code : undefined;
      const base = rawMessage || (fallbackError ? `${fallbackError}（HTTP ${response.status}）` : `请求失败 HTTP ${response.status}`);
      throw new Error(includeErrorCode && code ? `${base}（${code}）` : base);
    }
    return parsed as T;
  } catch (err) {
    if (timedOut) {
      throw new Error("请求超时，请检查本地服务或上游网络后重试");
    }
    if (isAbortError(err)) {
      throw err;
    }
    if (err instanceof TypeError) {
      throw new Error("无法连接到本地服务，请确认后端服务运行正常");
    }
    throw err;
  } finally {
    if (timer !== null) clearTimeout(timer);
    if (signal) signal.removeEventListener("abort", onExternalAbort);
  }
}
