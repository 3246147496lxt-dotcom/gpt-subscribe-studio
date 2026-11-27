import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Group,
  NumberInput,
  Paper,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import {
  IconAdjustments,
  IconCheck,
  IconCopy,
  IconCpu,
  IconInfoCircle,
  IconKey,
  IconRefresh,
  IconRotate,
  IconSearch,
  IconShieldCheck,
  IconSparkles,
  IconX,
} from "@tabler/icons-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  getRuntimeFlags,
  resetRuntimeFlags,
  updateRuntimeFlags,
  type RuntimeFlagItem,
  type RuntimeFlagsSnapshot,
  type RuntimeFlagValue,
} from "../api/flags";

type CategoryFilter = "all" | RuntimeFlagItem["category"];

const CATEGORY_META: Record<
  RuntimeFlagItem["category"],
  { title: string; short: string; subtitle: string }
> = {
  session: {
    title: "Session 与身份策略",
    short: "Session 与身份",
    subtitle: "控制 Session 是直通建单还是先重颁新 AT，以及客户端 OS 请求头与预热行为。",
  },
  protocol: {
    title: "协议引擎与代理容错",
    short: "协议与代理",
    subtitle: "控制结账协议实现、Stripe 遥测回放、代理超时自动换节点与账单国家匹配规则。",
  },
  runtime: {
    title: "队列并发与自动对账",
    short: "队列与对账",
    subtitle: "控制一键充值并发上限、缺账单前置拦截及后台只读自动核验补偿。",
  },
};

const CATEGORY_ORDER: RuntimeFlagItem["category"][] = ["session", "protocol", "runtime"];

const SOURCE_BADGE: Record<RuntimeFlagItem["source"], { label: string; color: string }> = {
  override: { label: "已自定义", color: "violet" },
  env: { label: "环境变量", color: "blue" },
  default: { label: "默认", color: "gray" },
};

const FLAG_HINT_TAG: Record<string, { label: string; color: string }> = {
  direct_session_checkout: { label: "实测推荐", color: "teal" },
  checkout_rotate: { label: "推荐开启", color: "teal" },
  client_os_profile: { label: "核心", color: "indigo" },
  reissue_session_on_exit: { label: "核心", color: "indigo" },
};

type PresetDef = {
  id: string;
  title: string;
  desc: string;
  badge: string;
  color: string;
  values: Record<string, RuntimeFlagValue>;
};

const PRESETS: PresetDef[] = [
  {
    id: "direct_recommended",
    title: "真机直通 + 代理容错",
    desc: "保留原 oai-did 与有效 AT 直接预热建单，遇代理超时自动换节点（对齐 11/26 实测）",
    badge: "推荐",
    color: "teal",
    values: {
      direct_session_checkout: true,
      checkout_rotate: true,
      client_os_profile: "auto",
      har_telemetry: "full",
    },
  },
  {
    id: "reissue_standard",
    title: "强制出口重颁模式",
    desc: "旧 AT 过期或批量跑裸 ST 时使用：先在目标出口换新 AT，遇隧道超时自动换节点",
    badge: "经典",
    color: "blue",
    values: {
      direct_session_checkout: false,
      reissue_session_on_exit: true,
      checkout_rotate: true,
      client_os_profile: "auto",
    },
  },
  {
    id: "mac_bare_st",
    title: "Mac 裸 ST 专跑（Linux 服务器）",
    desc: "服务器跑无 UA 的 Mac 导出裸 ST 时，强制统一发 macOS 请求头与 Sentinel 档案",
    badge: "跨机",
    color: "violet",
    values: {
      client_os_profile: "macos",
      checkout_rotate: true,
      reissue_session_on_exit: true,
    },
  },
];

function formatFlagValue(item: RuntimeFlagItem, val: RuntimeFlagValue | null | undefined): string {
  if (val === null || val === undefined) return "-";
  if (item.kind === "bool") return val ? "开启" : "关闭";
  if (item.kind === "enum") {
    const opt = item.options.find((o) => o.value === String(val));
    return opt ? opt.label : String(val);
  }
  return String(val);
}

function CategoryIcon({ category }: { category: RuntimeFlagItem["category"] }) {
  if (category === "session") return <IconKey size={18} />;
  if (category === "protocol") return <IconShieldCheck size={18} />;
  return <IconCpu size={18} />;
}

export function RuntimeFlagsPanel() {
  const [snapshot, setSnapshot] = useState<RuntimeFlagsSnapshot | null>(null);
  const [draft, setDraft] = useState<Record<string, RuntimeFlagValue>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [resettingKey, setResettingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [categoryFilter, setCategoryFilter] = useState<CategoryFilter>("all");
  const [onlyModified, setOnlyModified] = useState(false);
  const [showEnvVars, setShowEnvVars] = useState(false);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

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

  const dirtyItems = useMemo(() => {
    if (!snapshot) return [];
    return snapshot.items.filter((item) => {
      const current = draft[item.key];
      return current !== undefined && current !== item.value;
    });
  }, [snapshot, draft]);

  const dirtyCount = dirtyItems.length;

  function applyPreset(preset: PresetDef) {
    setError(null);
    setNotice(null);
    setDraft((prev) => {
      const next = { ...prev };
      for (const [k, v] of Object.entries(preset.values)) {
        if (k in next) {
          next[k] = v;
        }
      }
      return next;
    });
  }

  function discardDraft() {
    if (!snapshot) return;
    const next: Record<string, RuntimeFlagValue> = {};
    for (const item of snapshot.items) {
      next[item.key] = item.value;
    }
    setDraft(next);
  }

  async function handleSave() {
    if (dirtyCount === 0) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const data = await updateRuntimeFlags(dirtyUpdates);
      syncSnapshot(data);
      setNotice(`已保存 ${dirtyCount} 项策略开关并立即生效（已写入 runtime-flags.json）。`);
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
      setNotice(
        keys ? "已恢复该开关为系统默认。" : "已清除全部自定义覆盖，恢复为环境变量 / 系统默认。",
      );
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "恢复默认失败");
    } finally {
      setResettingKey(null);
    }
  }

  function copyKey(key: string) {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(key);
    }
    setCopiedKey(key);
    window.setTimeout(() => {
      setCopiedKey((prev) => (prev === key ? null : prev));
    }, 1200);
  }

  const categoryCounts = useMemo(() => {
    const counts: Record<CategoryFilter, number> = { all: 0, session: 0, protocol: 0, runtime: 0 };
    for (const item of snapshot?.items ?? []) {
      counts.all += 1;
      counts[item.category] += 1;
    }
    return counts;
  }, [snapshot]);

  const grouped = useMemo(() => {
    const map: Record<RuntimeFlagItem["category"], RuntimeFlagItem[]> = {
      session: [],
      protocol: [],
      runtime: [],
    };
    const kw = query.trim().toLowerCase();
    for (const item of snapshot?.items ?? []) {
      if (categoryFilter !== "all" && item.category !== categoryFilter) continue;
      const val = draft[item.key] ?? item.value;
      const isDirty = val !== item.value;
      if (onlyModified && !isDirty && item.source !== "override") continue;
      if (kw) {
        const hay = `${item.key} ${item.label} ${item.description} ${item.env_var}`.toLowerCase();
        if (!hay.includes(kw)) continue;
      }
      map[item.category]?.push(item);
    }
    return map;
  }, [snapshot, query, categoryFilter, onlyModified, draft]);

  const visibleCount = grouped.session.length + grouped.protocol.length + grouped.runtime.length;

  return (
    <Stack gap="lg">
      {/* 顶部标题 + 快捷操作 */}
      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="md">
          <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
            <div>
              <Group gap="xs" align="center">
                <Title order={2}>策略开关</Title>
                <Badge color="gray" variant="light" size="sm">
                  共 {categoryCounts.all} 项
                </Badge>
                {(snapshot?.override_count ?? 0) > 0 && (
                  <Badge color="violet" variant="light" size="sm">
                    已自定义 {snapshot?.override_count} 项
                  </Badge>
                )}
                {dirtyCount > 0 && (
                  <Badge color="orange" variant="filled" size="sm">
                    待保存 {dirtyCount} 项
                  </Badge>
                )}
              </Group>
              <Text c="dimmed" size="sm" mt={4}>
                在线调整建单会话策略、客户端 OS 画像、代理容错与队列参数，保存后立即对新任务生效，无需重启。
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

          {/* 场景预设一键套用 */}
          <Stack gap={6}>
            <Group gap={6} align="center">
              <IconSparkles size={15} />
              <Text size="xs" fw={600} c="dimmed">
                常用场景一键预填（点选后需点击「保存并生效」确认）
              </Text>
            </Group>
            <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="sm">
              {PRESETS.map((preset) => (
                <Paper
                  key={preset.id}
                  withBorder
                  radius="md"
                  p="sm"
                  style={{ cursor: "pointer", transition: "border-color 120ms ease" }}
                  onClick={() => applyPreset(preset)}
                >
                  <Group justify="space-between" align="center" mb={4} wrap="nowrap">
                    <Text fw={600} size="sm" lineClamp={1}>
                      {preset.title}
                    </Text>
                    <Badge size="xs" color={preset.color} variant="light">
                      {preset.badge}
                    </Badge>
                  </Group>
                  <Text size="xs" c="dimmed" lh={1.45}>
                    {preset.desc}
                  </Text>
                </Paper>
              ))}
            </SimpleGrid>
          </Stack>

          {/* 搜索与筛选栏 */}
          <Group justify="space-between" align="center" wrap="wrap" gap="sm" pt={4}>
            <TextInput
              placeholder="搜索开关名、key（如 checkout_rotate）或环境变量..."
              value={query}
              onChange={(e) => setQuery(e.currentTarget.value)}
              leftSection={<IconSearch size={15} />}
              rightSection={
                query ? (
                  <ActionIcon
                    size="sm"
                    variant="subtle"
                    color="gray"
                    aria-label="清空搜索"
                    onClick={() => setQuery("")}
                  >
                    <IconX size={14} />
                  </ActionIcon>
                ) : null
              }
              size="sm"
              style={{ flex: "1 1 280px", maxWidth: 420 }}
            />
            <Group gap="sm" wrap="wrap">
              <SegmentedControl
                size="xs"
                value={categoryFilter}
                onChange={(v) => setCategoryFilter(v as CategoryFilter)}
                data={[
                  { value: "all", label: `全部 (${categoryCounts.all})` },
                  { value: "session", label: `Session (${categoryCounts.session})` },
                  { value: "protocol", label: `协议与代理 (${categoryCounts.protocol})` },
                  { value: "runtime", label: `队列对账 (${categoryCounts.runtime})` },
                ]}
              />
              <Switch
                size="xs"
                label="仅看已修改"
                checked={onlyModified}
                onChange={(e) => setOnlyModified(e.currentTarget.checked)}
              />
              <Switch
                size="xs"
                label="显示环境变量"
                checked={showEnvVars}
                onChange={(e) => setShowEnvVars(e.currentTarget.checked)}
              />
            </Group>
          </Group>

          {/* 待保存变更醒目条 */}
          {dirtyCount > 0 && (
            <Alert color="orange" variant="light" title={`有 ${dirtyCount} 项修改尚未保存`}>
              <Stack gap="xs">
                <Group gap={6} wrap="wrap">
                  {dirtyItems.map((item) => (
                    <Badge key={item.key} color="orange" variant="outline" size="sm" radius="sm">
                      {item.key}: {formatFlagValue(item, item.value)} →{" "}
                      {formatFlagValue(item, draft[item.key])}
                    </Badge>
                  ))}
                </Group>
                <Group gap="xs">
                  <Button size="xs" color="orange" loading={saving} onClick={() => void handleSave()}>
                    立即保存并生效 ({dirtyCount})
                  </Button>
                  <Button size="xs" variant="subtle" color="gray" disabled={saving} onClick={discardDraft}>
                    撤销未保存修改
                  </Button>
                </Group>
              </Stack>
            </Alert>
          )}

          {snapshot?.fake_mode && (
            <Alert color="blue" variant="light" icon={<IconInfoCircle size={16} />}>
              当前处于离线模拟模式（<Code>GSS_USE_FAKE=true</Code>），「强制重颁新 AT」默认关闭；真实上游模式下默认开启。
            </Alert>
          )}

          {notice && <Alert color="green" icon={<IconCheck size={16} />}>{notice}</Alert>}
          {error && <Alert color="red" title="操作失败">{error}</Alert>}

          {snapshot?.updated_at && (
            <Text size="xs" c="dimmed">
              最近保存：{new Date(snapshot.updated_at).toLocaleString()} · 优先级：面板覆盖 &gt; 环境变量 &gt; 系统默认
            </Text>
          )}
        </Stack>
      </Card>

      {/* 空筛选结果提示 */}
      {snapshot && visibleCount === 0 && (
        <Card withBorder radius="lg" p="xl" className="workspace-panel">
          <Stack align="center" gap="xs">
            <Text fw={600} size="sm">
              没有匹配的策略开关
            </Text>
            <Text size="xs" c="dimmed">
              试试清空搜索词或切换回「全部」分类。
            </Text>
            <Button
              size="xs"
              variant="light"
              mt={4}
              onClick={() => {
                setQuery("");
                setCategoryFilter("all");
                setOnlyModified(false);
              }}
            >
              重置筛选条件
            </Button>
          </Stack>
        </Card>
      )}

      {/* 分组开关卡片列表 */}
      {CATEGORY_ORDER.map((category) => {
        const items = grouped[category];
        if (!items || items.length === 0) return null;
        const meta = CATEGORY_META[category];
        return (
          <Card key={category} withBorder radius="lg" p="lg" className="workspace-panel">
            <Stack gap="md">
              <Group justify="space-between" align="center" wrap="wrap">
                <div>
                  <Group gap="xs" align="center">
                    <CategoryIcon category={category} />
                    <Title order={3} size="h4">
                      {meta.title}
                    </Title>
                    <Badge size="xs" variant="light" color="gray">
                      {items.length} 项
                    </Badge>
                  </Group>
                  <Text c="dimmed" size="sm" mt={2}>
                    {meta.subtitle}
                  </Text>
                </div>
              </Group>

              <Stack gap="sm">
                {items.map((item) => {
                  const val = draft[item.key] ?? item.value;
                  const isDirty = val !== item.value;
                  const isBoolOn = item.kind === "bool" && Boolean(val);
                  const srcBadge = SOURCE_BADGE[item.source];
                  const hintTag = FLAG_HINT_TAG[item.key];
                  const accentColor = isDirty
                    ? "var(--mantine-color-orange-6)"
                    : item.source === "override"
                      ? "var(--mantine-color-violet-6)"
                      : isBoolOn
                        ? "var(--mantine-color-teal-6)"
                        : "var(--mantine-color-gray-3)";

                  return (
                    <Paper
                      key={item.key}
                      withBorder
                      radius="md"
                      p="md"
                      style={{
                        borderLeft: `4px solid ${accentColor}`,
                        backgroundColor: isDirty
                          ? "var(--mantine-color-orange-0)"
                          : undefined,
                      }}
                    >
                      <Group justify="space-between" align="flex-start" wrap="wrap" gap="md">
                        <Stack gap={6} style={{ flex: "1 1 380px", minWidth: 260, maxWidth: 760 }}>
                          <Group gap={8} wrap="wrap" align="center">
                            <Text fw={600} size="md" lh={1.35}>
                              {item.label}
                            </Text>
                            <Tooltip
                              label={copiedKey === item.key ? "已复制 key" : "点击复制开关 key"}
                              withArrow
                            >
                              <Badge
                                size="sm"
                                variant="default"
                                radius="sm"
                                leftSection={
                                  copiedKey === item.key ? (
                                    <IconCheck size={11} />
                                  ) : (
                                    <IconCopy size={11} />
                                  )
                                }
                                style={{
                                  cursor: "pointer",
                                  fontFamily: "var(--mantine-font-family-monospace)",
                                  textTransform: "none",
                                  fontWeight: 500,
                                }}
                                onClick={() => copyKey(item.key)}
                              >
                                {item.key}
                              </Badge>
                            </Tooltip>
                            {hintTag && (
                              <Badge size="xs" color={hintTag.color} variant="light">
                                {hintTag.label}
                              </Badge>
                            )}
                            {item.source !== "default" && (
                              <Badge size="xs" color={srcBadge.color} variant="light">
                                {srcBadge.label}
                              </Badge>
                            )}
                            {isDirty && (
                              <Badge size="xs" color="orange" variant="filled">
                                未保存
                              </Badge>
                            )}
                          </Group>

                          <Text size="sm" c="dimmed" lh={1.55}>
                            {item.description}
                          </Text>

                          <Group gap="sm" wrap="wrap" align="center" mt={2}>
                            <Text size="xs" c="dimmed">
                              默认：<Text span fw={500}>{formatFlagValue(item, item.default)}</Text>
                              {item.env_value !== null && item.env_value !== undefined
                                ? ` · 环境变量：${formatFlagValue(item, item.env_value)}`
                                : ""}
                            </Text>
                            {showEnvVars && <Code fz={11}>{item.env_var}</Code>}
                            {item.source === "override" && (
                              <Button
                                size="compact-xs"
                                variant="subtle"
                                color="violet"
                                loading={resettingKey === item.key}
                                disabled={saving || resettingKey !== null}
                                onClick={() => void handleReset([item.key])}
                              >
                                恢复默认
                              </Button>
                            )}
                          </Group>
                        </Stack>

                        <Group align="center" gap="xs" style={{ flexShrink: 0, paddingTop: 2 }}>
                          {item.kind === "bool" && (
                            <>
                              <Text
                                size="xs"
                                fw={600}
                                c={Boolean(val) ? "teal.7" : "dimmed"}
                                w={36}
                                ta="right"
                              >
                                {Boolean(val) ? "已开启" : "已关闭"}
                              </Text>
                              <Switch
                                size="md"
                                color="teal"
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
                            </>
                          )}
                          {item.kind === "enum" && item.options.length <= 2 && (
                            <SegmentedControl
                              size="xs"
                              value={String(val)}
                              disabled={saving || resettingKey !== null}
                              data={item.options.map((opt) => ({
                                value: opt.value,
                                label: opt.label,
                              }))}
                              onChange={(next) => {
                                setDraft((prev) => ({ ...prev, [item.key]: next }));
                              }}
                            />
                          )}
                          {item.kind === "enum" && item.options.length > 2 && (
                            <Select
                              size="sm"
                              w={230}
                              allowDeselect={false}
                              aria-label={item.label}
                              value={String(val)}
                              disabled={saving || resettingKey !== null}
                              data={item.options.map((opt) => ({
                                value: opt.value,
                                label: opt.label,
                              }))}
                              onChange={(next) => {
                                if (next) {
                                  setDraft((prev) => ({ ...prev, [item.key]: next }));
                                }
                              }}
                            />
                          )}
                          {item.kind === "int" && (
                            <NumberInput
                              size="sm"
                              w={110}
                              min={item.min_value ?? 1}
                              max={item.max_value ?? 32}
                              step={1}
                              clampBehavior="strict"
                              aria-label={item.label}
                              value={typeof val === "number" ? val : Number(val)}
                              disabled={saving || resettingKey !== null}
                              onChange={(next) => {
                                if (typeof next === "number" && Number.isFinite(next)) {
                                  setDraft((prev) => ({ ...prev, [item.key]: Math.trunc(next) }));
                                }
                              }}
                            />
                          )}
                        </Group>
                      </Group>
                    </Paper>
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
