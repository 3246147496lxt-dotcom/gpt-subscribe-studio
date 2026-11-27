import {
  Alert,
  Badge,
  Button,
  Group,
  Modal,
  Select,
  Skeleton,
  Stack,
  Table,
  Text,
} from "@mantine/core";
import {
  IconActivity,
  IconArrowRight,
  IconCheck,
  IconCpu,
  IconDatabase,
  IconDatabaseCog,
  IconDeviceFloppy,
  IconRefresh,
  IconServer,
  IconShieldCheck,
} from "@tabler/icons-react";
import { useCallback, useEffect, useState } from "react";

import {
  getSystemDiagnostics,
  runSystemMaintenance,
  type MaintenanceResult,
  type SystemDiagnostics,
} from "../api/system";
import { AdminPageHeader } from "./AdminPageHeader";

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

const TABLE_LABELS: Record<string, string> = {
  orders: "订单记录",
  quick_pay_jobs: "快速充值队列",
  order_events: "审计事件",
  account_locks: "资金锁",
  account_subscription_snapshots: "订阅快照",
  portal_users: "门户用户",
  portal_tasks: "门户任务",
  portal_task_ownership: "门户任务归属",
};

const CHECK_NAV_TARGET: Record<string, { tab: string; label: string }> = {
  verified_billing: { tab: "billing", label: "固定账单" },
  proxy: { tab: "proxy", label: "代理网关" },
  background_services: { tab: "flags", label: "策略开关" },
};

export function SystemDiagnosticsPanel({
  embedded = false,
  active = true,
  onNavigate,
}: {
  embedded?: boolean;
  active?: boolean;
  onNavigate?: (tab: string) => void;
}) {
  const [data, setData] = useState<SystemDiagnostics | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [retentionDays, setRetentionDays] = useState("90");
  const [maintLoading, setMaintLoading] = useState(false);
  const [maintResult, setMaintResult] = useState<MaintenanceResult | null>(null);
  const [beforeMaintBytes, setBeforeMaintBytes] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await getSystemDiagnostics();
      setData(next);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "读取系统自检信息失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active) {
      setMaintResult(null);
      void load();
    }
  }, [active, load]);

  async function handleMaintenance() {
    setMaintLoading(true);
    setError(null);
    setMaintResult(null);
    setBeforeMaintBytes(data ? data.db_bytes + data.wal_bytes : null);
    try {
      const days = Number.parseInt(retentionDays, 10) || 90;
      const res = await runSystemMaintenance(days);
      setMaintResult(res);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "执行 SQLite 维护失败");
    } finally {
      setMaintLoading(false);
    }
  }

  const passCount = data?.checks.filter((c) => c.status === "pass").length ?? 0;
  const failCount = data?.checks.filter((c) => c.status === "fail").length ?? 0;
  const totalBytes = data ? data.db_bytes + data.wal_bytes : 0;
  const walLarge = Boolean(data && data.wal_bytes > 512 * 1024 && data.wal_bytes > data.db_bytes * 2);
  const savedBytes =
    beforeMaintBytes != null && maintResult
      ? Math.max(0, beforeMaintBytes - (maintResult.db_bytes + maintResult.wal_bytes))
      : 0;

  const content = (
    <Stack gap="md">
      {!embedded ? (
        <AdminPageHeader
          title="系统自检"
          badge={
            data ? (
              <span className={`ops-live-pill ${data.ok ? "ops-live-pill--ok" : "ops-live-pill--error"}`}>
                <span className="ops-pulse-dot" aria-hidden="true" />
                {data.ok ? `全部 ${passCount}/${data.checks.length} 项通过` : `存在 ${failCount} 项阻断`}
              </span>
            ) : null
          }
          subtitle="核验本地运行环境、固定美国免税账单、代理网关与 SQLite 存储占用，并支持安全清理超期审计事件与 WAL 截断。"
          actions={
            <Button
              size="xs"
              variant="default"
              leftSection={<IconRefresh size={14} />}
              loading={loading}
              onClick={() => void load()}
            >
              重新自检
            </Button>
          }
        />
      ) : (
        <Group justify="space-between" align="center" wrap="wrap" gap="sm">
          <Group gap="xs">
            <IconShieldCheck size={18} />
            <Text size="sm" fw={700}>本地服务健康快照（只读脱敏）</Text>
            {data && (
              <Badge color={data.ok ? "green" : "red"} variant="light" size="sm">
                {data.ok ? `${passCount}/${data.checks.length} 通过` : `${failCount} 项阻断`}
              </Badge>
            )}
          </Group>
          <Button
            size="xs"
            variant="subtle"
            leftSection={<IconRefresh size={14} />}
            loading={loading}
            onClick={() => void load()}
          >
            重新自检
          </Button>
        </Group>
      )}

      {error && <Alert color="red" radius="md" title="操作异常">{error}</Alert>}

      {/* 4 格核心运行与存储指标卡 */}
      <div className="ops-kpi-grid">
        <div className="ops-kpi-card ops-kpi-static">
          <div className="ops-kpi-top">
            <span className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--blue"><IconCpu size={16} /></span>
              <span className="ops-kpi-title">运行时环境</span>
            </span>
            <Badge size="xs" color="blue" variant="light">本机进程</Badge>
          </div>
          {!data ? (
            <Skeleton height={26} width="70%" radius="sm" />
          ) : (
            <div className="ops-kpi-main">
              <span className="ops-kpi-value" style={{ fontSize: 20 }}>Py {data.python_version}</span>
            </div>
          )}
          <div className="ops-kpi-foot">
            <span>Node：{data?.node_version ?? "未检测到"}</span>
          </div>
        </div>

        <div className="ops-kpi-card ops-kpi-static">
          <div className="ops-kpi-top">
            <span className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--teal"><IconDatabase size={16} /></span>
              <span className="ops-kpi-title">SQLite 总占用</span>
            </span>
            <Badge size="xs" color={walLarge ? "orange" : "teal"} variant="light">
              {walLarge ? "可截断 WAL" : "正常"}
            </Badge>
          </div>
          {!data ? (
            <Skeleton height={26} width="55%" radius="sm" />
          ) : (
            <div className="ops-kpi-main">
              <span className="ops-kpi-value" style={{ fontSize: 22 }}>{formatBytes(totalBytes)}</span>
            </div>
          )}
          <div className="ops-kpi-foot">
            <span>
              {data ? `主库 ${formatBytes(data.db_bytes)} · WAL ${formatBytes(data.wal_bytes)}` : "检测中…"}
            </span>
          </div>
        </div>

        <div className="ops-kpi-card ops-kpi-static">
          <div className="ops-kpi-top">
            <span className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--indigo"><IconServer size={16} /></span>
              <span className="ops-kpi-title">内存热缓存</span>
            </span>
            <Badge size="xs" color="gray" variant="light">LRU 有界</Badge>
          </div>
          {!data ? (
            <Skeleton height={26} width="45%" radius="sm" />
          ) : (
            <div className="ops-kpi-main">
              <span className="ops-kpi-value" style={{ fontSize: 22 }}>{data.cached_orders}</span>
              <span className="ops-kpi-unit">单</span>
            </div>
          )}
          <div className="ops-kpi-foot">
            <span>{data ? `持久化数据表 ${Object.keys(data.table_counts).length} 张` : "检测中…"}</span>
          </div>
        </div>

        <div className="ops-kpi-card ops-kpi-static">
          <div className="ops-kpi-top">
            <span className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--emerald"><IconDeviceFloppy size={16} /></span>
              <span className="ops-kpi-title">磁盘可用空间</span>
            </span>
            <Badge size="xs" color={data && data.disk_free_mb < 1024 ? "red" : "green"} variant="light">
              {data && data.disk_free_mb < 1024 ? "空间偏低" : "充足"}
            </Badge>
          </div>
          {!data ? (
            <Skeleton height={26} width="55%" radius="sm" />
          ) : (
            <div className="ops-kpi-main">
              <span className="ops-kpi-value" style={{ fontSize: 22 }}>{formatDiskSpace(data.disk_free_mb)}</span>
            </div>
          )}
          <div className="ops-kpi-foot">
            <span>{data ? `${data.disk_free_mb.toLocaleString()} MB 剩余` : "检测中…"}</span>
          </div>
        </div>
      </div>

      {/* 自检项健康明细（支持一键跳转对应配置页） */}
      <section className="ops-panel">
        <div className="ops-panel-head">
          <div className="ops-panel-title-wrap">
            <IconActivity size={16} stroke={1.9} />
            <h3 className="ops-panel-title">核心检查清单</h3>
            {data && (
              <Badge size="xs" color={data.ok ? "green" : "red"} variant="light">
                {passCount}/{data.checks.length} 通过
              </Badge>
            )}
          </div>
        </div>

        {!data ? (
          <Stack gap="xs">
            <Skeleton height={36} radius="sm" />
            <Skeleton height={36} radius="sm" />
            <Skeleton height={36} radius="sm" />
          </Stack>
        ) : (
          <Table.ScrollContainer minWidth={560}>
            <Table className="ledger-clean-table" highlightOnHover={false}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th style={{ width: 92 }}>状态</Table.Th>
                  <Table.Th style={{ width: 190 }}>检查项</Table.Th>
                  <Table.Th>核验详情</Table.Th>
                  {onNavigate && <Table.Th style={{ width: 112, textAlign: "right" }}>快捷处理</Table.Th>}
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {data.checks.map((item) => {
                  const tone = item.status === "pass" ? "success" : item.status === "warn" ? "warning" : "failed";
                  const label = item.status === "pass" ? "通过" : item.status === "warn" ? "提醒" : "阻断";
                  const nav = CHECK_NAV_TARGET[item.key];
                  return (
                    <Table.Tr key={item.key}>
                      <Table.Td>
                        <span className={`ledger-pill ledger-pill--status-${tone}`}>
                          <span className={`ledger-dot ledger-dot--${tone}`} />
                          {label}
                        </span>
                      </Table.Td>
                      <Table.Td fw={600}>{item.label}</Table.Td>
                      <Table.Td>
                        <Text size="xs" c={item.status === "fail" ? "red" : "dimmed"}>{item.message}</Text>
                      </Table.Td>
                      {onNavigate && (
                        <Table.Td style={{ textAlign: "right" }}>
                          {nav ? (
                            <Button
                              size="compact-xs"
                              variant={item.status === "pass" ? "subtle" : "light"}
                              color={item.status === "fail" ? "red" : item.status === "warn" ? "orange" : "blue"}
                              rightSection={<IconArrowRight size={12} />}
                              onClick={() => onNavigate(nav.tab)}
                            >
                              {nav.label}
                            </Button>
                          ) : (
                            <span className="ledger-muted">—</span>
                          )}
                        </Table.Td>
                      )}
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
      </section>

      {/* 底部双栏 Bento：左侧数据表记录统计 + 右侧 SQLite 安全维护 */}
      {data && (
        <div className="ops-bento-grid">
          <section className="ops-panel">
            <div className="ops-panel-head">
              <div className="ops-panel-title-wrap">
                <IconDatabase size={16} stroke={1.9} />
                <h3 className="ops-panel-title">SQLite 表记录分布</h3>
              </div>
              <span className="ops-panel-sub">实时只读统计</span>
            </div>
            <div className="ops-table-counts-grid">
              {Object.entries(data.table_counts).map(([table, count]) => (
                <div key={table} className="ops-table-count-card" title={table}>
                  <span className="ops-table-count-name">{TABLE_LABELS[table] ?? table}</span>
                  <span className="ops-table-count-num">{count.toLocaleString()}</span>
                </div>
              ))}
            </div>
          </section>

          <section className="ops-panel">
            <div className="ops-panel-head">
              <div className="ops-panel-title-wrap">
                <IconDatabaseCog size={16} stroke={1.9} />
                <h3 className="ops-panel-title">SQLite 安全维护</h3>
              </div>
              {walLarge && (
                <Badge size="xs" color="orange" variant="light">建议执行 WAL 截断</Badge>
              )}
            </div>

            <Stack gap="sm" style={{ flex: 1, justifyContent: "space-between" }}>
              <Text size="xs" c="dimmed" lh={1.65}>
                仅清理超期已完结订单的历史审计事件并执行 WAL 截断（<code>PRAGMA wal_checkpoint(TRUNCATE)</code>），绝不会删除订单主记录、活跃任务或未核销资金锁。
              </Text>

              <Group gap="xs" wrap="wrap" align="center">
                <Select
                  size="xs"
                  w={136}
                  aria-label="事件保留天数"
                  value={retentionDays}
                  onChange={(val) => setRetentionDays(val || "90")}
                  data={[
                    { value: "30", label: "保留近 30 天" },
                    { value: "90", label: "保留近 90 天" },
                    { value: "180", label: "保留近 180 天" },
                    { value: "365", label: "保留近 365 天" },
                  ]}
                />
                <Button
                  size="xs"
                  variant={walLarge ? "filled" : "light"}
                  color={walLarge ? "blue" : "teal"}
                  leftSection={<IconDatabaseCog size={14} />}
                  loading={maintLoading}
                  onClick={() => void handleMaintenance()}
                >
                  执行安全维护
                </Button>
              </Group>

              {maintResult && (
                <Alert color="teal" radius="md" icon={<IconCheck size={16} />} title="维护已完成">
                  <Text size="xs">
                    已清理超过 {maintResult.retention_days} 天的终态审计事件 {maintResult.pruned_events} 条；
                    当前主库 {formatBytes(maintResult.db_bytes)}，WAL {formatBytes(maintResult.wal_bytes)}
                    {savedBytes > 0 ? `（释放 ${formatBytes(savedBytes)}）` : ""}。
                  </Text>
                </Alert>
              )}
            </Stack>
          </section>
        </div>
      )}
    </Stack>
  );

  return content;
}

export function SystemDiagnosticsModal({
  opened,
  onClose,
}: {
  opened: boolean;
  onClose: () => void;
}) {
  return (
    <Modal opened={opened} onClose={onClose} title="系统自检与维护" size="lg" centered>
      <SystemDiagnosticsPanel embedded active={opened} />
    </Modal>
  );
}
