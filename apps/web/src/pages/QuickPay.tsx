import {
  Alert, Badge, Button, Card, Code, Group, Progress, Select, Stack, Switch, Text, Textarea, TextInput, Title,
} from "@mantine/core";
import { IconBolt, IconCheck, IconX } from "@tabler/icons-react";
import { useMemo, useState } from "react";

import { getQuickPay, startQuickPay, type QuickPayJob } from "../api/quickPay";
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
  succeeded: "充值成功",
  failed: "充值失败",
  unknown: "扣款结果待核对",
};

const FAILURE_LABEL: Record<string, string> = {
  card_declined: "发卡行拒付或卡限额",
  insufficient_funds: "卡片余额不足",
  subscription_limit: "账号已达到订阅上限",
  unusual_activity: "上游判定异常流量",
  sentinel_challenge_failed: "安全验证失败",
  proxy_tunnel_failed: "代理连接失败",
  upstream_timeout: "上游超时",
  browser_decode_unavailable: "浏览器解码不可用",
};

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function terminal(job: QuickPayJob) {
  return job.status === "succeeded" || job.status === "failed" || job.status === "unknown";
}

export function QuickPay() {
  const [session, setSession] = useState("");
  const [plan, setPlan] = useState<Plan>("plus");
  const [exitCountry, setExitCountry] = useState("PH");
  const [number, setNumber] = useState("");
  const [expiry, setExpiry] = useState("");
  const [cvc, setCvc] = useState("");
  const [debugOnly, setDebugOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<QuickPayJob | null>(null);
  const [error, setError] = useState<string | null>(null);

  const progress = useMemo(() => {
    const order = ["creating_order", "reissuing", "browser_decode", "checkout_created", "confirming_payment", "verifying_delivery", "succeeded"];
    const idx = order.indexOf(job?.stage || "");
    if (job?.status === "failed") return 100;
    if (idx < 0) return 5;
    return Math.min(100, Math.round(((idx + 1) / order.length) * 100));
  }, [job]);

  async function submit() {
    if (busy) return;
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
          debug_checkout_only: false,
        }, crypto.randomUUID());
        setJob(created);
        for (let i = 0; i < 180; i += 1) {
          await sleep(i < 10 ? 1000 : i < 60 ? 2000 : 5000);
          const current = await getQuickPay(created.job_id);
          setJob(current);
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
        debug_checkout_only: true,
      }, crypto.randomUUID());
      setJob(created);
      for (let i = 0; i < 60; i += 1) {
        await sleep(1000);
        const current = await getQuickPay(created.job_id);
        setJob(current);
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
      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="md">
          <div>
            <Title order={2}>一键充值</Title>
            <Text c="dimmed" size="sm" mt={4}>Session、套餐和银行卡一次提交；账单地址由服务端固定为已核验的美国免税地址。</Text>
          </div>
          <Textarea label="ChatGPT Session JSON" placeholder="粘贴完整 session JSON" rows={8} value={session}
            onChange={(event) => setSession(event.currentTarget.value)} disabled={busy}
            styles={{ input: { height: 200, resize: "none" } }} />
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
            <Button leftSection={<IconBolt size={17} />} loading={busy} disabled={busy} onClick={() => void submit()}>
              {busy ? "处理中" : debugOnly ? "仅建单" : "立即充值"}
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
            <Group gap="lg">
              {job.order_id && <Text size="sm">订单：<Code>{job.order_id}</Code></Text>}
              {job.amount && <Text size="sm">金额：<strong>{job.amount} {job.currency}</strong></Text>}
              {job.purchase_attempt_id && <Text size="sm">尝试：<Code>{job.purchase_attempt_id}</Code></Text>}
            </Group>
            {job.failure_code && <Alert color="red" title={FAILURE_LABEL[job.failure_code] || "充值失败"}>{job.message}</Alert>}
            {job.status === "succeeded" && <Alert color="green" icon={<IconCheck size={18} />}>{job.message || "充值成功"}</Alert>}
            {job.status === "unknown" && <Alert color="orange" icon={<IconX size={18} />}>扣款结果待核对，请勿重复提交。</Alert>}
          </Stack>
        </Card>
      )}
    </Stack>
  );
}
