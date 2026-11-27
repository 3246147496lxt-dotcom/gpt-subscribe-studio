import React, { useCallback, useEffect, useState } from "react";

import type { AccountSubscription } from "../api/account";
import {
  cancelAutoRenewStandalone,
  fetchPublicBillingHistory,
  fetchPublicStripePortal,
  fetchPublicSubscriptionDetail,
  fetchPublicTasks,
  loginPortalUser,
  registerPortalUser,
  type PortalAuthState,
  type PortalSiteConfig,
  type PublicTask,
  type PublicTasksPage,
} from "./api";
import { PORTAL_TEXT, type PortalLang } from "./i18n";
import { CalendarIcon, ClockIcon, ExternalArrowIcon, SparkleAvatarIcon, StateMark } from "./icons";

function statusMeta(status: PublicTask["status"], lang: PortalLang): { label: string; cls: string } {
  const map: Record<PublicTask["status"], { zh: string; en: string; cls: string }> = {
    queued: { zh: "排队中", en: "Queued", cls: "running" },
    running: { zh: "处理中", en: "Processing", cls: "running" },
    succeeded: { zh: "已开通", en: "Activated", cls: "success" },
    failed: { zh: "未完成", en: "Failed", cls: "failed" },
    unknown: { zh: "核验中", en: "Verifying", cls: "warn" },
    paid_pending_delivery: { zh: "待到账", en: "Pending", cls: "warn" },
    requires_action: { zh: "需验证", en: "Action Needed", cls: "warn" },
  };
  const item = map[status] ?? { zh: status, en: status, cls: "" };
  return { label: lang === "en" ? item.en : item.zh, cls: item.cls };
}

function formatPlanName(plan?: string | null): string {
  const m: Record<string, string> = {
    go: "ChatGPT Go",
    plus: "ChatGPT Plus",
    pro_lite: "ChatGPT Pro100",
    pro: "ChatGPT Pro200",
    pro_max: "ChatGPT Pro500",
  };
  return (plan && m[plan]) || plan || "ChatGPT";
}

function planThemeKey(planName: string): string {
  const low = (planName || "").toLowerCase();
  if (low.includes("max") || low.includes("500")) return "promax";
  if (low.includes("pro lite") || low.includes("100")) return "pro5";
  if (low.includes("pro")) return "pro20";
  if (low.includes("go")) return "go";
  if (low.includes("plus")) return "plus";
  return "free";
}

function invoiceStatusClass(status: string): string {
  if (status === "已支付" || status.toLowerCase() === "paid") return "is-paid";
  if (status === "待支付" || status.toLowerCase() === "open") return "is-open";
  if (status === "支付失败" || status === "已作废" || status === "已退款") return "is-void";
  return "";
}

function formatTime(iso?: string | null): string {
  if (!iso) return "";
  try {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return iso;
    return dt.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return iso;
  }
}

export function MyTasksModal({
  open,
  lang,
  auth,
  onClose,
  onSelectTask,
}: {
  open: boolean;
  lang: PortalLang;
  auth: PortalAuthState;
  onClose: () => void;
  onSelectTask: (jobId: string) => void;
}) {
  const t = PORTAL_TEXT[lang];
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<PublicTasksPage | null>(null);

  const load = useCallback(async (q: string, p: number) => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchPublicTasks({ q, page: p, pageSize: 8 });
      setData(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载任务失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => void load(query, page), 160);
    return () => window.clearTimeout(timer);
  }, [open, query, page, load]);

  if (!open) return null;

  const items = data?.items ?? [];
  const totalPages = data?.total_pages ?? 1;

  return (
    <div className="portal-overlay" role="dialog" aria-modal="true" aria-label={t.tasksTitle} onClick={onClose}>
      <div className="portal-modal" onClick={(e) => e.stopPropagation()}>
        <div className="portal-modal-head">
          <h2 className="portal-modal-title">{t.tasksTitle}</h2>
          <button type="button" className="portal-modal-close" onClick={onClose} aria-label="Close">×</button>
        </div>

        <div className="portal-modal-body">
          <input
            type="text"
            className="portal-input"
            placeholder={t.tasksSearchPlaceholder}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setPage(1);
            }}
          />

          <p style={{ margin: "12px 0 16px", fontSize: 13, color: "#64748b", lineHeight: 1.6 }}>
            {auth.authenticated ? `${t.tasksUserHint}（${auth.username}）` : t.tasksGuestHint}
          </p>

          {error && <div className="portal-alert-error" style={{ marginBottom: 14 }}>{error}</div>}

          {loading && !data ? (
            <div style={{ padding: "26px 0", fontSize: 14, color: "#64748b" }}>{t.tasksLoading}</div>
          ) : items.length === 0 ? (
            <div style={{ padding: "28px 0", textAlign: "center", fontSize: 14, color: "#94a3b8" }}>
              {t.tasksEmpty}
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {items.map((item) => {
                const st = statusMeta(item.status, lang);
                return (
                  <div
                    key={item.job_id}
                    className="portal-task-item"
                    onClick={() => {
                      onSelectTask(item.job_id);
                      onClose();
                    }}
                  >
                    <div style={{ minWidth: 0 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                        <strong style={{ fontSize: 14.5, color: "#1e293b" }}>{formatPlanName(item.plan)}</strong>
                        {item.masked_email && (
                          <span style={{ fontSize: 13, color: "#475569" }}>{item.masked_email}</span>
                        )}
                        {item.exit_country && (
                          <span style={{ fontSize: 12, color: "#94a3b8" }}>{item.exit_country}</span>
                        )}
                      </div>
                      <div style={{ marginTop: 4, fontSize: 12, color: "#94a3b8" }}>
                        {formatTime(item.created_at)}
                        {item.amount ? ` · ${item.amount} ${item.currency || ""}` : ""}
                      </div>
                    </div>
                    <span className={`portal-status-pill ${st.cls}`}>{st.label}</span>
                  </div>
                );
              })}
            </div>
          )}

          <div className="portal-pagination">
            <button
              type="button"
              className="portal-outline-btn"
              disabled={page <= 1 || loading}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              {t.prevPage}
            </button>
            <span>
              {lang === "zh" ? `第 ${page} / ${totalPages} 页` : `Page ${page} / ${totalPages}`}
            </span>
            <button
              type="button"
              className="portal-outline-btn"
              disabled={page >= totalPages || loading}
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            >
              {t.nextPage}
            </button>
          </div>
        </div>

        <div className="portal-modal-footer">
          <button type="button" className="portal-outline-btn" onClick={onClose}>
            {t.backToRecharge}
          </button>
        </div>
      </div>
    </div>
  );
}

export function AuthModal({
  open,
  lang,
  siteConfig,
  onClose,
  onSuccess,
}: {
  open: boolean;
  lang: PortalLang;
  siteConfig: PortalSiteConfig;
  onClose: () => void;
  onSuccess: (next: PortalAuthState) => void;
}) {
  const t = PORTAL_TEXT[lang];
  const [showPasswordForm, setShowPasswordForm] = useState(false);
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tgHint, setTgHint] = useState(false);

  useEffect(() => {
    if (!open) {
      setError(null);
      setTgHint(false);
    }
  }, [open]);

  if (!open) return null;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      const next =
        mode === "register"
          ? await registerPortalUser(username.trim(), password)
          : await loginPortalUser(username.trim(), password);
      onSuccess(next);
      setPassword("");
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败，请重试。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="portal-overlay" role="dialog" aria-modal="true" aria-label={t.loginModalTitle} onClick={onClose}>
      <div className="portal-modal compact" onClick={(e) => e.stopPropagation()}>
        <div className="portal-modal-head">
          <h2 className="portal-modal-title">{t.loginModalTitle}</h2>
          <button type="button" className="portal-modal-close" onClick={onClose} aria-label="Close">×</button>
        </div>

        <div className="portal-modal-body">
          <p style={{ margin: "0 0 8px", fontSize: 14, color: "#475569", lineHeight: 1.65 }}>
            {t.loginModalLine1}
          </p>
          <p style={{ margin: "0 0 18px", fontSize: 13.5, color: "#64748b", lineHeight: 1.65 }}>
            {t.loginModalLine2}
          </p>

          {!showPasswordForm ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <button
                type="button"
                className="portal-tg-btn"
                onClick={() => {
                  if (!siteConfig.telegram_login_enabled) {
                    setTgHint(true);
                    setShowPasswordForm(true);
                  }
                }}
              >
                {t.telegramLoginBtn}
              </button>
              <div>
                <button
                  type="button"
                  className="portal-link-btn"
                  onClick={() => setShowPasswordForm(true)}
                >
                  {t.usePasswordLogin}
                </button>
              </div>
            </div>
          ) : (
            <form onSubmit={(e) => void handleSubmit(e)} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {tgHint && (
                <div className="portal-alert-info" style={{ marginTop: 0 }}>
                  {t.telegramNotConfigured}
                </div>
              )}
              {error && (
                <div className="portal-alert-error" style={{ marginTop: 0 }}>
                  {error}
                </div>
              )}
              <div>
                <label className="portal-field-label" htmlFor="portal-auth-user">{t.usernameLabel}</label>
                <input
                  id="portal-auth-user"
                  type="text"
                  autoComplete="username"
                  className="portal-input"
                  placeholder={t.usernamePlaceholder}
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required
                />
              </div>
              <div>
                <label className="portal-field-label" htmlFor="portal-auth-pass">{t.passwordLabel}</label>
                <input
                  id="portal-auth-pass"
                  type="password"
                  autoComplete={mode === "register" ? "new-password" : "current-password"}
                  className="portal-input"
                  placeholder={t.passwordPlaceholder}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
              </div>
              <button type="submit" className="portal-primary-btn" style={{ marginTop: 6 }} disabled={busy}>
                {busy ? "…" : mode === "register" ? t.registerSubmit : t.loginSubmit}
              </button>
              <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4 }}>
                <button
                  type="button"
                  className="portal-link-btn"
                  onClick={() => {
                    setMode((m) => (m === "login" ? "register" : "login"));
                    setError(null);
                  }}
                >
                  {mode === "login" ? t.switchToRegister : t.switchToLogin}
                </button>
              </div>
            </form>
          )}
        </div>

        <div className="portal-modal-footer">
          <button type="button" className="portal-outline-btn" onClick={onClose}>
            {t.backToRecharge}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 1:1 Account Subscription Dialog (`#aq-account-card`):
 * - 24px rounded elevated card + 42x42 sparkle avatar header
 * - Identity block + 订阅概况 with 5px colored dot state badge
 * - 1.15fr : 1fr asymmetric date + plan-tinted hero grid (30px remaining days)
 * - 1.1fr : 1.15fr : 0.7fr billing strip with inline cancel auto-renew action
 * - 4-column expandable history table with status pills & show-more toggle
 * - Footer: 最近查询 HH:mm 北京时间 + 重新查询 + 完成
 */
export function SubscriptionStatusModal({
  open,
  lang,
  session,
  onClose,
}: {
  open: boolean;
  lang: PortalLang;
  session: string;
  onClose: () => void;
}) {
  const [loading, setLoading] = useState(false);
  const [invoicesLoading, setInvoicesLoading] = useState(false);
  const [invoicesRetryable, setInvoicesRetryable] = useState(false);
  const [portalLoading, setPortalLoading] = useState(false);
  const [cancelingRenew, setCancelingRenew] = useState(false);
  const [detail, setDetail] = useState<AccountSubscription | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string; url?: string } | null>(null);
  const [expandedRow, setExpandedRow] = useState<number | null>(null);
  const [showAllInvoices, setShowAllInvoices] = useState(false);

  const loadBillingHistory = useCallback(
    async (base: AccountSubscription) => {
      if (!session.trim()) return;
      setInvoicesLoading(true);
      setInvoicesRetryable(false);
      try {
        const res = await fetchPublicBillingHistory({
          session: session.trim(),
          planName: base.planName,
          currency: base.currency === "-" ? "" : base.currency,
        });
        setInvoicesRetryable(Boolean(res.retryable));
        setDetail((prev) => {
          if (!prev) return prev;
          return {
            ...prev,
            invoices: res.invoices,
            billingSource: res.billingSource,
            billingNote: res.billingNote,
            currency: res.currency && res.currency !== "-" ? res.currency : prev.currency,
          };
        });
      } catch (err) {
        setInvoicesRetryable(true);
        setDetail((prev) =>
          prev
            ? {
                ...prev,
                billingSource: "stripe_portal_timeout",
                billingNote:
                  err instanceof Error
                    ? err.message
                    : "读取历史账单超时，请点击「重试读取账单」或右上角打开官方门户。",
              }
            : prev,
        );
      } finally {
        setInvoicesLoading(false);
      }
    },
    [session],
  );

  const run = useCallback(
    async (forceRefresh: boolean) => {
      if (!session.trim()) return;
      setLoading(true);
      setError(null);
      setNotice(null);
      setInvoicesRetryable(false);
      try {
        // Phase 1: fast basic subscription info (~1-2s) rendered immediately.
        const res = await fetchPublicSubscriptionDetail({
          session: session.trim(),
          forceRefresh,
          includeInvoices: false,
        });
        setDetail(res);
        setLoading(false);
        // Phase 2: only fall back to Stripe portal billing history when Phase 1 didn't already
        // return authoritative upstream transaction-history records.
        const needsHistory =
          res.billingSource !== "upstream_api"
          && (
            res.billingSource === "loading"
            || res.billingSource === "stripe_portal_timeout"
            || (forceRefresh && res.paymentChannel === "网页支付" && (res.invoices.length > 0 || res.currency !== "-" || res.status !== "未订阅"))
          );
        if (needsHistory) {
          void loadBillingHistory(res);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "查询订阅状态失败。");
        setLoading(false);
      }
    },
    [session, loadBillingHistory],
  );

  useEffect(() => {
    if (open && session.trim()) {
      setExpandedRow(null);
      setShowAllInvoices(false);
      void run(true);
    }
  }, [open, session, run]);

  if (!open) return null;

  async function openStripePortal() {
    if (portalLoading) return;
    setPortalLoading(true);
    setNotice(null);
    try {
      const portal = await fetchPublicStripePortal(session.trim());
      window.open(portal.url, "_blank", "noopener,noreferrer");
    } catch (err) {
      setNotice({ kind: "error", text: err instanceof Error ? err.message : "无法打开官方账单门户。" });
    } finally {
      setPortalLoading(false);
    }
  }

  async function handleInlineCancelRenew() {
    if (cancelingRenew || loading || invoicesLoading) return;
    setCancelingRenew(true);
    setNotice(null);
    try {
      const res = await cancelAutoRenewStandalone(session.trim(), detail?.planName || "");
      if (res.canceled) {
        const endText = res.end_time || detail?.endTime || "";
        const msg =
          res.message
          || (endText ? `自动续费已关闭，本期仍可使用至 ${endText}。` : "自动续费已关闭。");
        setDetail((prev) => {
          if (!prev) return prev;
          const now = new Date();
          const pad = (n: number) => String(n).padStart(2, "0");
          const bj = new Date(now.getTime() + (now.getTimezoneOffset() + 480) * 60_000);
          const refreshedAt = `${bj.getFullYear()}/${pad(bj.getMonth() + 1)}/${pad(bj.getDate())} ${pad(bj.getHours())}:${pad(bj.getMinutes())}`;
          return {
            ...prev,
            autoRenew: "已关闭",
            endTime: res.end_time || prev.endTime,
            queriedAt: refreshedAt,
          };
        });
        setNotice({ kind: "ok", text: msg });
      } else {
        setNotice({ kind: "error", text: res.message, url: res.portal_url || undefined });
      }
    } catch (err) {
      setNotice({ kind: "error", text: err instanceof Error ? err.message : "关闭自动续费失败。" });
    } finally {
      setCancelingRenew(false);
    }
  }

  const invoices = detail?.invoices ?? [];
  const visibleInvoices = showAllInvoices ? invoices : invoices.slice(0, 1);
  const isExpiringSoon = Boolean(
    detail && detail.status === "使用中" && detail.remainingDays != null && detail.remainingDays <= 3,
  );
  const stateLabel = !detail
    ? ""
    : isExpiringSoon
      ? (lang === "zh" ? "即将到期" : "Expiring Soon")
      : detail.status;
  const stateCls = !detail
    ? "is-idle"
    : isExpiringSoon || detail.status === "已过期"
      ? "is-warn"
      : detail.status === "使用中"
        ? ""
        : "is-idle";
  const planKey = detail && detail.status === "使用中" ? planThemeKey(detail.planName) : "free";
  const isRenewOn = detail?.autoRenew === "已开启";
  const renewChipCls =
    detail?.autoRenew === "已开启"
      ? ""
      : detail?.autoRenew === "已关闭"
        ? "is-off"
        : "is-na";
  const queriedHm = detail?.queriedAt ? detail.queriedAt.slice(11, 16) : "--:--";

  return (
    <div
      className="portal-overlay aq-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={lang === "zh" ? "ChatGPT 账号详情" : "ChatGPT Account Details"}
      onClick={onClose}
    >
      <div id="aq-account-card" className="aq-modal" onClick={(e) => e.stopPropagation()}>
        <div className="aq-card-header">
          <div className="aq-header-left">
            <span className="aq-avatar">
              <SparkleAvatarIcon size={22} />
            </span>
            <h2 className="aq-header-title">
              {lang === "zh" ? "ChatGPT 账号详情" : "ChatGPT Account Details"}
            </h2>
          </div>
          <button type="button" className="aq-close-btn" onClick={onClose} aria-label="Close">×</button>
        </div>

        <div className="aq-card-scroll">
          {loading && !detail && (
            <div style={{ padding: "14px 28px 22px", display: "flex", flexDirection: "column", gap: 14 }}>
              <div className="aq-skeleton" style={{ width: "52%", height: 26 }} />
              <div className="aq-date-grid">
                <div className="aq-skeleton" style={{ height: 132, borderRadius: 15 }} />
                <div className="aq-skeleton" style={{ height: 132, borderRadius: 15 }} />
              </div>
              <div className="aq-billing">
                <div className="aq-skeleton" style={{ height: 48, borderRadius: 10 }} />
                <div className="aq-skeleton" style={{ height: 48, borderRadius: 10 }} />
                <div className="aq-skeleton" style={{ height: 48, borderRadius: 10 }} />
              </div>
            </div>
          )}

          {error && (
            <div style={{ padding: "14px 28px 20px" }}>
              <div className="portal-alert-error" style={{ marginTop: 0 }}>{error}</div>
            </div>
          )}

          {detail && (
            <>
              {/* Account Email Identity + Divider */}
              <div className="aq-identity">
                <span className="aq-identity-label">{lang === "zh" ? "账户邮箱" : "Account Email"}</span>
                <span className="aq-email">{detail.email || (lang === "zh" ? "未知账号" : "Unknown Account")}</span>
              </div>

              <div className="aq-subscription-content">
                {detail.warning && <div className="aq-card-error" role="alert">{detail.warning}</div>}
                {notice && (
                  <div
                    className={notice.kind === "ok" ? "aq-card-notice" : "aq-card-error"}
                    role={notice.kind === "ok" ? "status" : "alert"}
                  >
                    <span>{notice.text}</span>
                    {notice.url && (
                      <a
                        href={notice.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="portal-link-btn"
                        style={{ marginLeft: 8, fontSize: 12 }}
                      >
                        {lang === "zh" ? "前往官方门户 ↗" : "Open Portal ↗"}
                      </a>
                    )}
                  </div>
                )}

                {/* Overview Heading + Status Dot Pill (Coral "即将到期" when <= 3 days) */}
                <div className="aq-section-heading">
                  <span className="aq-section-label">{lang === "zh" ? "订阅概况" : "Subscription Overview"}</span>
                  <span className={`aq-state ${stateCls}`}>{stateLabel}</span>
                </div>

                {/* 1.15fr : 1fr Date & Plan-Tinted Hero Grid */}
                <div className="aq-date-grid">
                  <div className={`aq-date-cell ${isExpiringSoon ? "is-warn" : ""}`}>
                    <div className="aq-card-kicker">
                      <CalendarIcon size={16} />
                      <span>{lang === "zh" ? "订阅时间" : "Subscription Period"}</span>
                    </div>
                    <div className="aq-period">
                      <div className="aq-date-pair">
                        <span className="aq-cell-label">{lang === "zh" ? "开始时间" : "Start Date"}</span>
                        <span className="aq-cell-time">
                          {detail.startTime || (lang === "zh" ? "暂未查到" : "Not found")}
                        </span>
                      </div>
                      <div className="aq-date-pair">
                        <span className="aq-cell-label">{lang === "zh" ? "到期时间" : "Expires At"}</span>
                        <span className="aq-cell-time">
                          {detail.endTime || (lang === "zh" ? "暂未查到" : "Not found")}
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="aq-date-cell aq-current-plan" data-plan={planKey}>
                    <div className="aq-card-kicker">
                      <ClockIcon size={16} />
                      <span>{lang === "zh" ? "当前套餐" : "Current Plan"}</span>
                    </div>
                    <div className="aq-package-content">
                      <div className="aq-plan-name">{detail.planName}</div>
                      <div className="aq-remaining-wrap">
                        {detail.status === "使用中" && detail.remainingDays != null ? (
                          <>
                            <strong className="aq-remaining">{detail.remainingDays}</strong>
                            <span className="aq-remaining-unit">
                              {lang === "zh" ? "天后到期" : "days left"}
                            </span>
                          </>
                        ) : (
                          <span className="aq-remaining-unit">
                            {detail.status === "未订阅"
                              ? (lang === "zh" ? "暂无付费订阅" : "No active subscription")
                              : (lang === "zh" ? "无到期时间" : "No expiry")}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                </div>

                {/* Flat 3-Column Billing Strip (1.1fr : 1.15fr : 0.7fr) */}
                <div className="aq-billing">
                  <div className="aq-billing-cell">
                    <span className="aq-billing-label">{lang === "zh" ? "自动续费" : "Auto-Renew"}</span>
                    <div className="aq-billing-val-row">
                      <span className={`aq-renew-chip ${renewChipCls}`}>
                        {detail.autoRenew}
                      </span>
                      {isRenewOn && detail.status === "使用中" && (
                        <button
                          type="button"
                          className="aq-inline-cancel-btn"
                          disabled={cancelingRenew || loading || invoicesLoading}
                          onClick={() => void handleInlineCancelRenew()}
                        >
                          {cancelingRenew && <StateMark state="running" size={13} />}
                          <span>{lang === "zh" ? "关闭" : "Cancel"}</span>
                        </button>
                      )}
                    </div>
                  </div>

                  <div className="aq-billing-cell">
                    <span className="aq-billing-label">{lang === "zh" ? "支付渠道" : "Payment Channel"}</span>
                    <span className="aq-billing-val">{detail.paymentChannel || "-"}</span>
                  </div>

                  <div className="aq-billing-cell">
                    <span className="aq-billing-label">{lang === "zh" ? "订阅币种" : "Currency"}</span>
                    <span className="aq-currency-pill">{detail.currency || "-"}</span>
                  </div>
                </div>

                {/* 4-Column Fixed-Layout History Table (Phase 2 Async Loaded) */}
                <div className="aq-history">
                  <div className="aq-section-heading">
                    <span className="aq-section-label" style={{ display: "inline-flex", alignItems: "center" }}>
                      <span>{lang === "zh" ? "历史账单" : "Billing History"}</span>
                      {invoicesLoading ? (
                        <span style={{ marginLeft: 8, display: "inline-flex", alignItems: "center" }}>
                          <StateMark state="running" size={14} />
                        </span>
                      ) : invoices.length > 0 ? (
                        <span className="aq-history-count">
                          {lang === "zh" ? `${invoices.length} 条` : `${invoices.length}`}
                        </span>
                      ) : null}
                    </span>
                    <div style={{ display: "inline-flex", alignItems: "center", gap: 12 }}>
                      {!invoicesLoading && (invoicesRetryable || detail.billingSource === "stripe_portal_timeout") && (
                        <button
                          type="button"
                          className="portal-link-btn"
                          style={{ fontSize: 12 }}
                          onClick={() => void loadBillingHistory(detail)}
                        >
                          {lang === "zh" ? "重试读取账单" : "Retry Invoices"}
                        </button>
                      )}
                      <span className="aq-history-tz">{lang === "zh" ? "北京时间" : "UTC+8"}</span>
                    </div>
                  </div>

                  {invoicesLoading && invoices.length === 0 ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "6px 0" }}>
                      <div style={{ fontSize: 12.5, color: "#647687" }}>
                        {lang === "zh" ? "正在读取历史账单…" : "Loading billing history…"}
                      </div>
                      <div className="aq-skeleton" style={{ height: 36, borderRadius: 10 }} />
                    </div>
                  ) : invoices.length === 0 ? (
                    <div style={{ fontSize: 13, color: "#647687", padding: "6px 0", lineHeight: 1.65 }}>
                      <div>{detail.billingNote || (lang === "zh" ? "暂无历史账单记录。" : "No historical invoices found.")}</div>
                      {detail.paymentChannel === "网页支付" && (
                        <button
                          type="button"
                          className="aq-history-link"
                          onClick={() => void openStripePortal()}
                          disabled={portalLoading}
                        >
                          {portalLoading ? "打开中…" : "打开 Stripe 官方账单明细"} <ExternalArrowIcon />
                        </button>
                      )}
                    </div>
                  ) : (
                    <>
                      <table className="aq-history-table">
                        <colgroup>
                          <col />
                          <col />
                          <col />
                          <col />
                        </colgroup>
                        <thead>
                          <tr>
                            <th>{lang === "zh" ? "套餐" : "Plan"}</th>
                            <th>{lang === "zh" ? "时间" : "Time"}</th>
                            <th>{lang === "zh" ? "支付状态" : "Status"}</th>
                            <th>{lang === "zh" ? "金额" : "Amount"}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {visibleInvoices.map((inv, idx) => {
                            const isExpanded = expandedRow === idx;
                            return (
                              <React.Fragment key={`${inv.invoiceId || inv.paidAt}-${inv.amount}-${idx}`}>
                                <tr className="aq-history-row">
                                  <td>
                                    <button
                                      type="button"
                                      className="aq-history-product"
                                      aria-expanded={isExpanded}
                                      onClick={() => setExpandedRow(isExpanded ? null : idx)}
                                    >
                                      <span className="aq-history-product-label">{inv.plan}</span>
                                      <span className={`aq-plan-chevron ${isExpanded ? "is-open" : ""}`}>⌄</span>
                                    </button>
                                  </td>
                                  <td className="aq-history-date">{inv.paidAt}</td>
                                  <td>
                                    <span className={`aq-history-status ${invoiceStatusClass(inv.status)}`}>
                                      {inv.status}
                                    </span>
                                  </td>
                                  <td className="aq-history-amount">{inv.amount}</td>
                                </tr>
                                {isExpanded && (
                                  <tr className="aq-history-detail">
                                    <td colSpan={4}>
                                      <p>{inv.plan}</p>
                                      <p>{lang === "zh" ? `时间：${inv.paidAt}` : `Time: ${inv.paidAt}`}</p>
                                      {inv.invoiceId && (
                                        <p>{lang === "zh" ? `交易编号：${inv.invoiceId}` : `Invoice ID: ${inv.invoiceId}`}</p>
                                      )}
                                      <p>{lang === "zh" ? `金额：${inv.amount}` : `Amount: ${inv.amount}`}</p>
                                      {inv.invoiceUrl ? (
                                        <a
                                          href={inv.invoiceUrl}
                                          target="_blank"
                                          rel="noopener noreferrer"
                                          className="aq-history-link"
                                        >
                                          {lang === "zh" ? "查看账单 ↗" : "View Invoice ↗"}
                                        </a>
                                      ) : (
                                        <button
                                          type="button"
                                          className="aq-history-link"
                                          onClick={() => void openStripePortal()}
                                          disabled={portalLoading}
                                        >
                                          {lang === "zh" ? "查看账单 ↗" : "View Invoice ↗"}
                                        </button>
                                      )}
                                    </td>
                                  </tr>
                                )}
                              </React.Fragment>
                            );
                          })}
                        </tbody>
                      </table>

                      {invoices.length > 1 && (
                        <button
                          type="button"
                          className="aq-expand-more-btn"
                          onClick={() => setShowAllInvoices((v) => !v)}
                        >
                          {showAllInvoices
                            ? (lang === "zh" ? "收起历史账单" : "Hide Billing History")
                            : (lang === "zh" ? `展开其余 ${invoices.length - 1} 条` : `Show ${invoices.length - 1} more`)}
                        </button>
                      )}

                      {showAllInvoices && (
                        <p className="aq-history-limit">
                          {lang === "zh" ? "最多显示最近 20 条账单。" : "Showing up to the 20 most recent invoices."}
                        </p>
                      )}
                    </>
                  )}
                </div>
              </div>
            </>
          )}
        </div>

        <div className="aq-card-footer">
          <span className="aq-queried-time">
            {lang === "zh" ? (
              <>
                最近查询 {queriedHm}
                <br />
                北京时间
              </>
            ) : (
              <>
                Updated {queriedHm}
                <br />
                Beijing Time
              </>
            )}
          </span>
          <div className="aq-footer-actions">
            <button
              id="aq-refresh"
              type="button"
              className="aq-refresh-link"
              onClick={() => void run(true)}
              disabled={loading || invoicesLoading || cancelingRenew}
            >
              {loading ? (lang === "zh" ? "查询中…" : "Refreshing…") : (lang === "zh" ? "重新查询" : "Refresh")}
            </button>
            <button id="aq-done" type="button" className="aq-done-btn" onClick={onClose}>
              {lang === "zh" ? "完成" : "Done"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function SupportModal({
  open,
  lang,
  siteConfig,
  onClose,
}: {
  open: boolean;
  lang: PortalLang;
  siteConfig: PortalSiteConfig;
  onClose: () => void;
}) {
  if (!open) return null;
  return (
    <div className="portal-overlay" role="dialog" aria-modal="true" aria-label={siteConfig.support_label} onClick={onClose}>
      <div className="portal-modal compact" onClick={(e) => e.stopPropagation()}>
        <div className="portal-modal-head">
          <h2 className="portal-modal-title">{lang === "en" ? siteConfig.support_label_en : siteConfig.support_label}</h2>
          <button type="button" className="portal-modal-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="portal-modal-body">
          <p style={{ margin: "4px 0 12px", fontSize: 14, color: "#475569", lineHeight: 1.7 }}>
            {lang === "en" ? siteConfig.support_note_en : siteConfig.support_note}
          </p>
          <p style={{ margin: 0, fontSize: 12.5, color: "#94a3b8" }}>
            {lang === "zh"
              ? "提示：管理员可在服务端配置文件 portal-site.json 或 /api/settings/portal-site 中设置 support_url 跳转链接。"
              : "Tip: Operators can configure support_url via portal-site.json or /api/settings/portal-site."}
          </p>
        </div>
        <div className="portal-modal-footer">
          <button type="button" className="portal-outline-btn" onClick={onClose}>
            {lang === "zh" ? "知道了" : "Close"}
          </button>
        </div>
      </div>
    </div>
  );
}
