import {
  Alert, Badge, Button, Card, Group, Modal, SegmentedControl, Select, Stack, Table, Text, TextInput, Title,
} from "@mantine/core";
import { IconDownload, IconListDetails, IconRefresh, IconSearch, IconShieldCheck } from "@tabler/icons-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { formatAmount } from "../api/client";
import type { HistoryRecord } from "../api/history";
import { isAbortError } from "../api/http";
import {
  buildOrderLedgerExportUrl,
  getOrderEvents,
  getOrderLedger,
  reconcileOrder,
  type OrderLedgerItem,
  type OrderLedgerPage,
} from "../api/ledger";
import type { QuickPayEvent } from "../api/quickPay";
import { planLabel, regionLabel } from "../plans";
import { CopyIdBadge } from "./CopyIdBadge";
import { HistoryTable } from "./HistoryTable";
import { STATUS_LABELS, statusColor } from "./OrderSummary";

const PAGE_SIZE = 15;

const PLAN_FILTER_OPTIONS = [
  { value: "all", label: "全部套餐" },
  { value: "plus", label: "Plus" },
  { value: "go", label: "Go" },
  { value: "pro_lite", label: "Pro Standard" },
  { value: "pro", label: "Pro More" },
  { value: "pro_max", label: "Pro Max" },
  { value: "team", label: "Team" },
];

export function ServerLedgerPanel({
  localHistory,
  onLocksChanged,
}: {
  localHistory: HistoryRecord[];
  onLocksChanged?: () => void;
}) {
  const [statusFilter, setStatusFilter] = useState("all");
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

  const [detailOrder, setDetailOrder] = useState<OrderLedgerItem | null>(null);
  const [detailEvents, setDetailEvents] = useState<QuickPayEvent[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);

  const loadControllerRef = useRef<AbortController | null>(null);

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
  }, [statusFilter, planFilter, appliedSearch, dateFrom, dateTo, offset]);

  useEffect(() => {
    void load();
    return () => {
      loadControllerRef.current?.abort();
    };
  }, [load]);

  async function openEvents(item: OrderLedgerItem) {
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

  const stats = page?.stats;
  const canPrev = offset > 0;
  const canNext = page != null && offset + PAGE_SIZE < page.total;
  const exportUrl = buildOrderLedgerExportUrl({
    status: statusFilter,
    plan: planFilter,
    search: appliedSearch,
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
  });

  return (
    <Stack gap="lg">
      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="md">
          <Group justify="space-between" align="flex-start" wrap="wrap">
            <div>
              <Title order={2}>服务端订单账本</Title>
              <Text c="dimmed" size="sm" mt={4}>
                数据持久化保存在服务端 SQLite（不含卡号、Session 等敏感凭证），跨设备不丢失。
              </Text>
            </div>
            <Group gap="xs">
              <Button
                component="a"
                href={exportUrl}
                download
                variant="light"
                size="xs"
                leftSection={<IconDownload size={14} />}
              >
                导出 CSV
              </Button>
              <Button variant="subtle" size="xs" leftSection={<IconRefresh size={14} />} loading={loading} onClick={() => void load()}>
                刷新账本
              </Button>
            </Group>
          </Group>

          {stats && (
            <Group gap="xs" wrap="wrap">
              <Badge variant="light" color="gray" size="lg">总订单 {stats.total}</Badge>
              <Badge variant="light" color="green" size="lg">已交付 {stats.delivered}</Badge>
              <Badge variant="light" color="cyan" size="lg">已付款待交付 {stats.paid_pending_delivery}</Badge>
              <Badge variant="light" color="orange" size="lg">待核对 unknown {stats.unknown}</Badge>
              <Badge variant="light" color="red" size="lg">失败 / 拒绝 {stats.failed}</Badge>
              <Badge variant="filled" color={stats.active_locks > 0 ? "red" : "gray"} size="lg">
                活跃资金锁 {stats.active_locks}
              </Badge>
            </Group>
          )}

          <Group justify="space-between" wrap="wrap" gap="sm">
            <SegmentedControl
              size="xs"
              value={statusFilter}
              onChange={(val) => { setStatusFilter(val); setOffset(0); }}
              data={[
                { label: "全部", value: "all" },
                { label: "已交付", value: "succeeded" },
                { label: "待交付", value: "paid_pending_delivery" },
                { label: "待核对", value: "unknown" },
                { label: "失败", value: "failed" },
              ]}
            />
            <Group gap="xs" wrap="wrap">
              <Select
                size="xs"
                w={128}
                aria-label="按套餐筛选"
                value={planFilter}
                onChange={(val) => { setPlanFilter(val || "all"); setOffset(0); }}
                data={PLAN_FILTER_OPTIONS}
              />
              <TextInput
                type="date"
                size="xs"
                w={136}
                aria-label="开始日期"
                value={dateFrom}
                onChange={(e) => { setDateFrom(e.currentTarget.value); setOffset(0); }}
              />
              <TextInput
                type="date"
                size="xs"
                w={136}
                aria-label="结束日期"
                value={dateTo}
                onChange={(e) => { setDateTo(e.currentTarget.value); setOffset(0); }}
              />
              <TextInput
                size="xs"
                placeholder="按订单号 / 尝试号检索"
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
            </Group>
          </Group>

          {notice && <Alert color="blue" title="对账提示" withCloseButton onClose={() => setNotice(null)}>{notice}</Alert>}
          {error && <Alert color="red" title="加载失败">{error}</Alert>}

          {!page || page.items.length === 0 ? (
            <Text c="dimmed" ta="center" py="xl">
              {loading ? "正在加载服务端订单账本..." : "当前筛选条件下暂无订单记录。"}
            </Text>
          ) : (
            <>
              <Table.ScrollContainer minWidth={780}>
                <Table verticalSpacing="sm" highlightOnHover fz="xs">
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>订单状态</Table.Th>
                      <Table.Th>支付 / 交付</Table.Th>
                      <Table.Th>套餐 / 地区</Table.Th>
                      <Table.Th>金额</Table.Th>
                      <Table.Th>时间</Table.Th>
                      <Table.Th>订单号</Table.Th>
                      <Table.Th>操作</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {page.items.map((item) => {
                      const canReconcile =
                        item.status === "unknown"
                        || (item.payment_status === "succeeded" && item.delivery_status !== "delivered");
                      return (
                        <Table.Tr key={item.id}>
                          <Table.Td>
                            <Group gap={6}>
                              <Badge color={statusColor(item.status)} variant="light" size="sm">
                                {STATUS_LABELS[item.status] ?? item.status}
                              </Badge>
                              {item.active_lock_id && <Badge color="red" size="xs">已加锁</Badge>}
                            </Group>
                            {item.failure_code && (
                              <Text size="xs" c="red" mt={4}>{item.failure_code}</Text>
                            )}
                          </Table.Td>
                          <Table.Td>
                            <Stack gap={2}>
                              <Text size="xs">支付：{item.payment_status}</Text>
                              <Text size="xs" c="dimmed">交付：{item.delivery_status}</Text>
                            </Stack>
                          </Table.Td>
                          <Table.Td>
                            <Stack gap={2}>
                              <Text size="xs" fw={500}>{planLabel(item.plan)}</Text>
                              <Text size="xs" c="dimmed">{regionLabel(item.exit_country || item.billing_country)}</Text>
                            </Stack>
                          </Table.Td>
                          <Table.Td>{formatAmount(item.amount, item.currency) ?? "-"}</Table.Td>
                          <Table.Td>
                            <Text size="xs" style={{ whiteSpace: "nowrap" }}>
                              {new Date(item.created_at).toLocaleString()}
                            </Text>
                          </Table.Td>
                          <Table.Td><CopyIdBadge value={item.id} prefixLength={10} /></Table.Td>
                          <Table.Td>
                            <Group gap={6} wrap="nowrap">
                              <Button size="xs" variant="subtle" leftSection={<IconListDetails size={13} />}
                                onClick={() => void openEvents(item)}>
                                时间线
                              </Button>
                              {canReconcile && (
                                <Button size="xs" variant="light" color="blue"
                                  leftSection={<IconShieldCheck size={13} />}
                                  loading={reconcilingId === item.id}
                                  onClick={() => void handleReconcile(item.id)}>
                                  只读对账
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

              <Group justify="space-between">
                <Text size="xs" c="dimmed">
                  显示第 {offset + 1} – {Math.min(offset + PAGE_SIZE, page.total)} 条，共 {page.total} 条
                </Text>
                <Group gap="xs">
                  <Button size="xs" variant="default" disabled={!canPrev || loading}
                    onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                    上一页
                  </Button>
                  <Button size="xs" variant="default" disabled={!canNext || loading}
                    onClick={() => setOffset(offset + PAGE_SIZE)}>
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

      <Modal opened={detailOrder !== null} onClose={() => setDetailOrder(null)} title="订单审计与事件时间线" size="lg" centered>
        {detailOrder && (
          <Stack gap="sm">
            <Group gap="md" wrap="wrap">
              <Group gap={4}><Text size="xs">订单：</Text><CopyIdBadge value={detailOrder.id} prefixLength={14} /></Group>
              {detailOrder.job_id && (
                <Group gap={4}><Text size="xs">作业：</Text><CopyIdBadge value={detailOrder.job_id} prefixLength={14} /></Group>
              )}
              {detailOrder.payment_intent_id && (
                <Group gap={4}><Text size="xs">PI：</Text><CopyIdBadge value={detailOrder.payment_intent_id} prefixLength={14} /></Group>
              )}
              <Text size="xs">自动对账次数：{detailOrder.reconcile_attempts}</Text>
            </Group>
            {detailOrder.last_error && (
              <Alert color="red" title="最近错误摘要">
                <Text size="xs">{detailOrder.last_error}</Text>
              </Alert>
            )}
            <Text size="sm" fw={600}>阶段事件</Text>
            {detailLoading ? (
              <Text size="xs" c="dimmed">加载事件中...</Text>
            ) : detailEvents.length === 0 ? (
              <Text size="xs" c="dimmed">该订单暂无关联审计事件。</Text>
            ) : (
              <Stack gap={4}>
                {detailEvents.map((ev, idx) => (
                  <Text key={`${ev.created_at}-${idx}`} size="xs" c="dimmed">
                    {new Date(ev.created_at).toLocaleString()} · <strong>{ev.stage}</strong> ({ev.state})
                    {ev.message ? ` · ${ev.message}` : ""}
                  </Text>
                ))}
              </Stack>
            )}
          </Stack>
        )}
      </Modal>
    </Stack>
  );
}
