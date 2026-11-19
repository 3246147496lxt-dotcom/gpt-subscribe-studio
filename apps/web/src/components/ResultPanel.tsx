import { Alert, Anchor, Button, Group, Loader, Stack, Text, ThemeIcon, Title } from "@mantine/core";
import { IconCheck, IconX } from "@tabler/icons-react";

import { formatAmount, type Order } from "../api/client";
import { FAILED_STATUSES, SUCCESS_STATUSES } from "../api/history";
import { planLabel, regionLabel } from "../plans";
import { verifiedPayerBilling } from "./FixedPayerBilling";
import { STATUS_LABELS } from "./OrderSummary";

interface Props {
  order: Order;
  protocolOnly?: boolean;
  onReset: () => void;
  onVerify?: () => void;
  verifying?: boolean;
}

export function ResultPanel({ order, protocolOnly = false, onReset, onVerify, verifying = false }: Props) {
  const success = SUCCESS_STATUSES.includes(order.status) || order.delivery_status === "delivered";
  const failed = FAILED_STATUSES.includes(order.status);
  const paymentSucceeded = order.payment_status === "succeeded" || order.receipt?.payment_status === "succeeded";
  const deliveryDelayed = paymentSucceeded && order.delivery_status === "verify_delayed";
  const amount = formatAmount(order.amount, order.currency);
  const paidAmount = order.receipt?.amount || order.paid_amount || order.amount;
  const paidCurrency = order.receipt?.currency || order.paid_currency || order.currency;
  const taxDelta = order.receipt?.tax_delta || order.tax_delta;
  const planType = order.receipt?.plan_type || order.plan_type;
  const summary = order.status_summary || order.receipt?.summary;
  const nextUrl = order.plan === "team"
    ? order.next_action?.url || order.next_action?.checkout_url || (!success && !failed ? order.checkout_url : null)
    : order.status === "requires_action" && verifiedPayerBilling(order.billing_verified, order.billing)
      && ["3ds_redirect", "redirect_to_url"].includes(order.next_action?.type ?? "")
      && order.next_action?.url !== order.checkout_url && order.next_action?.url !== order.next_action?.checkout_url
      ? order.next_action?.url : null;
  const color = success ? "green" : failed ? "red" : deliveryDelayed ? "blue" : "yellow";
  const title = success
    ? "开通成功"
    : failed
      ? "开通失败"
      : paymentSucceeded
        ? "扣款成功，订阅核验中"
        : (STATUS_LABELS[order.status] ?? order.status);

  return (
    <Stack gap="md" role="status" aria-live="polite">
      <Group align="center" wrap="nowrap">
        {success || failed ? (
          <ThemeIcon color={color} size={44} radius="xl" variant="light">
            {success ? <IconCheck size={26} /> : <IconX size={26} />}
          </ThemeIcon>
        ) : (
          <Loader size={44} color={color} />
        )}
        <div>
          <Title order={3}>{title}</Title>
          <Text size="sm" c="dimmed">
            {planLabel(order.plan)} · {regionLabel(order.billing_country)}
            {amount ? ` · ${amount}` : ""}
          </Text>
        </div>
      </Group>

      {paymentSucceeded && (
        <Alert color={success ? "green" : "blue"} variant="light" title={summary || (success ? "扣款与交付完成" : "扣款已确认")}>
          <Stack gap={4}>
            <Text size="sm">实际扣款：{formatAmount(paidAmount, paidCurrency) || "—"}</Text>
            {taxDelta && <Text size="sm">减免税额：{taxDelta}</Text>}
            {planType && <Text size="sm">账号套餐：{planType}</Text>}
            {order.payment_intent_id && <Text size="xs" c="dimmed">PaymentIntent：{order.payment_intent_id}</Text>}
            {deliveryDelayed && <Text size="sm">订阅核验暂时延迟，支付结果已锁定，无需再次付款。</Text>}
          </Stack>
        </Alert>
      )}
      {order.subscription?.active && (
        <Alert color="green" variant="light" title="订阅已生效">
          {order.subscription.expires_at ? `到期时间 ${order.subscription.expires_at}` : "订阅已经生效。"}
        </Alert>
      )}
      {!protocolOnly && nextUrl && (
        <Alert color="yellow" variant="light" title={order.next_action?.challenge_type === "intent_confirmation_challenge" ? "需要完成 hCaptcha 人机验证" : "支付验证与状态确认"}>
          <Anchor href={nextUrl} target="_blank" rel="noreferrer">
            {order.next_action?.url ? "打开支付验证页面" : "打开官方结账页完成验证"}
          </Anchor>
          {order.plan === "team" && <Text size="xs">页面会自动查询 Stripe Hosted 支付结果，请勿重复提交付款。</Text>}
        </Alert>
      )}
      {order.last_error && !success && !paymentSucceeded && (
        <Alert color="red" variant="light" title="失败原因">
          {order.last_error}
        </Alert>
      )}

      <Group>
        {deliveryDelayed && onVerify && (
          <Button onClick={onVerify} loading={verifying} variant="light">重新核验订阅</Button>
        )}
        {(success || failed) && <Button onClick={onReset}>再开通一单</Button>}
      </Group>
    </Stack>
  );
}
