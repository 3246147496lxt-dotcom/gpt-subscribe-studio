import "./portal.css";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { detectCardBrand, formatCardNumber, stripCardDigits } from "../utils/cardFormat";
import { inspectSession } from "../utils/sessionInspect";
import {
  cancelAutoRenewStandalone,
  fetchAuthMe,
  fetchPublicStripePortal,
  fetchPublicTask,
  fetchSiteConfig,
  logoutPortalUser,
  readLastTaskId,
  retryTaskCancelAutoRenew,
  saveLastTaskId,
  submitPublicRecharge,
  subscribePublicTaskStream,
  type PortalAuthState,
  type PortalPlanConfig,
  type PortalRegionConfig,
  type PortalSiteConfig,
  type PublicTask,
} from "./api";
import { formatExpiryInput, parseCardPaste, splitExpiry } from "./cardPasteParser";
import { PORTAL_TEXT, type PortalLang } from "./i18n";
import {
  BrandBoltIcon,
  ChatGptIcon,
  CheckCircleIcon,
  ClaudeIcon,
  ExternalArrowIcon,
  GrokIcon,
  HeadsetIcon,
  MegaphoneIcon,
  UserIcon,
} from "./icons";
import {
  AuthModal,
  MyTasksModal,
  SubscriptionStatusModal,
  SupportModal,
} from "./PortalModals";
import { ReceiptCard } from "./ReceiptCard";
import { parseSessionFile } from "./sessionFileParser";

type ProductTab = "chatgpt" | "claude" | "grok";
type DisplayCurrency = "USD" | "RMB";

const FALLBACK_SITE_CONFIG: PortalSiteConfig = {
  brand_name: "GPT 协议充值",
  brand_name_en: "GPT Protocol Recharge",
  footer_slogan: "给学习和灵感续航",
  footer_slogan_en: "Powering learning and inspiration",
  hero_title: "公益协议，安全快速",
  hero_title_en: "Fast, Safe Protocol Recharge",
  hero_subtitle_1: "优选全球优惠地区，开通自己的 ChatGPT。",
  hero_subtitle_1_en: "Pick discounted regions and activate your own ChatGPT.",
  hero_subtitle_2: "享受官方套餐权益，不为同样的订阅多花钱。",
  hero_subtitle_2_en: "Official subscription benefits without overpaying.",
  card_shop_url: "",
  card_shop_label: "去卡网购买",
  card_shop_label_en: "Buy a card",
  support_url: "",
  support_label: "联系客服",
  support_label_en: "Support",
  support_note: "如需排查失败订单或获取帮助，请联系站点管理员。",
  support_note_en: "Need help troubleshooting a failed order? Contact the site administrator.",
  telegram_login_enabled: false,
  usd_cny_rate: 6.7,
  price_updated_at: "11/25 09:00",
  price_note: "默认匹配当前最低价地区，实际扣款金额以提交后结账回执为准。",
  price_note_en: "Default matches the lowest reference region; final charge follows checkout confirmation.",
  announcements: [
    {
      id: "1",
      text: "支持粘贴 Session 或拖入文件本地解析，充值完成后自动尝试关闭续费。",
      text_en: "Paste or drop a Session file locally; auto-renewal cancellation runs right after activation.",
    },
    {
      id: "2",
      text: "充值失败或遇到扣款状态待确认时请勿重复下单，可在「我的任务」随时查看进度。",
      text_en: "Do not resubmit while payment status is pending; track live progress anytime in My Tasks.",
    },
  ],
  plans: [
    { id: "go", title: "Go", title_en: "Go", full_name: "ChatGPT Go", default_region: "EG", usd_price: 3.59, cny_price: 24.0, discount: "4.5折", discount_en: "55% OFF", available: true },
    { id: "plus", title: "Plus", title_en: "Plus", full_name: "ChatGPT Plus", default_region: "PH", usd_price: 15.35, cny_price: 102.74, discount: "7.7折", discount_en: "23% OFF", available: true },
    { id: "pro_lite", title: "Pro 100", title_en: "Pro 100", full_name: "ChatGPT Pro 100", default_region: "CL", usd_price: 85.17, cny_price: 570.04, discount: "8.5折", discount_en: "15% OFF", available: true },
    { id: "pro", title: "Pro 200", title_en: "Pro 200", full_name: "ChatGPT Pro 200", default_region: "PH", usd_price: 139.4, cny_price: 933.07, discount: "7折", discount_en: "30% OFF", available: true },
    { id: "pro_max", title: "Pro 500", title_en: "Pro 500", full_name: "ChatGPT Pro 500", default_region: "CL", usd_price: 422.61, cny_price: 2828.68, discount: "8.5折", discount_en: "15% OFF", available: true },
    { id: "codex", title: "Codex 点数", title_en: "Codex Credits", full_name: "Codex 点数", default_region: "PH", usd_price: null, cny_price: null, discount: "", discount_en: "", available: false, coming_soon: true },
  ],
  regions: [
    { code: "PH", label: "菲律宾", label_en: "Philippines", currency: "PHP" },
    { code: "JP", label: "日本", label_en: "Japan", currency: "JPY" },
    { code: "US", label: "美国", label_en: "United States", currency: "USD" },
    { code: "EG", label: "埃及", label_en: "Egypt", currency: "EGP" },
    { code: "CL", label: "智利", label_en: "Chile", currency: "CLP" },
  ],
  connected: true,
  guest_ttl_days: 7,
};

const CODEX_PRESETS = [250, 500, 2500, 5000];

function makeIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `pub-${crypto.randomUUID()}`;
  }
  return `pub-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

export function PortalApp() {
  const [lang, setLang] = useState<PortalLang>("zh");
  const [siteConfig, setSiteConfig] = useState<PortalSiteConfig>(FALLBACK_SITE_CONFIG);
  const [connected, setConnected] = useState<boolean>(true);
  const [auth, setAuth] = useState<PortalAuthState>({ authenticated: false, username: null, user_id: null });

  const [product, setProduct] = useState<ProductTab>("chatgpt");
  const [displayCurrency, setDisplayCurrency] = useState<DisplayCurrency>("USD");
  const [selectedPlanId, setSelectedPlanId] = useState<string>("plus");
  const [codexPoints, setCodexPoints] = useState<number>(500);
  const [regionCode, setRegionCode] = useState<string>("PH");
  const [regionManualOverride, setRegionManualOverride] = useState<boolean>(false);
  const [regionExpanded, setRegionExpanded] = useState<boolean>(false);

  const [sessionText, setSessionText] = useState<string>("");
  const [draggingFile, setDraggingFile] = useState<boolean>(false);
  const [cardNumber, setCardNumber] = useState<string>("");
  const [cardExpiry, setCardExpiry] = useState<string>("");
  const [cardCvc, setCardCvc] = useState<string>("");

  const [submitting, setSubmitting] = useState<boolean>(false);
  const [validationError, setValidationError] = useState<
    null | "errMissingSession" | "errInvalidCard" | "errCodexDisabled" | { custom: string }
  >(null);
  const [sessionFeedback, setSessionFeedback] = useState<{ text: string; isError?: boolean; url?: string } | null>(null);
  const [activeTask, setActiveTask] = useState<PublicTask | null>(null);
  const [retryingCancel, setRetryingCancel] = useState<boolean>(false);
  const [cancelingStandalone, setCancelingStandalone] = useState<boolean>(false);

  const [tasksOpen, setTasksOpen] = useState<boolean>(false);
  const [authOpen, setAuthOpen] = useState<boolean>(false);
  const [subModalOpen, setSubModalOpen] = useState<boolean>(false);
  const [supportOpen, setSupportOpen] = useState<boolean>(false);
  const [openFaqIdx, setOpenFaqIdx] = useState<number | null>(null);

  const [claudeSk, setClaudeSk] = useState<string>("");
  const [claudeNotice, setClaudeNotice] = useState<string | null>(null);

  const [toast, setToast] = useState<{ text: string; url?: string; urlLabel?: string } | null>(null);

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const streamStopRef = useRef<(() => void) | null>(null);
  const toastTimerRef = useRef<number | null>(null);

  const t = PORTAL_TEXT[lang];

  const showToast = useCallback((text: string, opts?: { url?: string; urlLabel?: string }) => {
    setToast({ text, url: opts?.url, urlLabel: opts?.urlLabel });
    if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current);
    toastTimerRef.current = window.setTimeout(() => setToast(null), 6500);
  }, []);

  const stopStream = useCallback(() => {
    streamStopRef.current?.();
    streamStopRef.current = null;
  }, []);

  useEffect(() => () => stopStream(), [stopStream]);

  // Sync page title with configured brand name.
  useEffect(() => {
    document.title = lang === "en" ? siteConfig.brand_name_en : siteConfig.brand_name;
  }, [lang, siteConfig.brand_name, siteConfig.brand_name_en]);

  const attachTask = useCallback(
    (jobId: string) => {
      stopStream();
      saveLastTaskId(jobId);
      streamStopRef.current = subscribePublicTaskStream(jobId, {
        onUpdate: (next) => {
          setActiveTask(next);
          if (next.status === "failed") {
            setValidationError(null);
          }
        },
        onDone: (finalTask) => {
          setActiveTask(finalTask);
        },
      });
    },
    [stopStream],
  );

  // Initial load: site-config, auth state, and recover last active task on refresh.
  useEffect(() => {
    const ctrl = new AbortController();
    void fetchSiteConfig(ctrl.signal)
      .then((cfg) => {
        setSiteConfig(cfg);
        setConnected(Boolean(cfg.connected));
      })
      .catch(() => {
        setConnected(false);
      });
    // Refresh live reference prices every 5 minutes while the tab is visible (matching reference portal).
    const refreshTimer = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      void fetchSiteConfig()
        .then((cfg) => {
          setSiteConfig(cfg);
          setConnected(Boolean(cfg.connected));
        })
        .catch(() => undefined);
    }, 5 * 60 * 1000);
    void fetchAuthMe(ctrl.signal)
      .then((me) => setAuth(me))
      .catch(() => undefined);

    const lastId = readLastTaskId();
    if (lastId) {
      void fetchPublicTask(lastId, ctrl.signal)
        .then((task) => {
          setActiveTask(task);
          if (task.status === "queued" || task.status === "running" || task.status === "paid_pending_delivery" || task.status === "unknown") {
            attachTask(task.job_id);
          }
        })
        .catch(() => {
          saveLastTaskId("");
        });
    }
    return () => {
      ctrl.abort();
      window.clearInterval(refreshTimer);
    };
  }, [attachTask]);

  // Keep settlement region synced to the winning lowest-price region unless user manually overrode it.
  useEffect(() => {
    if (regionManualOverride) return;
    const matched = siteConfig.plans.find((p) => p.id === selectedPlanId);
    if (matched?.default_region && siteConfig.regions.some((r) => r.code === matched.default_region)) {
      setRegionCode(matched.default_region);
    }
  }, [siteConfig.plans, siteConfig.regions, selectedPlanId, regionManualOverride]);

  const sessionInfo = useMemo(() => inspectSession(sessionText), [sessionText]);
  const rawSessionEmail = useMemo(() => {
    const trimmed = sessionText.trim();
    if (!trimmed) return "";
    try {
      if (trimmed.startsWith("{")) {
        const obj = JSON.parse(trimmed) as { user?: { email?: string }; email?: string; accessToken?: string };
        const direct = obj.user?.email || obj.email;
        if (typeof direct === "string" && direct.includes("@")) return direct.trim();
        if (typeof obj.accessToken === "string") {
          const seg = obj.accessToken.split(".")[1];
          if (seg) {
            const padded = seg.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (seg.length % 4)) % 4);
            const claims = JSON.parse(atob(padded)) as Record<string, unknown>;
            const prof = claims["https://api.openai.com/profile"] as { email?: string } | undefined;
            if (prof?.email) return String(prof.email).trim();
          }
        }
      }
    } catch {
      // ignore parse failures
    }
    return sessionInfo.maskedEmail || "";
  }, [sessionText, sessionInfo.maskedEmail]);
  const sessionReady =
    sessionInfo.state === "valid"
    || sessionInfo.state === "expiring"
    || sessionInfo.state === "refresh_only"
    || sessionInfo.state === "opaque"
    || (sessionInfo.state === "expired" && sessionInfo.hasRefreshCredential);

  const selectedPlan: PortalPlanConfig = useMemo(
    () => siteConfig.plans.find((p) => p.id === selectedPlanId) ?? siteConfig.plans[1] ?? siteConfig.plans[0],
    [siteConfig.plans, selectedPlanId],
  );

  const selectedRegion: PortalRegionConfig = useMemo(
    () => siteConfig.regions.find((r) => r.code === regionCode) ?? siteConfig.regions[0],
    [siteConfig.regions, regionCode],
  );

  const regionNameByCode = useCallback(
    (code: string) => {
      const found = siteConfig.regions.find((r) => r.code === code);
      if (!found) return code;
      return lang === "en" ? found.label_en : found.label;
    },
    [siteConfig.regions, lang],
  );

  const formatPlanRefPrice = useCallback(
    (plan: PortalPlanConfig) => {
      if (plan.usd_price == null && plan.cny_price == null) return "";
      const regionName = regionNameByCode(plan.default_region);
      if (displayCurrency === "RMB") {
        const cny = plan.cny_price ?? (plan.usd_price != null ? plan.usd_price * siteConfig.usd_cny_rate : 0);
        return `${regionName} · ¥${cny.toFixed(2)}`;
      }
      return `${regionName} · $${(plan.usd_price ?? 0).toFixed(2)}`;
    },
    [displayCurrency, regionNameByCode, siteConfig.usd_cny_rate],
  );

  const handleSelectPlan = (plan: PortalPlanConfig) => {
    setSelectedPlanId(plan.id);
    setRegionManualOverride(false);
    if (plan.default_region && siteConfig.regions.some((r) => r.code === plan.default_region)) {
      setRegionCode(plan.default_region);
    }
    setValidationError(null);
  };

  const handlePasteClipboard = async () => {
    try {
      const clip = await navigator.clipboard.readText();
      if (clip) {
        setSessionText(clip);
        setValidationError(null);
      }
    } catch {
      showToast(lang === "zh" ? "浏览器未授权剪贴板读取，请直接在框内 Ctrl/Cmd+V 粘贴。" : "Clipboard access denied. Press Ctrl/Cmd+V in the box.");
    }
  };

  const handleReadFile = async (file: File | null | undefined) => {
    if (!file) return;
    try {
      const extracted = await parseSessionFile(file);
      if (!extracted) {
        showToast(lang === "zh" ? "文件中未识别到 Session 内容。" : "No Session content found in the file.");
        return;
      }
      setSessionText(extracted);
      setValidationError(null);
      showToast(lang === "zh" ? `已从本地文件「${file.name}」读取 Session。` : `Loaded Session locally from ${file.name}.`);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "读取文件失败。");
    }
  };

  const handleCardNumberChange = (raw: string) => {
    if (raw.length > 22 || /[|\n\t/]/.test(raw)) {
      const parsed = parseCardPaste(raw);
      if (parsed.matched) {
        setCardNumber(parsed.number);
        if (parsed.expiry) setCardExpiry(parsed.expiry);
        if (parsed.cvc) setCardCvc(parsed.cvc);
        setValidationError(null);
        return;
      }
    }
    setCardNumber(formatCardNumber(raw));
  };

  const handleCardPaste = (e: React.ClipboardEvent<HTMLInputElement>) => {
    const pasted = e.clipboardData.getData("text");
    const parsed = parseCardPaste(pasted);
    if (parsed.matched) {
      e.preventDefault();
      setCardNumber(parsed.number);
      if (parsed.expiry) setCardExpiry(parsed.expiry);
      if (parsed.cvc) setCardCvc(parsed.cvc);
      setValidationError(null);
    }
  };

  const handleClearCard = () => {
    setCardNumber("");
    setCardExpiry("");
    setCardCvc("");
  };

  const handleStandaloneCancelRenew = async () => {
    if (!sessionReady || cancelingStandalone) return;
    setCancelingStandalone(true);
    setSessionFeedback(null);
    try {
      const res = await cancelAutoRenewStandalone(sessionText.trim(), selectedPlan.id);
      if (res.canceled) {
        setSessionFeedback({ text: res.message, isError: false });
      } else if (res.portal_url) {
        setSessionFeedback({ text: res.message, isError: false, url: res.portal_url });
      } else {
        setSessionFeedback({ text: res.message, isError: true });
      }
    } catch (err) {
      setSessionFeedback({ text: err instanceof Error ? err.message : "关闭自动续费失败。", isError: true });
    } finally {
      setCancelingStandalone(false);
    }
  };

  const handleOpenStripePortal = async () => {
    if (!sessionText.trim()) {
      showToast(lang === "zh" ? "请先在左侧粘贴该账号的有效 Session。" : "Please paste a valid Session first.");
      return;
    }
    try {
      const portal = await fetchPublicStripePortal(sessionText.trim());
      window.open(portal.url, "_blank", "noopener,noreferrer");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "无法打开官方账单门户。");
    }
  };

  const handleRetryTaskCancel = async () => {
    if (!activeTask || retryingCancel) return;
    if (!sessionReady) {
      showToast(lang === "zh" ? "请先在左侧粘贴该充值账号的有效 Session 再试。" : "Paste a valid Session for this account first.");
      return;
    }
    setRetryingCancel(true);
    try {
      const res = await retryTaskCancelAutoRenew(activeTask.job_id, sessionText.trim());
      if (res.canceled) {
        setActiveTask((prev) => (prev ? { ...prev, auto_renew_status: "canceled", auto_renew_error: null } : prev));
        showToast(res.message);
      } else if (res.portal_url) {
        showToast(res.message, { url: res.portal_url, urlLabel: t.openPortalBtn });
      } else {
        showToast(res.message);
      }
    } catch (err) {
      showToast(err instanceof Error ? err.message : "重新关闭自动续费失败。");
    } finally {
      setRetryingCancel(false);
    }
  };

  const isTaskBusy = Boolean(
    activeTask && (activeTask.status === "queued" || activeTask.status === "running"),
  );

  const handleSubmitRecharge = async () => {
    if (submitting || isTaskBusy) return;
    setValidationError(null);

    if (selectedPlan.coming_soon || !selectedPlan.available) {
      setValidationError("errCodexDisabled");
      return;
    }

    if (!sessionText.trim() || !sessionReady) {
      setValidationError("errMissingSession");
      return;
    }

    const digits = stripCardDigits(cardNumber);
    const exp = splitExpiry(cardExpiry);
    const cvcDigits = cardCvc.replace(/\D/g, "");
    if (digits.length < 12 || !exp || cvcDigits.length < 3 || cvcDigits.length > 4) {
      setValidationError("errInvalidCard");
      return;
    }

    setSubmitting(true);
    try {
      const created = await submitPublicRecharge(
        {
          plan: selectedPlan.id,
          exit_country: selectedRegion.code,
          session: sessionText.trim(),
          card: {
            number: digits,
            exp_month: exp.exp_month,
            exp_year: exp.exp_year,
            cvc: cvcDigits,
          },
          auto_cancel_renew: true,
        },
        makeIdempotencyKey(),
      );
      // Immediately clear sensitive card input from browser memory once submitted.
      handleClearCard();
      setActiveTask(created);
      attachTask(created.job_id);
    } catch (err) {
      setValidationError({ custom: err instanceof Error ? err.message : "提交充值失败，请稍后重试。" });
    } finally {
      setSubmitting(false);
    }
  };

  const handleSelectTaskFromModal = async (jobId: string) => {
    try {
      const snap = await fetchPublicTask(jobId);
      setProduct("chatgpt");
      setValidationError(null);
      setActiveTask(snap);
      saveLastTaskId(snap.job_id);
      if (snap.status === "queued" || snap.status === "running" || snap.status === "paid_pending_delivery" || snap.status === "unknown") {
        attachTask(snap.job_id);
      } else {
        stopStream();
      }
    } catch (err) {
      showToast(err instanceof Error ? err.message : "无法读取该任务详情。");
    }
  };

  const handleLogout = async () => {
    try {
      await logoutPortalUser();
      setAuth({ authenticated: false, username: null, user_id: null });
      showToast(lang === "zh" ? "已退出登录。" : "Signed out.");
    } catch {
      // ignore
    }
  };

  const scrollToHelp = (openIndex?: number) => {
    if (typeof openIndex === "number") {
      setOpenFaqIdx(openIndex);
    }
    const el = document.getElementById("portal-help");
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const brandName = lang === "en" ? siteConfig.brand_name_en : siteConfig.brand_name;
  const announcements = siteConfig.announcements.length > 0 ? siteConfig.announcements : FALLBACK_SITE_CONFIG.announcements;
  const marqueeLoop = [...announcements, ...announcements];
  const cardBrand = detectCardBrand(cardNumber);

  const summaryPlanText =
    selectedPlan.id === "codex"
      ? `${lang === "en" ? selectedPlan.title_en : selectedPlan.title} · ${codexPoints} ${lang === "en" ? "pts" : "点"}`
      : selectedPlan.full_name;

  const summaryRegionText = `${lang === "en" ? selectedRegion.label_en : selectedRegion.label} / ${selectedRegion.currency}`;
  const faqs = product === "claude" ? t.claudeFaqs : t.chatgptFaqs;

  return (
    <div className="portal-root">
      {/* ── Header ── */}
      <header className="portal-header">
        <div className="portal-container portal-header-inner">
          <div className="portal-header-left">
            <button
              type="button"
              className="portal-brand"
              onClick={() => {
                setProduct("chatgpt");
                window.scrollTo({ top: 0, behavior: "smooth" });
              }}
            >
              <span className="portal-brand-badge">
                <BrandBoltIcon size={19} />
              </span>
              <span>{brandName}</span>
            </button>

            <nav className="portal-nav" aria-label="Main navigation">
              <button
                type="button"
                className="portal-nav-link active"
                onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
              >
                {product === "chatgpt" ? t.navRecharge : t.navAccountCheck}
              </button>
              <button
                type="button"
                className="portal-nav-link"
                onClick={() => setTasksOpen(true)}
              >
                {t.navMyTasks}
              </button>
              <button
                type="button"
                className="portal-nav-link"
                onClick={() => scrollToHelp()}
              >
                {t.navHelp}
              </button>
            </nav>
          </div>

          <div className="portal-header-right">
            <button
              type="button"
              className="portal-outline-btn portal-lang-btn"
              aria-label={lang === "zh" ? "Switch to English" : "切换为中文"}
              onClick={() => setLang((prev) => (prev === "zh" ? "en" : "zh"))}
            >
              {lang === "zh" ? "EN" : "中"}
            </button>

            {auth.authenticated ? (
              <button
                type="button"
                className="portal-outline-btn"
                title={t.logoutBtn}
                onClick={() => void handleLogout()}
              >
                <UserIcon size={15} />
                <span>{auth.username}</span>
              </button>
            ) : (
              <button
                type="button"
                className="portal-outline-btn"
                onClick={() => setAuthOpen(true)}
              >
                <UserIcon size={15} />
                <span>{t.loginBtn}</span>
              </button>
            )}
          </div>
        </div>
      </header>

      {/* ── Main Content ── */}
      <main className="portal-container portal-main">
        {/* Hero */}
        <section className="portal-hero">
          {product === "chatgpt" && (
            <>
              <h1>{lang === "en" ? siteConfig.hero_title_en : siteConfig.hero_title}</h1>
              <p className="portal-hero-sub">
                {lang === "en" ? siteConfig.hero_subtitle_1_en : siteConfig.hero_subtitle_1}
                <br />
                {lang === "en" ? siteConfig.hero_subtitle_2_en : siteConfig.hero_subtitle_2}
              </p>
              <div className="portal-trust-row">
                <span className="portal-trust-item"><CheckCircleIcon /> {t.trustRegion}</span>
                <span className="portal-trust-item"><CheckCircleIcon /> {t.trustDirect}</span>
                <span className="portal-trust-item"><CheckCircleIcon /> {t.trustOfficial}</span>
              </div>
            </>
          )}
          {product === "claude" && (
            <>
              <h1>{t.claudeHeroTitle}</h1>
              <p className="portal-hero-sub">{t.claudeHeroSub}</p>
              <div className="portal-trust-row">
                <span className="portal-trust-item"><CheckCircleIcon /> {t.claudeTrust1}</span>
                <span className="portal-trust-item"><CheckCircleIcon /> {t.claudeTrust2}</span>
                <span className="portal-trust-item"><CheckCircleIcon /> {t.claudeTrust3}</span>
              </div>
            </>
          )}
          {product === "grok" && (
            <>
              <h1>{t.grokHeroTitle}</h1>
              <p className="portal-hero-sub">{t.grokHeroSub}</p>
            </>
          )}
        </section>

        {/* Announcement Bar */}
        <section className="portal-announcement" aria-label={t.announcementLabel}>
          <span className="portal-announcement-tag">
            <MegaphoneIcon size={15} />
            <span>{t.announcementLabel}</span>
          </span>
          <div className="portal-marquee-viewport">
            <div className="portal-marquee-track">
              {marqueeLoop.map((item, idx) => (
                <span key={`${item.id}-${idx}`} className="portal-marquee-item">
                  <strong>{item.id}:</strong>
                  {lang === "en" ? item.text_en : item.text}
                </span>
              ))}
            </div>
          </div>
        </section>

        {/* 3-Column Product Switcher */}
        <section className="portal-products" role="tablist" aria-label="Products">
          <button
            type="button"
            role="tab"
            aria-selected={product === "chatgpt"}
            className={`portal-product-tab ${product === "chatgpt" ? "active" : ""}`}
            onClick={() => setProduct("chatgpt")}
          >
            <span className="portal-product-name">
              <ChatGptIcon size={22} />
              <span>ChatGPT</span>
            </span>
            <span className="portal-product-status live">{t.tabChatGptStatus}</span>
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={product === "claude"}
            className={`portal-product-tab ${product === "claude" ? "active" : ""}`}
            onClick={() => setProduct("claude")}
          >
            <span className="portal-product-name">
              <ClaudeIcon size={22} />
              <span>Claude</span>
            </span>
            <span className="portal-product-status">{t.tabClaudeStatus}</span>
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={product === "grok"}
            className={`portal-product-tab ${product === "grok" ? "active" : ""}`}
            onClick={() => setProduct("grok")}
          >
            <span className="portal-product-name">
              <GrokIcon size={22} />
              <span>Grok</span>
            </span>
            <span className="portal-product-status">{t.tabGrokStatus}</span>
          </button>
        </section>

        {/* ── ChatGPT Recharge View ── */}
        {product === "chatgpt" && (
          <section className="portal-workspace">
            {/* Left Form Card */}
            <div className="portal-card">
              {/* Step 1: Plan Selector */}
              <div className="portal-section-head">
                <h2 className="portal-section-title">{t.choosePlan}</h2>
                <div className="portal-currency-toggle" role="group" aria-label="Currency">
                  <button
                    type="button"
                    className={`portal-currency-btn ${displayCurrency === "USD" ? "active" : ""}`}
                    onClick={() => setDisplayCurrency("USD")}
                  >
                    USD
                  </button>
                  <span>/</span>
                  <button
                    type="button"
                    className={`portal-currency-btn ${displayCurrency === "RMB" ? "active" : ""}`}
                    onClick={() => setDisplayCurrency("RMB")}
                  >
                    RMB
                  </button>
                </div>
              </div>

              <div className="portal-plan-grid">
                {siteConfig.plans.map((plan) => {
                  const active = selectedPlan.id === plan.id;
                  const priceStr = formatPlanRefPrice(plan);
                  const discountText = lang === "en" ? plan.discount_en : plan.discount;
                  return (
                    <button
                      key={plan.id}
                      type="button"
                      className={`portal-plan-card ${active ? "active" : ""}`}
                      onClick={() => handleSelectPlan(plan)}
                      onMouseMove={(e) => {
                        const rect = e.currentTarget.getBoundingClientRect();
                        e.currentTarget.style.setProperty("--mx", `${e.clientX - rect.left}px`);
                        e.currentTarget.style.setProperty("--my", `${e.clientY - rect.top}px`);
                      }}
                    >
                      <div className="portal-plan-top">
                        <span className="portal-plan-title">
                          {lang === "en" ? plan.title_en : plan.title}
                        </span>
                        {plan.coming_soon ? (
                          <span className="portal-plan-soon">{lang === "en" ? "Soon" : "即将开放"}</span>
                        ) : discountText ? (
                          <span className="portal-plan-discount">{discountText}</span>
                        ) : null}
                      </div>
                      {priceStr && <div className="portal-plan-price">{priceStr}</div>}
                    </button>
                  );
                })}
              </div>

              <p className="portal-price-caption">
                {selectedPlan.id === "codex"
                  ? t.codexComingSoon
                  : `${lang === "en" ? siteConfig.price_note_en : siteConfig.price_note}${siteConfig.price_updated_at ? ` · ${lang === "en" ? "Updated" : "参考价更新于"} ${siteConfig.price_updated_at}` : ""}${siteConfig.reference_price_status?.stale ? (lang === "en" ? " · Refresh pending, showing last valid rates." : " · 参考价更新暂未完成，沿用上次有效价格。") : ""}`}
              </p>

              {/* Codex Points Selector (1:1 layout preserved; marked coming soon) */}
              {selectedPlan.id === "codex" && (
                <div className="portal-codex-bar">
                  <div className="portal-codex-row">
                    <span className="portal-codex-label">{t.codexPointsLabel}</span>
                    {CODEX_PRESETS.map((pts) => (
                      <button
                        key={pts}
                        type="button"
                        className={`portal-point-pill ${codexPoints === pts ? "active" : ""}`}
                        onClick={() => setCodexPoints(pts)}
                      >
                        {pts.toLocaleString()}
                      </button>
                    ))}
                    <input
                      type="number"
                      min={100}
                      max={100000}
                      step={50}
                      className="portal-point-input"
                      value={codexPoints}
                      onChange={(e) => setCodexPoints(Math.max(0, Number(e.target.value) || 0))}
                    />
                  </div>
                  <p className="portal-codex-note">{t.codexComingSoon}</p>
                </div>
              )}

              {/* Settlement Region Bar */}
              <div className="portal-region-box">
                <div className="portal-region-top">
                  <div className="portal-region-summary">
                    <span className="portal-region-label">{t.settlementRegion}</span>
                    <span className="portal-region-value">{summaryRegionText}</span>
                  </div>
                  <button
                    type="button"
                    className="portal-link-btn"
                    onClick={() => setRegionExpanded((v) => !v)}
                  >
                    {regionExpanded ? t.collapse : t.modify}
                  </button>
                </div>

                {regionExpanded && (
                  <div className="portal-region-expanded">
                    <p className="portal-region-hint">{t.regionHint}</p>
                    <div className="portal-region-pills">
                      {siteConfig.regions.map((reg) => {
                        const active = reg.code === selectedRegion.code;
                        const regPrice = selectedPlan.region_prices?.[reg.code];
                        const regPriceLabel = regPrice
                          ? (displayCurrency === "RMB" ? `¥${regPrice.cny.toFixed(2)}` : `$${regPrice.usd.toFixed(2)}`)
                          : "";
                        return (
                          <button
                            key={reg.code}
                            type="button"
                            className={`portal-region-pill ${active ? "active" : ""}`}
                            onClick={() => {
                              setRegionManualOverride(true);
                              setRegionCode(reg.code);
                            }}
                          >
                            {lang === "en" ? reg.label_en : reg.label} / {reg.currency}
                            {regPriceLabel ? ` · ${regPriceLabel}` : ""}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>

              {/* Step 2: Session Dropzone */}
              <div className="portal-block-spacer">
                <div className="portal-section-head">
                  <h2 className="portal-section-title">{t.targetAccount}</h2>
                  <button
                    type="button"
                    className="portal-link-btn"
                    onClick={() => scrollToHelp(0)}
                  >
                    {t.howToGet}
                  </button>
                </div>

                <div
                  className={`portal-session-box ${draggingFile ? "dragging" : ""}`}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setDraggingFile(true);
                  }}
                  onDragLeave={() => setDraggingFile(false)}
                  onDrop={(e) => {
                    e.preventDefault();
                    setDraggingFile(false);
                    void handleReadFile(e.dataTransfer.files?.[0]);
                  }}
                >
                  <div className="portal-session-textarea-wrap">
                    <textarea
                      className="portal-session-textarea"
                      aria-label={t.targetAccount}
                      value={sessionText}
                      onChange={(e) => {
                        setSessionText(e.target.value);
                        setValidationError(null);
                        setSessionFeedback(null);
                      }}
                    />
                    {!sessionText && (
                      <div className="portal-session-placeholder">
                        <span className="portal-session-placeholder-main">{t.dropTitle}</span>
                        <span className="portal-session-placeholder-sub">{t.dropSub}</span>
                      </div>
                    )}
                  </div>

                  <div className="portal-session-toolbar">
                    <div className="portal-session-actions">
                      <button
                        type="button"
                        className="portal-link-btn"
                        onClick={() => void handlePasteClipboard()}
                      >
                        {t.pasteBtn}
                      </button>
                      <button
                        type="button"
                        className="portal-link-btn"
                        onClick={() => fileInputRef.current?.click()}
                      >
                        {t.chooseFileBtn}
                      </button>
                      <input
                        ref={fileInputRef}
                        type="file"
                        accept=".json,.txt,.rtf,.doc,.docx,.pdf,.pages,.template"
                        style={{ display: "none" }}
                        onChange={(e) => {
                          void handleReadFile(e.target.files?.[0]);
                          e.target.value = "";
                        }}
                      />
                      <button
                        type="button"
                        className="portal-link-btn"
                        disabled={!sessionText.trim() || cancelingStandalone}
                        onClick={() => {
                          if (!sessionReady) {
                            setSessionFeedback({
                              text: "Session（登录信息）不完整或已损坏。请关闭网页翻译，重新获取并复制完整内容后再试。",
                              isError: true,
                            });
                            return;
                          }
                          void handleStandaloneCancelRenew();
                        }}
                      >
                        {cancelingStandalone ? "处理中…" : t.cancelRenewBtn}
                      </button>
                      <button
                        type="button"
                        className="portal-link-btn"
                        disabled={!sessionText.trim()}
                        onClick={() => {
                          if (!sessionReady) {
                            setSessionFeedback({
                              text: "Session（登录信息）不完整或已损坏。请关闭网页翻译，重新获取并复制完整内容后再试。",
                              isError: true,
                            });
                            return;
                          }
                          setSessionFeedback(null);
                          setSubModalOpen(true);
                        }}
                      >
                        {t.checkSubBtn}
                      </button>
                    </div>
                    <span className="portal-session-local-hint">{t.localRead}</span>
                  </div>
                </div>

                {sessionFeedback && (
                  <p
                    className={`portal-action-feedback ${sessionFeedback.isError ? "is-error" : ""}`}
                    role={sessionFeedback.isError ? "alert" : "status"}
                  >
                    {sessionFeedback.text}
                    {sessionFeedback.url && (
                      <a href={sessionFeedback.url} target="_blank" rel="noopener noreferrer">
                        {t.openPortalBtn} ↗
                      </a>
                    )}
                  </p>
                )}
              </div>

              <hr className="portal-divider" />

              {/* Step 3: Payment Card */}
              <div>
                <div className="portal-section-head">
                  <h2 className="portal-section-title">{t.cardSection}</h2>
                  {cardBrand && (
                    <span key={cardBrand} className="portal-card-recognition">{cardBrand}</span>
                  )}
                </div>

                <div className="portal-card-grid">
                  <div>
                    <label className="portal-field-label" htmlFor="portal-card-number">{t.cardNumberLabel}</label>
                    <input
                      id="portal-card-number"
                      type="text"
                      inputMode="numeric"
                      autoComplete="off"
                      className="portal-input"
                      placeholder={t.cardNumberPlaceholder}
                      value={cardNumber}
                      onChange={(e) => handleCardNumberChange(e.target.value)}
                      onPaste={handleCardPaste}
                    />
                  </div>
                  <div>
                    <label className="portal-field-label" htmlFor="portal-card-exp">{t.cardExpiryLabel}</label>
                    <input
                      id="portal-card-exp"
                      type="text"
                      inputMode="numeric"
                      autoComplete="off"
                      className="portal-input"
                      placeholder={t.cardExpiryPlaceholder}
                      value={cardExpiry}
                      onChange={(e) => setCardExpiry(formatExpiryInput(e.target.value))}
                    />
                  </div>
                  <div>
                    <label className="portal-field-label" htmlFor="portal-card-cvc">{t.cardCvcLabel}</label>
                    <input
                      id="portal-card-cvc"
                      type="password"
                      inputMode="numeric"
                      autoComplete="off"
                      maxLength={4}
                      className="portal-input"
                      placeholder={t.cardCvcPlaceholder}
                      value={cardCvc}
                      onChange={(e) => setCardCvc(e.target.value.replace(/\D/g, "").slice(0, 4))}
                    />
                  </div>
                </div>

                <div className="portal-card-footer">
                  <div className="portal-card-shop">
                    {siteConfig.card_shop_url ? (
                      <>
                        <span>{t.noCardHint}</span>
                        <a
                          href={siteConfig.card_shop_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="portal-link-btn"
                        >
                          {lang === "en" ? siteConfig.card_shop_label_en : siteConfig.card_shop_label}
                          <ExternalArrowIcon />
                        </a>
                      </>
                    ) : (
                      <span style={{ color: "#94a3b8", fontSize: 12.5 }}>
                        {lang === "zh" ? "支持粘贴整段卡片信息（卡号 | 月/年 | 安全码），卡片信息不落库" : "Supports pasting full card lines; card details are never stored"}
                      </span>
                    )}
                  </div>
                  <button type="button" className="portal-link-btn" onClick={handleClearCard}>
                    {t.clearBtn}
                  </button>
                </div>
              </div>
            </div>

            {/* Right Sidebar: Summary + Receipt */}
            <aside className="portal-sidebar">
              <section className="portal-summary-card" aria-label={t.summaryTitle}>
                <h2 className="portal-summary-title">{t.summaryTitle}</h2>
                <div className="portal-summary-row">
                  <span className="portal-summary-key">{t.summaryAccount}</span>
                  <span className={`portal-summary-val ${rawSessionEmail ? "" : "muted"}`}>
                    {rawSessionEmail || activeTask?.masked_email || t.summaryAccountEmpty}
                  </span>
                </div>
                <div className="portal-summary-row">
                  <span className="portal-summary-key">{t.summaryPlan}</span>
                  <span className="portal-summary-val">{summaryPlanText}</span>
                </div>
                <div className="portal-summary-row">
                  <span className="portal-summary-key">{t.summaryRegion}</span>
                  <span className="portal-summary-val">{summaryRegionText}</span>
                </div>
                <button
                  type="button"
                  className="portal-primary-btn"
                  disabled={submitting || isTaskBusy || selectedPlan.coming_soon}
                  onClick={() => void handleSubmitRecharge()}
                >
                  {submitting ? t.submittingBtn : isTaskBusy ? t.processingBtn : t.submitBtn}
                </button>
              </section>

              <ReceiptCard
                lang={lang}
                task={activeTask}
                validationError={
                  validationError === null
                    ? null
                    : typeof validationError === "string"
                      ? t[validationError]
                      : validationError.custom
                }
                onRetryCancelRenew={() => void handleRetryTaskCancel()}
                retryingCancel={retryingCancel}
                onOpenStripePortal={() => void handleOpenStripePortal()}
                onResetTask={() => {
                  stopStream();
                  setActiveTask(null);
                  setValidationError(null);
                  saveLastTaskId("");
                }}
              />
            </aside>
          </section>
        )}

        {/* ── Claude Tab (1:1 Layout preserved, marked Coming Soon) ── */}
        {product === "claude" && (
          <section className="portal-workspace">
            <div className="portal-card">
              <div className="portal-section-head">
                <div>
                  <h2 className="portal-section-title">{t.claudeCardTitle}</h2>
                  <p style={{ margin: "6px 0 0", fontSize: 13.5, color: "#64748b" }}>{t.claudeCardSub}</p>
                </div>
                <span className="portal-claude-badge">{t.claudeCardBadge}</span>
              </div>

              <div className="portal-section-head" style={{ marginTop: 18, marginBottom: 8 }}>
                <span style={{ fontSize: 13.5, fontWeight: 600 }}>Claude SK</span>
                <button type="button" className="portal-link-btn" onClick={() => scrollToHelp(0)}>
                  {t.howToGet}
                </button>
              </div>

              <textarea
                className="portal-claude-textarea"
                placeholder={t.claudeSkPlaceholder}
                value={claudeSk}
                onChange={(e) => {
                  setClaudeSk(e.target.value);
                  setClaudeNotice(null);
                }}
              />
              <p style={{ margin: "8px 0 16px", fontSize: 12.5, color: "#94a3b8" }}>{t.claudeSkHint}</p>

              <div style={{ display: "flex", gap: 10 }}>
                <button
                  type="button"
                  className="portal-primary-btn"
                  style={{ marginTop: 0, flex: 1 }}
                  disabled={!claudeSk.trim()}
                  onClick={() => setClaudeNotice(t.claudeComingSoonNotice)}
                >
                  {t.claudeCheckBtn}
                </button>
                <button
                  type="button"
                  className="portal-outline-btn"
                  style={{ height: 46, padding: "0 20px" }}
                  onClick={() => {
                    setClaudeSk("");
                    setClaudeNotice(null);
                  }}
                >
                  {t.clearBtn}
                </button>
              </div>

              {claudeNotice && <div className="portal-alert-info">{claudeNotice}</div>}

              <div className="portal-claude-guide">
                <strong style={{ fontSize: 14, color: "#1e293b" }}>{t.claudeGuideTitle}</strong>
                <ol>
                  <li>打开并登录 <code>https://claude.ai</code>。</li>
                  <li>按 <code>F12</code> 打开浏览器开发者工具，切换到 <strong>Application</strong>。</li>
                  <li>在左侧展开 <strong>Storage → Cookies → https://claude.ai</strong>。</li>
                  <li>找到 <code>sessionKey</code>（以 <code>sk-ant-sid02-</code> 开头）并复制完整值。</li>
                </ol>
              </div>
            </div>

            <aside className="portal-sidebar">
              <section className="portal-summary-card">
                <h2 className="portal-summary-title">{t.claudeNoticeTitle}</h2>
                <p style={{ margin: 0, fontSize: 13.5, color: "#64748b", lineHeight: 1.65 }}>
                  {t.claudeComingSoonNotice}
                </p>
              </section>
              <section className="portal-receipt-card">
                <div className="portal-receipt-body">
                  <div className="portal-receipt-head">
                    <span className="portal-receipt-title">{t.claudeResultTitle}</span>
                    <span className="portal-status-pill">{t.claudeResultBadge}</span>
                  </div>
                  <p style={{ margin: "24px 0 10px", fontSize: 14, color: "#64748b", textAlign: "center" }}>
                    {t.claudeResultSub}
                  </p>
                </div>
              </section>
            </aside>
          </section>
        )}

        {/* ── Grok Tab (1:1 Empty State) ── */}
        {product === "grok" && (
          <section className="portal-grok-empty">
            <GrokIcon size={40} />
            <h2>{t.grokEmptyTitle}</h2>
            <p>{t.grokEmptySub}</p>
            <div className="portal-grok-actions">
              <button
                type="button"
                className="portal-primary-btn"
                style={{ marginTop: 0, width: "auto", padding: "0 22px" }}
                onClick={() => setProduct("claude")}
              >
                {t.grokGoClaude}
              </button>
              <button
                type="button"
                className="portal-outline-btn"
                style={{ height: 46, padding: "0 22px" }}
                onClick={() => setProduct("chatgpt")}
              >
                {t.grokGoChatGpt}
              </button>
            </div>
          </section>
        )}

        {/* ── Help / FAQ Accordion ── */}
        <section id="portal-help" className="portal-help" aria-label={t.helpTitle}>
          <h2 className="portal-help-title">{t.helpTitle}</h2>
          <div className="portal-faq-list">
            {faqs.map((item, idx) => {
              const isOpen = openFaqIdx === idx;
              return (
                <div key={item.q} className="portal-faq-item">
                  <button
                    type="button"
                    className="portal-faq-btn"
                    aria-expanded={isOpen}
                    onClick={() => setOpenFaqIdx(isOpen ? null : idx)}
                  >
                    <span>{item.q}</span>
                    <span className="portal-faq-toggle">{isOpen ? "−" : "+"}</span>
                  </button>
                  {isOpen && (
                    <div className="portal-faq-body">
                      {item.aPrefix}
                      {"aLinkHref" in item && item.aLinkHref && (
                        <a href={item.aLinkHref} target="_blank" rel="noopener noreferrer">
                          {item.aLinkText}
                        </a>
                      )}
                      {"aSuffix" in item && item.aSuffix}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>

        {/* ── Footer ── */}
        <footer className="portal-footer">
          <span>
            {brandName} · {lang === "en" ? siteConfig.footer_slogan_en : siteConfig.footer_slogan}
          </span>
          <span>{connected ? t.footerConnected : t.footerDisconnected}</span>
        </footer>
      </main>

      {/* ── Floating Support Pill ── */}
      {siteConfig.support_url ? (
        <a
          href={siteConfig.support_url}
          target="_blank"
          rel="noopener noreferrer"
          className="portal-support-fab"
        >
          <HeadsetIcon size={18} />
          <span>{lang === "en" ? siteConfig.support_label_en : siteConfig.support_label}</span>
        </a>
      ) : (
        <button
          type="button"
          className="portal-support-fab"
          onClick={() => setSupportOpen(true)}
        >
          <HeadsetIcon size={18} />
          <span>{lang === "en" ? siteConfig.support_label_en : siteConfig.support_label}</span>
        </button>
      )}

      {/* ── Toast Notification ── */}
      {toast && (
        <div className="portal-toast" role="status">
          <span>{toast.text}</span>
          {toast.url && (
            <a href={toast.url} target="_blank" rel="noopener noreferrer">
              {toast.urlLabel || t.openPortalBtn} ↗
            </a>
          )}
        </div>
      )}

      {/* ── Modals ── */}
      <MyTasksModal
        open={tasksOpen}
        lang={lang}
        auth={auth}
        onClose={() => setTasksOpen(false)}
        onSelectTask={(jobId) => void handleSelectTaskFromModal(jobId)}
      />

      <AuthModal
        open={authOpen}
        lang={lang}
        siteConfig={siteConfig}
        onClose={() => setAuthOpen(false)}
        onSuccess={(next) => {
          setAuth(next);
          showToast(
            lang === "zh"
              ? `欢迎，${next.username}${next.claimed_tasks ? `（已同步 ${next.claimed_tasks} 条游客任务）` : ""}`
              : `Signed in as ${next.username}`,
          );
        }}
      />

      <SubscriptionStatusModal
        open={subModalOpen}
        lang={lang}
        session={sessionText}
        onClose={() => setSubModalOpen(false)}
      />

      <SupportModal
        open={supportOpen}
        lang={lang}
        siteConfig={siteConfig}
        onClose={() => setSupportOpen(false)}
      />
    </div>
  );
}
