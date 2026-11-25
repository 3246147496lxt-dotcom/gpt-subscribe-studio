import {
  Alert, Badge, Button, Divider, Group, Modal, Paper, Select, SimpleGrid, Stack, Table, Text,
} from "@mantine/core";
import { IconDatabaseCog, IconRefresh, IconShieldCheck } from "@tabler/icons-react";
import { useCallback, useEffect, useState } from "react";

import {
  getSystemDiagnostics,
  runSystemMaintenance,
  type MaintenanceResult,
  type SystemDiagnostics,
} from "../api/system";

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function statusBadge(status: "pass" | "warn" | "fail") {
  if (status === "pass") return <Badge size="xs" color="green" variant="light">通过</Badge>;
  if (status === "warn") return <Badge size="xs" color="yellow" variant="light">提示</Badge>;
  return <Badge size="xs" color="red" variant="filled">阻断</Badge>;
}

const TABLE_LABELS: Record<string, string> = {
  orders: "订单账本",
  quick_pay_jobs: "快速充值队列",
  order_events: "审计事件",
  account_locks: "资金锁",
  account_subscription_snapshots: "账号订阅快照",
};

export function SystemDiagnosticsModal({
  opened,
  onClose,
}: {
  opened: boolean;
  onClose: () => void;
}) {
  const [data, setData] = useState<SystemDiagnostics | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [retentionDays, setRetentionDays] = useState("90");
  const [maintLoading, setMaintLoading] = useState(false);
  const [maintResult, setMaintResult] = useState<MaintenanceResult | null>(null);

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
    if (opened) {
      setMaintResult(null);
      void load();
    }
  }, [opened, load]);

  async function handleMaintenance() {
    setMaintLoading(true);
    setError(null);
    setMaintResult(null);
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

  return (
    <Modal opened={opened} onClose={onClose} title="系统运行自检与维护" size="xl" centered>
      <Stack gap="md">
        <Group justify="space-between" align="center" wrap="wrap">
          <Group gap="xs">
            <IconShieldCheck size={18} />
            <Text size="sm" fw={600}>本地服务健康快照（只读脱敏）</Text>
            {data && (
              <Badge color={data.ok ? "green" : "red"} variant="filled" size="sm">
                {data.ok ? "核心项正常" : "存在阻断项"}
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

        {error && <Alert color="red" title="操作异常">{error}</Alert>}

        {data && (
          <>
            <SimpleGrid cols={{ base: 2, sm: 3 }} spacing="xs">
              <Paper withBorder radius="md" p="xs">
                <Text size="xs" c="dimmed">Python / Node</Text>
                <Text size="sm" fw={600}>
                  {data.python_version} / {data.node_version ?? "未检测到"}
                </Text>
              </Paper>
              <Paper withBorder radius="md" p="xs">
                <Text size="xs" c="dimmed">SQLite 主库 / WAL</Text>
                <Text size="sm" fw={600}>
                  {formatBytes(data.db_bytes)} / {formatBytes(data.wal_bytes)}
                </Text>
              </Paper>
              <Paper withBorder radius="md" p="xs">
                <Text size="xs" c="dimmed">内存缓存单数 / 磁盘余量</Text>
                <Text size="sm" fw={600}>
                  {data.cached_orders} 单 / {data.disk_free_mb} MB
                </Text>
              </Paper>
            </SimpleGrid>

            <div>
              <Text size="xs" fw={600} c="dimmed" mb={6}>数据库表记录统计</Text>
              <Group gap="xs" wrap="wrap">
                {Object.entries(data.table_counts).map(([table, count]) => (
                  <Badge key={table} variant="light" color="gray" size="md">
                    {TABLE_LABELS[table] ?? table}: {count}
                  </Badge>
                ))}
              </Group>
            </div>

            <Table.ScrollContainer minWidth={520}>
              <Table verticalSpacing="xs" fz="xs" highlightOnHover>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th style={{ width: 72 }}>状态</Table.Th>
                    <Table.Th style={{ width: 150 }}>检查项</Table.Th>
                    <Table.Th>说明</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {data.checks.map((item) => (
                    <Table.Tr key={item.key}>
                      <Table.Td>{statusBadge(item.status)}</Table.Td>
                      <Table.Td fw={500}>{item.label}</Table.Td>
                      <Table.Td c="dimmed">{item.message}</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>

            <Divider />

            <Paper withBorder radius="md" p="sm">
              <Stack gap="xs">
                <Group justify="space-between" align="center" wrap="wrap" gap="sm">
                  <div>
                    <Text size="sm" fw={600}>SQLite 安全维护</Text>
                    <Text size="xs" c="dimmed">
                      仅清理超期已完结订单的历史审计事件并执行 WAL 截断（PRAGMA wal_checkpoint），不会删除订单账本、活跃任务或已加锁记录。
                    </Text>
                  </div>
                  <Group gap="xs" wrap="nowrap">
                    <Select
                      size="xs"
                      w={120}
                      aria-label="事件保留天数"
                      value={retentionDays}
                      onChange={(val) => setRetentionDays(val || "90")}
                      data={[
                        { value: "30", label: "保留 30 天" },
                        { value: "90", label: "保留 90 天" },
                        { value: "180", label: "保留 180 天" },
                        { value: "365", label: "保留 365 天" },
                      ]}
                    />
                    <Button
                      size="xs"
                      variant="light"
                      leftSection={<IconDatabaseCog size={14} />}
                      loading={maintLoading}
                      onClick={() => void handleMaintenance()}
                    >
                      执行维护
                    </Button>
                  </Group>
                </Group>

                {maintResult && (
                  <Alert color="teal" title="维护已完成">
                    <Text size="xs">
                      已清理超过 {maintResult.retention_days} 天的终态审计事件 {maintResult.pruned_events} 条；
                      当前主库 {formatBytes(maintResult.db_bytes)}，WAL {formatBytes(maintResult.wal_bytes)}。
                    </Text>
                  </Alert>
                )}
              </Stack>
            </Paper>
          </>
        )}
      </Stack>
    </Modal>
  );
}
