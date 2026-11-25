import type { PublicTask } from "./api";
import { PORTAL_TEXT, type PortalLang } from "./i18n";
import { ExternalArrowIcon, ReceiptIcon, ShieldIcon, StateMark } from "./icons";

type StepVisualState = "pending" | "active" | "done" | "failed" | "warn" | "skipped";
type MarkState = "pending" | "running" | "done" | "failed" | "warn";

const STEP1_STAGES = new Set([
  "queued",
  "draining",
  "preflight",
  "preflight_passed",
  "creating_order",
  "running",
  "reissue",
  "reissuing",
  "reissued",
  "session_refresh",
  "session_refreshed",
  "browser_decode",
  "browser_decoded",
  "browser_decode_skipped",
  "checkout",
  "create_checkout",
  "creating_checkout",
  "checkout_retry_started",
  "checkout_created",
]);

const STEP2_STAGES = new Set(["confirming_payment", "requires_action", "unknown"]);
const STEP3_STAGES = new Set(["verifying_delivery", "paid_pending_delivery"]);
const STEP4_STAGES = new Set(["canceling_auto_renew", "auto_renew_canceled"]);

function computeFourSteps(task: PublicTask | null): {
  states: [StepVisualState, StepVisualState, StepVisualState, StepVisualState];
  percent: number;
  liveText: string;
  badgeText: string;
  badgeClass: string;
  barClass: string;
  markState: MarkState;
  isComplete: boolean;
} {
  if (!task) {
    return {
      states: ["pending", "pending", "pending", "pending"],
      percent: 0,
      liveText: "",
      badgeText: "",
      badgeClass: "",
      barClass: "",
      markState: "pending",
      isComplete: false,
    };
  }

  const stage = task.stage || "queued";
  const status = task.status;

  if (status === "succeeded") {
    const cancelFailed =
      task.auto_renew_status === "manual_required" || task.auto_renew_status === "failed";
    return {
      states: ["done", "done", "done", cancelFailed ? "warn" : "done"],
      percent: 100,
      liveText: cancelFailed
        ? "订阅已开通，但自动关闭续费未完成，可在下方重试或前往账单门户关闭"
        : "订阅已开通，自动续费已关闭",
      badgeText: "已开通",
      badgeClass: "success",
      barClass: "success",
      markState: "done",
      isComplete: true,
    };
  }

  if (status === "failed") {
    let failIdx = 0;
    if (
      STEP2_STAGES.has(stage)
      || task.failure_code === "card_declined"
      || task.failure_code === "insufficient_funds"
    ) {
      failIdx = 1;
    } else if (STEP3_STAGES.has(stage)) {
      failIdx = 2;
    } else if (STEP4_STAGES.has(stage)) {
      failIdx = 3;
    }
    const states: [StepVisualState, StepVisualState, StepVisualState, StepVisualState] = [
      "pending",
      "pending",
      "pending",
      "pending",
    ];
    for (let i = 0; i < failIdx; i += 1) states[i] = "done";
    states[failIdx] = "failed";
    return {
      states,
      percent: [25, 50, 75, 92][failIdx],
      liveText: task.message || "本次充值未完成，请核对提示后处理",
      badgeText: "未完成",
      badgeClass: "failed",
      barClass: "failed",
      markState: "failed",
      isComplete: false,
    };
  }

  if (status === "requires_action") {
    return {
      states: ["done", "warn", "pending", "pending"],
      percent: 50,
      liveText: "发卡行或上游要求完成验证，请点击下方链接处理",
      badgeText: "需验证",
      badgeClass: "warn",
      barClass: "warn",
      markState: "warn",
      isComplete: false,
    };
  }

  if (status === "unknown") {
    return {
      states: ["done", "warn", "pending", "pending"],
      percent: 50,
      liveText: "付款结果待核对，系统持续确认中，请勿重复下单",
      badgeText: "核验中",
      badgeClass: "warn",
      barClass: "warn",
      markState: "running",
      isComplete: false,
    };
  }

  if (status === "paid_pending_delivery") {
    return {
      states: ["done", "done", "warn", "pending"],
      percent: 75,
      liveText: "已确认扣款，正在等待上游同步订阅权益",
      badgeText: "核验中",
      badgeClass: "warn",
      barClass: "warn",
      markState: "running",
      isComplete: false,
    };
  }

  if (STEP4_STAGES.has(stage)) {
    return {
      states: ["done", "done", "done", "active"],
      percent: 88,
      liveText: "套餐已开通，正在关闭自动续费…",
      badgeText: "处理中",
      badgeClass: "running",
      barClass: "",
      markState: "running",
      isComplete: false,
    };
  }

  if (STEP3_STAGES.has(stage)) {
    return {
      states: ["done", "done", "active", "pending"],
      percent: 75,
      liveText: "付款已提交，正在核验套餐是否开通…",
      badgeText: "处理中",
      badgeClass: "running",
      barClass: "",
      markState: "running",
      isComplete: false,
    };
  }

  if (STEP2_STAGES.has(stage)) {
    return {
      states: ["done", "active", "pending", "pending"],
      percent: 50,
      liveText: "已取得金额，正在提交卡片付款…",
      badgeText: "处理中",
      badgeClass: "running",
      barClass: "",
      markState: "running",
      isComplete: false,
    };
  }

  const queuedHint =
    status === "queued" && task.queue_position && task.queue_position > 1
      ? `排队第 ${task.queue_position} 位，即将开始确认账号与金额…`
      : STEP1_STAGES.has(stage)
        ? "正在确认账号与实时结账金额…"
        : "正在处理中…";

  return {
    states: ["active", "pending", "pending", "pending"],
    percent: status === "queued" ? 12 : 25,
    liveText: queuedHint,
    badgeText: status === "queued" ? "排队中" : "处理中",
    badgeClass: "running",
    barClass: "",
    markState: "running",
    isComplete: false,
  };
}

function formatDueAmount(task: PublicTask | null, placeholder: string): string {
  if (!task || !task.amount) return placeholder;
  const cur = task.currency ? ` ${task.currency.toUpperCase()}` : "";
  return `${task.amount}${cur}`;
}

export function ReceiptCard({
  lang,
  task,
  validationError,
  onRetryCancelRenew,
  retryingCancel,
  onOpenStripePortal,
  onResetTask,
}: {
  lang: PortalLang;
  task: PublicTask | null;
  validationError: string | null;
  onRetryCancelRenew: () => void;
  retryingCancel: boolean;
  onOpenStripePortal: () => void;
  onResetTask: () => void;
}) {
  const t = PORTAL_TEXT[lang];
  const computed = computeFourSteps(task);
  const stepTitles = [t.step1, t.step2, t.step3, t.step4];

  const badgeLabel = task
    ? lang === "en"
      ? {
          queued: t.receiptQueuedBadge,
          running: t.receiptRunningBadge,
          succeeded: t.receiptSuccessBadge,
          failed: t.receiptFailedBadge,
          unknown: t.receiptVerifyBadge,
          paid_pending_delivery: t.receiptVerifyBadge,
          requires_action: t.receiptActionBadge,
        }[task.status]
      : computed.badgeText
    : t.receiptPendingBadge;

  const liveCaption = task ? computed.liveText : t.receiptWaiting;
  const alertMessage =
    validationError
    || (task?.status === "failed" ? task.message || "本次充值未完成，请核对输入后重试。" : null);
  const showManualCancel = Boolean(
    task?.status === "succeeded"
    && (task.auto_renew_status === "manual_required" || task.auto_renew_status === "failed"),
  );
  const redirectUrl = task?.status === "requires_action" ? task.next_action?.redirect_url : undefined;
  const isTerminal = Boolean(task && ["succeeded", "failed"].includes(task.status));

  return (
    <section
      className={`portal-receipt-card ${computed.isComplete ? "is-complete" : ""}`}
      aria-label={t.receiptTitle}
    >
      <div className="portal-receipt-body">
        <div className="portal-receipt-head">
          <div className="portal-receipt-title">
            <ReceiptIcon size={18} />
            <span>{t.receiptTitle}</span>
          </div>
          <span className={`portal-receipt-stamp ${computed.badgeClass}`}>{badgeLabel}</span>
        </div>

        {alertMessage && (
          <div className="portal-alert-error" role="alert">
            {alertMessage}
          </div>
        )}

        {task && (task.status === "unknown" || task.status === "paid_pending_delivery") && (
          <div className="portal-alert-info">
            {lang === "zh"
              ? "付款结果核验中，切勿重复下单。刷新页面或打开「我的任务」可继续追踪。"
              : "Payment confirmation is in progress. Do not resubmit; track updates in My Tasks."}
          </div>
        )}

        <div className="portal-amount-block">
          <div className="portal-amount-label">{t.receiptDueLabel}</div>
          <div className="portal-amount-value">{formatDueAmount(task, t.receiptDuePlaceholder)}</div>
        </div>

        <hr className="portal-dashed-sep" />

        <div className="portal-live-row">
          <StateMark state={computed.markState} size={16} />
          <span>{liveCaption}</span>
        </div>

        <div className="portal-progress-track">
          <div
            className={`portal-progress-fill ${computed.barClass}`}
            style={{ width: `${computed.percent}%` }}
          />
        </div>

        <ol className="portal-steps">
          {stepTitles.map((title, idx) => {
            const st = computed.states[idx];
            return (
              <li key={title} className={`portal-step-item ${st}`}>
                <span className="portal-step-node">
                  {st === "done" ? "✓" : st === "failed" ? "!" : st === "skipped" ? "−" : idx + 1}
                </span>
                <div className="portal-step-content">
                  <div className="portal-step-title">{title}</div>
                  {idx === 3 && showManualCancel && (
                    <div className="portal-step-sub">
                      {task?.auto_renew_error
                        || (lang === "zh"
                          ? "可粘贴最新 Session 重试，或通过账单门户手动关闭"
                          : "Retry with a fresh Session or close via billing portal")}
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ol>

        {(redirectUrl || showManualCancel || isTerminal) && (
          <div className="portal-receipt-actions">
            {redirectUrl && (
              <a
                href={redirectUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="portal-receipt-action-btn"
              >
                {t.openVerifyBtn} <ExternalArrowIcon />
              </a>
            )}
            {showManualCancel && (
              <>
                <button
                  type="button"
                  className="portal-receipt-action-btn"
                  onClick={onRetryCancelRenew}
                  disabled={retryingCancel}
                >
                  {retryingCancel ? "…" : t.retryCancelBtn}
                </button>
                <button
                  type="button"
                  className="portal-receipt-action-btn neutral"
                  onClick={onOpenStripePortal}
                >
                  {t.openPortalBtn}
                </button>
              </>
            )}
            {isTerminal && (
              <button
                type="button"
                className="portal-receipt-action-btn neutral"
                onClick={onResetTask}
              >
                {t.newOrderBtn}
              </button>
            )}
          </div>
        )}

        <p className="portal-receipt-helper">{t.receiptHelper}</p>
      </div>

      <div className="portal-receipt-footer">
        <span>{t.receiptFooter}</span>
        <ShieldIcon size={16} />
      </div>
    </section>
  );
}
