import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Group,
  Modal,
  Paper,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import {
  IconActivity,
  IconAlertCircle,
  IconBolt,
  IconCheck,
  IconClock,
  IconExternalLink,
  IconListDetails,
  IconLockOpen,
  IconRefresh,
  IconShieldCheck,
  IconUserCircle,
} from "@tabler/icons-react";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  getHealth,
  refreshReferencePrices,
  type HealthSnapshot,
} from "../api/adminOps";
import { formatAmount } from "../api/client";
import { isAbortError } from "../api/http";
import { getOrderLedger, type OrderLedgerPage } from "../api/ledger";
import {
  getQuickPay,
  getQuickPayQueue,
  type QueueStatus,
  type QuickPayJob,
} from "../api/quickPay";
import { getSystemDiagnostics, type SystemDiagnostics } from "../api/system";
import { planLabel } from "../plans";
import {
  formatDurationClock,
  formatMonthDayTime,
  planPillTone,
  simpleOrderStatus,
} from "../utils/orderLedgerFormat";
import { CopyIdBadge } from "./CopyIdBadge";
import { QuickPayQueueTable } from "./quick-pay/QuickPayQueueTable";
import { STAGE_LABEL } from "./quick-pay/constants";

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export function OpsOverviewPanel({
  active = true,
  onNavigate,
  onHealthUpdate,
}: {
  active?: boolean;
  onNavigate: (tab: string) => void;
  onHealthUpdate?: (health: HealthSnapshot) => void;
}) {
  const [health, setHealth] = useState<HealthSnapshot | null>(null);
  const [diagnostics, setDiagnostics] = useState<SystemDiagnostics | null>(null);
  const [ledger, setLedger] = useState<OrderLedgerPage | null>(null);
  const [queue, setQueue] = useState<QueueStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [fxBusy, setFxBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [selectedJob, setSelectedJob] = useState<QuickPayJob | null>(null);
  const [jobModalOpen, setJobModalOpen] = useState(false);
  const [jobLoading, setJobLoading] = useState(false);

  const loadAll = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const [h, d, l, q] = await Promise.all([
        getHealth(signal),
        getSystemDiagnostics(signal),
        getOrderLedger({ limit: 8, offset: 0 }, signal),
        getQuickPayQueue(12, signal),
      ]);
      if (signal?.aborted) return;
      setHealth(h);
      setDiagnostics(d);
      setLedger(l);
      setQueue(q);
      onHealthUpdate?.(h);
    } catch (exc) {
      if (isAbortError(exc)) return;
      setError(exc instanceof Error ? exc.message : "加载运维总览数据失败");
    } finally {
      if (!signal?.aborted) {
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

  async function handleRefreshFx() {
    setFxBusy(true);
    setNotice(null);
    setError(null);
    try {
      const res = await refreshReferencePrices();
      setNotice(
        `汇率已刷新（来源：${res.status.fx_source || "-"} · USD/CNY = ${res.status.usd_cny_rate ?? "-"}）。`,
      );
      await loadAll();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "刷新实时汇率失败");
    } finally {
      setFxBusy(false);
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

  const stats = ledger?.stats;
  const activeLocks = stats?.active_locks ?? 0;
  const failedChecks = diagnostics?.checks.filter((c) => c.status === "fail") ?? [];
  const warnChecks = diagnostics?.checks.filter((c) => c.status === "warn") ?? [];
  const passChecks = diagnostics?.checks.filter((c) => c.status === "pass") ?? [];
  const deliveryRate = stats && stats.total > 0
    ? `${Math.round((stats.delivered / stats.total) * 100)}%`
    : "-";

  return (
    <Stack gap="lg">
      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="md">
          <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
            <div>
              <Group gap="xs" align="center">
                <Title order={2}>运维总览</Title>
                {health && (
                  <Badge color={health.status === "ok" ? "green" : "red"} variant="light">
                    {health.status === "ok" ? "服务正常" : health.status}
                  </Badge>
                )}
                {health?.fake_adapters && (
                  <Badge color="orange" variant="filled">离线模拟模式</Badge>
                )}
              </Group>
              <Text c="dimmed" size="sm" mt={4}>
                聚合服务健康度、并发队列负载、资金锁风险、订单状态与动态汇率同步，便于日常巡检与快速排障。
              </Text>
            </div>
            <Group gap="xs">
              <Button
                size="xs"
                variant="light"
                leftSection={<IconRefresh size={14} />}
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
                刷新总览
              </Button>
            </Group>
          </Group>

          {notice && (
            <Alert color="teal" icon={<IconCheck size={16} />} withCloseButton onClose={() => setNotice(null)}>
              {notice}
            </Alert>
          )}
          {error && <Alert color="red" icon={<IconAlertCircle size={16} />} title="加载异常">{error}</Alert>}

          {activeLocks > 0 && (
            <Alert color="red" icon={<IconLockOpen size={18} />} title={`存在 ${activeLocks} 个未核销资金锁`}>
              <Group justify="space-between" align="center" wrap="wrap" gap="sm">
                <Text size="sm">
                  有待核对（unknown）订单正在锁定对应账号，请及时触发只读对账或在核对银行账单后人工解锁。
                </Text>
                <Button size="xs" color="red" variant="white" onClick={() => onNavigate("locks")}>
                  前往资金对账
                </Button>
              </Group>
            </Alert>
          )}

          {failedChecks.length > 0 && (
            <Alert color="red" icon={<IconAlertCircle size={18} />} title={`系统自检存在 ${failedChecks.length} 项阻断问题`}>
              <Group justify="space-between" align="center" wrap="wrap" gap="sm">
                <Text size="sm">{failedChecks.map((c) => `${c.label}：${c.message}`).join("；")}</Text>
                <Button size="xs" color="red" variant="white" onClick={() => onNavigate("diagnostics")}>
                  查看系统自检
                </Button>
              </Group>
            </Alert>
          )}

          {health && !health.proxy_sid_pinned && !health.fake_adapters && (
            <Alert color="blue" variant="light" title="出口代理 SID 提示">
              <Group justify="space-between" align="center" wrap="wrap" gap="sm">
                <Text size="xs">
                  当前未设置 <Code>GSS_PROXY_SID</Code>，每笔结账会自动分配并固定单笔随机 SID；如需全局共享固定出口 IP，请在「代理网关」或环境变量中配置。
                </Text>
                <Button size="xs" variant="subtle" onClick={() => onNavigate("proxy")}>
                  查看代理设置
                </Button>
              </Group>
            </Alert>
          )}

          <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing="md">
            <Paper withBorder radius="md" p="md">
              <Stack gap={6}>
                <Group justify="space-between">
                  <Text size="xs" c="dimmed" fw={600}>系统健康与存储</Text>
                  <Badge size="xs" color={diagnostics?.ok ? "green" : "orange"} variant="light">
                    {diagnostics ? `${passChecks.length}/${diagnostics.checks.length} 通过` : "-"}
                  </Badge>
                </Group>
                <Text fz={22} fw={700}>
                  {diagnostics ? formatBytes(diagnostics.db_bytes + diagnostics.wal_bytes) : "-"}
                </Text>
                <Text size="xs" c="dimmed">
                  {diagnostics
                    ? `SQLite ${formatBytes(diagnostics.db_bytes)} + WAL ${formatBytes(diagnostics.wal_bytes)} · 磁盘余量 ${diagnostics.disk_free_mb} MB`
                    : "正在检测…"}
                </Text>
                {warnChecks.length > 0 && (
                  <Text size="xs" c="orange">{warnChecks.length} 项提示，点击系统自检查看</Text>
                )}
              </Stack>
            </Paper>

            <Paper withBorder radius="md" p="md">
              <Stack gap={6}>
                <Group justify="space-between">
                  <Text size="xs" c="dimmed" fw={600}>任务队列与并发</Text>
                  <Badge size="xs" color={health?.queue.draining ? "orange" : "blue"} variant="light">
                    {health?.queue.draining ? "排空中" : "运行中"}
                  </Badge>
                </Group>
                <Text fz={22} fw={700}>
                  {health ? `${health.queue.running} / ${health.queue.max_concurrency}` : "-"}
                </Text>
                <Text size="xs" c="dimmed">
                  {health
                    ? `当前排队 ${health.queue.queued} 个 · 可在「策略开关」热调并发上限`
                    : "正在读取…"}
                </Text>
              </Stack>
            </Paper>

            <Paper withBorder radius="md" p="md">
              <Stack gap={6}>
                <Group justify="space-between">
                  <Text size="xs" c="dimmed" fw={600}>资金锁与后台对账</Text>
                  <Badge size="xs" color={activeLocks > 0 ? "red" : "green"} variant="light">
                    {activeLocks > 0 ? `${activeLocks} 锁定中` : "无活跃锁"}
                  </Badge>
                </Group>
                <Text fz={22} fw={700}>
                  {health?.reconciler.running ? "对账器运行中" : "对账器已停用"}
                </Text>
                <Text size="xs" c="dimmed">
                  {health
                    ? `待复核 ${health.reconciler.pending_candidates} 单 · 累计核销 ${health.reconciler.resolved_count} 单 · 间隔 ${health.reconciler.interval_s}s`
                    : "正在读取…"}
                </Text>
              </Stack>
            </Paper>

            <Paper withBorder radius="md" p="md">
              <Stack gap={6}>
                <Group justify="space-between">
                  <Text size="xs" c="dimmed" fw={600}>订单记录概览</Text>
                  <Badge size="xs" color="teal" variant="light">交付率 {deliveryRate}</Badge>
                </Group>
                <Text fz={22} fw={700}>{stats ? `${stats.total} 单` : "-"}</Text>
                <Text size="xs" c="dimmed">
                  {stats
                    ? `已交付 ${stats.delivered} · 待交付 ${stats.paid_pending_delivery} · 待核对 ${stats.unknown} · 失败 ${stats.failed}`
                    : "正在读取…"}
                </Text>
              </Stack>
            </Paper>

            <Paper withBorder radius="md" p="md">
              <Stack gap={6}>
                <Group justify="space-between">
                  <Text size="xs" c="dimmed" fw={600}>动态汇率同步</Text>
                  <Badge
                    size="xs"
                    color={health?.reference_pricing.stale ? "orange" : "green"}
                    variant="light"
                  >
                    {health?.reference_pricing.stale ? "待刷新" : "已同步"}
                  </Badge>
                </Group>
                <Text fz={22} fw={700}>
                  {health?.reference_pricing.usd_cny_rate
                    ? `1 USD ≈ ¥${health.reference_pricing.usd_cny_rate}`
                    : "-"}
                </Text>
                <Text size="xs" c="dimmed">
                  {health
                    ? `来源 ${health.reference_pricing.fx_source} · 更新 ${health.reference_pricing.updated_label || "未记录"}`
                    : "正在读取…"}
                </Text>
              </Stack>
            </Paper>

            <Paper withBorder radius="md" p="md">
              <Stack gap={6}>
                <Group justify="space-between">
                  <Text size="xs" c="dimmed" fw={600}>Webhook 告警通知</Text>
                  <Badge
                    size="xs"
                    color={health?.webhooks.enabled && health?.webhooks.configured ? "green" : "gray"}
                    variant="light"
                  >
                    {health?.webhooks.enabled && health?.webhooks.configured ? "已启用" : "未启用"}
                  </Badge>
                </Group>
                <Text fz={22} fw={700}>
                  {health ? `${health.webhooks.recent_deliveries} 次投递` : "-"}
                </Text>
                <Text size="xs" c="dimmed">
                  {health
                    ? `已订阅 ${health.webhooks.subscribed_events} 类事件 · 可在「告警通知」测试推送`
                    : "正在读取…"}
                </Text>
              </Stack>
            </Paper>
          </SimpleGrid>

          <Group gap="xs" wrap="wrap">
            <Button size="xs" variant="light" leftSection={<IconListDetails size={14} />} onClick={() => onNavigate("history")}>
              订单记录
            </Button>
            <Button size="xs" variant="light" color={activeLocks > 0 ? "red" : "blue"} leftSection={<IconLockOpen size={14} />} onClick={() => onNavigate("locks")}>
              资金对账
            </Button>
            <Button size="xs" variant="light" leftSection={<IconUserCircle size={14} />} onClick={() => onNavigate("account")}>
              账号排查
            </Button>
            <Button size="xs" variant="light" leftSection={<IconActivity size={14} />} onClick={() => onNavigate("diagnostics")}>
              系统自检
            </Button>
            <Button size="xs" variant="light" leftSection={<IconShieldCheck size={14} />} onClick={() => onNavigate("pricing")}>
              地区比价
            </Button>
            <Button size="xs" variant="default" leftSection={<IconBolt size={14} />} onClick={() => onNavigate("quick")}>
              内部代充
            </Button>
            <Button
              component="a"
              href="/"
              target="_blank"
              rel="noopener noreferrer"
              size="xs"
              variant="subtle"
              rightSection={<IconExternalLink size={14} />}
            >
              打开用户门户
            </Button>
          </Group>
        </Stack>
      </Card>

      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="sm">
          <Group justify="space-between" align="center" wrap="wrap">
            <Title order={4}>近期订单记录（最新 {ledger?.items.length ?? 0} 条）</Title>
            <Button size="xs" variant="subtle" onClick={() => onNavigate("history")}>
              查看全部订单记录
            </Button>
          </Group>
          {!ledger || ledger.items.length === 0 ? (
            <Text size="sm" c="dimmed" py="md" ta="center">
              {loading ? "正在加载近期订单…" : "暂无订单记录。"}
            </Text>
          ) : (
            <Table.ScrollContainer minWidth={760}>
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
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {ledger.items.map((item, idx) => {
                    const emailText = item.account_email || item.masked_email || null;
                    const amountText = formatAmount(item.amount, item.currency);
                    const statusInfo = simpleOrderStatus(item.status);
                    const durationClock = formatDurationClock(item.total_ms);
                    const planTone = planPillTone(item.plan);
                    return (
                      <Table.Tr key={item.id}>
                        <Table.Td><span className="ledger-seq">{idx + 1}</span></Table.Td>
                        <Table.Td><CopyIdBadge value={item.id} prefixLength={8} /></Table.Td>
                        <Table.Td>
                          {emailText ? (
                            <div className="ledger-email" title={emailText}>{emailText}</div>
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
                      </Table.Tr>
                    );
                  })}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          )}
        </Stack>
      </Card>

      {queue && queue.jobs.length > 0 && (
        <QuickPayQueueTable
          jobs={queue.jobs}
          activeJobId={selectedJob?.job_id}
          loading={loading}
          onRefresh={() => void loadAll()}
          onSelectJob={(id) => void inspectJob(id)}
        />
      )}

      <Modal
        opened={jobModalOpen}
        onClose={() => setJobModalOpen(false)}
        title="任务队列详情（只读脱敏）"
        size="lg"
        centered
      >
        {jobLoading ? (
          <Text size="sm" c="dimmed" py="md">正在读取任务详情…</Text>
        ) : selectedJob ? (
          <Stack gap="sm">
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
                  {selectedJob.amount} {selectedJob.currency || ""}
                </Badge>
              )}
            </Group>
            <SimpleGrid cols={2} spacing="xs">
              <Text size="xs"><strong>作业 ID：</strong>{selectedJob.job_id}</Text>
              <Text size="xs"><strong>订单 ID：</strong>{selectedJob.order_id || "-"}</Text>
              <Text size="xs"><strong>重试 / 对账：</strong>{selectedJob.attempts ?? 0} / {selectedJob.reconcile_attempts ?? 0}</Text>
              <Text size="xs"><strong>续费状态：</strong>{selectedJob.auto_renew_status || "disabled"}</Text>
            </SimpleGrid>
            {selectedJob.message && <Alert color={selectedJob.status === "failed" ? "red" : "blue"}>{selectedJob.message}</Alert>}
            {selectedJob.diagnostics && Object.keys(selectedJob.diagnostics).length > 0 && (
              <div>
                <Text size="xs" fw={600} c="dimmed" mb={4}>上游诊断摘要</Text>
                <Code block>{JSON.stringify(selectedJob.diagnostics, null, 2)}</Code>
              </div>
            )}
            {selectedJob.timings_ms && Object.keys(selectedJob.timings_ms).length > 0 && (
              <div>
                <Text size="xs" fw={600} c="dimmed" mb={4}>阶段耗时（ms）</Text>
                <Group gap={6} wrap="wrap">
                  {Object.entries(selectedJob.timings_ms).map(([k, v]) => (
                    <Badge key={k} size="xs" variant="light" color="gray">{k}: {v} ms</Badge>
                  ))}
                </Group>
              </div>
            )}
          </Stack>
        ) : null}
      </Modal>
    </Stack>
  );
}
