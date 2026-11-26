import {
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Modal,
  SegmentedControl,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconClock,
  IconDownload,
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
import { CopyIdBadge } from "./CopyIdBadge";
import { HistoryTable } from "./HistoryTable";
import { JsonCodeBlock } from "./JsonCodeBlock";
import { OrderLedgerDetailModal } from "./OrderLedgerDetailModal";
import { FAILURE_LABEL } from "./quick-pay/constants";

const PAGE_SIZE = 20;

const PLAN_FILTER_OPTIONS = [
  { value: "all", label: "全部套餐" },
  { value: "plus", label: "Plus" },
  { value: "go", label: "Go" },
  { value: "pro_lite", label: "Pro Standard" },
  { value: "pro", label: "Pro More" },
  { value: "pro_max", label: "Pro Max" },
  { value: "team", label: "Team" },
];

const SOURCE_FILTER_OPTIONS = [
  { value: "all", label: "全部来源" },
  { value: "portal", label: "用户门户" },
  { value: "quick_pay", label: "后台一键" },
  { value: "step", label: "分步调试" },
];

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
    <Stack gap="lg">
      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="md">
          {/* 精简头部：标题 + 一行安静的汇总数据 + 导出/刷新 */}
          <Group justify="space-between" align="center" wrap="wrap" gap="sm">
            <Group gap="md" align="baseline" wrap="wrap">
              <Title order={2}>订单记录</Title>
              {stats && (
                <Group gap="xs" wrap="wrap">
                  <Text size="xs" c="dimmed">
                    共 <strong>{stats.total}</strong> 笔 · 成功 <strong style={{ color: "var(--studio-green)" }}>{stats.delivered}</strong> · 失败 <strong>{stats.failed}</strong>
                  </Text>
                  {stats.paid_pending_delivery > 0 && (
                    <Badge size="xs" variant="light" color="cyan">待交付 {stats.paid_pending_delivery}</Badge>
                  )}
                  {stats.unknown > 0 && (
                    <Badge size="xs" variant="light" color="orange">待核对 {stats.unknown}</Badge>
                  )}
                  {stats.active_locks > 0 && (
                    <Badge size="xs" variant="filled" color="red">资金锁 {stats.active_locks}</Badge>
                  )}
                </Group>
              )}
            </Group>
            <Group gap="xs">
              <Button
                component="a"
                href={exportUrl}
                download
                variant="subtle"
                color="gray"
                size="xs"
                leftSection={<IconDownload size={14} />}
              >
                导出 CSV
              </Button>
              <Button
                variant="subtle"
                color="gray"
                size="xs"
                leftSection={<IconRefresh size={14} />}
                loading={loading}
                onClick={() => void load()}
              >
                刷新
              </Button>
            </Group>
          </Group>

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

          {notice && <Alert color="blue" title="对账提示" withCloseButton onClose={() => setNotice(null)}>{notice}</Alert>}
          {error && <Alert color="red" title="加载失败">{error}</Alert>}

          {!page || page.items.length === 0 ? (
            <Text c="dimmed" ta="center" py="xl" size="sm">
              {loading ? "正在加载订单记录..." : "当前筛选条件下暂无订单记录。"}
            </Text>
          ) : (
            <>
              <Table.ScrollContainer minWidth={980}>
                <Table className="ledger-clean-table" highlightOnHover={false}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th style={{ width: 54 }}>序号</Table.Th>
                      <Table.Th>订单号</Table.Th>
                      <Table.Th>账号</Table.Th>
                      <Table.Th>套餐</Table.Th>
                      <Table.Th>状态</Table.Th>
                      <Table.Th>卡片</Table.Th>
                      <Table.Th>金额</Table.Th>
                      <Table.Th>总耗时</Table.Th>
                      <Table.Th>创建时间</Table.Th>
                      <Table.Th style={{ textAlign: "right" }}>操作</Table.Th>
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
                      const canReconcile =
                        item.status === "unknown"
                        || (item.payment_status === "succeeded" && item.delivery_status !== "delivered");
                      const planTone = planPillTone(item.plan);

                      return (
                        <Table.Tr key={item.id}>
                          {/* 1. 序号（最新在顶部，从 1 往下排） */}
                          <Table.Td>
                            <span className="ledger-seq">{seq}</span>
                          </Table.Td>

                          {/* 2. 订单号 */}
                          <Table.Td>
                            <CopyIdBadge value={item.id} prefixLength={8} />
                          </Table.Td>

                          {/* 3. 邮箱账号 */}
                          <Table.Td>
                            {emailText ? (
                              <Tooltip label={emailText} openDelay={300} withArrow>
                                <div className="ledger-email">{emailText}</div>
                              </Tooltip>
                            ) : (
                              <span className="ledger-muted">-</span>
                            )}
                          </Table.Td>

                          {/* 4. 套餐（仅展示套餐类型） */}
                          <Table.Td>
                            <span className={`ledger-pill ledger-pill--plan ledger-pill--plan-${planTone}`}>
                              {planLabel(item.plan)}
                            </span>
                          </Table.Td>

                          {/* 5. 状态（成功 / 失败 / 待核对 / 处理中） */}
                          <Table.Td>
                            <span className={`ledger-pill ledger-pill--status-${statusInfo.tone}`}>
                              <span className={`ledger-dot ledger-dot--${statusInfo.tone}`} />
                              {statusInfo.label}
                            </span>
                          </Table.Td>

                          {/* 6. 卡片（如 VISA / Mastercard） */}
                          <Table.Td>
                            {item.card_brand ? (
                              <span className="ledger-pill ledger-pill--card">{item.card_brand}</span>
                            ) : (
                              <span className="ledger-muted">-</span>
                            )}
                          </Table.Td>

                          {/* 7. 金额（仅金额，不标注免税或其他） */}
                          <Table.Td>
                            {amountText ? (
                              <span className="ledger-amount">{amountText}</span>
                            ) : (
                              <span className="ledger-muted">-</span>
                            )}
                          </Table.Td>

                          {/* 8. 总耗时 */}
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

                          {/* 9. 创建时间（仅月日时分 MM/DD HH:mm） */}
                          <Table.Td>
                            <span className="ledger-time">{formatMonthDayTime(item.created_at)}</span>
                          </Table.Td>

                          {/* 10. 操作（失败原因在详情左侧） */}
                          <Table.Td>
                            <Group gap={6} justify="flex-end" wrap="nowrap">
                              <Button
                                size="xs"
                                variant={hasFail ? "light" : "subtle"}
                                color={hasFail ? "red" : "gray"}
                                disabled={!hasFail}
                                leftSection={<IconAlertCircle size={13} />}
                                onClick={() => void openFailureDetail(item)}
                              >
                                失败原因
                              </Button>
                              <Button
                                size="xs"
                                variant="subtle"
                                color="gray"
                                leftSection={<IconListDetails size={13} />}
                                onClick={() => void openDetail(item)}
                              >
                                详情
                              </Button>
                              {canReconcile && (
                                <Button
                                  size="xs"
                                  variant="light"
                                  color="orange"
                                  leftSection={<IconShieldCheck size={13} />}
                                  loading={reconcilingId === item.id}
                                  onClick={() => void handleReconcile(item.id)}
                                >
                                  对账
                                </Button>
                              )}
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
      </Card>

      {localHistory.length > 0 && (
        <details className="studio-help">
          <summary>本机浏览器旧记录（共 {localHistory.length} 条）</summary>
          <div style={{ marginTop: 12 }}>
            <HistoryTable items={localHistory} />
          </div>
        </details>
      )}

      {/* 开通失败详情弹窗（延续后台精致 UI 风格 + 结构化接口诊断内容） */}
      <Modal
        opened={failureModalOrder !== null}
        onClose={() => {
          setFailureModalOrder(null);
          setFailureDetail(null);
          setFailureFetchError(null);
        }}
        title={
          failureModalOrder && (
            <Group gap="xs" align="center" wrap="nowrap">
              <span className="failure-modal-icon" aria-hidden="true">
                <IconAlertCircle size={17} stroke={2} />
              </span>
              <Text fw={700} size="md">
                {failureDetail?.title || "开通失败详情"}
              </Text>
              <span className={`ledger-pill ledger-pill--plan ledger-pill--plan-${planPillTone(failureModalOrder.plan)}`}>
                {planLabel(failureModalOrder.plan)}
              </span>
            </Group>
          )
        }
        size="lg"
        radius="lg"
        padding="lg"
        centered
      >
        {failureModalOrder && (
          <Stack gap="md">
            {/* 顶部柔红强调摘要卡 */}
            <div className="failure-summary-banner">
              <div className="failure-detail-summary">
                {failureLoading
                  ? "正在加载结构化失败诊断..."
                  : failureDetail?.summary
                    || failureModalOrder.last_error
                    || (failureModalOrder.failure_code ? FAILURE_LABEL[failureModalOrder.failure_code] : null)
                    || "该订单未成功完成，请结合下方请求与响应排查。"}
              </div>
              <div className="failure-detail-meta">
                <span className="failure-meta-chip">
                  错误码 <code>{failureDetail?.error_code || (failureModalOrder.failure_code || "UNKNOWN").toUpperCase()}</code>
                </span>
                <span className="failure-meta-chip">
                  阶段 <code>{failureDetail?.stage || failureModalOrder.diagnostics?.stage || failureModalOrder.job_stage || "-"}</code>
                </span>
                {failureDetail?.cf_ray && (
                  <span className="failure-meta-chip">
                    CF-Ray <code>{failureDetail.cf_ray}</code>
                  </span>
                )}
                {failureDetail?.request_id && (
                  <span className="failure-meta-chip">
                    Request-ID <code>{failureDetail.request_id}</code>
                  </span>
                )}
              </div>
            </div>

            {failureFetchError && (
              <Alert color="red" title="加载接口诊断失败">
                <Text size="xs">{failureFetchError}</Text>
              </Alert>
            )}

            {/* 结构化上游接口请求与响应卡片 */}
            {!failureLoading && failureDetail?.exchange && (
              <div className="failure-exchange-card">
                <div className="failure-exchange-header">
                  <div className="failure-exchange-head">
                    <span className="failure-exchange-title">{failureDetail.exchange.title}</span>
                    {failureDetail.exchange.http_status != null && (
                      <Badge
                        size="sm"
                        radius="sm"
                        variant="light"
                        color={
                          failureDetail.exchange.http_status >= 500
                            ? "red"
                            : failureDetail.exchange.http_status >= 400
                              ? "orange"
                              : "teal"
                        }
                      >
                        HTTP {failureDetail.exchange.http_status}
                      </Badge>
                    )}
                  </div>
                  {(failureDetail.exchange.method || failureDetail.exchange.url) && (
                    <div className="failure-exchange-url-row">
                      {failureDetail.exchange.method && (
                        <span className="failure-method-tag">{failureDetail.exchange.method}</span>
                      )}
                      {failureDetail.exchange.url && (
                        <span className="failure-exchange-url">{failureDetail.exchange.url}</span>
                      )}
                    </div>
                  )}
                </div>

                <Stack gap="sm" className="failure-exchange-body">
                  {failureDetail.exchange.request != null && (
                    <JsonCodeBlock label="请求参数" value={failureDetail.exchange.request} />
                  )}

                  {failureDetail.exchange.response != null && (
                    <JsonCodeBlock label="响应内容" value={failureDetail.exchange.response} />
                  )}

                  {failureDetail.exchange.captured === false && (
                    <Text size="xs" c="dimmed">
                      注：该笔为旧版历史记录，当前展示为已落库的非敏感摘要；新产生的失败订单将自动记录完整脱敏后的上游请求参数与响应内容。
                    </Text>
                  )}
                </Stack>
              </div>
            )}

            <Group justify="space-between" align="center" wrap="wrap" gap="xs" pt={4}>
              <Group gap={6}>
                <Text size="xs" c="dimmed">请求编号</Text>
                <CopyIdBadge value={failureModalOrder.id} prefixLength={14} />
              </Group>
              <Group gap="xs">
                <Button size="xs" variant="light" onClick={() => void openDetail(failureModalOrder)}>
                  查看完整审计详情
                </Button>
                <Button
                  size="xs"
                  variant="default"
                  onClick={() => {
                    setFailureModalOrder(null);
                    setFailureDetail(null);
                  }}
                >
                  关闭
                </Button>
              </Group>
            </Group>
          </Stack>
        )}
      </Modal>

      {/* 全新易读「订单详情」弹窗（含总耗时细分条与清晰分组信息） */}
      <OrderLedgerDetailModal
        order={detailOrder}
        events={detailEvents}
        eventsLoading={detailLoading}
        reconciling={detailOrder != null && reconcilingId === detailOrder.id}
        onClose={() => setDetailOrder(null)}
        onReconcile={(id) => void handleReconcile(id)}
        onOpenFailureDetail={(item) => void openFailureDetail(item)}
      />
    </Stack>
  );
}
