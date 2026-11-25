import {
  Alert, Badge, Button, Card, Group, Select, Stack, Switch, Table, Text, Textarea, Title,
} from "@mantine/core";
import { IconChecklist, IconPlayerPlay, IconTrash } from "@tabler/icons-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { Plan } from "../api/client";
import {
  runBatchPreflight,
  startQuickPay,
  type BatchPreflightItemInput,
  type BatchPreflightResult,
} from "../api/quickPay";
import { REGIONS } from "../plans";
import { parseBatchSessions, type ParsedBatchEntry } from "../utils/batchSessions";
import { CopyIdBadge } from "./CopyIdBadge";

type EnqueueState = {
  jobId?: string;
  status?: string;
  error?: string;
};

const PLAN_OPTIONS: { value: Plan; label: string }[] = [
  { value: "plus", label: "Plus" },
  { value: "go", label: "Go" },
  { value: "pro_lite", label: "Pro Standard ($100)" },
  { value: "pro", label: "Pro More ($200)" },
  { value: "pro_max", label: "Pro Max ($500)" },
];

const REGION_OPTIONS = REGIONS.map((item) => ({
  value: item.code,
  label: `${item.label} (${item.code})`,
}));

export function BatchPreflightPanel() {
  const [rawInput, setRawInput] = useState("");
  const [plan, setPlan] = useState<Plan>("plus");
  const [exitCountry, setExitCountry] = useState("");
  const exitTouchedRef = useRef(false);
  const [checkProxy, setCheckProxy] = useState(false);
  const [checkUpstream, setCheckUpstream] = useState(false);

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

  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BatchPreflightResult | null>(null);
  const [checkedEntries, setCheckedEntries] = useState<ParsedBatchEntry[]>([]);

  const [enqueueing, setEnqueueing] = useState(false);
  const [enqueued, setEnqueued] = useState<Record<number, EnqueueState>>({});

  const parsedEntries = useMemo(() => parseBatchSessions(rawInput), [rawInput]);
  const overLimit = parsedEntries.length > 20;

  async function handleRunBatch() {
    setError(null);
    setResult(null);
    setEnqueued({});
    if (parsedEntries.length === 0) {
      setError("请至少粘贴 1 条 Session JSON。");
      return;
    }
    if (parsedEntries.length > 20) {
      setError(`单次最多支持 20 条，当前识别到 ${parsedEntries.length} 条，请分批处理。`);
      return;
    }
    setRunning(true);
    try {
      const payload: BatchPreflightItemInput[] = parsedEntries.map((entry) => ({
        label: entry.label,
        plan,
        ...(exitCountry ? { exit_country: exitCountry } : {}),
        session: entry.session,
        check_proxy_connectivity: checkProxy,
        check_account_upstream: checkUpstream,
      }));
      const res = await runBatchPreflight(payload);
      setCheckedEntries(parsedEntries);
      setResult(res);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "批量体检失败");
    } finally {
      setRunning(false);
    }
  }

  async function handleEnqueuePassedCheckoutOnly() {
    if (!result) return;
    const passedItems = result.items.filter((item) => item.ok);
    if (passedItems.length === 0) return;

    setEnqueueing(true);
    setError(null);
    const stamp = Date.now().toString(36);
    const nextState: Record<number, EnqueueState> = { ...enqueued };

    for (const item of passedItems) {
      if (nextState[item.index]?.jobId) continue;
      const entry = checkedEntries[item.index];
      if (!entry) continue;
      const rand = Math.random().toString(36).slice(2, 8);
      const idempotencyKey = `batch-chk-${stamp}-${item.index}-${rand}`;
      try {
        const job = await startQuickPay(
          {
            plan,
            ...(exitCountry ? { exit_country: exitCountry } : {}),
            session: entry.session,
            debug_checkout_only: true,
            auto_cancel_renew: false,
            mode: {
              account_source: "customer_session",
              card_source: "customer",
              billing_mode: "standard",
              risk_acknowledged: false,
            },
          },
          idempotencyKey,
        );
        nextState[item.index] = { jobId: job.job_id, status: job.status };
      } catch (exc) {
        nextState[item.index] = { error: exc instanceof Error ? exc.message : "入队失败" };
      }
      setEnqueued({ ...nextState });
    }
    setEnqueueing(false);
  }

  function handleClear() {
    setRawInput("");
    setResult(null);
    setCheckedEntries([]);
    setEnqueued({});
    setError(null);
  }

  const passedCount = result?.passed ?? 0;

  return (
    <Card withBorder radius="lg" p="lg" className="workspace-panel">
      <Stack gap="md">
        <div>
          <Title order={2}>批量前置体检与安全入队</Title>
          <Text c="dimmed" size="sm" mt={4}>
            批量检查多条 Session 的凭证有效性、账单与地区一致性、活跃资金锁及队列状态（单次最多 20 条，绝不接收或落盘银行卡和 Session 明文）。
          </Text>
        </div>

        <Group gap="md" wrap="wrap" align="flex-end">
          <Select
            label="目标套餐"
            size="xs"
            w={180}
            value={plan}
            onChange={(val) => setPlan((val as Plan) || "plus")}
            data={PLAN_OPTIONS}
          />
          <Select
            label="出口地区"
            placeholder="正在同步代理默认地区…"
            size="xs"
            w={180}
            searchable
            value={exitCountry || null}
            onChange={(val) => {
              exitTouchedRef.current = true;
              setExitCountry(val || "");
            }}
            data={REGION_OPTIONS}
          />
          <Switch
            size="xs"
            label="实测出口代理连通性"
            checked={checkProxy}
            onChange={(e) => setCheckProxy(e.currentTarget.checked)}
          />
          <Switch
            size="xs"
            label="实测上游账号状态"
            checked={checkUpstream}
            onChange={(e) => setCheckUpstream(e.currentTarget.checked)}
          />
        </Group>

        <Textarea
          label={`Session 列表（当前已识别 ${parsedEntries.length} / 20 条）`}
          description="支持空行或独立 --- 行分隔多条 Session JSON；也支持每行前缀「自定义标签|{...}」便于识别。"
          placeholder={'账号A|{"accessToken":"...","user":{"email":"a@example.com"}}\n---\n账号B|{"accessToken":"..."}'}
          minRows={6}
          autosize
          maxRows={14}
          value={rawInput}
          onChange={(e) => setRawInput(e.currentTarget.value)}
        />

        <Group justify="space-between" wrap="wrap" gap="sm">
          <Group gap="xs">
            <Button
              size="sm"
              leftSection={<IconChecklist size={16} />}
              loading={running}
              disabled={parsedEntries.length === 0 || overLimit}
              onClick={() => void handleRunBatch()}
            >
              开始批量体检
            </Button>
            {passedCount > 0 && (
              <Button
                size="sm"
                variant="light"
                color="teal"
                leftSection={<IconPlayerPlay size={16} />}
                loading={enqueueing}
                onClick={() => void handleEnqueuePassedCheckoutOnly()}
              >
                将 {passedCount} 条通过项仅建单入队
              </Button>
            )}
          </Group>
          <Button
            size="xs"
            variant="subtle"
            color="gray"
            leftSection={<IconTrash size={14} />}
            disabled={!rawInput && !result}
            onClick={handleClear}
          >
            清空输入与结果
          </Button>
        </Group>

        {error && <Alert color="red" title="批量体检提示">{error}</Alert>}

        {result && (
          <Stack gap="sm">
            <Group gap="xs" wrap="wrap">
              <Badge size="lg" variant="light" color="gray">共检查 {result.total} 条</Badge>
              <Badge size="lg" variant="light" color="green">通过 {result.passed} 条</Badge>
              <Badge size="lg" variant="light" color={result.failed > 0 ? "red" : "gray"}>
                阻断 {result.failed} 条
              </Badge>
              <Badge size="lg" variant="outline" color="blue">总耗时 {result.elapsed_ms} ms</Badge>
            </Group>

            <Table.ScrollContainer minWidth={720}>
              <Table verticalSpacing="xs" fz="xs" highlightOnHover>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th style={{ width: 52 }}>#</Table.Th>
                    <Table.Th style={{ width: 130 }}>标签</Table.Th>
                    <Table.Th style={{ width: 84 }}>体检结果</Table.Th>
                    <Table.Th>诊断摘要与阻断项</Table.Th>
                    <Table.Th style={{ width: 170 }}>仅建单入队状态</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {result.items.map((item) => {
                    const failedChecks = item.checks.filter((c) => c.status === "fail");
                    const warnChecks = item.checks.filter((c) => c.status === "warn");
                    const eq = enqueued[item.index];
                    return (
                      <Table.Tr key={`${item.index}-${item.label}`}>
                        <Table.Td>{item.index + 1}</Table.Td>
                        <Table.Td fw={500}>{item.label}</Table.Td>
                        <Table.Td>
                          <Badge size="xs" color={item.ok ? "green" : "red"} variant={item.ok ? "light" : "filled"}>
                            {item.ok ? "通过" : "阻断"}
                          </Badge>
                        </Table.Td>
                        <Table.Td>
                          <Stack gap={2}>
                            <Text size="xs">{item.summary}</Text>
                            {failedChecks.map((chk) => (
                              <Text key={chk.key} size="xs" c="red">
                                [{chk.label}] {chk.message}
                              </Text>
                            ))}
                            {warnChecks.map((chk) => (
                              <Text key={chk.key} size="xs" c="yellow.8">
                                [{chk.label}] {chk.message}
                              </Text>
                            ))}
                          </Stack>
                        </Table.Td>
                        <Table.Td>
                          {eq?.jobId ? (
                            <Stack gap={2}>
                              <Badge size="xs" color="teal" variant="light">已入队 ({eq.status})</Badge>
                              <CopyIdBadge value={eq.jobId} prefixLength={10} />
                            </Stack>
                          ) : eq?.error ? (
                            <Text size="xs" c="red">{eq.error}</Text>
                          ) : (
                            <Text size="xs" c="dimmed">{item.ok ? "待入队" : "已跳过"}</Text>
                          )}
                        </Table.Td>
                      </Table.Tr>
                    );
                  })}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          </Stack>
        )}
      </Stack>
    </Card>
  );
}
