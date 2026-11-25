import {
  Alert, Badge, Button, Card, Code, Divider, Group, NumberInput, SegmentedControl, Stack, Switch, Text, Title,
} from "@mantine/core";
import { IconAdjustments, IconCheck, IconInfoCircle, IconRefresh, IconRotate } from "@tabler/icons-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  getRuntimeFlags,
  resetRuntimeFlags,
  updateRuntimeFlags,
  type RuntimeFlagItem,
  type RuntimeFlagsSnapshot,
  type RuntimeFlagValue,
} from "../api/flags";

const CATEGORY_META: Record<RuntimeFlagItem["category"], { title: string; subtitle: string }> = {
  session: {
    title: "Session 与出口重颁策略",
    subtitle: "控制建单前是否用 sessionToken（ST）在当前充值出口重新颁发新 accessToken（AT），以及 UA 与出口预检行为。",
  },
  protocol: {
    title: "协议引擎与风控遥测",
    subtitle: "控制 Plus 支付协议引擎、HAR 遥测回放、建单代理隧道自动轮换及账单/出口严格匹配模式。",
  },
  runtime: {
    title: "队列、预检与自动对账",
    subtitle: "控制一键充值全局并发槽位、入队前置拦截严格度及后台只读自动对账补偿器。",
  },
};

const CATEGORY_ORDER: RuntimeFlagItem["category"][] = ["session", "protocol", "runtime"];

const SOURCE_BADGE: Record<RuntimeFlagItem["source"], { label: string; color: string }> = {
  override: { label: "面板覆盖", color: "violet" },
  env: { label: "环境变量", color: "blue" },
  default: { label: "系统默认", color: "gray" },
};

function formatFlagValue(item: RuntimeFlagItem, val: RuntimeFlagValue | null | undefined): string {
  if (val === null || val === undefined) return "-";
  if (item.kind === "bool") return val ? "开启" : "关闭";
  if (item.kind === "enum") {
    const opt = item.options.find((o) => o.value === String(val));
    return opt ? opt.label : String(val);
  }
  return String(val);
}

export function RuntimeFlagsPanel() {
  const [snapshot, setSnapshot] = useState<RuntimeFlagsSnapshot | null>(null);
  const [draft, setDraft] = useState<Record<string, RuntimeFlagValue>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [resettingKey, setResettingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const syncSnapshot = useCallback((data: RuntimeFlagsSnapshot) => {
    setSnapshot(data);
    const nextDraft: Record<string, RuntimeFlagValue> = {};
    for (const item of data.items) {
      nextDraft[item.key] = item.value;
    }
    setDraft(nextDraft);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getRuntimeFlags();
      syncSnapshot(data);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "加载策略开关失败");
    } finally {
      setLoading(false);
    }
  }, [syncSnapshot]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirtyUpdates = useMemo(() => {
    const out: Record<string, RuntimeFlagValue> = {};
    if (!snapshot) return out;
    for (const item of snapshot.items) {
      const current = draft[item.key];
      if (current !== undefined && current !== item.value) {
        out[item.key] = current;
      }
    }
    return out;
  }, [snapshot, draft]);

  const dirtyCount = Object.keys(dirtyUpdates).length;

  async function handleSave() {
    if (dirtyCount === 0) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const data = await updateRuntimeFlags(dirtyUpdates);
      syncSnapshot(data);
      setNotice(`已保存 ${dirtyCount} 项策略开关并立即生效（持久化于 runtime-flags.json，权限 0600）。`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "保存策略开关失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleReset(keys?: string[]) {
    const marker = keys && keys.length === 1 ? keys[0] : "__all__";
    setResettingKey(marker);
    setError(null);
    setNotice(null);
    try {
      const data = await resetRuntimeFlags(keys);
      syncSnapshot(data);
      setNotice(keys ? "已恢复该开关为环境变量 / 系统默认。" : "已清除全部面板覆盖，恢复为环境变量 / 系统默认。");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "恢复默认失败");
    } finally {
      setResettingKey(null);
    }
  }

  const grouped = useMemo(() => {
    const map: Record<RuntimeFlagItem["category"], RuntimeFlagItem[]> = {
      session: [],
      protocol: [],
      runtime: [],
    };
    for (const item of snapshot?.items ?? []) {
      map[item.category]?.push(item);
    }
    return map;
  }, [snapshot]);

  return (
    <Stack gap="lg">
      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="md">
          <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
            <div>
              <Group gap="xs" align="center">
                <Title order={2}>协议与运行时策略开关</Title>
                {(snapshot?.override_count ?? 0) > 0 && (
                  <Badge color="violet" variant="light" size="sm">
                    已覆盖 {snapshot?.override_count} 项
                  </Badge>
                )}
                {dirtyCount > 0 && (
                  <Badge color="orange" variant="filled" size="sm">
                    {dirtyCount} 项未保存
                  </Badge>
                )}
              </Group>
              <Text c="dimmed" size="sm" mt={4}>
                在线调整 ST→AT 出口重颁、UA 保持、协议引擎、遥测回放与队列/对账策略；保存后立即对新任务生效，无需重启服务。优先级：面板覆盖 &gt; 环境变量 &gt; 系统默认。
              </Text>
            </div>
            <Group gap="xs">
              <Button
                size="xs"
                variant="subtle"
                leftSection={<IconRefresh size={14} />}
                loading={loading}
                onClick={() => void load()}
              >
                刷新
              </Button>
              <Button
                size="xs"
                variant="default"
                leftSection={<IconRotate size={14} />}
                disabled={(snapshot?.override_count ?? 0) === 0 || saving || resettingKey !== null}
                loading={resettingKey === "__all__"}
                onClick={() => void handleReset()}
              >
                全部恢复默认
              </Button>
              <Button
                size="xs"
                leftSection={<IconAdjustments size={14} />}
                disabled={dirtyCount === 0 || saving || resettingKey !== null}
                loading={saving}
                onClick={() => void handleSave()}
              >
                保存并生效{dirtyCount > 0 ? ` (${dirtyCount})` : ""}
              </Button>
            </Group>
          </Group>

          {snapshot?.fake_mode && (
            <Alert color="blue" variant="light" icon={<IconInfoCircle size={16} />}>
              当前处于离线模拟模式（<Code>GSS_USE_FAKE=true</Code>），「建单前始终用 ST 重颁新 AT」默认关闭以避免测试环境外联；真实上游模式下默认开启。
            </Alert>
          )}

          {notice && <Alert color="green" icon={<IconCheck size={16} />}>{notice}</Alert>}
          {error && <Alert color="red" title="操作失败">{error}</Alert>}

          {snapshot?.updated_at && (
            <Text size="xs" c="dimmed">
              最近保存时间：{new Date(snapshot.updated_at).toLocaleString()}（仅保存非敏感开关，绝不存储任何凭证）
            </Text>
          )}
        </Stack>
      </Card>

      {CATEGORY_ORDER.map((category) => {
        const items = grouped[category];
        if (!items || items.length === 0) return null;
        const meta = CATEGORY_META[category];
        return (
          <Card key={category} withBorder radius="lg" p="lg" className="workspace-panel">
            <Stack gap="md">
              <div>
                <Title order={3}>{meta.title}</Title>
                <Text c="dimmed" size="sm" mt={2}>{meta.subtitle}</Text>
              </div>

              <Stack gap="sm">
                {items.map((item, idx) => {
                  const val = draft[item.key] ?? item.value;
                  const isDirty = val !== item.value;
                  const srcBadge = SOURCE_BADGE[item.source];
                  return (
                    <div key={item.key}>
                      {idx > 0 && <Divider mb="sm" />}
                      <Group justify="space-between" align="flex-start" wrap="wrap" gap="md">
                        <Stack gap={4} style={{ flex: "1 1 360px", minWidth: 260 }}>
                          <Group gap={6} wrap="wrap" align="center">
                            <Text fw={600} size="sm">{item.label}</Text>
                            <Badge size="xs" color={srcBadge.color} variant="light">
                              {srcBadge.label}
                            </Badge>
                            {isDirty && (
                              <Badge size="xs" color="orange" variant="outline">
                                未保存
                              </Badge>
                            )}
                          </Group>
                          <Text size="xs" c="dimmed">{item.description}</Text>
                          <Group gap="xs" wrap="wrap" mt={2}>
                            <Code fz={11}>{item.env_var}</Code>
                            <Text size="xs" c="dimmed">
                              默认：{formatFlagValue(item, item.default)}
                              {item.env_value !== null && item.env_value !== undefined
                                ? ` · 环境变量：${formatFlagValue(item, item.env_value)}`
                                : ""}
                            </Text>
                            {item.source === "override" && (
                              <Button
                                size="compact-xs"
                                variant="subtle"
                                color="gray"
                                loading={resettingKey === item.key}
                                disabled={saving || resettingKey !== null}
                                onClick={() => void handleReset([item.key])}
                              >
                                恢复默认
                              </Button>
                            )}
                          </Group>
                        </Stack>

                        <Group align="center" style={{ flexShrink: 0 }}>
                          {item.kind === "bool" && (
                            <Switch
                              size="md"
                              checked={Boolean(val)}
                              onLabel="开"
                              offLabel="关"
                              disabled={saving || resettingKey !== null}
                              aria-label={item.label}
                              onChange={(e) => {
                                const next = e.currentTarget.checked;
                                setDraft((prev) => ({ ...prev, [item.key]: next }));
                              }}
                            />
                          )}
                          {item.kind === "enum" && (
                            <SegmentedControl
                              size="xs"
                              value={String(val)}
                              disabled={saving || resettingKey !== null}
                              data={item.options.map((opt) => ({ value: opt.value, label: opt.label }))}
                              onChange={(next) => {
                                setDraft((prev) => ({ ...prev, [item.key]: next }));
                              }}
                            />
                          )}
                          {item.kind === "int" && (
                            <NumberInput
                              size="xs"
                              w={110}
                              min={item.min_value ?? 1}
                              max={item.max_value ?? 32}
                              step={1}
                              clampBehavior="strict"
                              allowDecimal={false}
                              disabled={saving || resettingKey !== null}
                              aria-label={item.label}
                              value={typeof val === "number" ? val : Number(val)}
                              onChange={(next) => {
                                if (typeof next === "number" && Number.isFinite(next)) {
                                  setDraft((prev) => ({ ...prev, [item.key]: Math.trunc(next) }));
                                }
                              }}
                            />
                          )}
                        </Group>
                      </Group>
                    </div>
                  );
                })}
              </Stack>
            </Stack>
          </Card>
        );
      })}
    </Stack>
  );
}
