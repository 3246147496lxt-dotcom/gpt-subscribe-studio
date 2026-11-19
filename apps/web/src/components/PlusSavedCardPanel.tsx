import { Alert, Button, Paper, Stack, Text } from "@mantine/core";
import { useEffect, useState } from "react";
import { getPlusSavedCard, type Order, type SavedCardState, type SavedCardStatus } from "../api/client";

const STATUS_LABELS: Record<SavedCardState, string> = {
  not_started: "尚未保存付款方式",
  requires_payment_method: "待填写卡片",
  requires_confirmation: "待确认保存",
  requires_action: "待完成银行验证",
  processing: "银行正在处理",
  canceled: "本次卡片设置已取消",
  sync_pending: "等待账号同步付款方式",
  saved: "付款方式已保存",
  checkout_ready: "付款方式已保存",
  prepare_unknown: "卡片准备结果待核实",
};

// Legacy records can still be read. The old external setup and checkout pages
// cannot enforce the order-bound payer profile, so they have no write controls.
export function PlusSavedCardPanel({ order, disabled = false }: { order: Order; disabled?: boolean }) {
  const [refresh, setRefresh] = useState(0);
  const [status, setStatus] = useState<SavedCardStatus | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    setStatus(null); setError(""); setLoading(true);
    void getPlusSavedCard(order.id, controller.signal)
      .then(next => { if (!controller.signal.aborted) setStatus(next); })
      .catch(() => { if (!controller.signal.aborted) setError("暂时不能读取，原订单未被修改。"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [order.id, refresh]);
  return (
    <Paper withBorder p="lg" radius="md" data-testid="plus-saved-card-panel">
      <Stack gap="sm">
        <Text fw={600}>原订单付款方式记录（只读）</Text>
        <Text size="sm">已保存，不代表已付款。此旧入口仅可查询状态。</Text>
        <Alert role="status">{error || (loading ? "正在读取保存状态…" : status && STATUS_LABELS[status.state])}</Alert>
        <Button type="button" variant="default" disabled={disabled || loading} onClick={() => setRefresh(n => n + 1)}>查询保存状态</Button>
      </Stack>
    </Paper>
  );
}
