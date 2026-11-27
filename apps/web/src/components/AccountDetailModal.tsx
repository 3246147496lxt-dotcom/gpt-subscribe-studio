import {
  Alert,
  Anchor,
  Badge,
  Button,
  Group,
  Loader,
  Modal,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Textarea,
  Title,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconCheck,
  IconExternalLink,
  IconReceipt,
  IconRefresh,
  IconSearch,
  IconTrash,
  IconUserCircle,
} from "@tabler/icons-react";
import { useState } from "react";

import { fetchAccountSubscription, fetchStripePortal, type AccountSubscription } from "../api/account";
import { cancelAccountAutoRenew } from "../api/adminOps";
import { AdminPageHeader } from "./AdminPageHeader";
import { SessionPreviewBar } from "./SessionPreviewBar";

interface Props {
  opened: boolean;
  onClose: () => void;
}

function StatusBadge({ status }: { status: string }) {
  const tone = status === "使用中" ? "success" : status === "已过期" ? "warning" : "pending";
  return (
    <span className={`ledger-pill ledger-pill--status-${tone}`}>
      <span className={`ledger-dot ledger-dot--${tone}`} />
      {status}
    </span>
  );
}

export function AccountInspectorPanel({
  embedded = false,
  onClose,
}: {
  embedded?: boolean;
  onClose?: () => void;
}) {
  const [session, setSession] = useState("");
  const [detail, setDetail] = useState<AccountSubscription | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [portalLoading, setPortalLoading] = useState(false);
  const [portalError, setPortalError] = useState<string | null>(null);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [cancelNotice, setCancelNotice] = useState<string | null>(null);

  async function run(forceRefresh: boolean) {
    if (loading) return;
    if (!session.trim()) {
      setError("请先粘贴 ChatGPT Session JSON。");
      return;
    }
    setLoading(true);
    setError(null);
    setCancelNotice(null);
    try {
      const next = await fetchAccountSubscription({ session: session.trim(), forceRefresh });
      setDetail(next);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "查询失败，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }

  function handleReset() {
    setSession("");
    setDetail(null);
    setError(null);
    setPortalError(null);
    setCancelNotice(null);
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

  async function handleCancelAutoRenew() {
    if (cancelBusy || !session.trim() || !detail) return;
    setCancelBusy(true);
    setError(null);
    setCancelNotice(null);
    try {
      const res = await cancelAccountAutoRenew({
        session: session.trim(),
        accountId: detail.accountId,
      });
      if (res.canceled || res.already_canceled) {
        setCancelNotice(
          res.message
            || (res.already_canceled ? "该账号的自动续费已处于关闭状态。" : "已通过一等接口关闭下期自动续费，当前周期权益保持不变。"),
        );
        setDetail((prev) => (prev ? { ...prev, autoRenew: "已关闭" } : prev));
      } else {
        setError(res.message || "未能关闭自动续费，请稍后重试。");
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "关闭自动续费失败，请稍后重试。");
    } finally {
      setCancelBusy(false);
    }
  }

  return (
    <Stack gap="md">
      {!embedded && (
        <AdminPageHeader
          title="账号排查"
          badge={
            <span className="ops-live-pill ops-live-pill--ok">
              <span className="ops-pulse-dot" aria-hidden="true" />
              实时直查上游 · 零缓存
            </span>
          }
          subtitle="粘贴客户 Session JSON 实时解析凭证与 oai-did 前缀，直查订阅周期、历史账单与发票，并支持一键关闭自动续费。"
        />
      )}

      {/* 输入与即时预览卡 */}
      <section className={embedded ? undefined : "ops-panel"}>
        <Stack gap="xs">
          <Textarea
            label="ChatGPT Session JSON"
            description="仅在本机内存中用于本次查询，绝不入库；支持 ⌘/Ctrl + Enter 快捷查询。"
            placeholder="粘贴完整 Session JSON（自动识别邮箱、到期状态、导出系统与 oai-did 前缀）"
            autosize
            minRows={3}
            maxRows={6}
            value={session}
            onChange={(event) => setSession(event.currentTarget.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && session.trim() && !loading) {
                event.preventDefault();
                void run(true);
              }
            }}
            disabled={loading}
          />
          <SessionPreviewBar value={session} onClear={handleReset} disabled={loading} />

          <Group justify="space-between" align="center" wrap="wrap" gap="xs" mt={4}>
            <Text size="xs" c="dimmed">
              如遇住宅代理节点波动，后端会自动轮换最多 3 个新鲜出口 SID 重试。
            </Text>
            <Group gap="xs">
              {(session || detail) && (
                <Button
                  size="xs"
                  variant="subtle"
                  color="gray"
                  leftSection={<IconTrash size={14} />}
                  disabled={loading}
                  onClick={handleReset}
                >
                  清空
                </Button>
              )}
              <Button
                size="xs"
                leftSection={<IconSearch size={14} />}
                onClick={() => void run(true)}
                loading={loading}
                disabled={!session.trim()}
              >
                查询实时详情
              </Button>
            </Group>
          </Group>
        </Stack>
      </section>

      {error && (
        <Alert color="red" radius="md" icon={<IconAlertCircle size={18} />} title="操作未完成" withCloseButton onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      {cancelNotice && (
        <Alert color="teal" radius="md" icon={<IconCheck size={16} />} withCloseButton onClose={() => setCancelNotice(null)}>
          <Group justify="space-between" align="center" wrap="wrap" gap="xs">
            <Text size="xs">{cancelNotice}</Text>
            <Button size="compact-xs" variant="light" color="teal" loading={loading} onClick={() => void run(true)}>
              立即回查确认
            </Button>
          </Group>
        </Alert>
      )}

      {!detail && !loading && (
        <section className={embedded ? undefined : "ops-panel"}>
          <Text c="dimmed" size="sm" ta="center" py="lg">
            粘贴 Session 后点击「查询实时详情」，即可查看当前套餐、自动续费状态与历史账单发票。
          </Text>
        </section>
      )}

      {loading && !detail && (
        <section className={embedded ? undefined : "ops-panel"}>
          <Group justify="center" py="xl" gap="sm">
            <Loader size="sm" />
            <Text size="sm" c="dimmed">正在直连上游实时核验订阅状态与账单…</Text>
          </Group>
        </section>
      )}

      {detail && (
        <>
          {/* 账号订阅档案卡 */}
          <section className={embedded ? undefined : "ops-panel"}>
            <Stack gap="md">
              {detail.warning && <Alert color="orange" radius="md">{detail.warning}</Alert>}

              <div className="ops-panel-head" style={{ marginBottom: 0 }}>
                <div className="ops-panel-title-wrap">
                  <IconUserCircle size={18} stroke={1.9} />
                  <h3 className="ops-panel-title" style={{ fontSize: 16 }}>
                    {detail.email || "未知账号"}
                  </h3>
                  <StatusBadge status={detail.status} />
                </div>
                <Group gap="xs">
                  <Text size="xs" c="dimmed">
                    查询于 {detail.queriedAt.slice(11, 16)}（北京时间）
                  </Text>
                  <Button
                    size="xs"
                    variant="default"
                    leftSection={<IconRefresh size={13} />}
                    loading={loading}
                    onClick={() => void run(true)}
                  >
                    重新查询
                  </Button>
                  {embedded && onClose && (
                    <Button size="xs" variant="light" onClick={onClose} disabled={loading}>
                      完成
                    </Button>
                  )}
                </Group>
              </div>

              <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }} spacing="xs">
                <div className="order-kpi-card">
                  <span className="order-kpi-label">当前套餐</span>
                  <span className="order-kpi-value">{detail.planName}</span>
                  <span
                    className="order-kpi-sub"
                    style={{
                      color:
                        detail.status !== "使用中"
                          ? undefined
                          : detail.remainingDays != null && detail.remainingDays <= 7
                            ? "#d97706"
                            : "#0f766e",
                      fontWeight: 600,
                    }}
                  >
                    {detail.status === "使用中" && detail.remainingDays != null
                      ? `剩余 ${detail.remainingDays} 天到期`
                      : detail.status === "未订阅"
                        ? "暂无付费订阅"
                        : "无到期时间"}
                  </span>
                </div>

                <div className="order-kpi-card">
                  <span className="order-kpi-label">自动续费</span>
                  <Group justify="space-between" align="center" mt={4} wrap="nowrap" gap={6}>
                    <span className="order-kpi-value" style={{ marginTop: 0, fontSize: 16 }}>
                      {detail.autoRenew}
                    </span>
                    {detail.status === "使用中" && detail.autoRenew === "已开启" && (
                      <Button
                        size="compact-xs"
                        variant="light"
                        color="orange"
                        loading={cancelBusy}
                        disabled={!session.trim()}
                        onClick={() => void handleCancelAutoRenew()}
                      >
                        关闭续费
                      </Button>
                    )}
                  </Group>
                  <span className="order-kpi-sub">
                    {detail.autoRenew === "已关闭" ? "到期后不再自动扣款" : "支持一等接口实时关闭"}
                  </span>
                </div>

                <div className="order-kpi-card">
                  <span className="order-kpi-label">订阅周期（北京时间）</span>
                  <span className="order-kpi-value order-kpi-value--sm">
                    {detail.endTime ?? "暂未查到"}
                  </span>
                  <span className="order-kpi-sub">
                    开始：{detail.startTime ?? "暂未查到"}
                  </span>
                </div>

                <div className="order-kpi-card">
                  <span className="order-kpi-label">支付渠道与币种</span>
                  <span className="order-kpi-value order-kpi-value--sm">
                    {detail.paymentChannel || "-"}
                  </span>
                  <span className="order-kpi-sub">
                    结算币种：{detail.currency || "-"}
                  </span>
                </div>
              </SimpleGrid>
            </Stack>
          </section>

          {/* 历史账单与发票卡 */}
          <section className={embedded ? undefined : "ops-panel"}>
            <div className="ops-panel-head">
              <div className="ops-panel-title-wrap">
                <IconReceipt size={16} stroke={1.9} />
                <h3 className="ops-panel-title">
                  历史账单{invoices.length > 0 ? ` (${invoices.length} 条)` : ""}
                </h3>
                {detail.billingSource === "upstream_api" && invoices.length > 0 && (
                  <Badge size="xs" variant="light" color="teal">官方账单接口</Badge>
                )}
                {detail.billingSource === "stripe_portal" && invoices.length > 0 && (
                  <Badge size="xs" variant="light" color="blue">Stripe 账单门户</Badge>
                )}
              </div>
              <Button
                variant="subtle"
                size="xs"
                leftSection={<IconExternalLink size={14} />}
                onClick={() => void openPortal()}
                loading={portalLoading}
                disabled={!session.trim()}
              >
                打开 Stripe 官方账单明细
              </Button>
            </div>

            {portalError && <Alert color="red" radius="md" mb="sm">{portalError}</Alert>}

            {invoices.length === 0 ? (
              <Text c="dimmed" size="sm" py="md" ta="center">
                {detail.billingNote || "暂无历史账单记录。"}
              </Text>
            ) : (
              <Table.ScrollContainer minWidth={hasInvoiceLinks ? 560 : 480}>
                <Table className="ledger-clean-table" highlightOnHover={false}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>套餐</Table.Th>
                      <Table.Th>支付时间（北京时间）</Table.Th>
                      <Table.Th>状态</Table.Th>
                      <Table.Th>金额</Table.Th>
                      {hasInvoiceLinks && <Table.Th style={{ textAlign: "right" }}>凭证</Table.Th>}
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {invoices.map((invoice, index) => (
                      <Table.Tr key={`${invoice.paidAt}-${invoice.plan}-${index}`}>
                        <Table.Td fw={550}>{invoice.plan}</Table.Td>
                        <Table.Td><span className="ledger-time">{invoice.paidAt}</span></Table.Td>
                        <Table.Td>
                          <Badge
                            size="xs"
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
                        <Table.Td><span className="ledger-amount">{invoice.amount}</span></Table.Td>
                        {hasInvoiceLinks && (
                          <Table.Td style={{ textAlign: "right" }}>
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
          </section>
        </>
      )}
    </Stack>
  );
}

export function AccountDetailModal({ opened, onClose }: Props) {
  return (
    <Modal opened={opened} onClose={onClose} size="lg" radius="lg" title={<Title order={3}>账号详情</Title>} centered>
      <AccountInspectorPanel embedded onClose={onClose} />
    </Modal>
  );
}
