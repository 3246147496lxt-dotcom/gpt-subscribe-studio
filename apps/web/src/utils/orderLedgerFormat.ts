import type { OrderLedgerItem } from "../api/ledger";

export const SOURCE_LABEL: Record<string, string> = {
  portal: "用户门户",
  quick_pay: "后台一键",
  step: "分步调试",
};

export const SOURCE_COLOR: Record<string, string> = {
  portal: "blue",
  quick_pay: "violet",
  step: "gray",
};

export const PAYMENT_STATUS_LABEL: Record<string, string> = {
  unpaid: "未支付",
  pending: "待支付",
  processing: "支付中",
  requires_action: "待验证",
  succeeded: "已支付",
  failed: "支付失败",
  canceled: "已取消",
  refunded: "已退款",
};

export const DELIVERY_STATUS_LABEL: Record<string, string> = {
  pending: "待交付",
  verifying: "核验中",
  verify_delayed: "延迟核验",
  delivered: "已交付",
  failed: "未交付",
  skipped: "已跳过",
};

export const AUTO_RENEW_LABEL: Record<string, string> = {
  disabled: "未开启",
  pending: "待关续费",
  canceled: "已关续费",
  manual_required: "需人工关续费",
  failed: "关续费失败",
};

export const AUTO_RENEW_COLOR: Record<string, string> = {
  disabled: "gray",
  pending: "blue",
  canceled: "teal",
  manual_required: "orange",
  failed: "red",
};

export const CHECKOUT_KIND_LABEL: Record<string, string> = {
  custom: "自研协议",
  hosted: "Hosted",
  stripe_hosted: "Hosted",
};

export function paymentStatusLabel(value?: string | null): string {
  if (!value) return "-";
  return PAYMENT_STATUS_LABEL[value] ?? value;
}

export function deliveryStatusLabel(value?: string | null): string {
  if (!value) return "-";
  return DELIVERY_STATUS_LABEL[value] ?? value;
}

export function sourceLabel(value?: string | null): string {
  if (!value) return "分步调试";
  return SOURCE_LABEL[value] ?? value;
}

export function sourceColor(value?: string | null): string {
  if (!value) return "gray";
  return SOURCE_COLOR[value] ?? "gray";
}

export function autoRenewLabel(value?: string | null): string {
  if (!value) return "未开启";
  return AUTO_RENEW_LABEL[value] ?? value;
}

export function autoRenewColor(value?: string | null): string {
  if (!value) return "gray";
  return AUTO_RENEW_COLOR[value] ?? "gray";
}

export function checkoutKindLabel(value?: string | null): string | null {
  if (!value) return null;
  return CHECKOUT_KIND_LABEL[value] ?? value;
}

export function formatDurationMs(ms?: number | null): string | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const mins = Math.floor(ms / 60_000);
  const secs = Math.round((ms % 60_000) / 1000);
  return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
}

/** Compact clock-style MM:SS duration matching clean ops tables (e.g. 00:48, 01:03). */
export function formatDurationClock(ms?: number | null): string | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  const totalSec = ms > 0 && ms < 1000 ? 1 : Math.round(ms / 1000);
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

export function formatDateTimeCompact(iso?: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Compact MM/DD HH:mm timestamp without year or seconds (e.g. 11/24 19:40). */
export function formatMonthDayTime(iso?: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export type SimpleStatusTone = "success" | "failed" | "warning" | "pending";

export function simpleOrderStatus(status: string): { label: string; tone: SimpleStatusTone } {
  if (status === "active" || status === "delivered") {
    return { label: "成功", tone: "success" };
  }
  if (status === "failed" || status === "rejected") {
    return { label: "失败", tone: "failed" };
  }
  if (status === "unknown") {
    return { label: "待核对", tone: "warning" };
  }
  return { label: "处理中", tone: "pending" };
}

export function planPillTone(plan: string): "plus" | "pro" | "other" {
  const norm = (plan || "").toLowerCase();
  if (norm.startsWith("pro")) return "pro";
  if (norm === "plus") return "plus";
  return "other";
}

export function hasFailureReason(item: Pick<
  OrderLedgerItem,
  "status" | "failure_code" | "last_error" | "auto_renew_error" | "diagnostics"
>): boolean {
  if (item.failure_code || item.last_error || item.auto_renew_error) return true;
  if (item.status === "failed" || item.status === "rejected") return true;
  return Boolean(item.diagnostics && Object.keys(item.diagnostics).length > 0);
}

export type TimingSegment = {
  key: string;
  label: string;
  ms: number | null;
  percent: number;
  color: string;
  emptyText?: string;
};

export function formatPreciseDuration(ms?: number | null): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "-";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`;
  const mins = Math.floor(ms / 60_000);
  const secs = (ms % 60_000) / 1000;
  return `${mins}m ${secs.toFixed(1)}s`;
}

/** Build a non-overlapping sequential timing breakdown for the Order Detail modal. */
export function buildTimingBreakdown(item: Pick<
  OrderLedgerItem,
  "total_ms" | "timings_ms" | "auto_cancel_renew"
>): { totalMs: number | null; segments: TimingSegment[] } {
  const t = item.timings_ms ?? {};
  const num = (k: string): number | null => {
    const v = t[k];
    return typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
  };

  const queueMs = num("queue_ms");
  const preflightMs = num("preflight_ms");
  const reissueMs = num("reissue_ms");
  const decodeMs = num("browser_decode_ms");
  const rawCheckoutMs = num("create_checkout_ms");
  const rawCreateOrderMs = num("create_order_ms");
  const checkoutMs =
    rawCheckoutMs != null
      ? rawCheckoutMs
      : rawCreateOrderMs != null
        ? Math.max(0, rawCreateOrderMs - (preflightMs ?? 0) - (reissueMs ?? 0) - (decodeMs ?? 0))
        : null;
  const paymentMs = num("payment_ms");
  const cancelRenewMs = num("cancel_renew_ms");

  const recordedList: Array<{ key: string; label: string; ms: number | null; color: string; emptyText?: string; alwaysShow?: boolean }> = [
    { key: "queue_ms", label: "排队等待", ms: queueMs, color: "#94a3b8" },
    { key: "preflight_ms", label: "前置体检", ms: preflightMs, color: "#06b6d4" },
    { key: "reissue_ms", label: "会话重签", ms: reissueMs, color: "#6366f1" },
    { key: "browser_decode_ms", label: "浏览器解码", ms: decodeMs, color: "#8b5cf6" },
    { key: "create_checkout_ms", label: "建单耗时", ms: checkoutMs, color: "#2563eb", emptyText: "未记录", alwaysShow: true },
    { key: "payment_ms", label: "支付耗时", ms: paymentMs, color: "#10b981", emptyText: "未进入支付", alwaysShow: true },
    {
      key: "cancel_renew_ms",
      label: "关闭续费",
      ms: cancelRenewMs,
      color: "#f59e0b",
      emptyText: "未执行",
      alwaysShow: Boolean(item.auto_cancel_renew || cancelRenewMs != null),
    },
  ];

  const sumRecorded = recordedList.reduce((acc, s) => acc + (s.ms ?? 0), 0);
  const totalMs = item.total_ms ?? num("total_ms") ?? (sumRecorded > 0 ? sumRecorded : null);
  const denom = Math.max(totalMs ?? 0, sumRecorded, 1);

  const visible = recordedList.filter((s) => s.alwaysShow || (s.ms != null && s.ms > 0));
  const segments: TimingSegment[] = visible.map((s) => ({
    key: s.key,
    label: s.label,
    ms: s.ms,
    percent: s.ms != null && totalMs != null && totalMs > 0 ? Math.min(100, Math.round((s.ms / denom) * 100)) : 0,
    color: s.color,
    emptyText: s.emptyText,
  }));

  if (totalMs != null && totalMs - sumRecorded >= 100) {
    const otherMs = totalMs - sumRecorded;
    segments.push({
      key: "other_ms",
      label: sumRecorded <= 20 ? "其他 / 未细分耗时" : "核验及其他开销",
      ms: otherMs,
      percent: Math.min(100, Math.round((otherMs / denom) * 100)),
      color: "#cbd5e1",
    });
  }

  return { totalMs, segments };
}
