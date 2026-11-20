import {
  Alert, Badge, Button, Card, Checkbox, Code, Group, Modal, Progress, Select, Stack, Switch, Text, Textarea, TextInput, Title,
} from "@mantine/core";
import { IconBolt, IconCheck, IconX } from "@tabler/icons-react";
import { useMemo, useState } from "react";

import { getQuickPay, releasePaymentLock, startQuickPay, type QuickPayJob } from "../api/quickPay";
import type { Plan } from "../api/client";
import { REGIONS } from "../plans";

const STAGE_LABEL: Record<string, string> = {
  queued: "等待执行",
  creating_order: "正在生成订单",
  reissuing: "正在重签发 Session",
  reissued: "Session 重签发完成",
  session_refresh: "正在刷新 Session",
  session_refreshed: "Session 刷新完成",
  browser_decode: "正在初始化解码环境",
  browser_decoded: "解码环境完成",
  browser_decode_skipped: "已跳过浏览器解码",
  creating_checkout: "正在创建结账单",
  checkout_created: "结账单已生成",
  confirming_payment: "正在完成扣款确认",
  verifying_delivery: "正在核验订阅交付",
  canceling_auto_renew: "正在关闭自动续费",
  auto_renew_canceled: "自动续费已关闭",
  succeeded: "充值成功",
  failed: "充值失败",
  unknown: "扣款结果待核对",
};

const FAILURE_LABEL: Record<string, string> = {
  card_declined: "发卡行拒付或卡限额；请换卡或联系发卡行",
  insufficient_funds: "卡片余额不足；请充值或换卡",
  subscription_limit: "账号已达到订阅上限；请换账号或先处理现有订阅",
  unusual_activity: "上游判定异常流量；请换同国家 SID 或换干净 Session",
  sentinel_challenge_failed: "安全验证失败；已熔断，未继续扣款",
  proxy_tunnel_failed: "代理连接失败；请检查出口节点是否可用",
  proxy_authentication_failed: "代理认证失败；请检查代理账号或权限",
  upstream_timeout: "上游超时；请勿自动重复扣款，先核对账单",
  browser_decode_unavailable: "浏览器解码不可用；请检查 Chromium/Playwright 环境",
  checkout_upstream_error: "建单被上游拒绝；请查看阶段日志和 failure_code",
  invalid_session: "Session 无效或缺少必要凭证；请重新导出完整 Session JSON",
  session_expired: "Session 已过期；请重新登录并导出新 Session",
  payment_unknown_state: "扣款结果待核对；已锁定，请人工确认账单",
  unknown_upstream: "未分类上游错误；请保留订单号和阶段日志排查",
};

const TIMING_LABEL: Record<string, string> = {
  queue_ms: "排队",
  create_order_ms: "建单",
  reissue_ms: "重签发",
  browser_decode_ms: "浏览器解码",
  create_checkout_ms: "创建结账单",
  payment_ms: "支付确认",
  total_ms: "总耗时",
};

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function terminal(job: QuickPayJob) {
  return job.status === "succeeded" || job.status === "failed" || job.status === "unknown";
}

const LOCK_KEY = "gss.quickpay.unknown_lock";

type StoredLock = { lock_id: string; order_id?: string | null; payment_intent_id?: string | null; locked_at: string; reason: string };

function readLock(): StoredLock | null {
  try {
    const raw = window.localStorage.getItem(LOCK_KEY);
    return raw ? JSON.parse(raw) as StoredLock : null;
  } catch {
    return null;
  }
}

export function QuickPay() {
  const [session, setSession] = useState("");
  const [plan, setPlan] = useState<Plan>("plus");
  const [exitCountry, setExitCountry] = useState("PH");
  const [number, setNumber] = useState("");
  const [expiry, setExpiry] = useState("");
  const [cvc, setCvc] = useState("");
  const [debugOnly, setDebugOnly] = useState(false);
  const [autoCancelRenew, setAutoCancelRenew] = useState(true);
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<QuickPayJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [locked, setLocked] = useState<StoredLock | null>(() => readLock());
  const [unlockOpen, setUnlockOpen] = useState(false);
  const [unlockConfirmed, setUnlockConfirmed] = useState(false);
  const [unlockNote, setUnlockNote] = useState("");
  const [unlockBusy, setUnlockBusy] = useState(false);

  const progress = useMemo(() => {
    const order = ["creating_order", "reissuing", "browser_decode", "checkout_created", "confirming_payment", "verifying_delivery", "succeeded"];
    const idx = order.indexOf(job?.stage || "");
    if (job?.status === "failed") return 100;
    if (idx < 0) return 5;
    return Math.min(100, Math.round(((idx + 1) / order.length) * 100));
  }, [job]);

  function persistLock(next: StoredLock) {
    window.localStorage.setItem(LOCK_KEY, JSON.stringify(next));
    setLocked(next);
  }

  async function unlock() {
    if (!locked || !unlockConfirmed || !unlockNote.trim()) return;
    setUnlockBusy(true);
    try {
      await releasePaymentLock(locked.lock_id, unlockNote.trim());
      window.localStorage.removeItem(LOCK_KEY);
      setLocked(null);
      setUnlockOpen(false);
      setUnlockConfirmed(false);
      setUnlockNote("");
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "解锁失败");
    } finally {
      setUnlockBusy(false);
    }
  }

  async function submit() {
    if (busy || locked) return;
    if (!session.trim()) { setError("请粘贴 ChatGPT Session JSON。"); return; }
    if (!debugOnly) {
      const [month, year] = expiry.split("/").map((part) => part.trim());
      const digits = number.replace(/\D/g, "");
      if (digits.length < 12 || digits.length > 19 || !month || !year || cvc.length < 3) {
        setError("请填写完整卡号、有效期和 CVC。"); return;
      }
      setBusy(true); setError(null); setJob(null);
      try {
        const created = await startQuickPay({
          plan, exit_country: exitCountry, billing_country: "US", session: session.trim(),
          card: { number: digits, exp_month: month, exp_year: year.length === 2 ? `20${year}` : year, cvc },
          mode: { account_source: "customer_session", card_source: "customer", billing_mode: "standard", risk_acknowledged: false },
          auto_cancel_renew: autoCancelRenew,
          debug_checkout_only: false,
        }, crypto.randomUUID());
        setJob(created);
        for (let i = 0; i < 180; i += 1) {
          await sleep(i < 10 ? 1000 : i < 60 ? 2000 : 5000);
          const current = await getQuickPay(created.job_id);
          setJob(current);
          if (current.status === "unknown") {
            persistLock({
              lock_id: current.lock_id || "",
              order_id: current.order_id,
              payment_intent_id: null,
              locked_at: new Date().toISOString(),
              reason: "unknown_payment",
            });
          }
          if (terminal(current)) break;
        }
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : "请求失败");
      } finally {
        setBusy(false);
      }
      return;
    }
    setBusy(true); setError(null); setJob(null);
    try {
      const created = await startQuickPay({
        plan, exit_country: exitCountry, billing_country: "US", session: session.trim(),
        card: { number: "0000000000000000", cvc: "000", exp_month: "01", exp_year: "2030" },
        mode: { account_source: "customer_session", card_source: "customer", billing_mode: "standard", risk_acknowledged: false },
        auto_cancel_renew: autoCancelRenew,
        debug_checkout_only: true,
      }, crypto.randomUUID());
      setJob(created);
      for (let i = 0; i < 60; i += 1) {
        await sleep(1000);
        const current = await getQuickPay(created.job_id);
        setJob(current);
        if (current.status === "unknown") {
          persistLock({
            lock_id: current.lock_id || "",
            order_id: current.order_id,
            payment_intent_id: null,
            locked_at: new Date().toISOString(),
            reason: "unknown_payment",
          });
        }
        if (terminal(current)) break;
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "请求失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Stack gap="lg">
      {locked && (
        <Alert color="red" title="已锁定：存在未核销的 unknown 订单">
          订单 {locked.order_id || "-"} 的扣款结果待核对。当前页面禁止再次提交；请核对银行账单后再解锁。
          <Group mt="sm">
            <Button color="red" variant="outline" onClick={() => setUnlockOpen(true)}>人工确认账单未扣款</Button>
          </Group>
        </Alert>
      )}
      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="md">
          <div>
            <Title order={2}>一键充值</Title>
            <Text c="dimmed" size="sm" mt={4}>Session、套餐和银行卡一次提交；账单地址由服务端固定为已核验的美国免税地址。</Text>
          </div>
          <Textarea label="ChatGPT Session JSON" placeholder="粘贴完整 session JSON" rows={8} value={session}
            onChange={(event) => setSession(event.currentTarget.value)} disabled={busy}
            styles={{ input: { height: 200, resize: "none" } }} />
          <Switch checked={autoCancelRenew} onChange={(event) => setAutoCancelRenew(event.currentTarget.checked)}
            disabled={busy} label="充值后自动关闭连续订阅"
            description="扣款成功后设置到期不自动续费，保留当前周期权益；不会退款。" />
          <Group grow align="end">
            <Select label="套餐" value={plan} onChange={(value) => setPlan((value as Plan) || "plus")} disabled={busy}
              data={[{ value: "plus", label: "Plus" }, { value: "go", label: "Go" }, { value: "pro_lite", label: "Pro Standard" }, { value: "pro", label: "Pro More" }, { value: "pro_max", label: "Pro Max" }]} />
            <Select label="出口国家 / 币种" value={exitCountry} onChange={(value) => setExitCountry(value || "PH")} searchable disabled={busy}
              data={REGIONS.map((item) => ({ value: item.code, label: `${item.label} (${item.code})` }))} />
          </Group>
          <Switch checked={debugOnly} onChange={(event) => setDebugOnly(event.currentTarget.checked)} disabled={busy}
            label="仅建单调试（不提交卡）" description="开发测试用：只创建结账单，不执行扣款。" />
          {!debugOnly && (
            <Stack gap="sm">
              <TextInput label="卡号" placeholder="卡号" value={number} onChange={(event) => setNumber(event.currentTarget.value)} disabled={busy} />
              <Group grow>
                <TextInput label="有效期" placeholder="11/28" value={expiry} onChange={(event) => setExpiry(event.currentTarget.value)} disabled={busy} />
                <TextInput label="CVC" placeholder="CVC" value={cvc} onChange={(event) => setCvc(event.currentTarget.value)} disabled={busy} />
              </Group>
            </Stack>
          )}
          <Group justify="space-between">
            <Text size="sm" c="dimmed">账单地址：美国免税地址（服务端固定，只读）</Text>
            <Button leftSection={<IconBolt size={17} />} loading={busy} disabled={busy || Boolean(locked)} onClick={() => void submit()}>
              {locked ? "已锁定" : busy ? "处理中" : debugOnly ? "仅建单" : "立即充值"}
            </Button>
          </Group>
          {error && <Alert color="red" title="请求失败">{error}</Alert>}
        </Stack>
      </Card>

      {job && (
        <Card withBorder radius="lg" p="lg" className="workspace-panel">
          <Stack gap="sm">
            <Group justify="space-between">
              <Title order={3}>{STAGE_LABEL[job.stage] || job.stage}</Title>
              <Badge color={job.status === "succeeded" ? "green" : job.status === "failed" ? "red" : "blue"}>
                {job.status}
              </Badge>
            </Group>
            <Progress value={progress} animated={!terminal(job)} />
            {job.timings_ms && Object.keys(job.timings_ms).length > 0 && (
              <Group gap="xs" wrap="wrap">
                {Object.entries(TIMING_LABEL).map(([key, label]) => {
                  const value = job.timings_ms?.[key];
                  return value === undefined ? null : (
                    <Badge key={key} variant="light">{label} {value} ms</Badge>
                  );
                })}
              </Group>
            )}
            <Group gap="lg">
              {job.order_id && <Text size="sm">订单：<Code>{job.order_id}</Code></Text>}
              {job.amount && <Text size="sm">金额：<strong>{job.amount} {job.currency}</strong></Text>}
              {job.purchase_attempt_id && <Text size="sm">尝试：<Code>{job.purchase_attempt_id}</Code></Text>}
              {job.auto_renew_status === "canceled" && <Badge color="green">自动续费已关闭</Badge>}
              {job.auto_renew_status === "manual_required" && <Badge color="orange">自动续费待人工处理</Badge>}
            </Group>
            {job.failure_code && <Alert color="red" title={FAILURE_LABEL[job.failure_code] || "充值失败"}>{job.message}</Alert>}
            {job.events && job.events.length > 0 && (
              <Stack gap={4}>
                <Text size="sm" fw={600}>阶段时间线</Text>
                {job.events.slice(-8).map((event, index) => (
                  <Text key={`${event.created_at}-${index}`} size="xs" c="dimmed">
                    {new Date(event.created_at).toLocaleTimeString()} · {STAGE_LABEL[event.stage] || event.stage} · {event.message}
                  </Text>
                ))}
              </Stack>
            )}
            {job.status === "succeeded" && <Alert color="green" icon={<IconCheck size={18} />}>{job.message || "充值成功"}</Alert>}
            {job.status === "unknown" && <Alert color="orange" icon={<IconX size={18} />}>扣款结果待核对，请勿重复提交。</Alert>}
          </Stack>
        </Card>
      )}

      <Modal opened={unlockOpen} onClose={() => setUnlockOpen(false)} title="确认账单未扣款" centered>
        <Stack>
          <Text size="sm">请先登录银行或 Stripe 核对，确认本笔没有扣款。解锁后不会恢复原支付，只会允许发起新单。</Text>
          <Checkbox checked={unlockConfirmed} onChange={(event) => setUnlockConfirmed(event.currentTarget.checked)}
            label="我已核对账单，确认没有扣款" />
          <Textarea label="操作备注" placeholder="例如：已核对 11-20 账单，无扣款" value={unlockNote}
            onChange={(event) => setUnlockNote(event.currentTarget.value)} minRows={3} />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setUnlockOpen(false)} disabled={unlockBusy}>取消</Button>
            <Button color="red" loading={unlockBusy} disabled={!unlockConfirmed || !unlockNote.trim()} onClick={() => void unlock()}>
              确认解锁
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
