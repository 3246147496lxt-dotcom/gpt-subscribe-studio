import {
  Alert,
  Badge,
  Button,
  Group,
  Modal,
  SimpleGrid,
  Stack,
  Text,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconClock,
  IconReceipt,
  IconShieldCheck,
} from "@tabler/icons-react";

import { formatAmount } from "../api/client";
import type { OrderLedgerItem } from "../api/ledger";
import type { QuickPayEvent } from "../api/quickPay";
import { planLabel, regionLabel } from "../plans";
import {
  autoRenewColor,
  autoRenewLabel,
  buildTimingBreakdown,
  checkoutKindLabel,
  deliveryStatusLabel,
  formatDateTimeCompact,
  formatDurationMs,
  formatPreciseDuration,
  hasFailureReason,
  paymentStatusLabel,
  planPillTone,
  simpleOrderStatus,
  sourceColor,
  sourceLabel,
} from "../utils/orderLedgerFormat";
import { CopyIdBadge } from "./CopyIdBadge";
import { STAGE_LABEL } from "./quick-pay/constants";

function InfoRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="order-detail-kv">
      <span className="order-detail-k">{label}</span>
      <div className="order-detail-v">{children}</div>
    </div>
  );
}

export function OrderLedgerDetailModal({
  order,
  events,
  eventsLoading,
  reconciling,
  onClose,
  onReconcile,
  onOpenFailureDetail,
}: {
  order: OrderLedgerItem | null;
  events: QuickPayEvent[];
  eventsLoading: boolean;
  reconciling: boolean;
  onClose: () => void;
  onReconcile: (orderId: string) => void;
  onOpenFailureDetail: (item: OrderLedgerItem) => void;
}) {
  if (!order) {
    return <Modal opened={false} onClose={onClose} title="订单详情" size="lg" centered />;
  }

  const statusInfo = simpleOrderStatus(order.status);
  const planTone = planPillTone(order.plan);
  const amountText = formatAmount(order.amount, order.currency);
  const emailText = order.account_email || order.masked_email || null;
  const hasFail = hasFailureReason(order);
  const canReconcile =
    order.status === "unknown"
    || (order.payment_status === "succeeded" && order.delivery_status !== "delivered");
  const { totalMs, segments } = buildTimingBreakdown(order);
  const barSegments = segments.filter((s) => s.ms != null && s.ms > 0);
  const barTotal = barSegments.reduce((acc, s) => acc + (s.ms ?? 0), 0);

  const idRows = [
    { label: "订单号", value: order.id },
    order.job_id ? { label: "作业号", value: order.job_id } : null,
    order.checkout_session_id ? { label: "Checkout ID", value: order.checkout_session_id } : null,
    order.payment_intent_id ? { label: "PaymentIntent", value: order.payment_intent_id } : null,
    order.purchase_attempt_id ? { label: "购买尝试号", value: order.purchase_attempt_id } : null,
    order.active_lock_id ? { label: "活跃资金锁", value: order.active_lock_id } : null,
  ].filter((x): x is { label: string; value: string } => x !== null);

  return (
    <Modal
      opened
      onClose={onClose}
      size="lg"
      radius="lg"
      padding="lg"
      centered
      title={
        <Group gap="xs" align="center" wrap="wrap">
          <span className="order-detail-title-icon" aria-hidden="true">
            <IconReceipt size={17} stroke={1.9} />
          </span>
          <Text fw={700} size="md">订单详情</Text>
          <span className={`ledger-pill ledger-pill--status-${statusInfo.tone}`}>
            <span className={`ledger-dot ledger-dot--${statusInfo.tone}`} />
            {statusInfo.label}
          </span>
          <span className={`ledger-pill ledger-pill--plan ledger-pill--plan-${planTone}`}>
            {planLabel(order.plan)}
          </span>
          {order.active_lock_id && (
            <Badge color="red" variant="filled" size="xs">资金锁</Badge>
          )}
        </Group>
      }
    >
      <Stack gap="md">
        {/* 1. 顶部 4 格核心指标条 */}
        <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="xs">
          <div className="order-kpi-card">
            <span className="order-kpi-label">订单金额</span>
            <span className="order-kpi-value">{amountText ?? "—"}</span>
            <span className="order-kpi-sub">
              {order.currency ? `${order.currency}${order.tax_delta ? ` · 税差 ${order.tax_delta}` : ""}` : "尚未报价"}
            </span>
          </div>
          <div className="order-kpi-card">
            <span className="order-kpi-label">总耗时</span>
            <span className="order-kpi-value">
              {totalMs != null ? formatDurationMs(totalMs) : "—"}
            </span>
            <span className="order-kpi-sub">
              {totalMs != null ? `${totalMs.toLocaleString()} ms` : "未记录耗时"}
            </span>
          </div>
          <div className="order-kpi-card">
            <span className="order-kpi-label">账号与卡片</span>
            <span className="order-kpi-value order-kpi-value--sm" title={emailText || undefined}>
              {emailText || (order.account_tag ? `#${order.account_tag}` : "—")}
            </span>
            <span className="order-kpi-sub">
              {order.card_brand ? `卡片 ${order.card_brand}` : "未提交卡片"}
            </span>
          </div>
          <div className="order-kpi-card">
            <span className="order-kpi-label">自动续费</span>
            <div style={{ marginTop: 4 }}>
              <Badge size="sm" variant="light" color={autoRenewColor(order.auto_renew_status)}>
                {autoRenewLabel(order.auto_renew_status)}
              </Badge>
            </div>
            <span className="order-kpi-sub">
              {order.auto_renew_canceled_at
                ? formatDateTimeCompact(order.auto_renew_canceled_at)
                : order.auto_cancel_renew
                  ? `已勾选${(order.auto_renew_attempts ?? 0) > 0 ? ` · 尝试 ${order.auto_renew_attempts} 次` : ""}`
                  : "下单未勾选"}
            </span>
          </div>
        </SimpleGrid>

        {/* 2. 耗时细分卡片（核心可视化拆解：建单耗时、支付耗时、关闭续费等） */}
        <section className="order-detail-section">
          <div className="order-detail-section-head">
            <Group gap={6} align="center">
              <IconClock size={15} stroke={1.9} />
              <span>总耗时细分</span>
            </Group>
            <span className="order-detail-section-right">
              {totalMs != null ? `总计 ${formatPreciseDuration(totalMs)}` : "暂无耗时记录"}
            </span>
          </div>

          <div className="order-detail-section-body">
            {barSegments.length > 0 && barTotal > 0 && (
              <div className="timing-stacked-bar" role="img" aria-label="阶段耗时占比条">
                {barSegments.map((seg) => {
                  const widthPct = Math.max(2, ((seg.ms ?? 0) / barTotal) * 100);
                  return (
                    <div
                      key={seg.key}
                      className="timing-stacked-seg"
                      style={{ width: `${widthPct}%`, background: seg.color }}
                      title={`${seg.label}：${formatPreciseDuration(seg.ms)} (${seg.percent}%)`}
                    />
                  );
                })}
              </div>
            )}

            <div className="timing-rows">
              {segments.map((seg) => {
                const hasVal = seg.ms != null;
                return (
                  <div key={seg.key} className="timing-row">
                    <div className="timing-row-name">
                      <span
                        className="timing-dot"
                        style={{ background: hasVal ? seg.color : "#cbd5e1" }}
                      />
                      <span>{seg.label}</span>
                    </div>
                    <div className="timing-row-track">
                      <div
                        className="timing-row-fill"
                        style={{
                          width: hasVal ? `${Math.max(seg.ms! > 0 ? 3 : 0, seg.percent)}%` : "0%",
                          background: seg.color,
                        }}
                      />
                    </div>
                    <div className="timing-row-val">
                      {hasVal ? (
                        <>
                          <strong>{formatPreciseDuration(seg.ms)}</strong>
                          <span className="timing-row-pct">{seg.percent}%</span>
                        </>
                      ) : (
                        <span className="timing-row-empty">{seg.emptyText || "—"}</span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </section>

        {/* 3. 异常速览条（有失败原因时一键直达结构化失败详情） */}
        {hasFail && (
          <Alert
            color="red"
            radius="md"
            icon={<IconAlertCircle size={16} />}
            title={order.failure_code ? `异常码：${order.failure_code.toUpperCase()}` : "订单异常"}
          >
            <Group justify="space-between" align="center" wrap="wrap" gap="xs">
              <Text size="xs">
                {order.last_error || order.auto_renew_error || "该订单执行未成功，可查看结构化接口请求与响应。"}
              </Text>
              <Button
                size="xs"
                variant="white"
                color="red"
                onClick={() => onOpenFailureDetail(order)}
              >
                查看接口失败详情
              </Button>
            </Group>
          </Alert>
        )}

        {/* 4. 业务与网络信息（整齐双栏 key-value，告别拥挤拼接文本） */}
        <section className="order-detail-section">
          <div className="order-detail-section-head">
            <span>履约与通道信息</span>
            <Badge size="xs" variant="light" color={sourceColor(order.source)}>
              {sourceLabel(order.source)}
            </Badge>
          </div>
          <div className="order-detail-kv-grid">
            <InfoRow label="支付 / 交付">
              <span>{paymentStatusLabel(order.payment_status)} / {deliveryStatusLabel(order.delivery_status)}</span>
            </InfoRow>
            <InfoRow label="结算地区">
              <span>
                {regionLabel(order.exit_country || order.billing_country)}
                {order.exit_country && order.exit_country !== order.billing_country ? ` · 账单 ${order.billing_country}` : ""}
              </span>
            </InfoRow>
            <InfoRow label="协议与路由">
              <span>
                {[
                  checkoutKindLabel(order.checkout_kind) ?? "未建单",
                  order.route_profile ? `Route ${order.route_profile}` : null,
                ].filter(Boolean).join(" · ")}
              </span>
            </InfoRow>
            <InfoRow label="凭证与合规">
              <Group gap={6} wrap="wrap">
                <Badge size="xs" variant="light" color={order.billing_verified ? "teal" : "gray"}>
                  {order.billing_verified ? "已免税核验" : "未核验账单"}
                </Badge>
                {order.session_reissued && (
                  <Badge size="xs" variant="light" color="indigo">Session 已重签</Badge>
                )}
              </Group>
            </InfoRow>
            <InfoRow label="创建时间">
              <span className="order-detail-mono">{formatDateTimeCompact(order.created_at)}</span>
            </InfoRow>
            <InfoRow label="完成 / 更新">
              <span className="order-detail-mono">{formatDateTimeCompact(order.finished_at || order.updated_at)}</span>
            </InfoRow>
            <InfoRow label="执行与对账">
              <span>尝试 {order.job_attempts ?? 0} 次 · 对账 {order.reconcile_attempts} 次</span>
            </InfoRow>
            <InfoRow label="出口网络">
              <span>
                {order.proxy_last_exit_ip || order.proxy_last_sid || (order.proxy_rotations ?? 0) > 0
                  ? [
                    order.proxy_last_exit_ip,
                    (order.proxy_rotations ?? 0) > 0 ? `切线 ${order.proxy_rotations} 次` : null,
                    order.proxy_last_sid ? `SID ${order.proxy_last_sid}` : null,
                  ].filter(Boolean).join(" · ")
                  : "默认出口"}
              </span>
            </InfoRow>
          </div>
        </section>

        {/* 5. 关联标识（仅展示有值的 ID，零冗余） */}
        <section className="order-detail-section">
          <div className="order-detail-section-head">
            <span>关联标识</span>
            <span className="order-detail-section-right">点击复制完整 ID</span>
          </div>
          <div className="order-detail-kv-grid">
            {idRows.map((row) => (
              <InfoRow key={row.label} label={row.label}>
                <CopyIdBadge value={row.value} prefixLength={14} />
              </InfoRow>
            ))}
          </div>
        </section>

        {/* 6. 审计事件时间线 */}
        <section className="order-detail-section">
          <div className="order-detail-section-head">
            <span>执行时间线</span>
            <span className="order-detail-section-right">共 {events.length} 条</span>
          </div>
          <div className="order-detail-section-body">
            {eventsLoading ? (
              <Text size="xs" c="dimmed">加载事件中...</Text>
            ) : events.length === 0 ? (
              <Text size="xs" c="dimmed">该订单暂无关联审计事件。</Text>
            ) : (
              <div className="order-timeline">
                {events.map((ev, idx) => (
                  <div key={`${ev.created_at}-${idx}`} className="order-timeline-item">
                    <span
                      className={`order-timeline-dot order-timeline-dot--${
                        ev.state === "failed" ? "failed" : ev.state === "succeeded" ? "success" : "info"
                      }`}
                    />
                    <div className="order-timeline-main">
                      <Group justify="space-between" gap="xs" wrap="nowrap">
                        <Text size="xs" fw={600}>
                          {STAGE_LABEL[ev.stage] ?? ev.stage}
                        </Text>
                        <span className="order-detail-mono order-timeline-time">
                          {formatDateTimeCompact(ev.created_at)}
                        </span>
                      </Group>
                      {ev.message && (
                        <Text size="xs" c="dimmed" mt={2}>{ev.message}</Text>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>

        <Group justify="flex-end" gap="xs" pt={2}>
          {canReconcile && (
            <Button
              size="xs"
              color="orange"
              variant="light"
              leftSection={<IconShieldCheck size={14} />}
              loading={reconciling}
              onClick={() => onReconcile(order.id)}
            >
              触发只读对账
            </Button>
          )}
          <Button size="xs" variant="default" onClick={onClose}>
            关闭
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
