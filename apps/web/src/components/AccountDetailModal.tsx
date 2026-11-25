import {
  Alert, Anchor, Badge, Button, Group, Loader, Modal, Paper, SimpleGrid, Stack, Table, Text, Textarea, Title,
} from "@mantine/core";
import { IconAlertCircle, IconExternalLink, IconRefresh } from "@tabler/icons-react";
import { useState } from "react";

import { fetchAccountSubscription, fetchStripePortal, type AccountSubscription } from "../api/account";

interface Props {
  opened: boolean;
  onClose: () => void;
}

function StatusBadge({ status }: { status: string }) {
  const active = status === "使用中";
  return (
    <Badge color={active ? "green" : status === "已过期" ? "orange" : "gray"} variant="light" size="lg"
      leftSection={<span aria-hidden="true" style={{
        width: 7, height: 7, borderRadius: "50%", background: active ? "#2f9e44" : "#adb5bd",
      }} />}>
      {status}
    </Badge>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <Stack gap={2}>
      <Text size="xs" c="dimmed">{label}</Text>
      <Text size="sm" fw={600}>{value || "-"}</Text>
    </Stack>
  );
}

export function AccountDetailModal({ opened, onClose }: Props) {
  const [session, setSession] = useState("");
  const [detail, setDetail] = useState<AccountSubscription | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [portalLoading, setPortalLoading] = useState(false);
  const [portalError, setPortalError] = useState<string | null>(null);

  async function run(forceRefresh: boolean) {
    if (loading) return;
    if (!session.trim()) {
      setError("请先粘贴 ChatGPT Session JSON。");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const next = await fetchAccountSubscription({ session: session.trim(), forceRefresh });
      setDetail(next);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "查询失败，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }

  const invoices = detail?.invoices ?? [];
  const hasInvoiceLinks = invoices.some((item) => Boolean(item.invoiceUrl));

  async function openPortal() {
    if (portalLoading) return;
    if (!session.trim()) {
      setPortalError("请先粘贴 Session JSON。");
      return;
    }
    setPortalLoading(true);
    setPortalError(null);
    try {
      const portal = await fetchStripePortal({ session: session.trim() });
      window.open(portal.url, "_blank", "noopener,noreferrer");
    } catch (exc) {
      setPortalError(exc instanceof Error ? exc.message : "无法打开账单门户。");
    } finally {
      setPortalLoading(false);
    }
  }

  return (
    <Modal opened={opened} onClose={onClose} size="lg" title={<Title order={3}>账号详情</Title>} centered>
      <Stack gap="md">
        <Textarea
          label="ChatGPT Session JSON"
          description="只在本机内存中用于本次查询，不会写入数据库。"
          placeholder="粘贴完整 session JSON 后点击查询"
          autosize minRows={3} maxRows={6}
          value={session}
          onChange={(event) => setSession(event.currentTarget.value)}
          disabled={loading}
        />
        <Group justify="flex-end">
          <Button onClick={() => void run(false)} loading={loading} disabled={!session.trim()}>
            查询详情
          </Button>
        </Group>

        {error && (
          <Alert color="red" icon={<IconAlertCircle size={18} />} title="查询未完成">{error}</Alert>
        )}

        {!detail && !loading && (
          <Text c="dimmed" ta="center" py="xl">粘贴 Session 后查询，即可看到订阅状态与历史账单。</Text>
        )}

        {loading && !detail && (
          <Group justify="center" py="xl"><Loader size="sm" /><Text size="sm" c="dimmed">正在查询上游订阅状态…</Text></Group>
        )}

        {detail && (
          <Stack gap="md">
            {detail.warning && <Alert color="orange">{detail.warning}</Alert>}

            <Group justify="space-between" align="center">
              <Group gap="xs">
                <Text fw={700} size="lg">{detail.email || "未知账号"}</Text>
                {detail.cached && <Badge variant="outline" color="gray">缓存</Badge>}
              </Group>
              <StatusBadge status={detail.status} />
            </Group>

            <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
              <Paper withBorder radius="md" p="md">
                <Stack gap="sm">
                  <Text size="xs" c="dimmed" tt="uppercase" fw={700}>订阅周期（北京时间）</Text>
                  <Field label="开始时间" value={detail.startTime ?? "-"} />
                  <Field label="到期时间" value={detail.endTime ?? "-"} />
                </Stack>
              </Paper>
              <Paper withBorder radius="md" p="md">
                <Stack gap="xs">
                  <Text size="xs" c="dimmed" tt="uppercase" fw={700}>当前套餐</Text>
                  <Title order={3}>{detail.planName}</Title>
                  <Text fw={700} c={detail.remainingDays != null && detail.remainingDays <= 7 ? "orange" : "teal"}>
                    {detail.remainingDays == null ? "无到期时间" : `剩余 ${detail.remainingDays} 天到期`}
                  </Text>
                </Stack>
              </Paper>
            </SimpleGrid>

            <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="md">
              <Field label="自动续费" value={detail.autoRenew} />
              <Field label="支付渠道" value={detail.paymentChannel} />
              <Field label="订阅币种" value={detail.currency} />
            </SimpleGrid>

            <Stack gap="xs">
              <Group justify="space-between" align="center">
                <Group gap="xs">
                  <Text fw={600} size="sm">历史账单</Text>
                  {detail.billingSource === "stripe_portal" && invoices.length > 0 && (
                    <Badge size="xs" variant="light" color="blue">Stripe 账单门户</Badge>
                  )}
                </Group>
                <Button variant="subtle" size="xs" leftSection={<IconExternalLink size={14} />}
                  onClick={() => void openPortal()} loading={portalLoading}
                  disabled={!session.trim()}>
                  打开 Stripe 官方账单明细
                </Button>
              </Group>
              {portalError && <Text size="xs" c="red">{portalError}</Text>}
              {invoices.length === 0 ? (
                <Text c="dimmed" size="sm" py="sm">{detail.billingNote || "暂无历史账单。"}</Text>
              ) : (
                <Table.ScrollContainer minWidth={hasInvoiceLinks ? 540 : 480}>
                  <Table verticalSpacing="sm" highlightOnHover>
                    <Table.Thead>
                      <Table.Tr>
                        <Table.Th>套餐</Table.Th>
                        <Table.Th>支付时间（北京时间）</Table.Th>
                        <Table.Th>状态</Table.Th>
                        <Table.Th>金额</Table.Th>
                        {hasInvoiceLinks && <Table.Th>凭证</Table.Th>}
                      </Table.Tr>
                    </Table.Thead>
                    <Table.Tbody>
                      {invoices.map((invoice, index) => (
                        <Table.Tr key={`${invoice.paidAt}-${invoice.plan}-${index}`}>
                          <Table.Td>{invoice.plan}</Table.Td>
                          <Table.Td>{invoice.paidAt}</Table.Td>
                          <Table.Td>
                            <Badge
                              color={
                                invoice.status === "已支付"
                                  ? "green"
                                  : invoice.status === "已退款"
                                    ? "blue"
                                    : invoice.status === "支付失败"
                                      ? "red"
                                      : "gray"
                              }
                              variant="light"
                            >
                              {invoice.status}
                            </Badge>
                          </Table.Td>
                          <Table.Td>{invoice.amount}</Table.Td>
                          {hasInvoiceLinks && (
                            <Table.Td>
                              {invoice.invoiceUrl ? (
                                <Anchor
                                  href={invoice.invoiceUrl}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  size="xs"
                                >
                                  查看发票
                                </Anchor>
                              ) : (
                                <Text size="xs" c="dimmed">-</Text>
                              )}
                            </Table.Td>
                          )}
                        </Table.Tr>
                      ))}
                    </Table.Tbody>
                  </Table>
                </Table.ScrollContainer>
              )}
            </Stack>

            <Group justify="space-between" align="center">
              <Text size="xs" c="dimmed">
                最近查询 {detail.queriedAt.slice(11, 16)}（北京时间）
              </Text>
              <Group gap="xs">
                <Button variant="light" leftSection={<IconRefresh size={16} />}
                  onClick={() => void run(true)} loading={loading}>
                  重新查询
                </Button>
                <Button variant="default" onClick={onClose} disabled={loading}>完成</Button>
              </Group>
            </Group>
          </Stack>
        )}
      </Stack>
    </Modal>
  );
}
