import { Badge, Button, Card, Group, Stack, Table, Text, Title } from "@mantine/core";
import { IconRefresh } from "@tabler/icons-react";

import type { QueueJobSummary } from "../../api/quickPay";
import { CopyIdBadge } from "../CopyIdBadge";
import { FAILURE_LABEL, STAGE_LABEL } from "./constants";

export function QuickPayQueueTable({
  jobs,
  activeJobId,
  loading,
  onRefresh,
  onSelectJob,
}: {
  jobs: QueueJobSummary[];
  activeJobId?: string | null;
  loading: boolean;
  onRefresh: () => void;
  onSelectJob: (jobId: string) => void;
}) {
  if (jobs.length === 0) return null;

  return (
    <Card withBorder radius="lg" p="lg" className="workspace-panel">
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Title order={4}>服务端持久化任务队列（近期 {jobs.length} 条）</Title>
        <Button size="xs" variant="subtle" leftSection={<IconRefresh size={14} />} loading={loading} onClick={onRefresh}>
          刷新
        </Button>
      </Group>
      <Table.ScrollContainer minWidth={660} mt="sm">
        <Table highlightOnHover withTableBorder fz="xs">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>作业 ID</Table.Th>
              <Table.Th>状态 / 阶段与失败原因</Table.Th>
              <Table.Th>金额</Table.Th>
              <Table.Th>耗时</Table.Th>
              <Table.Th>创建时间</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {jobs.map((item) => {
              const selected = activeJobId === item.job_id;
              const failHint = item.status === "failed"
                ? (item.failure_code ? (FAILURE_LABEL[item.failure_code] || item.failure_code) : "点击查看失败步骤与诊断")
                : null;
              return (
                <Table.Tr
                  key={item.job_id}
                  style={{
                    cursor: "pointer",
                    background: selected ? "var(--studio-tint)" : undefined,
                  }}
                  onClick={() => onSelectJob(item.job_id)}
                >
                  <Table.Td><CopyIdBadge value={item.job_id} prefixLength={10} /></Table.Td>
                  <Table.Td>
                    <Stack gap={2}>
                      <Group gap={6} wrap="nowrap">
                        <Badge
                          size="xs"
                          color={
                            item.status === "succeeded"
                              ? "green"
                              : item.status === "failed"
                                ? "red"
                                : item.status === "unknown" || item.status === "requires_action"
                                  ? "orange"
                                  : "blue"
                          }
                        >
                          {item.status}
                        </Badge>
                        <span>
                          {item.status === "queued" && item.queue_position
                            ? `排队第 ${item.queue_position} 位`
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
                  <Table.Td>{item.amount ? `${item.amount} ${item.currency || ""}` : "-"}</Table.Td>
                  <Table.Td>{item.timings_ms?.total_ms != null ? `${item.timings_ms.total_ms} ms` : "-"}</Table.Td>
                  <Table.Td style={{ whiteSpace: "nowrap" }}>{new Date(item.created_at).toLocaleString()}</Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
    </Card>
  );
}
