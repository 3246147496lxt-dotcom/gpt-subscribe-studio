import {
  Alert, Badge, Button, Card, Checkbox, Group, Modal, PasswordInput, Select, Stack, Switch, Text, Textarea, TextInput, Title,
} from "@mantine/core";
import { IconBolt, IconRefresh, IconShieldCheck } from "@tabler/icons-react";
import { useCallback, useEffect, useRef, useState } from "react";

import type { Plan } from "../api/client";
import { isAbortError } from "../api/http";
import { getPaymentLocks } from "../api/ledger";
import {
  getQuickPay,
  getQuickPayQueue,
  reconcileQuickPay,
  releasePaymentLock,
  runQuickPayPreflight,
  startQuickPay,
  subscribeQuickPayStream,
  type PreflightResult,
  type QueueStatus,
  type QuickPayJob,
} from "../api/quickPay";
import { getSystemDiagnostics } from "../api/system";
import { CardExpiryInput } from "../card-payment/CardExpiryInput";
import { isValidCardExpiry } from "../card-payment/cardExpiry";
import { SessionPreviewBar } from "../components/SessionPreviewBar";
import {
  isConfirmedRejection,
  isQuickPayTerminal,
} from "../components/quick-pay/constants";
import { QuickPayJobCard } from "../components/quick-pay/QuickPayJobCard";
import { QuickPayQueueTable } from "../components/quick-pay/QuickPayQueueTable";
import { REGIONS } from "../plans";
import { detectCardBrand, formatCardNumber, isLuhnValid, stripCardDigits } from "../utils/cardFormat";
import { describeFailureDetail } from "../utils/quickPayProgress";

const LOCK_KEY = "gss.quickpay.unknown_lock";

type StoredLock = {
  lock_id: string;
  order_id?: string | null;
  payment_intent_id?: string | null;
  locked_at: string;
  reason: string;
};

function readLock(): StoredLock | null {
  try {
    const raw = window.localStorage.getItem(LOCK_KEY);
    return raw ? (JSON.parse(raw) as StoredLock) : null;
  } catch {
    return null;
  }
}

export function QuickPayFailure({
  job,
  locked,
  debugOnly,
  autoCancelRenew,
}: {
  job: QuickPayJob;
  locked: boolean;
  debugOnly?: boolean;
  autoCancelRenew?: boolean;
}) {
  if (job.status !== "failed") return null;
  const detail = describeFailureDetail(job, debugOnly, autoCancelRenew);

  const diagnosticBadges = (
    <Group gap={6} wrap="wrap" mt={4}>
      {detail.failureCode && (
        <Badge size="xs" color="red" variant="outline">错误码：{detail.failureCode}</Badge>
      )}
      {detail.upstreamStatus != null && (
        <Badge size="xs" color="orange" variant="light">上游 HTTP {detail.upstreamStatus}</Badge>
      )}
      {detail.upstreamCode && detail.upstreamCode !== detail.failureCode && (
        <Badge size="xs" color="orange" variant="outline">上游码：{detail.upstreamCode}</Badge>
      )}
      {detail.traceId && (
        <Badge size="xs" color="gray" variant="light">追踪：{detail.traceId}</Badge>
      )}
    </Group>
  );

  if (isConfirmedRejection(job, locked)) {
    const offerOfficial = job.failure_code === "checkout_upstream_error" || job.failure_code === "unusual_activity";
    return (
      <Alert color="red" title="未创建结账单，未提交付款">
        <Stack gap={4}>
          <Text size="xs" fw={600}>
            失败位置：第 {detail.stepNumber}/{detail.totalSteps} 步 · {detail.stepTitle}（{detail.substageLabel}）
          </Text>
          {offerOfficial ? (
            <>
              <Text size="sm">
                {detail.reason} 本地订单已停止。账户本人可以在 ChatGPT 中查看或办理订阅；这是独立操作，不会恢复本单或将其标为付款成功。
              </Text>
              <a href="https://chatgpt.com/" target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">
                本人前往 ChatGPT 查看订阅
              </a>
            </>
          ) : (
            <Text size="sm">{detail.headline}：{detail.reason}</Text>
          )}
          <Text size="xs" c="dimmed">排查建议：{detail.advice}</Text>
          {diagnosticBadges}
        </Stack>
      </Alert>
    );
  }

  return (
    <Alert color="red" title={`${detail.headline}（第 ${detail.stepNumber}/${detail.totalSteps} 步）`}>
      <Stack gap={4}>
        <Text size="xs" fw={600}>
          失败位置：第 {detail.stepNumber}/{detail.totalSteps} 步 · {detail.stepTitle}（{detail.substageLabel}）
        </Text>
        <Text size="sm">{detail.reason}</Text>
        <Text size="xs" c="dimmed">建议处理：{detail.advice}</Text>
        {diagnosticBadges}
      </Stack>
    </Alert>
  );
}

export function QuickPay({
  lockRevision = 0,
  onLocksChanged,
}: {
  lockRevision?: number;
  onLocksChanged?: () => void;
}) {
  const [session, setSession] = useState("");
  const [plan, setPlan] = useState<Plan>("plus");
  const [exitCountry, setExitCountry] = useState("");
  const exitTouchedRef = useRef(false);
  const [cardNumber, setCardNumber] = useState("");
  const [cardBlurred, setCardBlurred] = useState(false);
  const [expMonth, setExpMonth] = useState("");
  const [expYear, setExpYear] = useState("");
  const [cvc, setCvc] = useState("");
  const [debugOnly, setDebugOnly] = useState(false);
  const [autoCancelRenew, setAutoCancelRenew] = useState(true);

  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<QuickPayJob | null>(null);
  const [jobDebugOnly, setJobDebugOnly] = useState<boolean | undefined>(undefined);
  const [jobAutoCancelRenew, setJobAutoCancelRenew] = useState<boolean | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  const [locked, setLocked] = useState<StoredLock | null>(() => readLock());
  const [unlockOpen, setUnlockOpen] = useState(false);
  const [unlockConfirmed, setUnlockConfirmed] = useState(false);
  const [unlockNote, setUnlockNote] = useState("");
  const [unlockBusy, setUnlockBusy] = useState(false);

  const [billingHint, setBillingHint] = useState("账单地址：由服务端固定核验配置决定（只读）");
  const [preflightBusy, setPreflightBusy] = useState(false);
  const [preflightDeep, setPreflightDeep] = useState(false);
  const [preflight, setPreflight] = useState<PreflightResult | null>(null);

  const [queueStatus, setQueueStatus] = useState<QueueStatus | null>(null);
  const [queueLoading, setQueueLoading] = useState(false);
  const [streamMode, setStreamMode] = useState<"idle" | "sse" | "fallback">("idle");
  const [reconcilingJob, setReconcilingJob] = useState(false);
  const unsubscribeRef = useRef<(() => void) | null>(null);

  const stopStream = useCallback(() => {
    unsubscribeRef.current?.();
    unsubscribeRef.current = null;
    setStreamMode("idle");
  }, []);

  useEffect(() => () => stopStream(), [stopStream]);

  const clearCardMemory = useCallback(() => {
    setCardNumber("");
    setCardBlurred(false);
    setExpMonth("");
    setExpYear("");
    setCvc("");
  }, []);

  const persistLock = useCallback((next: StoredLock) => {
    try {
      window.localStorage.setItem(LOCK_KEY, JSON.stringify(next));
    } catch {
      // ignore storage quota/privacy failures
    }
    setLocked(next);
  }, []);

  const clearStoredLock = useCallback(() => {
    try {
      window.localStorage.removeItem(LOCK_KEY);
    } catch {
      // ignore
    }
    setLocked(null);
  }, []);

  // Reconcile local lock banner against authoritative server held locks.
  const syncServerLocks = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await getPaymentLocks({ state: "held", limit: 1 }, signal);
      const first = res.items?.[0];
      if (first && first.state === "held") {
        persistLock({
          lock_id: first.id,
          order_id: first.order_id,
          payment_intent_id: first.payment_intent_id,
          locked_at: first.created_at,
          reason: first.reason,
        });
      } else {
        clearStoredLock();
      }
    } catch (err) {
      if (!isAbortError(err)) {
        // Keep existing local state on transient connectivity issues.
      }
    }
  }, [clearStoredLock, persistLock]);

  useEffect(() => {
    const controller = new AbortController();
    void syncServerLocks(controller.signal);
    return () => controller.abort();
  }, [syncServerLocks, lockRevision]);

  // Default the exit country to the proxy currently configured by the operator
  // (same behavior as OrderNew), falling back to US only when none is set.
  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/settings/proxies", { signal: controller.signal })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (controller.signal.aborted || exitTouchedRef.current) return;
        const region = String(data?.checkout?.region || "").toUpperCase();
        setExitCountry((current) => current || (/^[A-Z]{2}$/.test(region) ? region : "US"));
      })
      .catch(() => {
        if (!controller.signal.aborted && !exitTouchedRef.current) {
          setExitCountry((current) => current || "US");
        }
      });
    return () => controller.abort();
  }, []);

  // Read-only verified billing summary so the UI never hardcodes a stale region.
  useEffect(() => {
    const controller = new AbortController();
    void getSystemDiagnostics(controller.signal)
      .then((diag) => {
        const billingCheck = diag.checks.find((c) => c.key === "verified_billing");
        if (billingCheck) {
          setBillingHint(`固定核验账单：${billingCheck.message}`);
        }
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  const refreshQueue = useCallback(async (signal?: AbortSignal) => {
    setQueueLoading(true);
    try {
      const next = await getQuickPayQueue(15, signal);
      setQueueStatus(next);
    } catch {
      // Queue overview is auxiliary; never overwrite an active submit error.
    } finally {
      setQueueLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refreshQueue(controller.signal);
    return () => controller.abort();
  }, [refreshQueue]);

  // Auto-refresh queue every 5s only while there are running or queued jobs.
  const hasActiveQueueJobs = Boolean(
    queueStatus && (queueStatus.stats.running > 0 || queueStatus.stats.queued > 0),
  );
  useEffect(() => {
    if (!hasActiveQueueJobs) return;
    const timer = window.setInterval(() => {
      void refreshQueue();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [hasActiveQueueJobs, refreshQueue]);

  async function runPreflight() {
    if (preflightBusy || busy) return;
    setPreflightBusy(true);
    setError(null);
    try {
      const result = await runQuickPayPreflight({
        plan,
        ...(exitCountry ? { exit_country: exitCountry } : {}),
        session: session.trim(),
        debug_checkout_only: debugOnly,
        check_proxy_connectivity: preflightDeep,
        check_account_upstream: preflightDeep,
      });
      setPreflight(result);
      void refreshQueue();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "自动体检失败");
    } finally {
      setPreflightBusy(false);
    }
  }

  const attachJobStream = useCallback((jobId: string) => {
    stopStream();
    setStreamMode("sse");
    unsubscribeRef.current = subscribeQuickPayStream(jobId, {
      onUpdate: (current) => {
        setJob(current);
        if (current.status === "unknown") {
          persistLock({
            lock_id: current.lock_id || "",
            order_id: current.order_id,
            payment_intent_id: null,
            locked_at: new Date().toISOString(),
            reason: "unknown_payment",
          });
          onLocksChanged?.();
        }
        if (current.status === "requires_action") {
          setBusy(false);
          void refreshQueue();
        }
      },
      onFallback: () => {
        setStreamMode("fallback");
      },
      onDone: () => {
        unsubscribeRef.current = null;
        setStreamMode("idle");
        setBusy(false);
        void refreshQueue();
        void syncServerLocks();
        onLocksChanged?.();
      },
    });
  }, [onLocksChanged, persistLock, refreshQueue, stopStream, syncServerLocks]);

  async function inspectQueueJob(jobId: string) {
    try {
      const detail = await getQuickPay(jobId);
      setJobDebugOnly(undefined);
      setJobAutoCancelRenew(undefined);
      setJob(detail);
      if (!isQuickPayTerminal(detail)) {
        attachJobStream(jobId);
      } else {
        stopStream();
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "读取任务详情失败");
    }
  }

  async function handleActionReconcile() {
    if (!job) return;
    setReconcilingJob(true);
    setError(null);
    try {
      await reconcileQuickPay(job.job_id);
      const updated = await getQuickPay(job.job_id);
      setJob(updated);
      void refreshQueue();
      void syncServerLocks();
      onLocksChanged?.();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "只读核验失败");
    } finally {
      setReconcilingJob(false);
    }
  }

  async function unlock() {
    if (!locked || !unlockConfirmed || !unlockNote.trim()) return;
    setUnlockBusy(true);
    try {
      await releasePaymentLock(locked.lock_id, unlockNote.trim());
      clearStoredLock();
      setUnlockOpen(false);
      setUnlockConfirmed(false);
      setUnlockNote("");
      setError(null);
      onLocksChanged?.();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "解锁失败");
    } finally {
      setUnlockBusy(false);
    }
  }

  async function submit() {
    if (busy || locked) return;
    if (!session.trim()) {
      setError("请粘贴 ChatGPT Session JSON。");
      return;
    }
    if (!debugOnly) {
      const digits = stripCardDigits(cardNumber);
      const cleanCvc = cvc.replace(/\D/g, "");
      const expiryOk = isValidCardExpiry({ exp_month: expMonth, exp_year: expYear });
      if (digits.length < 12 || digits.length > 19 || !expiryOk || cleanCvc.length < 3) {
        setError("请填写完整且有效的卡号、有效期（MM/YY）和 CVC。");
        return;
      }
      setBusy(true);
      setError(null);
      setJob(null);
      setJobDebugOnly(false);
      setJobAutoCancelRenew(autoCancelRenew);
      try {
        const created = await startQuickPay(
          {
            plan,
            ...(exitCountry ? { exit_country: exitCountry } : {}),
            session: session.trim(),
            card: {
              number: digits,
              exp_month: expMonth.padStart(2, "0"),
              exp_year: expYear.length === 2 ? `20${expYear}` : expYear,
              cvc: cleanCvc,
            },
            mode: {
              account_source: "customer_session",
              card_source: "customer",
              billing_mode: "standard",
              risk_acknowledged: false,
            },
            auto_cancel_renew: autoCancelRenew,
            debug_checkout_only: false,
          },
          crypto.randomUUID(),
        );
        // Clear card fields from browser component state immediately after handoff.
        clearCardMemory();
        setJob(created);
        void refreshQueue();
        if (isQuickPayTerminal(created)) {
          setBusy(false);
          onLocksChanged?.();
        } else {
          attachJobStream(created.job_id);
        }
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : "请求失败");
        setBusy(false);
      }
      return;
    }

    setBusy(true);
    setError(null);
    setJob(null);
    setJobDebugOnly(true);
    setJobAutoCancelRenew(false);
    try {
      const created = await startQuickPay(
        {
          plan,
          ...(exitCountry ? { exit_country: exitCountry } : {}),
          session: session.trim(),
          mode: {
            account_source: "customer_session",
            card_source: "customer",
            billing_mode: "standard",
            risk_acknowledged: false,
          },
          auto_cancel_renew: autoCancelRenew,
          debug_checkout_only: true,
        },
        crypto.randomUUID(),
      );
      setJob(created);
      void refreshQueue();
      if (isQuickPayTerminal(created)) {
        setBusy(false);
      } else {
        attachJobStream(created.job_id);
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "请求失败");
      setBusy(false);
    }
  }

  const cardDigits = stripCardDigits(cardNumber);
  const cardBrand = detectCardBrand(cardDigits);
  const cardLuhnWarn = cardBlurred && cardDigits.length >= 12 && !isLuhnValid(cardDigits);

  return (
    <Stack gap="lg">
      {locked && (
        <Alert color="red" title="已锁定：存在未核销的 unknown 订单">
          订单 {locked.order_id || "-"} 的扣款结果待核对。当前页面禁止再次提交；请核对银行账单后再解锁。
          <Group mt="sm">
            <Button color="red" variant="outline" onClick={() => setUnlockOpen(true)}>
              人工确认账单未扣款
            </Button>
          </Group>
        </Alert>
      )}

      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="md">
          <Group justify="space-between" align="flex-start" wrap="wrap">
            <div>
              <Title order={2}>一键充值</Title>
              <Text c="dimmed" size="sm" mt={4}>
                支持提交前自动体检、持久化队列排队与优雅停机保护；仅建单不收集银行卡资料。
              </Text>
            </div>
            {queueStatus && (
              <Group gap="xs">
                <Badge color={queueStatus.stats.draining ? "orange" : "blue"} variant="light">
                  {queueStatus.stats.draining
                    ? "服务排空中"
                    : `运行中 ${queueStatus.stats.running}/${queueStatus.stats.max_concurrency} · 排队 ${queueStatus.stats.queued}`}
                </Badge>
                <Button
                  size="xs"
                  variant="subtle"
                  leftSection={<IconRefresh size={14} />}
                  loading={queueLoading}
                  onClick={() => void refreshQueue()}
                >
                  刷新队列
                </Button>
              </Group>
            )}
          </Group>

          {queueStatus?.stats.draining && (
            <Alert color="orange" title="服务正在重启排空">
              当前服务正等待在途任务安全收尾，暂不接收新任务，请稍后刷新再提交。
            </Alert>
          )}

          <div>
            <Textarea
              label="ChatGPT Session JSON"
              placeholder="粘贴完整 session JSON"
              rows={7}
              value={session}
              onChange={(event) => {
                setSession(event.currentTarget.value);
                if (preflight) setPreflight(null);
              }}
              disabled={busy}
              styles={{
                input: {
                  height: 180,
                  resize: "none",
                  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                  fontSize: 12,
                },
              }}
            />
            <SessionPreviewBar
              value={session}
              disabled={busy}
              onClear={() => {
                setSession("");
                setPreflight(null);
              }}
            />
          </div>

          <Switch
            checked={autoCancelRenew}
            onChange={(event) => setAutoCancelRenew(event.currentTarget.checked)}
            disabled={busy}
            label="充值后自动关闭连续订阅"
            description="扣款成功后设置到期不自动续费，保留当前周期权益；不会退款。"
          />

          <Group grow align="end">
            <Select
              label="套餐"
              value={plan}
              onChange={(value) => setPlan((value as Plan) || "plus")}
              disabled={busy}
              data={[
                { value: "plus", label: "Plus" },
                { value: "go", label: "Go" },
                { value: "pro_lite", label: "Pro Standard" },
                { value: "pro", label: "Pro More" },
                { value: "pro_max", label: "Pro Max" },
              ]}
            />
            <Select
              label="出口国家 / 币种"
              description="默认同步「代理设置」的出口地区，可按单自由更换；账单地址由服务端固定核验配置决定。"
              placeholder="正在同步代理默认地区…"
              value={exitCountry || null}
              onChange={(value) => {
                exitTouchedRef.current = true;
                setExitCountry(value || "");
              }}
              searchable
              disabled={busy}
              data={REGIONS.map((item) => ({ value: item.code, label: `${item.label} (${item.code})` }))}
            />
          </Group>

          <Switch
            checked={debugOnly}
            onChange={(event) => {
              const next = event.currentTarget.checked;
              if (next) clearCardMemory();
              setDebugOnly(next);
            }}
            disabled={busy}
            label="仅建单调试（不提交卡）"
            description="开发测试用：只创建结账单，不执行扣款。"
          />

          {!debugOnly && (
            <Stack gap="sm">
              <TextInput
                label="卡号"
                placeholder="4 位自动分组输入"
                inputMode="numeric"
                autoComplete="off"
                value={cardNumber}
                onFocus={() => setCardBlurred(false)}
                onBlur={() => setCardBlurred(true)}
                onChange={(event) => setCardNumber(formatCardNumber(event.currentTarget.value))}
                disabled={busy}
                error={cardLuhnWarn ? "卡号校验位疑似有误，请核对是否漏输或输错数字" : undefined}
                rightSection={cardBrand ? <Badge size="xs" variant="light">{cardBrand}</Badge> : undefined}
                rightSectionWidth={cardBrand ? 96 : undefined}
              />
              <Group grow align="flex-start">
                <CardExpiryInput
                  month={expMonth}
                  year={expYear}
                  autoComplete="off"
                  disabled={busy}
                  onChange={({ exp_month, exp_year }) => {
                    setExpMonth(exp_month);
                    setExpYear(exp_year);
                  }}
                />
                <PasswordInput
                  label="CVC"
                  placeholder="3-4 位安全码"
                  inputMode="numeric"
                  autoComplete="off"
                  maxLength={4}
                  value={cvc}
                  onChange={(event) => setCvc(event.currentTarget.value.replace(/\D/g, "").slice(0, 4))}
                  disabled={busy}
                />
              </Group>
            </Stack>
          )}

          <Group justify="space-between" align="center" wrap="wrap" gap="sm">
            <Stack gap={4}>
              <Text size="sm" c="dimmed">{billingHint}</Text>
              <Checkbox
                size="xs"
                checked={preflightDeep}
                onChange={(event) => setPreflightDeep(event.currentTarget.checked)}
                disabled={busy || preflightBusy}
                label="体检时含上游订阅状态与代理连通实测（不扣款）"
              />
            </Stack>
            <Group gap="sm">
              <Button
                variant="default"
                leftSection={<IconShieldCheck size={17} />}
                loading={preflightBusy}
                disabled={busy || preflightBusy}
                onClick={() => void runPreflight()}
              >
                提交前体检
              </Button>
              <Button
                leftSection={<IconBolt size={17} />}
                loading={busy}
                disabled={busy || Boolean(locked) || Boolean(queueStatus?.stats.draining)}
                onClick={() => void submit()}
              >
                {locked ? "已锁定" : busy ? "处理中" : debugOnly ? "仅建单" : "立即充值"}
              </Button>
            </Group>
          </Group>

          {preflight && (
            <Alert color={preflight.ok ? "green" : "red"} title={`前置体检结果（${preflight.elapsed_ms} ms）`}>
              <Stack gap={6}>
                <Text size="sm">{preflight.summary}</Text>
                {preflight.checks.map((item) => (
                  <Group key={item.key} gap="xs" wrap="nowrap" align="flex-start">
                    <Badge
                      size="xs"
                      variant="light"
                      color={item.status === "pass" ? "green" : item.status === "warn" ? "orange" : "red"}
                    >
                      {item.status === "pass" ? "通过" : item.status === "warn" ? "提示" : "阻断"}
                    </Badge>
                    <Text size="xs">
                      <strong>{item.label}</strong>：{item.message}
                    </Text>
                  </Group>
                ))}
              </Stack>
            </Alert>
          )}

          {error && <Alert color="red" title="请求失败">{error}</Alert>}
        </Stack>
      </Card>

      {job && (
        <QuickPayJobCard
          job={job}
          locked={Boolean(locked)}
          debugOnly={jobDebugOnly}
          autoCancelRenew={jobAutoCancelRenew}
          streamMode={streamMode}
          reconcilingJob={reconcilingJob}
          onActionReconcile={() => void handleActionReconcile()}
          failureSlot={
            <QuickPayFailure
              job={job}
              locked={Boolean(locked)}
              debugOnly={jobDebugOnly}
              autoCancelRenew={jobAutoCancelRenew}
            />
          }
        />
      )}

      {queueStatus && (
        <QuickPayQueueTable
          jobs={queueStatus.jobs}
          activeJobId={job?.job_id}
          loading={queueLoading}
          onRefresh={() => void refreshQueue()}
          onSelectJob={(id) => void inspectQueueJob(id)}
        />
      )}

      <Modal opened={unlockOpen} onClose={() => setUnlockOpen(false)} title="确认账单未扣款" centered>
        <Stack>
          <Text size="sm">请先登录银行或 Stripe 核对，确认本笔没有扣款。解锁后不会恢复原支付，只会允许发起新单。</Text>
          <Checkbox
            checked={unlockConfirmed}
            onChange={(event) => setUnlockConfirmed(event.currentTarget.checked)}
            label="我已核对账单，确认没有扣款"
          />
          <Textarea
            label="操作备注"
            placeholder="例如：已核对 11-20 账单，无扣款"
            value={unlockNote}
            onChange={(event) => setUnlockNote(event.currentTarget.value)}
            minRows={3}
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setUnlockOpen(false)} disabled={unlockBusy}>取消</Button>
            <Button
              color="red"
              loading={unlockBusy}
              disabled={!unlockConfirmed || !unlockNote.trim()}
              onClick={() => void unlock()}
            >
              确认解锁
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
