import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Group,
  Modal,
  SimpleGrid,
  Stack,
  Text,
  Textarea,
  TextInput,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconCheck,
  IconGavel,
  IconReceiptRefund,
  IconX,
} from "@tabler/icons-react";
import { useEffect, useState } from "react";

import { formatAmount } from "../api/client";
import {
  submitOrderManualVerdict,
  type OrderLedgerItem,
  type OrderManualVerdictResult,
  type OrderManualVerdictValue,
} from "../api/ledger";
import { planLabel } from "../plans";
import {
  deliveryStatusLabel,
  paymentStatusLabel,
  planPillTone,
  simpleOrderStatus,
} from "../utils/orderLedgerFormat";
import { CopyIdBadge } from "./CopyIdBadge";

const QUICK_NOTES: Record<OrderManualVerdictValue, string[]> = {
  delivered: [
    "已核查目标账号订阅权益已到账",
    "已核查权益生效，并已人工关闭自动续费",
  ],
  failed: [
    "已核对银行 / Stripe 账单确认未扣款",
    "早期测试单 / 未进入扣款环节，人工清理归档",
  ],
  refunded: [
    "已在 Stripe 后台办理全额退款并通知客户",
    "上游异常重复扣款，已核实退款到账",
  ],
};

function defaultVerdictFor(order: OrderLedgerItem): OrderManualVerdictValue {
  if (order.status === "delivered" || order.status === "active") return "refunded";
  if (order.payment_status === "succeeded") return "delivered";
  return "failed";
}

export function OrderManualVerdictModal({
  order,
  onClose,
  onSuccess,
}: {
  order: OrderLedgerItem | null;
  onClose: () => void;
  onSuccess: (result: OrderManualVerdictResult) => void;
}) {
  const [verdict, setVerdict] = useState<OrderManualVerdictValue>("delivered");
  const [note, setNote] = useState("");
  const [operator, setOperator] = useState("web-operator");
  const [confirmNoCharge, setConfirmNoCharge] = useState(false);
  const [markAutoRenewCanceled, setMarkAutoRenewCanceled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!order) return;
    setVerdict(defaultVerdictFor(order));
    setNote("");
    setConfirmNoCharge(false);
    setMarkAutoRenewCanceled(order.auto_renew_status === "manual_required");
    setError(null);
  }, [order]);

  if (!order) {
    return <Modal opened={false} onClose={onClose} title="人工判定订单状态" centered />;
  }

  const paidConfirmed =
    order.payment_status === "succeeded"
    || order.status === "delivered"
    || order.status === "active";

  const needsNoChargeConfirm =
    verdict === "failed"
    && (order.status === "unknown"
      || order.payment_status === "processing"
      || Boolean(order.active_lock_id));

  const statusInfo = simpleOrderStatus(order.status);
  const amountText = formatAmount(order.amount, order.currency);
  const canSubmit =
    !busy
    && Boolean(note.trim())
    && !(verdict === "failed" && paidConfirmed)
    && (!needsNoChargeConfirm || confirmNoCharge);

  async function handleSubmit() {
    if (!order || !canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const result = await submitOrderManualVerdict(order.id, {
        verdict,
        note,
        operator,
        confirm_no_charge: confirmNoCharge,
        mark_auto_renew_canceled: verdict === "delivered" ? markAutoRenewCanceled : false,
      });
      onSuccess(result);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "提交人工判定失败");
    } finally {
      setBusy(false);
    }
  }

  const options: Array<{
    value: OrderManualVerdictValue;
    title: string;
    desc: string;
    tone: "teal" | "red" | "blue";
    icon: React.ReactNode;
    disabled?: boolean;
  }> = [
    {
      value: "delivered",
      title: "已成功交付",
      desc: "确认已扣款且订阅生效；自动核销关联资金锁",
      tone: "teal",
      icon: <IconCheck size={15} />,
    },
    {
      value: "failed",
      title: "未扣款 / 失败",
      desc: paidConfirmed ? "本单已确认扣款，禁止判失败（请选已退款）" : "确认未扣款或已终止；同步释放关联资金锁",
      tone: "red",
      icon: <IconX size={15} />,
      disabled: paidConfirmed,
    },
    {
      value: "refunded",
      title: "已退款",
      desc: "标记已办理退款并回收权益；同步释放关联锁",
      tone: "blue",
      icon: <IconReceiptRefund size={15} />,
    },
  ];

  return (
    <Modal
      opened
      onClose={() => { if (!busy) onClose(); }}
      size="lg"
      radius="lg"
      padding="lg"
      centered
      title={
        <Group gap="xs" align="center" wrap="nowrap">
          <span className="order-detail-title-icon" aria-hidden="true">
            <IconGavel size={17} stroke={1.9} />
          </span>
          <Text fw={700} size="md">人工判定订单状态</Text>
          <span className={`ledger-pill ledger-pill--plan ledger-pill--plan-${planPillTone(order.plan)}`}>
            {planLabel(order.plan)}
          </span>
        </Group>
      }
    >
      <Stack gap="md">
        {/* 当前订单核对摘要 */}
        <div className="order-detail-section">
          <div className="order-detail-section-head">
            <Group gap={8} wrap="wrap">
              <CopyIdBadge value={order.id} prefixLength={12} />
              <span className={`ledger-pill ledger-pill--status-${statusInfo.tone}`}>
                <span className={`ledger-dot ledger-dot--${statusInfo.tone}`} />
                当前：{statusInfo.label}
              </span>
              {order.active_lock_id && (
                <Badge size="xs" color="red" variant="filled">存在活跃资金锁</Badge>
              )}
            </Group>
            <span className="order-detail-section-right">
              {amountText ?? "尚未报价"}
            </span>
          </div>
          <div className="order-detail-kv-grid">
            <div className="order-detail-kv">
              <span className="order-detail-k">账号</span>
              <span className="order-detail-v">{order.account_email || order.masked_email || "-"}</span>
            </div>
            <div className="order-detail-kv">
              <span className="order-detail-k">支付 / 交付</span>
              <span className="order-detail-v">
                {paymentStatusLabel(order.payment_status)} / {deliveryStatusLabel(order.delivery_status)}
              </span>
            </div>
          </div>
        </div>

        {/* 3 选 1 判定结果卡片 */}
        <div>
          <Text size="xs" fw={700} c="dimmed" mb={6}>选择最终判定结论</Text>
          <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="xs">
            {options.map((opt) => {
              const selected = verdict === opt.value;
              const borderColor = selected
                ? opt.tone === "teal"
                  ? "#0f766e"
                  : opt.tone === "red"
                    ? "#dc2626"
                    : "#246be8"
                : undefined;
              const bgTint = selected
                ? opt.tone === "teal"
                  ? "rgba(15, 118, 110, 0.08)"
                  : opt.tone === "red"
                    ? "rgba(220, 38, 38, 0.08)"
                    : "rgba(36, 107, 232, 0.08)"
                : undefined;

              return (
                <button
                  key={opt.value}
                  type="button"
                  disabled={opt.disabled || busy}
                  className="ops-config-tile"
                  style={{
                    borderColor,
                    background: bgTint,
                    opacity: opt.disabled ? 0.5 : 1,
                    cursor: opt.disabled ? "not-allowed" : "pointer",
                  }}
                  onClick={() => {
                    setVerdict(opt.value);
                    setError(null);
                  }}
                >
                  <Group justify="space-between" align="center" gap={6} wrap="nowrap">
                    <Group gap={6} wrap="nowrap">
                      <Badge size="xs" circle color={opt.tone} variant={selected ? "filled" : "light"}>
                        {opt.icon}
                      </Badge>
                      <Text size="sm" fw={700}>{opt.title}</Text>
                    </Group>
                  </Group>
                  <Text size="xs" c="dimmed" lh={1.45} mt={4}>{opt.desc}</Text>
                </button>
              );
            })}
          </SimpleGrid>
        </div>

        {/* 条件勾选框 */}
        {verdict === "delivered" && (
          <Checkbox
            checked={markAutoRenewCanceled}
            onChange={(e) => setMarkAutoRenewCanceled(e.currentTarget.checked)}
            label="同时将本单「自动续费」标记为已关闭（适用于已手工/在账号排查页关闭续费的情况）"
            size="sm"
          />
        )}

        {needsNoChargeConfirm && (
          <Alert color="orange" radius="md" icon={<IconAlertCircle size={16} />} title="防重复扣款安全确认">
            <Checkbox
              mt={4}
              checked={confirmNoCharge}
              onChange={(e) => setConfirmNoCharge(e.currentTarget.checked)}
              label="我已核对银行流水或 Stripe 后台，确认本笔订单未发生实际扣款"
              size="sm"
            />
          </Alert>
        )}

        {/* 审计备注与快捷填入 */}
        <Stack gap="xs">
          <Group gap={6} wrap="wrap" align="center">
            <Text size="xs" c="dimmed">快捷备注：</Text>
            {QUICK_NOTES[verdict].map((preset) => (
              <Button
                key={preset}
                size="compact-xs"
                variant="light"
                color="gray"
                onClick={() => setNote(preset)}
              >
                {preset}
              </Button>
            ))}
          </Group>

          <Textarea
            label="人工判定备注（必填，将永久写入订单审计时间线）"
            placeholder="请写明核对依据，例如：已在账号排查确认 Pro100 生效并关闭自动续费"
            value={note}
            onChange={(e) => setNote(e.currentTarget.value)}
            minRows={2}
            autosize
            maxRows={4}
            disabled={busy}
          />

          <TextInput
            label="操作员标识"
            size="xs"
            w={220}
            value={operator}
            onChange={(e) => setOperator(e.currentTarget.value)}
            placeholder="web-operator"
            disabled={busy}
          />
        </Stack>

        {error && (
          <Alert color="red" radius="md" icon={<IconAlertCircle size={16} />} title="判定未通过">
            {error}
          </Alert>
        )}

        <Group justify="flex-end" gap="xs" pt={4}>
          <Button size="xs" variant="default" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button
            size="xs"
            color={verdict === "delivered" ? "teal" : verdict === "failed" ? "red" : "blue"}
            leftSection={<IconGavel size={14} />}
            loading={busy}
            disabled={!canSubmit}
            onClick={() => void handleSubmit()}
          >
            确认写入判定
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
