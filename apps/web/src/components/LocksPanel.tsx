import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Code,
  Group,
  Modal,
  SegmentedControl,
  Skeleton,
  Stack,
  Table,
  Text,
  Textarea,
  TextInput,
  Tooltip,
} from "@mantine/core";
import {
  IconActivity,
  IconCheck,
  IconLockOpen,
  IconReceipt,
  IconRefresh,
  IconShieldCheck,
} from "@tabler/icons-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { formatAmount } from "../api/client";
import { isAbortError } from "../api/http";
import {
  getOrderEvents,
  getOrderFailureDetail,
  getOrderLedger,
  getPaymentLocks,
  getReconcilerStatus,
  reconcileOrder,
  reconcilePaymentLock,
  releasePaymentLockManual,
  type OrderFailureDetail,
  type OrderLedgerItem,
  type PaymentLockSummary,
  type PaymentLocksPage,
  type ReconcilerStatus,
} from "../api/ledger";
import type { QuickPayEvent } from "../api/quickPay";
import { planLabel } from "../plans";
import { formatMonthDayTime, planPillTone } from "../utils/orderLedgerFormat";
import { AdminPageHeader } from "./AdminPageHeader";
import { CopyIdBadge } from "./CopyIdBadge";
import { OrderFailureDetailModal } from "./OrderFailureDetailModal";
import { OrderLedgerDetailModal } from "./OrderLedgerDetailModal";

const STATE_LABEL: Record<string, { label: string; tone: "failed" | "success" | "pending" }> = {
  held: { label: "锁定中", tone: "failed" },
  resolved: { label: "已核销", tone: "success" },
  released: { label: "已解锁", tone: "pending" },
};

export function LocksPanel({
  active = true,
  onCountChange,
  onLocksChanged,
}: {
  active?: boolean;
  onCountChange?: (heldCount: number) => void;
  onLocksChanged?: () => void;
}) {
  const [stateFilter, setStateFilter] = useState("held");
  const [locksPage, setLocksPage] = useState<PaymentLocksPage | null>(null);
  const [reconciler, setReconciler] = useState<ReconcilerStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyLockId, setBusyLockId] = useState<string | null>(null);
  const [batchReconciling, setBatchReconciling] = useState<string | null>(null);

  const [releaseTarget, setReleaseTarget] = useState<PaymentLockSummary | null>(null);
  const [releaseConfirmed, setReleaseConfirmed] = useState(false);
  const [releaseOperator, setReleaseOperator] = useState("web-operator");
  const [releaseNote, setReleaseNote] = useState("");
  const [releaseBusy, setReleaseBusy] = useState(false);

  // Order detail & failure diagnostic modals directly accessible from lock rows
  const [detailOrder, setDetailOrder] = useState<OrderLedgerItem | null>(null);
  const [detailEvents, setDetailEvents] = useState<QuickPayEvent[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [reconcilingOrderId, setReconcilingOrderId] = useState<string | null>(null);
  const [inspectBusyOrderId, setInspectBusyOrderId] = useState<string | null>(null);

  const [failureModalOrder, setFailureModalOrder] = useState<OrderLedgerItem | null>(null);
  const [failureDetail, setFailureDetail] = useState<OrderFailureDetail | null>(null);
  const [failureLoading, setFailureLoading] = useState(false);
  const [failureFetchError, setFailureFetchError] = useState<string | null>(null);

  const loadControllerRef = useRef<AbortController | null>(null);
  const wasActiveRef = useRef(active);

  const load = useCallback(async () => {
    loadControllerRef.current?.abort();
    const controller = new AbortController();
    loadControllerRef.current = controller;

    setLoading(true);
    setError(null);
    try {
      const [page, rec] = await Promise.all([
        getPaymentLocks({ state: stateFilter, limit: 50, offset: 0 }, controller.signal),
        getReconcilerStatus(controller.signal),
      ]);
      if (loadControllerRef.current === controller) {
        setLocksPage(page);
        setReconciler(rec);
        onCountChange?.(page.counts?.held ?? 0);
      }
    } catch (exc) {
      if (isAbortError(exc)) return;
      setError(exc instanceof Error ? exc.message : "加载资金锁状态失败");
    } finally {
      if (loadControllerRef.current === controller) {
        setLoading(false);
      }
    }
  }, [stateFilter, onCountChange]);

  useEffect(() => {
    void load();
    return () => {
      loadControllerRef.current?.abort();
    };
  }, [load]);

  useEffect(() => {
    const prev = wasActiveRef.current;
    wasActiveRef.current = active;
    if (!prev && active) {
      void load();
    }
  }, [active, load]);

  async function handleReconcile(lockId: string) {
    setBusyLockId(lockId);
    setError(null);
    setNotice(null);
    try {
      const res = await reconcilePaymentLock(lockId);
      setNotice(`资金锁 ${lockId.slice(0, 8)} 只读对账结果：${res.message}`);
      await load();
      onLocksChanged?.();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "只读对账失败");
    } finally {
      setBusyLockId(null);
    }
  }

  async function handleBatchReconcileHeld() {
    const heldItems = (locksPage?.items ?? []).filter((item) => item.state === "held");
    if (heldItems.length === 0) return;
    setError(null);
    setNotice(null);
    let changedCount = 0;
    let failedCount = 0;
    for (let i = 0; i < heldItems.length; i += 1) {
      const target = heldItems[i];
      setBatchReconciling(`${i + 1}/${heldItems.length}`);
      try {
        const res = await reconcilePaymentLock(target.id);
        if (res.changed) changedCount += 1;
      } catch {
        failedCount += 1;
      }
    }
    setBatchReconciling(null);
    setNotice(
      `批量只读对账完成：共核对 ${heldItems.length} 笔，状态收敛 ${changedCount} 笔${failedCount > 0 ? `，请求异常 ${failedCount} 笔` : ""}。`,
    );
    await load();
    onLocksChanged?.();
  }

  async function handleRelease() {
    if (!releaseTarget || !releaseConfirmed || !releaseNote.trim()) return;
    setReleaseBusy(true);
    setError(null);
    try {
      await releasePaymentLockManual(releaseTarget.id, releaseNote.trim(), releaseOperator);
      setReleaseTarget(null);
      setReleaseConfirmed(false);
      setReleaseNote("");
      setNotice("已记录人工核对备注并释放该账号资金锁。");
      await load();
      onLocksChanged?.();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "解锁失败");
    } finally {
      setReleaseBusy(false);
    }
  }

  async function openLinkedOrder(orderId: string) {
    setInspectBusyOrderId(orderId);
    setError(null);
    try {
      const page = await getOrderLedger({ search: orderId, limit: 5, offset: 0 });
      const match = page.items.find((o) => o.id === orderId) ?? page.items[0] ?? null;
      if (!match) {
        setError(`未在服务端账本中找到订单 ${orderId.slice(0, 8)}。`);
        return;
      }
      setFailureModalOrder(null);
      setFailureDetail(null);
      setDetailOrder(match);
      setDetailEvents([]);
      setDetailLoading(true);
      try {
        const evs = await getOrderEvents(match.id);
        setDetailEvents(evs);
      } catch {
        setDetailEvents([]);
      } finally {
        setDetailLoading(false);
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "读取关联订单详情失败");
    } finally {
      setInspectBusyOrderId(null);
    }
  }

  async function openFailureDetail(order: OrderLedgerItem) {
    setDetailOrder(null);
    setFailureModalOrder(order);
    setFailureDetail(null);
    setFailureFetchError(null);
    setFailureLoading(true);
    try {
      const detail = await getOrderFailureDetail(order.id);
      setFailureDetail(detail);
    } catch (exc) {
      setFailureFetchError(exc instanceof Error ? exc.message : "读取失败诊断详情出错");
    } finally {
      setFailureLoading(false);
    }
  }

  async function handleReconcileFromDetail(orderId: string) {
    setReconcilingOrderId(orderId);
    setError(null);
    setNotice(null);
    try {
      const res = await reconcileOrder(orderId);
      setNotice(`订单 ${orderId.slice(0, 8)} 只读对账结果：${res.message}`);
      await load();
      onLocksChanged?.();
      setDetailOrder(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "触发只读对账失败");
    } finally {
      setReconcilingOrderId(null);
    }
  }

  const counts = locksPage?.counts ?? { held: 0, resolved: 0, released: 0 };
  const heldCount = counts.held ?? 0;
  const visibleHeldCount = (locksPage?.items ?? []).filter((item) => item.state === "held").length;

  return (
    <Stack gap="md">
      <AdminPageHeader
        title="资金对账"
        badge={
          <span className={`ops-live-pill ${heldCount > 0 ? "ops-live-pill--error" : "ops-live-pill--ok"}`}>
            <span className="ops-pulse-dot" aria-hidden="true" />
            {heldCount > 0 ? `${heldCount} 个活跃资金锁` : "当前无活跃锁"}
          </span>
        }
        subtitle="自动对账器按阶梯退避只读复核待交付与未知订单，绝不重复扣款；可直接在列表查看关联订单详情并核对解锁。"
        actions={
          <>
            {visibleHeldCount > 0 && (
              <Button
                size="xs"
                variant="light"
                color="blue"
                leftSection={<IconShieldCheck size={14} />}
                loading={batchReconciling !== null}
                onClick={() => void handleBatchReconcileHeld()}
              >
                {batchReconciling ? `批量对账 ${batchReconciling}` : `批量只读对账 (${visibleHeldCount})`}
              </Button>
            )}
            <Button
              size="xs"
              variant="default"
              leftSection={<IconRefresh size={14} />}
              loading={loading}
              onClick={() => void load()}
            >
              刷新
            </Button>
          </>
        }
      />

      {/* 4 格紧凑 KPI 卡片 */}
      <div className="ops-kpi-grid">
        <div className={`ops-kpi-card ops-kpi-static ${heldCount > 0 ? "ops-kpi-card--danger" : ""}`}>
          <div className="ops-kpi-top">
            <span className="ops-kpi-label-wrap">
              <span className={`ops-kpi-icon ${heldCount > 0 ? "ops-kpi-icon--red" : "ops-kpi-icon--emerald"}`}>
                <IconLockOpen size={16} />
              </span>
              <span className="ops-kpi-title">活跃资金锁</span>
            </span>
            <Badge size="xs" color={heldCount > 0 ? "red" : "green"} variant="light">
              {heldCount > 0 ? "需人工核对" : "安全"}
            </Badge>
          </div>
          {!locksPage ? (
            <Skeleton height={26} width="40%" radius="sm" />
          ) : (
            <div className="ops-kpi-main">
              <span className={`ops-kpi-value ${heldCount > 0 ? "ops-kpi-value--danger" : "ops-kpi-value--ok"}`}>
                {heldCount}
              </span>
              <span className="ops-kpi-unit">个锁定中</span>
            </div>
          )}
          <div className="ops-kpi-foot">
            <span>人工解锁 {counts.released ?? 0} · 历史核销 {counts.resolved ?? 0}</span>
          </div>
        </div>

        <div className="ops-kpi-card ops-kpi-static">
          <div className="ops-kpi-top">
            <span className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--blue"><IconActivity size={16} /></span>
              <span className="ops-kpi-title">自动对账器</span>
            </span>
            <Badge size="xs" color={reconciler?.running ? "green" : reconciler?.enabled ? "blue" : "gray"} variant="light">
              {reconciler?.running ? "运行中" : reconciler?.enabled ? "就绪" : "已禁用"}
            </Badge>
          </div>
          {!reconciler ? (
            <Skeleton height={26} width="55%" radius="sm" />
          ) : (
            <div className="ops-kpi-main">
              <span className="ops-kpi-value" style={{ fontSize: 22 }}>{reconciler.interval_s}s</span>
              <span className="ops-kpi-unit">轮询周期</span>
            </div>
          )}
          <div className="ops-kpi-foot">
            <span>{reconciler ? `单笔上限 ${reconciler.max_attempts} 次 · 已跑 ${reconciler.total_runs} 轮` : "检测中…"}</span>
          </div>
        </div>

        <div className="ops-kpi-card ops-kpi-static">
          <div className="ops-kpi-top">
            <span className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--indigo"><IconReceipt size={16} /></span>
              <span className="ops-kpi-title">待对账候选</span>
            </span>
            <Badge size="xs" color="cyan" variant="light">纯只读</Badge>
          </div>
          {!reconciler ? (
            <Skeleton height={26} width="45%" radius="sm" />
          ) : (
            <div className="ops-kpi-main">
              <span className="ops-kpi-value" style={{ fontSize: 22 }}>{reconciler.pending_candidates}</span>
              <span className="ops-kpi-unit">单</span>
            </div>
          )}
          <div className="ops-kpi-foot">
            <span>包含待交付与 unknown 订单</span>
          </div>
        </div>

        <div className="ops-kpi-card ops-kpi-static">
          <div className="ops-kpi-top">
            <span className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--teal"><IconShieldCheck size={16} /></span>
              <span className="ops-kpi-title">自动收敛核销</span>
            </span>
            <Badge size="xs" color="teal" variant="light">累计</Badge>
          </div>
          {!reconciler ? (
            <Skeleton height={26} width="45%" radius="sm" />
          ) : (
            <div className="ops-kpi-main">
              <span className="ops-kpi-value" style={{ fontSize: 22 }}>{reconciler.resolved_count}</span>
              <span className="ops-kpi-unit">单</span>
            </div>
          )}
          <div className="ops-kpi-foot">
            <span>
              {reconciler?.last_run_at ? `最近扫描 ${formatMonthDayTime(reconciler.last_run_at)}` : "等待下次自动扫描"}
            </span>
          </div>
        </div>
      </div>

      {notice && (
        <Alert color="teal" radius="md" icon={<IconCheck size={16} />} title="处理结果" withCloseButton onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}
      {error && <Alert color="red" radius="md" title="操作失败" withCloseButton onClose={() => setError(null)}>{error}</Alert>}

      {/* 资金锁列表面板 */}
      <section className="ops-panel">
        <div className="ops-panel-head">
          <SegmentedControl
            size="xs"
            value={stateFilter}
            onChange={setStateFilter}
            data={[
              { label: `锁定中 (${counts.held ?? 0})`, value: "held" },
              { label: `已对账核销 (${counts.resolved ?? 0})`, value: "resolved" },
              { label: `人工已解锁 (${counts.released ?? 0})`, value: "released" },
              { label: "全部", value: "all" },
            ]}
          />
          <span className="ops-panel-sub">
            共 {locksPage?.items.length ?? 0} 条记录 · 点击关联订单可直接审阅详情
          </span>
        </div>

        {!locksPage || locksPage.items.length === 0 ? (
          <Text c="dimmed" size="sm" ta="center" py="xl">
            {loading ? "正在加载资金锁记录…" : "当前筛选状态下没有资金锁记录。"}
          </Text>
        ) : (
          <Table.ScrollContainer minWidth={840}>
            <Table className="ledger-clean-table" highlightOnHover={false}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>状态</Table.Th>
                  <Table.Th>账号指纹</Table.Th>
                  <Table.Th>关联订单</Table.Th>
                  <Table.Th>金额</Table.Th>
                  <Table.Th>原因 / 备注</Table.Th>
                  <Table.Th>锁定时间</Table.Th>
                  <Table.Th style={{ textAlign: "right" }}>操作</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {locksPage.items.map((item) => {
                  const meta = STATE_LABEL[item.state] ?? { label: item.state, tone: "pending" as const };
                  const amountText = formatAmount(item.order_amount, item.order_currency);
                  const fullCreated = new Date(item.created_at).toLocaleString();
                  return (
                    <Table.Tr key={item.id}>
                      <Table.Td>
                        <span className={`ledger-pill ledger-pill--status-${meta.tone}`}>
                          <span className={`ledger-dot ledger-dot--${meta.tone}`} />
                          {meta.label}
                        </span>
                      </Table.Td>
                      <Table.Td><Code>{item.lock_key_prefix}</Code></Table.Td>
                      <Table.Td>
                        <Group gap={6} wrap="nowrap">
                          {item.order_id ? <CopyIdBadge value={item.order_id} prefixLength={8} /> : <span className="ledger-muted">-</span>}
                          {item.order_plan && (
                            <span className={`ledger-pill ledger-pill--plan ledger-pill--plan-${planPillTone(item.order_plan)}`}>
                              {planLabel(item.order_plan)}
                            </span>
                          )}
                        </Group>
                      </Table.Td>
                      <Table.Td>
                        {amountText ? <span className="ledger-amount">{amountText}</span> : <span className="ledger-muted">-</span>}
                      </Table.Td>
                      <Table.Td>
                        <Stack gap={2}>
                          <Text size="xs" lineClamp={1} title={item.reason}>{item.reason}</Text>
                          {item.release_note && (
                            <Text size="xs" c="dimmed" lineClamp={1} title={item.release_note}>
                              备注：{item.release_note}{item.released_by ? ` (${item.released_by})` : ""}
                            </Text>
                          )}
                        </Stack>
                      </Table.Td>
                      <Table.Td>
                        <Tooltip label={fullCreated} openDelay={250} withArrow>
                          <span className="ledger-time">{formatMonthDayTime(item.created_at)}</span>
                        </Tooltip>
                      </Table.Td>
                      <Table.Td>
                        <Group gap={6} justify="flex-end" wrap="nowrap">
                          {item.order_id && (
                            <Button
                              size="compact-xs"
                              variant="subtle"
                              loading={inspectBusyOrderId === item.order_id}
                              onClick={() => void openLinkedOrder(item.order_id!)}
                            >
                              订单详情
                            </Button>
                          )}
                          {item.state === "held" ? (
                            <>
                              <Button
                                size="compact-xs"
                                variant="light"
                                color="blue"
                                leftSection={<IconShieldCheck size={13} />}
                                loading={busyLockId === item.id}
                                disabled={batchReconciling !== null}
                                onClick={() => void handleReconcile(item.id)}
                              >
                                只读对账
                              </Button>
                              <Button
                                size="compact-xs"
                                variant="light"
                                color="red"
                                leftSection={<IconLockOpen size={13} />}
                                disabled={batchReconciling !== null}
                                onClick={() => {
                                  setReleaseTarget(item);
                                  setReleaseConfirmed(false);
                                  setReleaseNote("");
                                }}
                              >
                                核对解锁
                              </Button>
                            </>
                          ) : (
                            <span className="ledger-muted">
                              {item.released_at ? `已处理 ${formatMonthDayTime(item.released_at)}` : "已归档"}
                            </span>
                          )}
                        </Group>
                      </Table.Td>
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
      </section>

      <Modal opened={releaseTarget !== null} onClose={() => setReleaseTarget(null)} title="人工核对后解锁资金锁" radius="lg" centered>
        {releaseTarget && (
          <Stack gap="sm">
            <Alert color="orange" title="防重复扣款安全确认">
              解锁仅允许发起新单，不会恢复原支付。若本地已确认原订单扣款成功，系统将自动拒绝解锁。
            </Alert>
            <Group gap="md" wrap="wrap">
              <Group gap={4}><Text size="xs">锁 ID：</Text><CopyIdBadge value={releaseTarget.id} prefixLength={14} /></Group>
              <Group gap={4}>
                <Text size="xs">订单：</Text>
                {releaseTarget.order_id ? <CopyIdBadge value={releaseTarget.order_id} prefixLength={14} /> : <Text size="xs">-</Text>}
              </Group>
            </Group>
            <Checkbox
              checked={releaseConfirmed}
              onChange={(e) => setReleaseConfirmed(e.currentTarget.checked)}
              label="我已核对银行或 Stripe 账单，明确确认本笔订单没有扣款"
            />
            <TextInput
              label="操作员标识"
              value={releaseOperator}
              onChange={(e) => setReleaseOperator(e.currentTarget.value)}
              placeholder="例如：ops-alice"
            />
            <Textarea
              label="核对备注（必填，将写入审计日志）"
              placeholder="例如：已核对银行流水与 Stripe 后台，确认无扣款"
              value={releaseNote}
              onChange={(e) => setReleaseNote(e.currentTarget.value)}
              minRows={3}
            />
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setReleaseTarget(null)} disabled={releaseBusy}>取消</Button>
              <Button
                color="red"
                loading={releaseBusy}
                disabled={!releaseConfirmed || !releaseNote.trim()}
                onClick={() => void handleRelease()}
              >
                确认解锁
              </Button>
            </Group>
          </Stack>
        )}
      </Modal>

      <OrderLedgerDetailModal
        order={detailOrder}
        events={detailEvents}
        eventsLoading={detailLoading}
        reconciling={detailOrder != null && reconcilingOrderId === detailOrder.id}
        onClose={() => setDetailOrder(null)}
        onReconcile={(id) => void handleReconcileFromDetail(id)}
        onOpenFailureDetail={(ord) => void openFailureDetail(ord)}
      />

      <OrderFailureDetailModal
        order={failureModalOrder}
        detail={failureDetail}
        loading={failureLoading}
        fetchError={failureFetchError}
        onClose={() => {
          setFailureModalOrder(null);
          setFailureDetail(null);
        }}
        onOpenAuditDetail={(ord) => void openLinkedOrder(ord.id)}
      />
    </Stack>
  );
}
