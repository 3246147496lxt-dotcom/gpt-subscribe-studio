import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Group,
  Modal,
  SegmentedControl,
  SimpleGrid,
  Skeleton,
  Stack,
  Table,
  Text,
  Tooltip,
} from "@mantine/core";
import {
  IconActivity,
  IconAdjustments,
  IconAlertCircle,
  IconAlertTriangle,
  IconArrowRight,
  IconBolt,
  IconCheck,
  IconClock,
  IconCurrencyDollar,
  IconExternalLink,
  IconLockOpen,
  IconReceipt,
  IconRefresh,
  IconShieldCheck,
} from "@tabler/icons-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  getHealth,
  refreshReferencePrices,
  type HealthSnapshot,
} from "../api/adminOps";
import { formatAmount } from "../api/client";
import { getFixedBillingSettings, type FixedBillingSettings } from "../api/fixedBilling";
import { getRuntimeFlags, type RuntimeFlagsSnapshot } from "../api/flags";
import { isAbortError } from "../api/http";
import {
  getOrderEvents,
  getOrderFailureDetail,
  getOrderLedger,
  reconcileOrder,
  type OrderFailureDetail,
  type OrderLedgerItem,
  type OrderLedgerPage,
} from "../api/ledger";
import {
  getQuickPay,
  getQuickPayQueue,
  type QueueStatus,
  type QuickPayEvent,
  type QuickPayJob,
} from "../api/quickPay";
import { getSystemDiagnostics, type SystemDiagnostics } from "../api/system";
import { planLabel } from "../plans";
import {
  autoRenewColor,
  autoRenewLabel,
  formatDurationClock,
  formatDurationMs,
  formatMonthDayTime,
  hasFailureReason,
  planPillTone,
  simpleOrderStatus,
} from "../utils/orderLedgerFormat";
import { CopyIdBadge } from "./CopyIdBadge";
import { JsonCodeBlock } from "./JsonCodeBlock";
import { OrderFailureDetailModal } from "./OrderFailureDetailModal";
import { OrderLedgerDetailModal } from "./OrderLedgerDetailModal";
import { OrderManualVerdictModal } from "./OrderManualVerdictModal";
import { FAILURE_LABEL, STAGE_LABEL, TIMING_LABEL } from "./quick-pay/constants";

type FeedTab = "orders" | "queue";
type OrderQuickFilter = "all" | "delivered" | "attention" | "failed";

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function formatDiskSpace(mb: number): string {
  if (!Number.isFinite(mb) || mb <= 0) return "-";
  if (mb >= 1024 * 1024) return `${(mb / (1024 * 1024)).toFixed(2)} TB`;
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  return `${Math.round(mb)} MB`;
}

function formatClockHms(ts: number): string {
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function formatOsProfileLabel(val: unknown): string {
  const s = String(val || "auto");
  if (s === "macos") return "强制 macOS";
  if (s === "windows") return "强制 Windows";
  if (s === "linux") return "强制 Linux";
  return "自动跟随 Session";
}

export function OpsOverviewPanel({
  active = true,
  onNavigate,
  onHealthUpdate,
  onLocksChanged,
}: {
  active?: boolean;
  onNavigate: (tab: string) => void;
  onHealthUpdate?: (health: HealthSnapshot) => void;
  onLocksChanged?: () => void;
}) {
  const [health, setHealth] = useState<HealthSnapshot | null>(null);
  const [diagnostics, setDiagnostics] = useState<SystemDiagnostics | null>(null);
  const [ledger, setLedger] = useState<OrderLedgerPage | null>(null);
  const [queue, setQueue] = useState<QueueStatus | null>(null);
  const [flags, setFlags] = useState<RuntimeFlagsSnapshot | null>(null);
  const [billing, setBilling] = useState<FixedBillingSettings | null>(null);

  const [loading, setLoading] = useState(false);
  const [initialLoaded, setInitialLoaded] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null);
  const [fxBusy, setFxBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [feedTab, setFeedTab] = useState<FeedTab>("orders");
  const [orderFilter, setOrderFilter] = useState<OrderQuickFilter>("all");

  // Order detail & failure detail modals (interactive directly from overview)
  const [detailOrder, setDetailOrder] = useState<OrderLedgerItem | null>(null);
  const [detailEvents, setDetailEvents] = useState<QuickPayEvent[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [reconcilingId, setReconcilingId] = useState<string | null>(null);

  const [failureModalOrder, setFailureModalOrder] = useState<OrderLedgerItem | null>(null);
  const [failureDetail, setFailureDetail] = useState<OrderFailureDetail | null>(null);
  const [failureLoading, setFailureLoading] = useState(false);
  const [failureFetchError, setFailureFetchError] = useState<string | null>(null);
  const [verdictOrder, setVerdictOrder] = useState<OrderLedgerItem | null>(null);

  // Queue job detail modal
  const [selectedJob, setSelectedJob] = useState<QuickPayJob | null>(null);
  const [jobModalOpen, setJobModalOpen] = useState(false);
  const [jobLoading, setJobLoading] = useState(false);

  const loadAll = useCallback(async (signal?: AbortSignal, silent = false) => {
    if (!silent) setLoading(true);
    setError(null);
    try {
      const [h, d, l, q, fRes, bRes] = await Promise.all([
        getHealth(signal),
        getSystemDiagnostics(signal),
        getOrderLedger({ limit: 10, offset: 0 }, signal),
        getQuickPayQueue(12, signal),
        getRuntimeFlags(signal).catch(() => null),
        getFixedBillingSettings(signal).catch(() => null),
      ]);
      if (signal?.aborted) return;
      setHealth(h);
      setDiagnostics(d);
      setLedger(l);
      setQueue(q);
      if (fRes) setFlags(fRes);
      if (bRes) setBilling(bRes);
      setLastUpdatedAt(Date.now());
      setInitialLoaded(true);
      onHealthUpdate?.(h);
    } catch (exc) {
      if (isAbortError(exc)) return;
      setError(exc instanceof Error ? exc.message : "加载运维总览数据失败");
    } finally {
      if (!signal?.aborted && !silent) {
        setLoading(false);
      }
    }
  }, [onHealthUpdate]);

  const wasActiveRef = useRef(active);

  useEffect(() => {
    const controller = new AbortController();
    void loadAll(controller.signal);
    return () => controller.abort();
  }, [loadAll]);

  useEffect(() => {
    const prev = wasActiveRef.current;
    wasActiveRef.current = active;
    if (!prev && active) {
      const controller = new AbortController();
      void loadAll(controller.signal);
      return () => controller.abort();
    }
    return undefined;
  }, [active, loadAll]);

  // Lightweight 15s auto-refresh while overview tab is active and page is visible
  useEffect(() => {
    if (!active || !autoRefresh) return undefined;
    let currentCtrl: AbortController | null = null;
    const tick = () => {
      if (document.visibilityState !== "visible") return;
      currentCtrl?.abort();
      currentCtrl = new AbortController();
      void loadAll(currentCtrl.signal, true);
    };
    const timer = window.setInterval(tick, 15_000);
    return () => {
      window.clearInterval(timer);
      currentCtrl?.abort();
    };
  }, [active, autoRefresh, loadAll]);

  async function handleRefreshFx() {
    setFxBusy(true);
    setNotice(null);
    setError(null);
    try {
      const res = await refreshReferencePrices();
      setNotice(
        `实时汇率已同步（${res.status.fx_source || "-"} · 1 USD ≈ ¥${res.status.usd_cny_rate ?? "-"}）。`,
      );
      await loadAll();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "刷新实时汇率失败");
    } finally {
      setFxBusy(false);
    }
  }

  async function openOrderDetail(item: OrderLedgerItem) {
    setFailureModalOrder(null);
    setFailureDetail(null);
    setDetailOrder(item);
    setDetailEvents([]);
    setDetailLoading(true);
    try {
      const evs = await getOrderEvents(item.id);
      setDetailEvents(evs);
    } catch {
      setDetailEvents([]);
    } finally {
      setDetailLoading(false);
    }
  }

  async function openFailureDetail(item: OrderLedgerItem) {
    setDetailOrder(null);
    setFailureModalOrder(item);
    setFailureDetail(null);
    setFailureFetchError(null);
    setFailureLoading(true);
    try {
      const detail = await getOrderFailureDetail(item.id);
      setFailureDetail(detail);
    } catch (exc) {
      setFailureFetchError(exc instanceof Error ? exc.message : "读取失败诊断详情出错");
    } finally {
      setFailureLoading(false);
    }
  }

  async function handleReconcileOrder(orderId: string) {
    setReconcilingId(orderId);
    setNotice(null);
    setError(null);
    try {
      const res = await reconcileOrder(orderId);
      setNotice(`只读对账完成（${orderId.slice(0, 8)}）：${res.message}`);
      onLocksChanged?.();
      await loadAll();
      setDetailOrder((prev) => (prev && prev.id === orderId ? null : prev));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "触发只读对账失败");
    } finally {
      setReconcilingId(null);
    }
  }

  async function inspectJob(jobId: string) {
    setJobModalOpen(true);
    setJobLoading(true);
    setSelectedJob(null);
    try {
      const detail = await getQuickPay(jobId);
      setSelectedJob(detail);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "读取任务详情失败");
      setJobModalOpen(false);
    } finally {
      setJobLoading(false);
    }
  }

  const flagMap = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const item of flags?.items ?? []) {
      out[item.key] = item.value;
    }
    return out;
  }, [flags]);

  const stats = ledger?.stats;
  const activeLocks = stats?.active_locks ?? 0;
  const failedChecks = diagnostics?.checks.filter((c) => c.status === "fail") ?? [];
  const warnChecks = diagnostics?.checks.filter((c) => c.status === "warn") ?? [];
  const passChecks = diagnostics?.checks.filter((c) => c.status === "pass") ?? [];

  const totalOrders = stats?.total ?? 0;
  const deliveredCount = stats?.delivered ?? 0;
  const pendingDelivCount = stats?.paid_pending_delivery ?? 0;
  const unknownCount = stats?.unknown ?? 0;
  const failedCount = stats?.failed ?? 0;
  const paidCount = deliveredCount + pendingDelivCount;
  const deliveryRateNum = totalOrders > 0 ? Math.round((deliveredCount / totalOrders) * 100) : null;

  const avgDurationMs = useMemo(() => {
    const valid = (ledger?.items ?? [])
      .map((i) => i.total_ms)
      .filter((v): v is number => typeof v === "number" && Number.isFinite(v) && v > 0);
    if (valid.length === 0) return null;
    return Math.round(valid.reduce((a, b) => a + b, 0) / valid.length);
  }, [ledger]);

  const filteredOrders = useMemo(() => {
    const list = ledger?.items ?? [];
    if (orderFilter === "all") return list;
    if (orderFilter === "delivered") {
      return list.filter((i) => i.status === "delivered" || i.status === "succeeded");
    }
    if (orderFilter === "attention") {
      return list.filter(
        (i) =>
          i.status === "unknown"
          || i.status === "verifying"
          || i.status === "paid_pending_delivery"
          || i.status === "awaiting_payment"
          || i.status === "requires_action",
      );
    }
    return list.filter((i) => i.status === "failed" || i.status === "rejected" || i.status === "declined");
  }, [ledger, orderFilter]);

  const queueRunning = health?.queue.running ?? 0;
  const queueMax = Math.max(1, health?.queue.max_concurrency ?? 1);
  const queueQueued = health?.queue.queued ?? 0;
  const queuePct = Math.min(100, Math.round((queueRunning / queueMax) * 100));

  const heroStatusTone = !health
    ? "ok"
    : health.status !== "ok" || failedChecks.length > 0
      ? "error"
      : health.queue.draining || health.fake_adapters
        ? "warn"
        : "ok";

  const heroStatusText = !health
    ? "正在连接…"
    : health.queue.draining
      ? "排空中"
      : health.fake_adapters
        ? "离线模拟模式"
        : failedChecks.length > 0
          ? `存在 ${failedChecks.length} 项阻断`
          : `服务正常 · v${health.version}`;

  const directSessionOn = Boolean(flagMap.direct_session_checkout);
  const checkoutRotateOn = Boolean(flagMap.checkout_rotate);
  const plusEngineVal = String(flagMap.plus_engine || "har");
  const telemetryVal = String(flagMap.har_telemetry || "full");
  const osProfileVal = formatOsProfileLabel(flagMap.client_os_profile);

  return (
    <Stack gap="md">
      {/* 1. 顶部轻量页头与快捷操作条 */}
      <div className="ops-hero">
        <div>
          <div className="ops-hero-title-row">
            <h2 className="ops-hero-title">运维总览</h2>
            <span className={`ops-live-pill ops-live-pill--${heroStatusTone}`}>
              <span className="ops-pulse-dot" aria-hidden="true" />
              {heroStatusText}
            </span>
          </div>
          <div className="ops-hero-sub">
            <span>全局健康度、策略运行态、资金风控与近期订单动态</span>
            {lastUpdatedAt && (
              <span>· 最近同步 {formatClockHms(lastUpdatedAt)}</span>
            )}
          </div>
        </div>

        <div className="ops-toolbar">
          <Tooltip label={autoRefresh ? "每 15 秒自动刷新（页面切后台时暂停）" : "已暂停自动刷新，点击开启"} withArrow>
            <button
              type="button"
              className={`ops-auto-btn ${autoRefresh ? "ops-auto-btn--on" : ""}`}
              onClick={() => setAutoRefresh((v) => !v)}
            >
              <span className="ops-auto-dot" aria-hidden="true" />
              {autoRefresh ? "自动 15s" : "手动刷新"}
            </button>
          </Tooltip>
          <Button
            size="xs"
            variant="default"
            leftSection={<IconCurrencyDollar size={14} />}
            loading={fxBusy}
            onClick={() => void handleRefreshFx()}
          >
            刷新汇率
          </Button>
          <Button
            size="xs"
            variant="default"
            leftSection={<IconRefresh size={14} />}
            loading={loading}
            onClick={() => void loadAll()}
          >
            刷新
          </Button>
          <Button
            size="xs"
            leftSection={<IconBolt size={14} />}
            onClick={() => onNavigate("quick")}
          >
            内部代充
          </Button>
          <Tooltip label="打开用户端充值门户" withArrow>
            <ActionIcon
              component="a"
              href="/"
              target="_blank"
              rel="noopener noreferrer"
              variant="default"
              size={30}
              radius="md"
              aria-label="打开用户端充值门户"
            >
              <IconExternalLink size={15} />
            </ActionIcon>
          </Tooltip>
        </div>
      </div>

      {/* 2. 反馈与高优先级风险条（仅在确实存在需要人工介入的问题时出现） */}
      {notice && (
        <Alert color="teal" radius="md" icon={<IconCheck size={16} />} withCloseButton onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}
      {error && (
        <Alert color="red" radius="md" icon={<IconAlertCircle size={16} />} title="请求异常" withCloseButton onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {activeLocks > 0 && (
        <div className="ops-alert-strip ops-alert-strip--danger" role="alert">
          <div className="ops-alert-strip-left">
            <span className="ops-alert-icon"><IconLockOpen size={16} /></span>
            <div>
              <Text size="sm" fw={700} c="red">存在 {activeLocks} 个未核销资金锁</Text>
              <Text size="xs" c="dimmed">
                有待核对（unknown）订单正在锁定账号防重付，请执行只读对账或核实银行账单后再人工解锁。
              </Text>
            </div>
          </div>
          <Button size="xs" color="red" onClick={() => onNavigate("locks")}>
            前往资金对账
          </Button>
        </div>
      )}

      {failedChecks.length > 0 && (
        <div className="ops-alert-strip ops-alert-strip--danger" role="alert">
          <div className="ops-alert-strip-left">
            <span className="ops-alert-icon"><IconAlertCircle size={16} /></span>
            <div>
              <Text size="sm" fw={700} c="red">系统自检发现 {failedChecks.length} 项阻断异常</Text>
              <Text size="xs" c="dimmed" lineClamp={2}>
                {failedChecks.map((c) => `${c.label}：${c.message}`).join("；")}
              </Text>
            </div>
          </div>
          <Button size="xs" color="red" variant="light" onClick={() => onNavigate("diagnostics")}>
            查看系统自检
          </Button>
        </div>
      )}

      {(warnChecks.length > 0 || health?.reference_pricing.stale) && (
        <div className="ops-alert-strip ops-alert-strip--warn">
          <div className="ops-alert-strip-left">
            <span className="ops-alert-icon"><IconAlertTriangle size={16} /></span>
            <Text size="xs" fw={550}>
              {[
                warnChecks.length > 0 ? `系统自检有 ${warnChecks.length} 项提醒（${warnChecks[0].label}）` : null,
                health?.reference_pricing.stale ? "动态参考汇率已过期，建议刷新汇率" : null,
              ].filter(Boolean).join(" · ")}
            </Text>
          </div>
          <Group gap={6}>
            {health?.reference_pricing.stale && (
              <Button size="xs" color="orange" variant="light" loading={fxBusy} onClick={() => void handleRefreshFx()}>
                立即刷新汇率
              </Button>
            )}
            {warnChecks.length > 0 && (
              <Button size="xs" color="orange" variant="subtle" onClick={() => onNavigate("diagnostics")}>
                检查详情
              </Button>
            )}
          </Group>
        </div>
      )}

      {/* 3. 核心 4 格可点击 KPI 卡片 */}
      <div className="ops-kpi-grid">
        {/* KPI 1: 订单与交付 */}
        <button type="button" className="ops-kpi-card" onClick={() => onNavigate("history")}>
          <div className="ops-kpi-top">
            <span className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--teal"><IconReceipt size={16} /></span>
              <span className="ops-kpi-title">订单与交付</span>
            </span>
            <Badge size="xs" color={paidCount > 0 ? "teal" : "gray"} variant="light">
              {deliveryRateNum != null ? `交付 ${deliveryRateNum}%` : "暂无数据"}
            </Badge>
          </div>

          {!initialLoaded ? (
            <Skeleton height={28} width="55%" radius="sm" />
          ) : (
            <div className="ops-kpi-main">
              <span className="ops-kpi-value">{totalOrders}</span>
              <span className="ops-kpi-unit">单 · 已付 {paidCount}</span>
            </div>
          )}

          <div>
            <div className="ops-kpi-bar" aria-label="订单状态分布条">
              {totalOrders > 0 ? (
                <>
                  {deliveredCount > 0 && (
                    <div
                      className="ops-kpi-bar-seg"
                      style={{ width: `${(deliveredCount / totalOrders) * 100}%`, background: "#22c55e" }}
                    />
                  )}
                  {pendingDelivCount > 0 && (
                    <div
                      className="ops-kpi-bar-seg"
                      style={{ width: `${(pendingDelivCount / totalOrders) * 100}%`, background: "#f59e0b" }}
                    />
                  )}
                  {unknownCount > 0 && (
                    <div
                      className="ops-kpi-bar-seg"
                      style={{ width: `${(unknownCount / totalOrders) * 100}%`, background: "#3b82f6" }}
                    />
                  )}
                  {failedCount > 0 && (
                    <div
                      className="ops-kpi-bar-seg"
                      style={{ width: `${(failedCount / totalOrders) * 100}%`, background: "#ef4444" }}
                    />
                  )}
                </>
              ) : null}
            </div>
            <div className="ops-kpi-foot" style={{ marginTop: 8 }}>
              <div className="ops-kpi-legend">
                <span className="ops-kpi-legend-item">
                  <span className="ledger-dot ledger-dot--success" />交付 {deliveredCount}
                </span>
                <span className="ops-kpi-legend-item">
                  <span className="ledger-dot ledger-dot--warning" />待交付 {pendingDelivCount}
                </span>
                <span className="ops-kpi-legend-item">
                  <span className="ledger-dot ledger-dot--pending" />核对 {unknownCount}
                </span>
                <span className="ops-kpi-legend-item">
                  <span className="ledger-dot ledger-dot--failed" />失败 {failedCount}
                </span>
              </div>
              <IconArrowRight size={14} className="ops-kpi-arrow" />
            </div>
          </div>
        </button>

        {/* KPI 2: 并发队列与耗时 */}
        <button type="button" className="ops-kpi-card" onClick={() => onNavigate("quick")}>
          <div className="ops-kpi-top">
            <span className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--blue"><IconBolt size={16} /></span>
              <span className="ops-kpi-title">并发队列负载</span>
            </span>
            <Badge
              size="xs"
              color={health?.queue.draining ? "orange" : queueRunning > 0 ? "blue" : "gray"}
              variant="light"
            >
              {health?.queue.draining ? "排空中" : queueRunning > 0 ? "执行中" : "空闲"}
            </Badge>
          </div>

          {!initialLoaded ? (
            <Skeleton height={28} width="50%" radius="sm" />
          ) : (
            <div className="ops-kpi-main">
              <span className="ops-kpi-value">{queueRunning} / {queueMax}</span>
              <span className="ops-kpi-unit">排队 {queueQueued}</span>
            </div>
          )}

          <div>
            <div className="ops-kpi-bar" aria-label="并发槽位占用">
              {queuePct > 0 && (
                <div
                  className="ops-kpi-bar-seg"
                  style={{
                    width: `${queuePct}%`,
                    background: queuePct >= 90 ? "#f59e0b" : "#246be8",
                  }}
                />
              )}
            </div>
            <div className="ops-kpi-foot" style={{ marginTop: 8 }}>
              <span>
                {avgDurationMs != null ? `近期均耗 ${formatDurationMs(avgDurationMs)}` : "可在「策略开关」热调并发"}
              </span>
              <IconArrowRight size={14} className="ops-kpi-arrow" />
            </div>
          </div>
        </button>

        {/* KPI 3: 资金锁与后台对账 */}
        <button
          type="button"
          className={`ops-kpi-card ${activeLocks > 0 ? "ops-kpi-card--danger" : ""}`}
          onClick={() => onNavigate("locks")}
        >
          <div className="ops-kpi-top">
            <span className="ops-kpi-label-wrap">
              <span className={`ops-kpi-icon ${activeLocks > 0 ? "ops-kpi-icon--red" : "ops-kpi-icon--emerald"}`}>
                <IconLockOpen size={16} />
              </span>
              <span className="ops-kpi-title">资金锁与对账器</span>
            </span>
            <Badge size="xs" color={health?.reconciler.running ? "green" : "gray"} variant="light">
              {health?.reconciler.running ? `对账 ${health.reconciler.interval_s}s` : "已停用"}
            </Badge>
          </div>

          {!initialLoaded ? (
            <Skeleton height={28} width="50%" radius="sm" />
          ) : (
            <div className="ops-kpi-main">
              <span className={`ops-kpi-value ${activeLocks > 0 ? "ops-kpi-value--danger" : "ops-kpi-value--ok"}`}>
                {activeLocks}
              </span>
              <span className="ops-kpi-unit">
                {activeLocks > 0 ? "个账号锁定中" : "无活跃资金锁"}
              </span>
            </div>
          )}

          <div className="ops-kpi-foot">
            <span>
              {health
                ? `待复核 ${health.reconciler.pending_candidates} 单 · 累计核销 ${health.reconciler.resolved_count} 单`
                : "正在读取对账状态…"}
            </span>
            <IconArrowRight size={14} className="ops-kpi-arrow" />
          </div>
        </button>

        {/* KPI 4: 动态汇率同步 */}
        <button type="button" className="ops-kpi-card" onClick={() => onNavigate("pricing")}>
          <div className="ops-kpi-top">
            <span className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--indigo"><IconCurrencyDollar size={16} /></span>
              <span className="ops-kpi-title">实时参考汇率</span>
            </span>
            <Badge
              size="xs"
              color={health?.reference_pricing.stale ? "orange" : "green"}
              variant="light"
            >
              {health?.reference_pricing.stale ? "待刷新" : "已同步"}
            </Badge>
          </div>

          {!initialLoaded ? (
            <Skeleton height={28} width="60%" radius="sm" />
          ) : (
            <div className="ops-kpi-main">
              <span className="ops-kpi-value">
                {health?.reference_pricing.usd_cny_rate ? `¥${health.reference_pricing.usd_cny_rate}` : "-"}
              </span>
              <span className="ops-kpi-unit">/ 1 USD</span>
            </div>
          )}

          <div className="ops-kpi-foot">
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {health
                ? `${health.reference_pricing.fx_source} · ${health.reference_pricing.updated_label || "未记录"}`
                : "正在同步汇率…"}
            </span>
            <IconArrowRight size={14} className="ops-kpi-arrow" />
          </div>
        </button>
      </div>

      {/* 4. 中部双栏 Bento：左侧策略与配置快照 + 右侧系统自检与存储健康 */}
      <div className="ops-bento-grid">
        {/* 左栏：核心运行策略与环境快照 */}
        <section className="ops-panel">
          <div className="ops-panel-head">
            <div className="ops-panel-title-wrap">
              <IconAdjustments size={16} stroke={1.9} />
              <h3 className="ops-panel-title">运行策略与环境快照</h3>
              {flags && flags.override_count > 0 && (
                <Badge size="xs" color="blue" variant="light">
                  {flags.override_count} 项自定义覆盖
                </Badge>
              )}
            </div>
            <Button size="xs" variant="subtle" onClick={() => onNavigate("flags")}>
              调整策略
            </Button>
          </div>

          <div className="ops-config-grid">
            <button type="button" className="ops-config-tile" onClick={() => onNavigate("flags")}>
              <div className="ops-config-head">
                <span className="ops-config-label">直通建单模式</span>
                <Badge size="xs" color={directSessionOn ? "teal" : "gray"} variant="light">
                  {directSessionOn ? "已开启" : "已关闭"}
                </Badge>
              </div>
              <div className="ops-config-val">
                {directSessionOn ? "保留原 oai-did 直通" : "出口重颁新 AT"}
              </div>
              <div className="ops-config-hint">
                {directSessionOn ? "多账号导出请确保浏览器 Cookie 隔离" : "使用 ST 在目标出口重签发"}
              </div>
            </button>

            <button type="button" className="ops-config-tile" onClick={() => onNavigate("flags")}>
              <div className="ops-config-head">
                <span className="ops-config-label">建单代理自愈</span>
                <Badge size="xs" color={checkoutRotateOn ? "teal" : "gray"} variant="light">
                  {checkoutRotateOn ? "已开启" : "已关闭"}
                </Badge>
              </div>
              <div className="ops-config-val">
                {checkoutRotateOn ? "隧道失败自动换 SID" : "单出口直连不切换"}
              </div>
              <div className="ops-config-hint">
                系统画像：{osProfileVal}
              </div>
            </button>

            <button type="button" className="ops-config-tile" onClick={() => onNavigate("flags")}>
              <div className="ops-config-head">
                <span className="ops-config-label">协议引擎与遥测</span>
                <Badge size="xs" color={plusEngineVal === "har" ? "blue" : "orange"} variant="light">
                  {plusEngineVal.toUpperCase()}
                </Badge>
              </div>
              <div className="ops-config-val">
                {plusEngineVal === "har" ? "HAR 逐请求复刻" : "Legacy 旧版直付"}
              </div>
              <div className="ops-config-hint">
                Stripe 遥测：{telemetryVal === "full" ? "完整指纹 + 遥测回放" : "精简/关闭"}
              </div>
            </button>

            <button type="button" className="ops-config-tile" onClick={() => onNavigate("billing")}>
              <div className="ops-config-head">
                <span className="ops-config-label">固定美国免税账单</span>
                <Badge
                  size="xs"
                  color={billing?.configured && billing?.valid ? "green" : "red"}
                  variant="light"
                >
                  {billing?.configured && billing?.valid ? "已就绪" : "待配置"}
                </Badge>
              </div>
              <div className="ops-config-val">
                {billing?.address
                  ? `${billing.address.country} · ${billing.address.state}（免税州）`
                  : "未检测到有效账单"}
              </div>
              <div className="ops-config-hint">
                {billing?.strict_exit_match ? "严格匹配出口国" : "出口国家自由选择，账单固定 US"}
              </div>
            </button>

            <button type="button" className="ops-config-tile" onClick={() => onNavigate("proxy")}>
              <div className="ops-config-head">
                <span className="ops-config-label">出口代理 SID 策略</span>
                <Badge size="xs" color={health?.proxy_sid_pinned ? "blue" : "teal"} variant="light">
                  {health?.proxy_sid_pinned ? "全局固定" : "按单分配"}
                </Badge>
              </div>
              <div className="ops-config-val">
                {health?.proxy_sid_pinned ? "共享固定 GSS_PROXY_SID" : "单笔随机固定 SID"}
              </div>
              <div className="ops-config-hint">
                查号/核验/关续费遇 TLS 断连自动换节点
              </div>
            </button>

            <button type="button" className="ops-config-tile" onClick={() => onNavigate("webhooks")}>
              <div className="ops-config-head">
                <span className="ops-config-label">告警通知（TG / Webhook）</span>
                <Badge
                  size="xs"
                  color={health?.webhooks.enabled && health?.webhooks.configured ? "green" : "gray"}
                  variant="light"
                >
                  {health?.webhooks.enabled && health?.webhooks.configured
                    ? (health.webhooks.active_channels?.length
                      ? health.webhooks.active_channels
                          .map((c) => (c === "telegram" ? "Telegram" : "Webhook"))
                          .join(" + ")
                      : "已启用")
                    : "未启用"}
                </Badge>
              </div>
              <div className="ops-config-val">
                {health ? `近期 ${health.webhooks.recent_deliveries} 次投递` : "-"}
              </div>
              <div className="ops-config-hint">
                {health ? `已订阅 ${health.webhooks.subscribed_events} 类事件` : "配置异常实时推送"}
              </div>
            </button>
          </div>
        </section>

        {/* 右栏：系统自检与存储健康 */}
        <section className="ops-panel">
          <div className="ops-panel-head">
            <div className="ops-panel-title-wrap">
              <IconActivity size={16} stroke={1.9} />
              <h3 className="ops-panel-title">系统健康与存储</h3>
              {diagnostics && (
                <Badge size="xs" color={diagnostics.ok ? "green" : "red"} variant="light">
                  {passChecks.length}/{diagnostics.checks.length} 通过
                </Badge>
              )}
            </div>
            <Button size="xs" variant="subtle" onClick={() => onNavigate("diagnostics")}>
              自检维护
            </Button>
          </div>

          <div className="ops-storage-strip">
            <div className="ops-storage-chip">
              <div className="ops-storage-k">SQLite 主库</div>
              <div className="ops-storage-v">
                {diagnostics ? formatBytes(diagnostics.db_bytes) : "-"}
              </div>
            </div>
            <div className="ops-storage-chip">
              <div className="ops-storage-k">WAL 日志</div>
              <div className="ops-storage-v">
                {diagnostics ? formatBytes(diagnostics.wal_bytes) : "-"}
              </div>
            </div>
            <div className="ops-storage-chip">
              <div className="ops-storage-k">磁盘可用空间</div>
              <div className="ops-storage-v">
                {diagnostics ? formatDiskSpace(diagnostics.disk_free_mb) : "-"}
              </div>
            </div>
          </div>

          <div className="ops-checks-list">
            {!diagnostics ? (
              <>
                <Skeleton height={32} radius="sm" />
                <Skeleton height={32} radius="sm" />
                <Skeleton height={32} radius="sm" />
              </>
            ) : (
              diagnostics.checks.map((chk) => {
                const dotClass = chk.status === "pass"
                  ? "ledger-dot--success"
                  : chk.status === "warn"
                    ? "ledger-dot--warning"
                    : "ledger-dot--failed";
                return (
                  <div key={chk.key} className="ops-check-row" title={chk.message}>
                    <span className="ops-check-left">
                      <span className={`ledger-dot ${dotClass}`} />
                      <span>{chk.label}</span>
                    </span>
                    <span className="ops-check-msg">{chk.message}</span>
                  </div>
                );
              })
            )}
          </div>
        </section>
      </div>

      {/* 5. 底部统一业务动态面板：近期订单（可点击看详情/失败诊断/对账） + 持久化任务队列 */}
      <section className="ops-panel">
        <div className="ops-panel-head">
          <Group gap="sm" align="center" wrap="wrap">
            <SegmentedControl
              size="xs"
              value={feedTab}
              onChange={(v) => setFeedTab(v as FeedTab)}
              data={[
                { value: "orders", label: `近期订单 (${ledger?.items.length ?? 0})` },
                {
                  value: "queue",
                  label: queueRunning > 0
                    ? `任务队列 (${queueRunning} 运行中)`
                    : `任务队列 (${queue?.jobs.length ?? 0})`,
                },
              ]}
            />

            {feedTab === "orders" && (
              <div className="ops-filter-pills" role="group" aria-label="近期订单筛选">
                {(
                  [
                    ["all", "全部"],
                    ["delivered", "已交付"],
                    ["attention", "待处理"],
                    ["failed", "失败"],
                  ] as Array<[OrderQuickFilter, string]>
                ).map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    className={`ops-filter-pill ${orderFilter === key ? "ops-filter-pill--active" : ""}`}
                    onClick={() => setOrderFilter(key)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            )}
          </Group>

          <Group gap="xs">
            {feedTab === "orders" ? (
              <Button size="xs" variant="subtle" rightSection={<IconArrowRight size={13} />} onClick={() => onNavigate("history")}>
                完整订单账本
              </Button>
            ) : (
              <Button size="xs" variant="subtle" rightSection={<IconArrowRight size={13} />} onClick={() => onNavigate("quick")}>
                前往内部代充
              </Button>
            )}
          </Group>
        </div>

        {feedTab === "orders" ? (
          !ledger || filteredOrders.length === 0 ? (
            <Text size="sm" c="dimmed" py="xl" ta="center">
              {loading ? "正在加载近期订单…" : "当前筛选条件下暂无订单记录。"}
            </Text>
          ) : (
            <Table.ScrollContainer minWidth={880}>
              <Table className="ledger-clean-table" highlightOnHover={false}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>订单号</Table.Th>
                    <Table.Th>账号</Table.Th>
                    <Table.Th>套餐</Table.Th>
                    <Table.Th>路由</Table.Th>
                    <Table.Th>状态</Table.Th>
                    <Table.Th>自动续费</Table.Th>
                    <Table.Th>金额</Table.Th>
                    <Table.Th>总耗时</Table.Th>
                    <Table.Th>创建时间</Table.Th>
                    <Table.Th style={{ textAlign: "right" }}>操作</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {filteredOrders.map((item) => {
                    const emailText = item.account_email || item.masked_email || null;
                    const amountText = formatAmount(item.amount, item.currency);
                    const statusInfo = simpleOrderStatus(item.status);
                    const durationClock = formatDurationClock(item.total_ms);
                    const planTone = planPillTone(item.plan);
                    const failPresent = hasFailureReason(item);
                    const exitCode = (item.exit_country || "").toUpperCase();
                    const billCode = (item.billing_country || "").toUpperCase();
                    const routeText = exitCode && billCode && exitCode !== billCode
                      ? `${exitCode} → ${billCode}`
                      : exitCode || billCode || "-";

                    return (
                      <Table.Tr
                        key={item.id}
                        className="ops-clickable-row"
                        onClick={() => void openOrderDetail(item)}
                      >
                        <Table.Td onClick={(e) => e.stopPropagation()}>
                          <CopyIdBadge value={item.id} prefixLength={8} />
                        </Table.Td>
                        <Table.Td>
                          {emailText ? (
                            <div className="ledger-email" title={emailText}>{emailText}</div>
                          ) : (
                            <span className="ledger-muted">{item.account_tag ? `#${item.account_tag}` : "-"}</span>
                          )}
                        </Table.Td>
                        <Table.Td>
                          <span className={`ledger-pill ledger-pill--plan ledger-pill--plan-${planTone}`}>
                            {planLabel(item.plan)}
                          </span>
                        </Table.Td>
                        <Table.Td>
                          <span className="ops-route-tag">{routeText}</span>
                        </Table.Td>
                        <Table.Td>
                          <div>
                            <span className={`ledger-pill ledger-pill--status-${statusInfo.tone}`}>
                              <span className={`ledger-dot ledger-dot--${statusInfo.tone}`} />
                              {statusInfo.label}
                            </span>
                            {item.failure_code && (
                              <div className="ops-fail-code-sub" title={FAILURE_LABEL[item.failure_code] || item.failure_code}>
                                {item.failure_code}
                              </div>
                            )}
                          </div>
                        </Table.Td>
                        <Table.Td style={{ whiteSpace: "nowrap" }}>
                          {item.auto_renew_status && item.auto_renew_status !== "disabled" ? (
                            <Badge
                              size="xs"
                              variant="light"
                              color={autoRenewColor(item.auto_renew_status)}
                              styles={{ label: { overflow: "visible" } }}
                            >
                              {autoRenewLabel(item.auto_renew_status)}
                            </Badge>
                          ) : (
                            <span className="ledger-muted">-</span>
                          )}
                        </Table.Td>
                        <Table.Td>
                          {amountText ? (
                            <span className="ledger-amount">{amountText}</span>
                          ) : (
                            <span className="ledger-muted">-</span>
                          )}
                        </Table.Td>
                        <Table.Td>
                          {durationClock ? (
                            <span className="ledger-duration">
                              <IconClock size={13} stroke={1.8} />
                              {durationClock}
                            </span>
                          ) : (
                            <span className="ledger-muted">-</span>
                          )}
                        </Table.Td>
                        <Table.Td>
                          <span className="ledger-time">{formatMonthDayTime(item.created_at)}</span>
                        </Table.Td>
                        <Table.Td style={{ textAlign: "right" }} onClick={(e) => e.stopPropagation()}>
                          <Group gap={4} justify="flex-end" wrap="nowrap">
                            {failPresent && (
                              <Button
                                size="compact-xs"
                                variant="light"
                                color="red"
                                onClick={() => void openFailureDetail(item)}
                              >
                                诊断
                              </Button>
                            )}
                            <Button
                              size="compact-xs"
                              variant="subtle"
                              onClick={() => void openOrderDetail(item)}
                            >
                              详情
                            </Button>
                          </Group>
                        </Table.Td>
                      </Table.Tr>
                    );
                  })}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          )
        ) : !queue || queue.jobs.length === 0 ? (
          <Text size="sm" c="dimmed" py="xl" ta="center">
            {loading ? "正在加载持久化任务队列…" : "近期暂无后台任务记录。"}
          </Text>
        ) : (
          <Table.ScrollContainer minWidth={720}>
            <Table className="ledger-clean-table" highlightOnHover={false}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>作业 ID</Table.Th>
                  <Table.Th>状态与当前阶段</Table.Th>
                  <Table.Th>报价金额</Table.Th>
                  <Table.Th>总耗时</Table.Th>
                  <Table.Th>创建时间</Table.Th>
                  <Table.Th style={{ textAlign: "right" }}>操作</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {queue.jobs.map((item) => {
                  const failHint = item.status === "failed"
                    ? (item.failure_code ? (FAILURE_LABEL[item.failure_code] || item.failure_code) : "点击查看失败诊断")
                    : null;
                  const jobTone = item.status === "succeeded"
                    ? "success"
                    : item.status === "failed"
                      ? "failed"
                      : item.status === "unknown" || item.status === "requires_action"
                        ? "warning"
                        : "pending";
                  const totalClock = formatDurationClock(item.timings_ms?.total_ms);

                  return (
                    <Table.Tr
                      key={item.job_id}
                      className="ops-clickable-row"
                      onClick={() => void inspectJob(item.job_id)}
                    >
                      <Table.Td onClick={(e) => e.stopPropagation()}>
                        <CopyIdBadge value={item.job_id} prefixLength={10} />
                      </Table.Td>
                      <Table.Td>
                        <Stack gap={2}>
                          <Group gap={6} wrap="nowrap">
                            <span className={`ledger-pill ledger-pill--status-${jobTone}`}>
                              <span className={`ledger-dot ledger-dot--${jobTone}`} />
                              {item.status === "queued" && item.queue_position
                                ? `排队 #${item.queue_position}`
                                : STAGE_LABEL[item.stage] || item.stage}
                            </span>
                            {item.failure_code && (
                              <Badge size="xs" color="red" variant="outline">{item.failure_code}</Badge>
                            )}
                          </Group>
                          {failHint && (
                            <Text size="xs" c="red" lineClamp={1}>{failHint}</Text>
                          )}
                        </Stack>
                      </Table.Td>
                      <Table.Td>
                        {item.amount ? (
                          <span className="ledger-amount">{formatAmount(item.amount, item.currency)}</span>
                        ) : (
                          <span className="ledger-muted">-</span>
                        )}
                      </Table.Td>
                      <Table.Td>
                        {totalClock ? (
                          <span className="ledger-duration">
                            <IconClock size={13} stroke={1.8} />
                            {totalClock}
                          </span>
                        ) : (
                          <span className="ledger-muted">-</span>
                        )}
                      </Table.Td>
                      <Table.Td>
                        <span className="ledger-time">{formatMonthDayTime(item.created_at)}</span>
                      </Table.Td>
                      <Table.Td style={{ textAlign: "right" }}>
                        <Button size="compact-xs" variant="subtle">查看</Button>
                      </Table.Td>
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
      </section>

      {/* 弹窗 1：完整订单详情（含总耗时细分条、时间线、一键只读对账与人工判定） */}
      <OrderLedgerDetailModal
        order={detailOrder}
        events={detailEvents}
        eventsLoading={detailLoading}
        reconciling={detailOrder != null && reconcilingId === detailOrder.id}
        onClose={() => setDetailOrder(null)}
        onReconcile={(id) => void handleReconcileOrder(id)}
        onOpenFailureDetail={(item) => void openFailureDetail(item)}
        onManualVerdict={(item) => {
          setDetailOrder(null);
          setVerdictOrder(item);
        }}
      />

      {/* 弹窗 1b：人工判定订单状态 */}
      <OrderManualVerdictModal
        order={verdictOrder}
        onClose={() => setVerdictOrder(null)}
        onSuccess={(res) => {
          setVerdictOrder(null);
          setNotice(`订单 ${res.order_id.slice(0, 8)} ${res.message}（${res.previous_status} → ${res.order_status}）。`);
          onLocksChanged?.();
          void loadAll();
        }}
      />

      {/* 弹窗 2：共享结构化失败诊断详情 */}
      <OrderFailureDetailModal
        order={failureModalOrder}
        detail={failureDetail}
        loading={failureLoading}
        fetchError={failureFetchError}
        onClose={() => {
          setFailureModalOrder(null);
          setFailureDetail(null);
          setFailureFetchError(null);
        }}
        onOpenAuditDetail={(ord) => void openOrderDetail(ord)}
      />

      {/* 弹窗 3：任务队列详情（使用语法高亮 JsonCodeBlock 与人性化耗时格式） */}
      <Modal
        opened={jobModalOpen}
        onClose={() => setJobModalOpen(false)}
        title={
          <Group gap="xs" align="center">
            <span className="order-detail-title-icon" aria-hidden="true">
              <IconShieldCheck size={17} stroke={1.9} />
            </span>
            <Text fw={700} size="md">任务队列详情（只读脱敏）</Text>
          </Group>
        }
        size="lg"
        radius="lg"
        padding="lg"
        centered
      >
        {jobLoading ? (
          <Stack gap="sm" py="md">
            <Skeleton height={24} width="45%" />
            <Skeleton height={80} />
          </Stack>
        ) : selectedJob ? (
          <Stack gap="md">
            <Group gap="xs" wrap="wrap">
              <Badge color={selectedJob.status === "succeeded" ? "green" : selectedJob.status === "failed" ? "red" : "blue"}>
                {selectedJob.status}
              </Badge>
              <Badge variant="outline">{STAGE_LABEL[selectedJob.stage] || selectedJob.stage}</Badge>
              {selectedJob.failure_code && (
                <Badge color="red" variant="light">{selectedJob.failure_code}</Badge>
              )}
              {selectedJob.amount && (
                <Badge color="teal" variant="light">
                  {formatAmount(selectedJob.amount, selectedJob.currency)}
                </Badge>
              )}
            </Group>

            <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="xs">
              <div className="order-kpi-card">
                <span className="order-kpi-label">作业 / 订单号</span>
                <Group gap={6} mt={6} wrap="wrap">
                  <CopyIdBadge value={selectedJob.job_id} prefixLength={10} />
                  {selectedJob.order_id && <CopyIdBadge value={selectedJob.order_id} prefixLength={8} />}
                </Group>
              </div>
              <div className="order-kpi-card">
                <span className="order-kpi-label">重试 / 对账 / 续费</span>
                <span className="order-kpi-value order-kpi-value--sm">
                  重试 {selectedJob.attempts ?? 0} · 对账 {selectedJob.reconcile_attempts ?? 0} · {autoRenewLabel(selectedJob.auto_renew_status || "disabled")}
                </span>
              </div>
            </SimpleGrid>

            {selectedJob.message && (
              <Alert color={selectedJob.status === "failed" ? "red" : "blue"} radius="md">
                {selectedJob.message}
              </Alert>
            )}

            {selectedJob.timings_ms && Object.keys(selectedJob.timings_ms).length > 0 && (
              <div>
                <Text size="xs" fw={700} c="dimmed" mb={6}>阶段耗时</Text>
                <Group gap={6} wrap="wrap">
                  {Object.entries(selectedJob.timings_ms).map(([k, v]) => (
                    <Badge key={k} size="sm" variant="light" color="gray">
                      {TIMING_LABEL[k] || k}：{formatDurationMs(v)}
                    </Badge>
                  ))}
                </Group>
              </div>
            )}

            {selectedJob.diagnostics && Object.keys(selectedJob.diagnostics).length > 0 && (
              <JsonCodeBlock label="上游诊断摘要" value={selectedJob.diagnostics} />
            )}
          </Stack>
        ) : null}
      </Modal>
    </Stack>
  );
}
