import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Group,
  Menu,
  SegmentedControl,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Tooltip,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconCheck,
  IconClock,
  IconDotsVertical,
  IconDownload,
  IconGavel,
  IconListDetails,
  IconRefresh,
  IconRotate2,
  IconSearch,
  IconShieldCheck,
} from "@tabler/icons-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { formatAmount } from "../api/client";
import type { HistoryRecord } from "../api/history";
import { isAbortError } from "../api/http";
import {
  buildOrderLedgerExportUrl,
  getOrderEvents,
  getOrderFailureDetail,
  getOrderLedger,
  reconcileOrder,
  type OrderFailureDetail,
  type OrderLedgerItem,
  type OrderLedgerPage,
  type OrderManualVerdictResult,
} from "../api/ledger";
import type { QuickPayEvent } from "../api/quickPay";
import { planLabel } from "../plans";
import {
  formatDurationClock,
  formatMonthDayTime,
  hasFailureReason,
  planPillTone,
  simpleOrderStatus,
} from "../utils/orderLedgerFormat";
import { AdminPageHeader } from "./AdminPageHeader";
import { CopyIdBadge } from "./CopyIdBadge";
import { HistoryTable } from "./HistoryTable";
import { OrderFailureDetailModal } from "./OrderFailureDetailModal";
import { OrderLedgerDetailModal } from "./OrderLedgerDetailModal";
import { OrderManualVerdictModal } from "./OrderManualVerdictModal";

const PAGE_SIZE = 20;

const PLAN_FILTER_OPTIONS = [
  { value: "all", label: "全部套餐" },
  { value: "plus", label: "Plus" },
  { value: "go", label: "Go" },
  { value: "pro_lite", label: "Pro100" },
  { value: "pro", label: "Pro200" },
  { value: "pro_max", label: "Pro500" },
  { value: "team", label: "Team" },
];

const SOURCE_FILTER_OPTIONS = [
  { value: "all", label: "全部来源" },
  { value: "portal", label: "用户门户" },
  { value: "api", label: "开放 API" },
  { value: "quick_pay", label: "后台一键" },
  { value: "step", label: "分步调试" },
];

function needsManualAttention(item: OrderLedgerItem): boolean {
  return (
    item.status === "unknown"
    || item.status === "verifying"
    || item.status === "paid_pending_delivery"
    || item.status === "awaiting_payment"
    || item.status === "requires_action"
  );
}

export function ServerLedgerPanel({
  active = true,
  localHistory,
  onLocksChanged,
}: {
  active?: boolean;
  localHistory: HistoryRecord[];
  onLocksChanged?: () => void;
}) {
  const [statusFilter, setStatusFilter] = useState("all");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [planFilter, setPlanFilter] = useState("all");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<OrderLedgerPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reconcilingId, setReconcilingId] = useState<string | null>(null);

  const [failureModalOrder, setFailureModalOrder] = useState<OrderLedgerItem | null>(null);
  const [failureDetail, setFailureDetail] = useState<OrderFailureDetail | null>(null);
  const [failureLoading, setFailureLoading] = useState(false);
  const [failureFetchError, setFailureFetchError] = useState<string | null>(null);
  const [detailOrder, setDetailOrder] = useState<OrderLedgerItem | null>(null);
  const [detailEvents, setDetailEvents] = useState<QuickPayEvent[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [verdictOrder, setVerdictOrder] = useState<OrderLedgerItem | null>(null);

  const loadControllerRef = useRef<AbortController | null>(null);
  const wasActiveRef = useRef(active);

  const load = useCallback(async () => {
    loadControllerRef.current?.abort();
    const controller = new AbortController();
    loadControllerRef.current = controller;

    setLoading(true);
    setError(null);
    try {
      const next = await getOrderLedger(
        {
          status: statusFilter,
          plan: planFilter,
          source: sourceFilter,
          search: appliedSearch,
          date_from: dateFrom || undefined,
          date_to: dateTo || undefined,
          limit: PAGE_SIZE,
          offset,
        },
        controller.signal,
      );
      if (loadControllerRef.current === controller) {
        setPage(next);
      }
    } catch (exc) {
      if (isAbortError(exc)) return;
      setError(exc instanceof Error ? exc.message : "加载服务端订单账本失败");
    } finally {
      if (loadControllerRef.current === controller) {
        setLoading(false);
      }
    }
  }, [statusFilter, sourceFilter, planFilter, appliedSearch, dateFrom, dateTo, offset]);

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

  async function openFailureDetail(item: OrderLedgerItem) {
    setDetailOrder(null);
    setVerdictOrder(null);
    setFailureModalOrder(item);
    setFailureDetail(null);
    setFailureFetchError(null);
    setFailureLoading(true);
    try {
      const data = await getOrderFailureDetail(item.id);
      setFailureDetail(data);
    } catch (exc) {
      setFailureFetchError(exc instanceof Error ? exc.message : "加载失败详情失败");
    } finally {
      setFailureLoading(false);
    }
  }

  async function openDetail(item: OrderLedgerItem) {
    setFailureModalOrder(null);
    setFailureDetail(null);
    setVerdictOrder(null);
    setDetailOrder(item);
    setDetailEvents([]);
    setDetailLoading(true);
    try {
      const events = await getOrderEvents(item.id);
      setDetailEvents(events);
    } catch {
      setDetailEvents([]);
    } finally {
      setDetailLoading(false);
    }
  }

  function openManualVerdict(item: OrderLedgerItem) {
    setDetailOrder(null);
    setFailureModalOrder(null);
    setVerdictOrder(item);
  }

  async function handleReconcile(orderId: string) {
    setReconcilingId(orderId);
    setNotice(null);
    setError(null);
    try {
      const res = await reconcileOrder(orderId);
      setNotice(`订单 ${orderId.slice(0, 8)} 只读对账结果：${res.message}`);
      await load();
      onLocksChanged?.();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "只读对账失败");
    } finally {
      setReconcilingId(null);
    }
  }

  async function handleManualVerdictDone(res: OrderManualVerdictResult) {
    setVerdictOrder(null);
    setNotice(
      `订单 ${res.order_id.slice(0, 8)} ${res.message}（${res.previous_status} → ${res.order_status} · 操作员 ${res.operator}）。`,
    );
    await load();
    onLocksChanged?.();
  }

  function resetFilters() {
    setStatusFilter("all");
    setSourceFilter("all");
    setPlanFilter("all");
    setDateFrom("");
    setDateTo("");
    setSearch("");
    setAppliedSearch("");
    setOffset(0);
  }

  const hasActiveFilters =
    statusFilter !== "all"
    || sourceFilter !== "all"
    || planFilter !== "all"
    || Boolean(dateFrom)
    || Boolean(dateTo)
    || Boolean(appliedSearch);

  const stats = page?.stats;
  const canPrev = offset > 0;
  const canNext = page != null && offset + PAGE_SIZE < page.total;
  const exportUrl = buildOrderLedgerExportUrl({
    status: statusFilter,
    plan: planFilter,
    source: sourceFilter,
    search: appliedSearch,
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
  });

  return (
    <Stack gap="md">
      <AdminPageHeader
        title="订单记录"
        badge={
          stats ? (
            <Group gap={6} wrap="wrap">
              <Badge size="sm" variant="light" color="gray">共 {stats.total} 笔</Badge>
              <Badge size="sm" variant="light" color="green">成功 {stats.delivered}</Badge>
              {stats.paid_pending_delivery > 0 && (
                <Badge size="sm" variant="light" color="cyan">待交付 {stats.paid_pending_delivery}</Badge>
              )}
              {stats.unknown > 0 && (
                <Badge size="sm" variant="light" color="orange">待核对 {stats.unknown}</Badge>
              )}
              {stats.active_locks > 0 && (
                <Badge size="sm" variant="filled" color="red">资金锁 {stats.active_locks}</Badge>
              )}
            </Group>
          ) : null
        }
        subtitle="点击任意行即可查看订单详情与耗时细分；待核对或滞留单支持只读对账与留痕人工判定。"
        actions={
          <>
            <Button
              component="a"
              href={exportUrl}
              download
              variant="default"
              size="xs"
              leftSection={<IconDownload size={14} />}
            >
              导出 CSV
            </Button>
            <Button
              variant="default"
              size="xs"
              leftSection={<IconRefresh size={14} />}
              loading={loading}
              onClick={() => void load()}
            >
              刷新
            </Button>
          </>
        }
      />

      {notice && (
        <Alert color="teal" radius="md" icon={<IconCheck size={16} />} title="处理结果" withCloseButton onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}
      {error && (
        <Alert color="red" radius="md" title="加载失败" withCloseButton onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      <section className="ops-panel">
        <Stack gap="md">
          {/* 精简筛选栏 */}
          <Group justify="space-between" wrap="wrap" gap="xs">
            <SegmentedControl
              size="xs"
              value={statusFilter}
              onChange={(val) => { setStatusFilter(val); setOffset(0); }}
              data={[
                { label: "全部", value: "all" },
                { label: "成功", value: "succeeded" },
                { label: "失败", value: "failed" },
                { label: "待核对", value: "unknown" },
              ]}
            />
            <Group gap="xs" wrap="wrap">
              <Select
                size="xs"
                w={110}
                aria-label="按套餐筛选"
                value={planFilter}
                onChange={(val) => { setPlanFilter(val || "all"); setOffset(0); }}
                data={PLAN_FILTER_OPTIONS}
              />
              <Select
                size="xs"
                w={106}
                aria-label="按来源筛选"
                value={sourceFilter}
                onChange={(val) => { setSourceFilter(val || "all"); setOffset(0); }}
                data={SOURCE_FILTER_OPTIONS}
              />
              <TextInput
                type="date"
                size="xs"
                w={128}
                aria-label="开始日期"
                value={dateFrom}
                onChange={(e) => { setDateFrom(e.currentTarget.value); setOffset(0); }}
              />
              <TextInput
                type="date"
                size="xs"
                w={128}
                aria-label="结束日期"
                value={dateTo}
                onChange={(e) => { setDateTo(e.currentTarget.value); setOffset(0); }}
              />
              <TextInput
                size="xs"
                w={196}
                placeholder="订单号 / 邮箱 / 失败码"
                leftSection={<IconSearch size={14} />}
                value={search}
                onChange={(e) => setSearch(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") { setOffset(0); setAppliedSearch(search.trim()); }
                }}
              />
              <Button size="xs" variant="default" onClick={() => { setOffset(0); setAppliedSearch(search.trim()); }}>
                搜索
              </Button>
              {hasActiveFilters && (
                <Button size="xs" variant="subtle" color="gray" leftSection={<IconRotate2 size={13} />} onClick={resetFilters}>
                  重置
                </Button>
              )}
            </Group>
          </Group>

          {!page || page.items.length === 0 ? (
            <Text c="dimmed" ta="center" py="xl" size="sm">
              {loading ? "正在加载订单记录…" : "当前筛选条件下暂无订单记录。"}
            </Text>
          ) : (
            <>
              <Table.ScrollContainer minWidth={940}>
                <Table className="ledger-clean-table" highlightOnHover={false}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th style={{ width: 48 }}>序号</Table.Th>
                      <Table.Th>订单号</Table.Th>
                      <Table.Th>账号</Table.Th>
                      <Table.Th>套餐</Table.Th>
                      <Table.Th>状态</Table.Th>
                      <Table.Th>卡片</Table.Th>
                      <Table.Th>金额</Table.Th>
                      <Table.Th>总耗时</Table.Th>
                      <Table.Th>创建时间</Table.Th>
                      <Table.Th style={{ width: 120, textAlign: "right" }}>操作</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {page.items.map((item, idx) => {
                      const seq = offset + idx + 1;
                      const emailText = item.account_email || item.masked_email || null;
                      const amountText = formatAmount(item.amount, item.currency);
                      const statusInfo = simpleOrderStatus(item.status);
                      const durationClock = formatDurationClock(item.total_ms);
                      const hasFail = hasFailureReason(item);
                      const attention = needsManualAttention(item);
                      const canReconcile =
                        item.status === "unknown"
                        || (item.payment_status === "succeeded" && item.delivery_status !== "delivered");
                      const planTone = planPillTone(item.plan);

                      return (
                        <Table.Tr
                          key={item.id}
                          className="ops-clickable-row"
                          onClick={() => void openDetail(item)}
                        >
                          <Table.Td>
                            <span className="ledger-seq">{seq}</span>
                          </Table.Td>

                          <Table.Td onClick={(e) => e.stopPropagation()}>
                            <CopyIdBadge value={item.id} prefixLength={8} />
                          </Table.Td>

                          <Table.Td>
                            {emailText ? (
                              <Tooltip label={emailText} openDelay={300} withArrow>
                                <div className="ledger-email">{emailText}</div>
                              </Tooltip>
                            ) : (
                              <span className="ledger-muted">-</span>
                            )}
                          </Table.Td>

                          <Table.Td>
                            <span className={`ledger-pill ledger-pill--plan ledger-pill--plan-${planTone}`}>
                              {planLabel(item.plan)}
                            </span>
                          </Table.Td>

                          <Table.Td>
                            <span className={`ledger-pill ledger-pill--status-${statusInfo.tone}`}>
                              <span className={`ledger-dot ledger-dot--${statusInfo.tone}`} />
                              {statusInfo.label}
                            </span>
                          </Table.Td>

                          <Table.Td>
                            {item.card_brand ? (
                              <span className="ledger-pill ledger-pill--card">{item.card_brand}</span>
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

                          {/* 统一整齐的操作列：每行固定 1 个场景主按钮 + 1 个右对齐 ⋯ 更多菜单 */}
                          <Table.Td style={{ textAlign: "right" }} onClick={(e) => e.stopPropagation()}>
                            <Group gap={4} justify="flex-end" wrap="nowrap">
                              {attention ? (
                                <Button
                                  size="compact-xs"
                                  variant="light"
                                  color="orange"
                                  miw={64}
                                  onClick={() => openManualVerdict(item)}
                                >
                                  人工判定
                                </Button>
                              ) : hasFail ? (
                                <Button
                                  size="compact-xs"
                                  variant="light"
                                  color="red"
                                  miw={64}
                                  onClick={() => void openFailureDetail(item)}
                                >
                                  失败诊断
                                </Button>
                              ) : (
                                <Button
                                  size="compact-xs"
                                  variant="subtle"
                                  color="gray"
                                  miw={64}
                                  onClick={() => void openDetail(item)}
                                >
                                  查看详情
                                </Button>
                              )}

                              <Menu position="bottom-end" withinPortal shadow="md" width={164}>
                                <Menu.Target>
                                  <ActionIcon
                                    size="sm"
                                    variant="subtle"
                                    color="gray"
                                    aria-label="更多订单操作"
                                    loading={reconcilingId === item.id}
                                  >
                                    <IconDotsVertical size={15} />
                                  </ActionIcon>
                                </Menu.Target>
                                <Menu.Dropdown>
                                  <Menu.Item
                                    leftSection={<IconListDetails size={14} />}
                                    onClick={() => void openDetail(item)}
                                  >
                                    订单详情与耗时
                                  </Menu.Item>
                                  {hasFail && (
                                    <Menu.Item
                                      color="red"
                                      leftSection={<IconAlertCircle size={14} />}
                                      onClick={() => void openFailureDetail(item)}
                                    >
                                      失败诊断详情
                                    </Menu.Item>
                                  )}
                                  {canReconcile && (
                                    <Menu.Item
                                      color="orange"
                                      leftSection={<IconShieldCheck size={14} />}
                                      disabled={reconcilingId === item.id}
                                      onClick={() => void handleReconcile(item.id)}
                                    >
                                      执行只读对账
                                    </Menu.Item>
                                  )}
                                  <Menu.Divider />
                                  <Menu.Item
                                    color="blue"
                                    leftSection={<IconGavel size={14} />}
                                    onClick={() => openManualVerdict(item)}
                                  >
                                    人工判定状态…
                                  </Menu.Item>
                                </Menu.Dropdown>
                              </Menu>
                            </Group>
                          </Table.Td>
                        </Table.Tr>
                      );
                    })}
                  </Table.Tbody>
                </Table>
              </Table.ScrollContainer>

              <Group justify="space-between" pt={4}>
                <Text size="xs" c="dimmed">
                  显示第 {offset + 1} – {Math.min(offset + PAGE_SIZE, page.total)} 条，共 {page.total} 条
                </Text>
                <Group gap="xs">
                  <Button
                    size="xs"
                    variant="default"
                    disabled={!canPrev || loading}
                    onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  >
                    上一页
                  </Button>
                  <Button
                    size="xs"
                    variant="default"
                    disabled={!canNext || loading}
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                  >
                    下一页
                  </Button>
                </Group>
              </Group>
            </>
          )}
        </Stack>
      </section>

      {localHistory.length > 0 && (
        <details className="studio-help">
          <summary>本机浏览器旧记录（共 {localHistory.length} 条）</summary>
          <div style={{ marginTop: 12 }}>
            <HistoryTable items={localHistory} />
          </div>
        </details>
      )}

      {/* 共享结构化失败诊断弹窗 */}
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
        onOpenAuditDetail={(ord) => void openDetail(ord)}
      />

      {/* 订单详情弹窗（含人工判定与只读对账入口） */}
      <OrderLedgerDetailModal
        order={detailOrder}
        events={detailEvents}
        eventsLoading={detailLoading}
        reconciling={detailOrder != null && reconcilingId === detailOrder.id}
        onClose={() => setDetailOrder(null)}
        onReconcile={(id) => void handleReconcile(id)}
        onOpenFailureDetail={(item) => void openFailureDetail(item)}
        onManualVerdict={(item) => openManualVerdict(item)}
      />

      {/* 人工判定状态弹窗 */}
      <OrderManualVerdictModal
        order={verdictOrder}
        onClose={() => setVerdictOrder(null)}
        onSuccess={(res) => void handleManualVerdictDone(res)}
      />
    </Stack>
  );
}
