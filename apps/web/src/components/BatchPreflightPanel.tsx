import {
  Alert,
  Badge,
  Button,
  Group,
  Select,
  Stack,
  Switch,
  Table,
  Text,
  Textarea,
  Tooltip,
} from "@mantine/core";
import {
  IconAlertTriangle,
  IconChecklist,
  IconDeviceDesktopAnalytics,
  IconPlayerPlay,
  IconTrash,
} from "@tabler/icons-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { Plan } from "../api/client";
import {
  runBatchPreflight,
  startQuickPay,
  type BatchPreflightItemInput,
  type BatchPreflightResult,
} from "../api/quickPay";
import { REGIONS } from "../plans";
import {
  detectDuplicateDevices,
  parseBatchSessions,
  type ParsedBatchEntry,
} from "../utils/batchSessions";
import { AdminPageHeader } from "./AdminPageHeader";
import { CopyIdBadge } from "./CopyIdBadge";

type EnqueueState = {
  jobId?: string;
  status?: string;
  error?: string;
};

const PLAN_OPTIONS: { value: Plan; label: string }[] = [
  { value: "plus", label: "Plus" },
  { value: "go", label: "Go" },
  { value: "pro_lite", label: "Pro100 ($100)" },
  { value: "pro", label: "Pro200 ($200)" },
  { value: "pro_max", label: "Pro500 ($500)" },
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
  const liveDeviceAudit = useMemo(() => detectDuplicateDevices(parsedEntries), [parsedEntries]);
  const checkedDeviceAudit = useMemo(() => detectDuplicateDevices(checkedEntries), [checkedEntries]);
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
    <Stack gap="md">
      <AdminPageHeader
        title="批量体检"
        badge={
          parsedEntries.length > 0 ? (
            <Badge
              size="sm"
              variant="light"
              color={overLimit ? "red" : liveDeviceAudit.conflicts.length > 0 ? "orange" : "blue"}
            >
              已识别 {parsedEntries.length} / 20 条
            </Badge>
          ) : null
        }
        subtitle="批量核验多条 Session 有效性、固定免税账单一致性、活跃资金锁，并自动检测多账号共享同一浏览器设备指纹（oai-did）风险。"
        actions={
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
        }
      />

      {/* 实时本地 oai-did 设备冲突预警（发单前即刻拦截 subscription_limit 风险） */}
      {liveDeviceAudit.conflicts.length > 0 && (
        <div className="ops-alert-strip ops-alert-strip--warn" role="alert">
          <div className="ops-alert-strip-left">
            <span className="ops-alert-icon"><IconAlertTriangle size={16} /></span>
            <div>
              <Text size="sm" fw={700} c="orange.8">
                检测到 {liveDeviceAudit.conflicts.length} 组账号共享同一浏览器设备指纹（oai-did）
              </Text>
              <Text size="xs" c="dimmed" mt={2}>
                {liveDeviceAudit.conflicts
                  .map((c) => `oai-did ${c.deviceIdPrefix} → ${c.labels.join("、")}`)
                  .join("；")}
                。在「直通建单模式」下共用设备极易触发 OpenAI 的 <code>subscription_limit</code> 拦截，建议使用独立无痕窗口/浏览器配置重新导出，或关闭直通模式。
              </Text>
            </div>
          </div>
        </div>
      )}

      {/* 输入与参数配置卡 */}
      <section className="ops-panel">
        <Stack gap="md">
          <Group gap="md" wrap="wrap" align="flex-end">
            <Select
              label="目标套餐"
              size="xs"
              w={190}
              value={plan}
              onChange={(val) => setPlan((val as Plan) || "plus")}
              data={PLAN_OPTIONS}
            />
            <Select
              label="出口地区"
              placeholder="正在同步代理默认地区…"
              size="xs"
              w={190}
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
            description="支持空行或独立 --- 行分隔多条 Session JSON；也支持每行前缀「自定义标签|{...}」便于识别。绝不接收或落盘银行卡和明文凭证。"
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
                size="xs"
                leftSection={<IconChecklist size={15} />}
                loading={running}
                disabled={parsedEntries.length === 0 || overLimit}
                onClick={() => void handleRunBatch()}
              >
                开始批量体检
              </Button>
              {passedCount > 0 && (
                <Button
                  size="xs"
                  variant="light"
                  color="teal"
                  leftSection={<IconPlayerPlay size={15} />}
                  loading={enqueueing}
                  onClick={() => void handleEnqueuePassedCheckoutOnly()}
                >
                  将 {passedCount} 条通过项仅建单入队
                </Button>
              )}
            </Group>
            {liveDeviceAudit.conflicts.length === 0 && parsedEntries.length > 1 && (
              <Group gap={6}>
                <IconDeviceDesktopAnalytics size={14} color="var(--studio-green)" />
                <Text size="xs" c="teal" fw={550}>未发现重复 oai-did 设备指纹</Text>
              </Group>
            )}
          </Group>
        </Stack>
      </section>

      {error && <Alert color="red" radius="md" title="批量体检提示">{error}</Alert>}

      {/* 体检结果面板 */}
      {result && (
        <section className="ops-panel">
          <div className="ops-panel-head">
            <div className="ops-panel-title-wrap">
              <h3 className="ops-panel-title">体检结果</h3>
              <Badge size="xs" variant="light" color="gray">共 {result.total} 条</Badge>
              <Badge size="xs" variant="light" color="green">通过 {result.passed}</Badge>
              <Badge size="xs" variant="light" color={result.failed > 0 ? "red" : "gray"}>
                阻断 {result.failed}
              </Badge>
              {checkedDeviceAudit.conflicts.length > 0 && (
                <Badge size="xs" variant="light" color="orange">
                  设备冲突 {checkedDeviceAudit.conflicts.length} 组
                </Badge>
              )}
            </div>
            <span className="ops-panel-sub">耗时 {result.elapsed_ms} ms</span>
          </div>

          <Table.ScrollContainer minWidth={780}>
            <Table className="ledger-clean-table" highlightOnHover={false}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th style={{ width: 48 }}>#</Table.Th>
                  <Table.Th style={{ width: 120 }}>标签</Table.Th>
                  <Table.Th style={{ width: 136 }}>设备指纹</Table.Th>
                  <Table.Th style={{ width: 88 }}>结果</Table.Th>
                  <Table.Th>诊断摘要与阻断项</Table.Th>
                  <Table.Th style={{ width: 160 }}>仅建单入队状态</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {result.items.map((item) => {
                  const failedChecks = item.checks.filter((c) => c.status === "fail");
                  const warnChecks = item.checks.filter((c) => c.status === "warn");
                  const eq = enqueued[item.index];
                  const devMeta = checkedDeviceAudit.byIndex[item.index];
                  const hasDup = Boolean(devMeta && devMeta.duplicateLabels.length > 0);
                  const tone = item.ok ? "success" : "failed";

                  return (
                    <Table.Tr key={`${item.index}-${item.label}`}>
                      <Table.Td><span className="ledger-seq">{item.index + 1}</span></Table.Td>
                      <Table.Td fw={600}>{item.label}</Table.Td>
                      <Table.Td>
                        {devMeta?.deviceIdPrefix ? (
                          <Stack gap={3}>
                            <span className="ops-route-tag">{devMeta.deviceIdPrefix}</span>
                            {hasDup && (
                              <Tooltip label={`与 ${devMeta.duplicateLabels.join("、")} 共用同一 oai-did`} withArrow>
                                <Badge size="xs" color="orange" variant="light">设备复用</Badge>
                              </Tooltip>
                            )}
                          </Stack>
                        ) : (
                          <span className="ledger-muted">未提供</span>
                        )}
                      </Table.Td>
                      <Table.Td>
                        <span className={`ledger-pill ledger-pill--status-${tone}`}>
                          <span className={`ledger-dot ledger-dot--${tone}`} />
                          {item.ok ? "通过" : "阻断"}
                        </span>
                      </Table.Td>
                      <Table.Td>
                        <Stack gap={2}>
                          <Text size="xs">{item.summary}</Text>
                          {hasDup && (
                            <Text size="xs" c="orange.8">
                              [设备隔离] 与 {devMeta.duplicateLabels.join("、")} 共享 oai-did，直通模式下易触发 subscription_limit
                            </Text>
                          )}
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
                          <span className="ledger-muted">{item.ok ? "待入队" : "已跳过"}</span>
                        )}
                      </Table.Td>
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </section>
      )}
    </Stack>
  );
}
