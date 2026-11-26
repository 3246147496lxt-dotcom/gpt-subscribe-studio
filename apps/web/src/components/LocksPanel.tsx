import {
  Alert, Badge, Button, Card, Checkbox, Code, Group, Modal, SegmentedControl, Stack, Table, Text, Textarea, TextInput, Title, Tooltip,
} from "@mantine/core";
import { IconLockOpen, IconRefresh, IconShieldCheck } from "@tabler/icons-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { formatAmount } from "../api/client";
import { isAbortError } from "../api/http";
import {
  getPaymentLocks,
  getReconcilerStatus,
  reconcilePaymentLock,
  releasePaymentLockManual,
  type PaymentLockSummary,
  type PaymentLocksPage,
  type ReconcilerStatus,
} from "../api/ledger";
import { planLabel } from "../plans";
import { formatMonthDayTime, planPillTone } from "../utils/orderLedgerFormat";
import { CopyIdBadge } from "./CopyIdBadge";

const STATE_LABEL: Record<string, { label: string; tone: "failed" | "success" | "muted" }> = {
  held: { label: "锁定中", tone: "failed" },
  resolved: { label: "已核销", tone: "success" },
  released: { label: "已解锁", tone: "muted" },
};

export function LocksPanel({
  active = true,
  onCountChange,
  onLocksChanged,
}: {
  active?: boolean;
  onCountChange?: (heldCount: number) => void;
  onLocksChanged?: () => void;
}) {
  const [stateFilter, setStateFilter] = useState("held");
  const [locksPage, setLocksPage] = useState<PaymentLocksPage | null>(null);
  const [reconciler, setReconciler] = useState<ReconcilerStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyLockId, setBusyLockId] = useState<string | null>(null);

  const [releaseTarget, setReleaseTarget] = useState<PaymentLockSummary | null>(null);
  const [releaseConfirmed, setReleaseConfirmed] = useState(false);
  const [releaseOperator, setReleaseOperator] = useState("web-operator");
  const [releaseNote, setReleaseNote] = useState("");
  const [releaseBusy, setReleaseBusy] = useState(false);
  const loadControllerRef = useRef<AbortController | null>(null);
  const wasActiveRef = useRef(active);

  const load = useCallback(async () => {
    loadControllerRef.current?.abort();
    const controller = new AbortController();
    loadControllerRef.current = controller;

    setLoading(true);
    setError(null);
    try {
      const [page, rec] = await Promise.all([
        getPaymentLocks({ state: stateFilter, limit: 50, offset: 0 }, controller.signal),
        getReconcilerStatus(controller.signal),
      ]);
      if (loadControllerRef.current === controller) {
        setLocksPage(page);
        setReconciler(rec);
        onCountChange?.(page.counts?.held ?? 0);
      }
    } catch (exc) {
      if (isAbortError(exc)) return;
      setError(exc instanceof Error ? exc.message : "加载资金锁状态失败");
    } finally {
      if (loadControllerRef.current === controller) {
        setLoading(false);
      }
    }
  }, [stateFilter, onCountChange]);

  useEffect(() => {
    void load();
    return () => {
      loadControllerRef.current?.abort();
    };
  }, [load]);

  useEffect(() => {
    const prev = wasActiveRef.current;
    wasActiveRef.current = active;
    if (!prev && active) {
      void load();
    }
  }, [active, load]);

  async function handleReconcile(lockId: string) {
    setBusyLockId(lockId);
    setError(null);
    setNotice(null);
    try {
      const res = await reconcilePaymentLock(lockId);
      setNotice(`资金锁 ${lockId.slice(0, 8)} 只读对账结果：${res.message}`);
      await load();
      onLocksChanged?.();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "只读对账失败");
    } finally {
      setBusyLockId(null);
    }
  }

  async function handleRelease() {
    if (!releaseTarget || !releaseConfirmed || !releaseNote.trim()) return;
    setReleaseBusy(true);
    setError(null);
    try {
      await releasePaymentLockManual(releaseTarget.id, releaseNote.trim(), releaseOperator);
      setReleaseTarget(null);
      setReleaseConfirmed(false);
      setReleaseNote("");
      setNotice("已记录人工核对备注并释放该账号资金锁。");
      await load();
      onLocksChanged?.();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "解锁失败");
    } finally {
      setReleaseBusy(false);
    }
  }

  const counts = locksPage?.counts ?? { held: 0, resolved: 0, released: 0 };

  return (
    <Stack gap="lg">
      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="md">
          <Group justify="space-between" align="flex-start" wrap="wrap">
            <div>
              <Title order={2}>资金对账</Title>
              <Text c="dimmed" size="sm" mt={4}>
                自动对账器按阶梯退避只读复核待交付与未知订单，绝不重复发起扣款；未确认扣款结果的账号锁需人工核对账单后释放。
              </Text>
            </div>
            <Button size="xs" variant="subtle" leftSection={<IconRefresh size={14} />} loading={loading} onClick={() => void load()}>
              刷新
            </Button>
          </Group>

          {reconciler && (
            <Group gap="xs" wrap="wrap">
              <Badge color={reconciler.running ? "green" : reconciler.enabled ? "blue" : "gray"} variant="light" size="lg">
                自动对账器：{reconciler.running ? "运行中" : reconciler.enabled ? "就绪" : "已禁用"}
              </Badge>
              <Badge variant="light" color="gray" size="lg">轮询间隔 {reconciler.interval_s}s · 上限 {reconciler.max_attempts} 次</Badge>
              <Badge variant="light" color="cyan" size="lg">待对账候选 {reconciler.pending_candidates}</Badge>
              <Badge variant="light" color="green" size="lg">累计自动收敛 {reconciler.resolved_count}</Badge>
            </Group>
          )}

          <SegmentedControl
            size="xs"
            value={stateFilter}
            onChange={setStateFilter}
            data={[
              { label: `锁定中 (${counts.held ?? 0})`, value: "held" },
              { label: `已对账核销 (${counts.resolved ?? 0})`, value: "resolved" },
              { label: `人工已解锁 (${counts.released ?? 0})`, value: "released" },
              { label: "全部", value: "all" },
            ]}
          />

          {notice && <Alert color="blue" title="处理结果" withCloseButton onClose={() => setNotice(null)}>{notice}</Alert>}
          {error && <Alert color="red" title="操作失败">{error}</Alert>}

          {!locksPage || locksPage.items.length === 0 ? (
            <Text c="dimmed" ta="center" py="xl">
              {loading ? "正在加载资金锁记录..." : "当前筛选状态下没有资金锁记录。"}
            </Text>
          ) : (
            <Table.ScrollContainer minWidth={820}>
              <Table className="ledger-clean-table" highlightOnHover={false}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>状态</Table.Th>
                    <Table.Th>账号指纹</Table.Th>
                    <Table.Th>关联订单</Table.Th>
                    <Table.Th>金额</Table.Th>
                    <Table.Th>原因 / 备注</Table.Th>
                    <Table.Th>锁定时间</Table.Th>
                    <Table.Th style={{ textAlign: "right" }}>操作</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {locksPage.items.map((item) => {
                    const meta = STATE_LABEL[item.state] ?? { label: item.state, tone: "muted" as const };
                    const amountText = formatAmount(item.order_amount, item.order_currency);
                    const fullCreated = new Date(item.created_at).toLocaleString();
                    return (
                      <Table.Tr key={item.id}>
                        <Table.Td>
                          <span className={`ledger-pill ledger-pill--status-${meta.tone}`}>
                            <span className={`ledger-dot ledger-dot--${meta.tone}`} />
                            {meta.label}
                          </span>
                        </Table.Td>
                        <Table.Td><Code>{item.lock_key_prefix}</Code></Table.Td>
                        <Table.Td>
                          <Group gap={6} wrap="nowrap">
                            {item.order_id ? <CopyIdBadge value={item.order_id} prefixLength={8} /> : <span className="ledger-muted">-</span>}
                            {item.order_plan && (
                              <span className={`ledger-pill ledger-pill--plan ledger-pill--plan-${planPillTone(item.order_plan)}`}>
                                {planLabel(item.order_plan)}
                              </span>
                            )}
                          </Group>
                        </Table.Td>
                        <Table.Td>
                          {amountText ? <span className="ledger-amount">{amountText}</span> : <span className="ledger-muted">-</span>}
                        </Table.Td>
                        <Table.Td>
                          <Stack gap={2}>
                            <Text size="xs" lineClamp={1} title={item.reason}>{item.reason}</Text>
                            {item.release_note && (
                              <Text size="xs" c="dimmed" lineClamp={1} title={item.release_note}>
                                备注：{item.release_note}{item.released_by ? ` (${item.released_by})` : ""}
                              </Text>
                            )}
                          </Stack>
                        </Table.Td>
                        <Table.Td>
                          <Tooltip label={fullCreated} openDelay={250} withArrow>
                            <span className="ledger-time">{formatMonthDayTime(item.created_at)}</span>
                          </Tooltip>
                        </Table.Td>
                        <Table.Td>
                          <Group gap={6} justify="flex-end" wrap="nowrap">
                            {item.state === "held" ? (
                              <>
                                <Button size="xs" variant="light" color="blue"
                                  leftSection={<IconShieldCheck size={13} />}
                                  loading={busyLockId === item.id}
                                  onClick={() => void handleReconcile(item.id)}>
                                  只读对账
                                </Button>
                                <Button size="xs" variant="light" color="red"
                                  leftSection={<IconLockOpen size={13} />}
                                  onClick={() => {
                                    setReleaseTarget(item);
                                    setReleaseConfirmed(false);
                                    setReleaseNote("");
                                  }}>
                                  核对解锁
                                </Button>
                              </>
                            ) : (
                              <span className="ledger-muted">
                                {item.released_at ? `已处理 ${formatMonthDayTime(item.released_at)}` : "已归档"}
                              </span>
                            )}
                          </Group>
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

      <Modal opened={releaseTarget !== null} onClose={() => setReleaseTarget(null)} title="人工核对后解锁资金锁" centered>
        {releaseTarget && (
          <Stack gap="sm">
            <Alert color="orange" title="防重复扣款安全确认">
              解锁仅允许发起新单，不会恢复原支付。若本地已确认原订单扣款成功，系统将自动拒绝解锁。
            </Alert>
            <Group gap="md" wrap="wrap">
              <Group gap={4}><Text size="xs">锁 ID：</Text><CopyIdBadge value={releaseTarget.id} prefixLength={14} /></Group>
              <Group gap={4}>
                <Text size="xs">订单：</Text>
                {releaseTarget.order_id ? <CopyIdBadge value={releaseTarget.order_id} prefixLength={14} /> : <Text size="xs">-</Text>}
              </Group>
            </Group>
            <Checkbox
              checked={releaseConfirmed}
              onChange={(e) => setReleaseConfirmed(e.currentTarget.checked)}
              label="我已核对银行或 Stripe 账单，明确确认本笔订单没有扣款"
            />
            <TextInput
              label="操作员标识"
              value={releaseOperator}
              onChange={(e) => setReleaseOperator(e.currentTarget.value)}
              placeholder="例如：ops-alice"
            />
            <Textarea
              label="核对备注（必填，将写入审计日志）"
              placeholder="例如：已核对银行流水与 Stripe 后台，确认无扣款"
              value={releaseNote}
              onChange={(e) => setReleaseNote(e.currentTarget.value)}
              minRows={3}
            />
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setReleaseTarget(null)} disabled={releaseBusy}>取消</Button>
              <Button color="red" loading={releaseBusy}
                disabled={!releaseConfirmed || !releaseNote.trim()}
                onClick={() => void handleRelease()}>
                确认解锁
              </Button>
            </Group>
          </Stack>
        )}
      </Modal>
    </Stack>
  );
}
