import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Code,
  CopyButton,
  Group,
  Menu,
  Modal,
  MultiSelect,
  NumberInput,
  SegmentedControl,
  SimpleGrid,
  Skeleton,
  Stack,
  Table,
  Text,
  Textarea,
  TextInput,
  Tooltip,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconBook2,
  IconCheck,
  IconCopy,
  IconDotsVertical,
  IconEdit,
  IconKey,
  IconPlayerPause,
  IconPlayerPlay,
  IconPlus,
  IconRefresh,
  IconRotateClockwise,
  IconTrash,
} from "@tabler/icons-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  createApiKey,
  listApiKeys,
  revokeApiKey,
  rotateApiKey,
  updateApiKey,
  type ApiKeyItem,
  type ApiKeySecretResponse,
} from "../api/developer";
import { isAbortError } from "../api/http";
import { planLabel } from "../plans";
import { formatDateTimeCompact, formatMonthDayTime, planPillTone } from "../utils/orderLedgerFormat";
import { AdminPageHeader } from "./AdminPageHeader";

const ALL_PLAN_OPTIONS = [
  { value: "go", label: "Go" },
  { value: "plus", label: "Plus" },
  { value: "pro_lite", label: "Pro100 (pro_lite)" },
  { value: "pro", label: "Pro200 (pro)" },
  { value: "pro_max", label: "Pro500 (pro_max)" },
];

const DEFAULT_PLANS = ["go", "plus", "pro_lite", "pro", "pro_max"];

function statusMeta(status: string): { label: string; tone: "success" | "warning" | "failed" } {
  if (status === "active") return { label: "启用中", tone: "success" };
  if (status === "disabled") return { label: "已停用", tone: "warning" };
  return { label: "已吊销", tone: "failed" };
}

export function DeveloperApiPanel({ active = true }: { active?: boolean }) {
  const [subTab, setSubTab] = useState<"keys" | "docs">("keys");
  const [items, setItems] = useState<ApiKeyItem[]>([]);
  const [retentionDays, setRetentionDays] = useState(30);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  // Create / Edit modal state
  const [editorMode, setEditorMode] = useState<"create" | "edit" | null>(null);
  const [editingKey, setEditingKey] = useState<ApiKeyItem | null>(null);
  const [formName, setFormName] = useState("");
  const [formRpm, setFormRpm] = useState<number>(30);
  const [formPlans, setFormPlans] = useState<string[]>(DEFAULT_PLANS);
  const [formNote, setFormNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // One-time secret display modal
  const [mintedSecret, setMintedSecret] = useState<ApiKeySecretResponse | null>(null);

  // Revoke confirm modal
  const [revokingKey, setRevokingKey] = useState<ApiKeyItem | null>(null);

  const origin = useMemo(() => {
    try {
      return window.location.origin;
    } catch {
      return "https://your-domain.example";
    }
  }, []);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const res = await listApiKeys(signal);
      if (!signal?.aborted) {
        setItems(res.items || []);
        setRetentionDays(res.vault_retention_days || 30);
      }
    } catch (exc) {
      if (isAbortError(exc)) return;
      setError(exc instanceof Error ? exc.message : "加载 API Key 列表失败");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [active, load]);

  const stats = useMemo(() => {
    let activeKeys = 0;
    let totalOrders = 0;
    let succeededOrders = 0;
    let pendingOrders = 0;
    for (const k of items) {
      if (k.status === "active") activeKeys += 1;
      totalOrders += k.total_orders || 0;
      succeededOrders += k.succeeded_orders || 0;
      pendingOrders += k.pending_orders || 0;
    }
    return { activeKeys, totalOrders, succeededOrders, pendingOrders };
  }, [items]);

  function openCreateModal() {
    setEditingKey(null);
    setFormName("");
    setFormRpm(30);
    setFormPlans(DEFAULT_PLANS);
    setFormNote("");
    setFormError(null);
    setEditorMode("create");
  }

  function openEditModal(item: ApiKeyItem) {
    setEditingKey(item);
    setFormName(item.name);
    setFormRpm(item.rate_limit_rpm);
    setFormPlans(item.allowed_plans.length > 0 ? item.allowed_plans : DEFAULT_PLANS);
    setFormNote(item.note || "");
    setFormError(null);
    setEditorMode("edit");
  }

  async function handleSaveEditor() {
    if (saving) return;
    const cleanName = formName.trim();
    if (!cleanName) {
      setFormError("请填写商户 / 开发者名称");
      return;
    }
    if (formPlans.length === 0) {
      setFormError("至少选择一个允许调用的套餐");
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      if (editorMode === "create") {
        const res = await createApiKey({
          name: cleanName,
          rate_limit_rpm: formRpm || 30,
          allowed_plans: formPlans,
          note: formNote.trim(),
        });
        setEditorMode(null);
        setMintedSecret(res);
      } else if (editorMode === "edit" && editingKey) {
        await updateApiKey(editingKey.id, {
          name: cleanName,
          rate_limit_rpm: formRpm || 30,
          allowed_plans: formPlans,
          note: formNote.trim(),
        });
        setEditorMode(null);
      }
      await load();
    } catch (exc) {
      setFormError(exc instanceof Error ? exc.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleToggleStatus(item: ApiKeyItem) {
    if (busyId || item.status === "revoked") return;
    setBusyId(item.id);
    setError(null);
    try {
      await updateApiKey(item.id, {
        status: item.status === "active" ? "disabled" : "active",
      });
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "切换状态失败");
    } finally {
      setBusyId(null);
    }
  }

  async function handleRotate(item: ApiKeyItem) {
    if (busyId) return;
    setBusyId(item.id);
    setError(null);
    try {
      const res = await rotateApiKey(item.id);
      setMintedSecret(res);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "轮换密钥失败");
    } finally {
      setBusyId(null);
    }
  }

  async function handleConfirmRevoke() {
    if (!revokingKey || busyId) return;
    setBusyId(revokingKey.id);
    setError(null);
    try {
      await revokeApiKey(revokingKey.id);
      setRevokingKey(null);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "吊销密钥失败");
    } finally {
      setBusyId(null);
    }
  }

  const curlQuickstart = useMemo(() => {
    const sampleKey = mintedSecret?.api_key || "gss_live_your_api_key_here";
    return `curl -X POST "${origin}/api/v1/recharges" \\
  -H "Authorization: Bearer ${sampleKey}" \\
  -H "Idempotency-Key: order-20261127-0001" \\
  -H "Content-Type: application/json" \\
  -d '{
    "plan": "pro100",
    "exit_country": "CL",
    "external_order_id": "MCH-20261127-0001",
    "auto_cancel_renew": true,
    "session": "{\\"accessToken\\":\\"...\\",\\"sessionToken\\":\\"...\\"}",
    "card": {
      "number": "<16位虚拟卡号>",
      "exp_month": "12",
      "exp_year": "2028",
      "cvc": "<3位安全码>"
    }
  }'`;
  }, [origin, mintedSecret]);

  const pythonExample = useMemo(() => {
    return `import time, uuid, httpx

BASE_URL = "${origin}"
API_KEY = "gss_live_your_api_key_here"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

with httpx.Client(base_url=BASE_URL, headers=HEADERS, timeout=30.0) as client:
    # 1. 创建异步充值任务（务必携带稳定 Idempotency-Key）
    idem = f"mch-{uuid.uuid4().hex}"
    resp = client.post(
        "/api/v1/recharges",
        headers={"Idempotency-Key": idem},
        json={
            "plan": "plus",              # go / plus / pro100 / pro200 / pro500
            "exit_country": "PH",        # 留空则沿用平台默认低价区
            "external_order_id": idem,
            "auto_cancel_renew": True,
            "session": session_json_str, # 客户 ChatGPT Session JSON 字符串
            "card": {
                "number": card_number,
                "exp_month": "12",
                "exp_year": "2028",
                "cvc": "123",
            },
        },
    )
    resp.raise_for_status()
    job_id = resp.json()["job_id"]

    # 2. 轮询直到终态（也可直接订阅 GET /api/v1/recharges/{job_id}/stream SSE）
    while True:
        st = client.get(f"/api/v1/recharges/{job_id}").json()
        if st["status"] in ("succeeded", "failed", "unknown", "paid_pending_delivery", "requires_action"):
            print("终态:", st["status"], "金额:", st.get("amount"), st.get("currency"))
            break
        time.sleep(2)`;
  }, [origin]);

  return (
    <Stack gap="md">
      <AdminPageHeader
        title="开发者 API"
        badge={
          <Group gap={6} wrap="wrap">
            <Badge size="sm" variant="light" color="teal">活跃密钥 {stats.activeKeys}</Badge>
            <Badge size="sm" variant="light" color="blue">API 订单 {stats.totalOrders}</Badge>
            <Badge size="sm" variant="light" color="indigo">凭证留存 {retentionDays} 天</Badge>
          </Group>
        }
        subtitle="为合作开发者与工作室签发独立 API Key（Bearer 鉴权 + 租户订单隔离 + 限流），支持异步建单、SSE 实时进度、订阅直查与自动关续费。"
        actions={
          <Group gap="xs">
            <SegmentedControl
              size="xs"
              value={subTab}
              onChange={(v) => setSubTab(v as "keys" | "docs")}
              data={[
                { value: "keys", label: "API 密钥管理" },
                { value: "docs", label: "快速对接文档" },
              ]}
            />
            <Button
              size="xs"
              variant="default"
              leftSection={<IconRefresh size={14} />}
              loading={loading}
              onClick={() => void load()}
            >
              刷新
            </Button>
            <Button
              size="xs"
              leftSection={<IconPlus size={14} />}
              onClick={openCreateModal}
            >
              新建 API Key
            </Button>
          </Group>
        }
      />

      {error && (
        <Alert color="red" radius="md" icon={<IconAlertCircle size={16} />} withCloseButton onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* KPI 统计条 */}
      <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="sm">
        <div className="ops-kpi-card" style={{ cursor: "default" }}>
          <div className="ops-kpi-top">
            <div className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--blue"><IconKey size={15} /></span>
              <span className="ops-kpi-title">活跃 API Key</span>
            </div>
          </div>
          <div className="ops-kpi-value">{stats.activeKeys}</div>
          <div className="ops-kpi-sub">共 {items.length} 个密钥（哈希存储）</div>
        </div>
        <div className="ops-kpi-card" style={{ cursor: "default" }}>
          <div className="ops-kpi-top">
            <div className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--indigo"><IconBook2 size={15} /></span>
              <span className="ops-kpi-title">累计 API 订单</span>
            </div>
          </div>
          <div className="ops-kpi-value">{stats.totalOrders}</div>
          <div className="ops-kpi-sub">通过 /api/v1/recharges 提交</div>
        </div>
        <div className="ops-kpi-card" style={{ cursor: "default" }}>
          <div className="ops-kpi-top">
            <div className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--teal"><IconCheck size={15} /></span>
              <span className="ops-kpi-title">成功交付</span>
            </div>
          </div>
          <div className="ops-kpi-value">{stats.succeededOrders}</div>
          <div className="ops-kpi-sub">可按成功单量与合作方结算</div>
        </div>
        <div className="ops-kpi-card" style={{ cursor: "default" }}>
          <div className="ops-kpi-top">
            <div className="ops-kpi-label-wrap">
              <span className="ops-kpi-icon ops-kpi-icon--amber"><IconAlertCircle size={15} /></span>
              <span className="ops-kpi-title">进行中 / 待核对</span>
            </div>
          </div>
          <div className="ops-kpi-value">{stats.pendingOrders}</div>
          <div className="ops-kpi-sub">全部订单凭证加密留存 {retentionDays} 天</div>
        </div>
      </SimpleGrid>

      {subTab === "keys" ? (
        <section className="ops-panel" style={{ padding: 0, overflow: "hidden" }}>
          <div style={{ padding: "14px 16px 10px", borderBottom: "1px solid var(--studio-line)" }}>
            <Group justify="space-between" align="center" wrap="wrap" gap="xs">
              <div>
                <Text fw={650} size="sm">API 密钥列表</Text>
                <Text size="xs" c="dimmed">
                  数据库仅保存密钥 SHA-256 哈希，明文仅在创建或轮换时展示一次；停用或吊销立即生效。
                </Text>
              </div>
              <Code fz={12}>{origin}/api/v1</Code>
            </Group>
          </div>

          {loading && items.length === 0 ? (
            <Stack gap="xs" p="md">
              <Skeleton height={34} radius="sm" />
              <Skeleton height={34} radius="sm" />
            </Stack>
          ) : items.length === 0 ? (
            <Stack align="center" gap="xs" py="xl" px="md">
              <Text size="sm" fw={600}>暂未创建任何开发者 API Key</Text>
              <Text size="xs" c="dimmed">点击右上角「新建 API Key」即可生成密钥并交付给接入方调用。</Text>
              <Button size="xs" mt={4} leftSection={<IconPlus size={14} />} onClick={openCreateModal}>
                立即创建首个 API Key
              </Button>
            </Stack>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <Table className="ledger-table" verticalSpacing="sm" horizontalSpacing="md" highlightOnHover>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>名称 / 备注</Table.Th>
                    <Table.Th>密钥前缀</Table.Th>
                    <Table.Th>状态</Table.Th>
                    <Table.Th>开放套餐</Table.Th>
                    <Table.Th>限流</Table.Th>
                    <Table.Th>成功 / 总单</Table.Th>
                    <Table.Th>最近调用</Table.Th>
                    <Table.Th style={{ textAlign: "right", width: 112 }}>操作</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {items.map((item) => {
                    const st = statusMeta(item.status);
                    const isRevoked = item.status === "revoked";
                    return (
                      <Table.Tr key={item.id}>
                        <Table.Td>
                          <Text size="sm" fw={600}>{item.name}</Text>
                          {item.note && (
                            <Text size="xs" c="dimmed" lineClamp={1} maw={220}>{item.note}</Text>
                          )}
                        </Table.Td>
                        <Table.Td>
                          <Code fz={12}>{item.key_prefix}…</Code>
                        </Table.Td>
                        <Table.Td>
                          <span className={`ledger-pill ledger-pill--status-${st.tone}`}>
                            <span className={`ledger-dot ledger-dot--${st.tone}`} />
                            {st.label}
                          </span>
                        </Table.Td>
                        <Table.Td>
                          <Group gap={4} wrap="wrap" maw={230}>
                            {item.allowed_plans.map((p) => (
                              <span key={p} className={`ledger-pill ledger-pill--plan ledger-pill--plan-${planPillTone(p)}`}>
                                {planLabel(p)}
                              </span>
                            ))}
                          </Group>
                        </Table.Td>
                        <Table.Td>
                          <Text size="xs" fw={550}>{item.rate_limit_rpm} / min</Text>
                        </Table.Td>
                        <Table.Td>
                          <Text size="xs" fw={650} style={{ fontVariantNumeric: "tabular-nums" }}>
                            <span style={{ color: "#15803d" }}>{item.succeeded_orders}</span>
                            {" / "}
                            {item.total_orders}
                          </Text>
                          <Text size="xs" c="dimmed" fz={11}>
                            调用 {item.total_requests} 次
                          </Text>
                        </Table.Td>
                        <Table.Td>
                          {item.last_used_at ? (
                            <Tooltip label={formatDateTimeCompact(item.last_used_at)} withArrow>
                              <span className="ledger-time">{formatMonthDayTime(item.last_used_at)}</span>
                            </Tooltip>
                          ) : (
                            <span className="ledger-muted">尚未使用</span>
                          )}
                        </Table.Td>
                        <Table.Td>
                          <Group gap={4} justify="flex-end" wrap="nowrap">
                            {!isRevoked ? (
                              <Button
                                size="compact-xs"
                                variant="light"
                                color={item.status === "active" ? "orange" : "teal"}
                                loading={busyId === item.id}
                                onClick={() => void handleToggleStatus(item)}
                              >
                                {item.status === "active" ? "停用" : "启用"}
                              </Button>
                            ) : (
                              <Button
                                size="compact-xs"
                                variant="light"
                                color="blue"
                                loading={busyId === item.id}
                                onClick={() => void handleRotate(item)}
                              >
                                重签
                              </Button>
                            )}
                            <Menu position="bottom-end" withinPortal shadow="md" width={160}>
                              <Menu.Target>
                                <ActionIcon
                                  size="sm"
                                  variant="subtle"
                                  color="gray"
                                  aria-label="更多 API Key 操作"
                                >
                                  <IconDotsVertical size={15} />
                                </ActionIcon>
                              </Menu.Target>
                              <Menu.Dropdown>
                                <Menu.Item leftSection={<IconEdit size={14} />} onClick={() => openEditModal(item)}>
                                  编辑配额与备注
                                </Menu.Item>
                                {!isRevoked && (
                                  <Menu.Item
                                    leftSection={item.status === "active" ? <IconPlayerPause size={14} /> : <IconPlayerPlay size={14} />}
                                    onClick={() => void handleToggleStatus(item)}
                                  >
                                    {item.status === "active" ? "临时停用" : "恢复启用"}
                                  </Menu.Item>
                                )}
                                <Menu.Item
                                  leftSection={<IconRotateClockwise size={14} />}
                                  onClick={() => void handleRotate(item)}
                                >
                                  轮换新密钥
                                </Menu.Item>
                                {!isRevoked && (
                                  <>
                                    <Menu.Divider />
                                    <Menu.Item
                                      color="red"
                                      leftSection={<IconTrash size={14} />}
                                      onClick={() => setRevokingKey(item)}
                                    >
                                      吊销密钥
                                    </Menu.Item>
                                  </>
                                )}
                              </Menu.Dropdown>
                            </Menu>
                          </Group>
                        </Table.Td>
                      </Table.Tr>
                    );
                  })}
                </Table.Tbody>
              </Table>
            </div>
          )}
        </section>
      ) : (
        <Stack gap="md">
          <section className="ops-panel">
            <Stack gap="sm">
              <Group justify="space-between" align="center" wrap="wrap">
                <div>
                  <Text fw={700} size="sm">接口基础信息</Text>
                  <Text size="xs" c="dimmed">
                    完整 Markdown 对接手册位于仓库 <Code fz={11}>docs/DEVELOPER_API.md</Code>，可直接发给合作开发者。
                  </Text>
                </div>
                <CopyButton value={`${origin}/api/v1`}>
                  {({ copied, copy }) => (
                    <Button size="xs" variant="light" leftSection={copied ? <IconCheck size={14} /> : <IconCopy size={14} />} onClick={copy}>
                      {copied ? "已复制 Base URL" : `复制 ${origin}/api/v1`}
                    </Button>
                  )}
                </CopyButton>
              </Group>

              <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="xs">
                <div className="order-kpi-card">
                  <span className="order-kpi-label">鉴权请求头</span>
                  <Code mt={4} fz={12}>Authorization: Bearer gss_live_...</Code>
                  <span className="order-kpi-sub">同时兼容 X-API-Key 请求头</span>
                </div>
                <div className="order-kpi-card">
                  <span className="order-kpi-label">下单幂等键（必填）</span>
                  <Code mt={4} fz={12}>Idempotency-Key: &lt;唯一单号&gt;</Code>
                  <span className="order-kpi-sub">按 API Key 隔离，重试须复用同一键</span>
                </div>
                <div className="order-kpi-card">
                  <span className="order-kpi-label">支持套餐代码</span>
                  <Code mt={4} fz={12}>go / plus / pro100 / pro200 / pro500</Code>
                  <span className="order-kpi-sub">兼容 pro_lite / pro / pro_max</span>
                </div>
              </SimpleGrid>

              <Table withTableBorder verticalSpacing="xs" fz="xs">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>方法与路径</Table.Th>
                    <Table.Th>用途</Table.Th>
                    <Table.Th>说明</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  <Table.Tr>
                    <Table.Td><Code>GET /api/v1/ping</Code></Table.Td>
                    <Table.Td>连通性与密钥校验</Table.Td>
                    <Table.Td>返回允许套餐、限流配额与当前队列状态</Table.Td>
                  </Table.Tr>
                  <Table.Tr>
                    <Table.Td><Code>GET /api/v1/plans</Code></Table.Td>
                    <Table.Td>查询套餐与参考价格</Table.Td>
                    <Table.Td>返回各套餐低价区 USD/CNY 及当地币种参考价</Table.Td>
                  </Table.Tr>
                  <Table.Tr>
                    <Table.Td><Code>POST /api/v1/recharges</Code></Table.Td>
                    <Table.Td>发起异步协议充值</Table.Td>
                    <Table.Td>返回 HTTP 202 + job_id；携带 Session、虚拟卡与 Idempotency-Key</Table.Td>
                  </Table.Tr>
                  <Table.Tr>
                    <Table.Td><Code>GET /api/v1/recharges/{"{job_id}"}</Code></Table.Td>
                    <Table.Td>轮询单笔任务状态</Table.Td>
                    <Table.Td>终态：succeeded / failed / unknown / paid_pending_delivery / requires_action</Table.Td>
                  </Table.Tr>
                  <Table.Tr>
                    <Table.Td><Code>GET /api/v1/recharges/{"{job_id}"}/stream</Code></Table.Td>
                    <Table.Td>SSE 实时进度推送</Table.Td>
                    <Table.Td>实时接收建单、支付、交付核验与关续费事件</Table.Td>
                  </Table.Tr>
                  <Table.Tr>
                    <Table.Td><Code>GET /api/v1/recharges</Code></Table.Td>
                    <Table.Td>分页查询本密钥历史单</Table.Td>
                    <Table.Td>支持 status / external_order_id / q 筛选，严格租户隔离</Table.Td>
                  </Table.Tr>
                  <Table.Tr>
                    <Table.Td><Code>POST /api/v1/account/subscription</Code></Table.Td>
                    <Table.Td>实时直查账号订阅</Table.Td>
                    <Table.Td>零缓存直查上游当前套餐、到期日、自动续费与账单</Table.Td>
                  </Table.Tr>
                  <Table.Tr>
                    <Table.Td><Code>POST /api/v1/account/cancel-auto-renew</Code></Table.Td>
                    <Table.Td>关闭账号自动续费</Table.Td>
                    <Table.Td>一等接口关闭下期自动续费，不退款、保留当前周期权益</Table.Td>
                  </Table.Tr>
                </Table.Tbody>
              </Table>
            </Stack>
          </section>

          <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md">
            <section className="ops-panel">
              <Group justify="space-between" mb="xs">
                <Text fw={650} size="sm">cURL 发起充值示例</Text>
                <CopyButton value={curlQuickstart}>
                  {({ copied, copy }) => (
                    <Button size="compact-xs" variant="light" leftSection={copied ? <IconCheck size={13} /> : <IconCopy size={13} />} onClick={copy}>
                      {copied ? "已复制" : "复制 cURL"}
                    </Button>
                  )}
                </CopyButton>
              </Group>
              <Code block fz={11.5} style={{ whiteSpace: "pre", overflowX: "auto" }}>
                {curlQuickstart}
              </Code>
            </section>

            <section className="ops-panel">
              <Group justify="space-between" mb="xs">
                <Text fw={650} size="sm">Python (httpx) 完整轮询示例</Text>
                <CopyButton value={pythonExample}>
                  {({ copied, copy }) => (
                    <Button size="compact-xs" variant="light" leftSection={copied ? <IconCheck size={13} /> : <IconCopy size={13} />} onClick={copy}>
                      {copied ? "已复制" : "复制 Python"}
                    </Button>
                  )}
                </CopyButton>
              </Group>
              <Code block fz={11.5} style={{ whiteSpace: "pre", overflowX: "auto" }}>
                {pythonExample}
              </Code>
            </section>
          </SimpleGrid>
        </Stack>
      )}

      {/* 创建 / 编辑 API Key 弹窗 */}
      <Modal
        opened={editorMode !== null}
        onClose={() => { if (!saving) setEditorMode(null); }}
        title={editorMode === "create" ? "新建开发者 API Key" : `编辑 API Key · ${editingKey?.name || ""}`}
        size="md"
        radius="lg"
        centered
      >
        <Stack gap="sm">
          <TextInput
            label="商户 / 开发者名称"
            placeholder="例如：星河代充工作室 / Partner-A"
            withAsterisk
            value={formName}
            onChange={(e) => setFormName(e.currentTarget.value)}
            disabled={saving}
          />
          <NumberInput
            label="每分钟请求上限（RPM）"
            description="用于防止高频刷单或异常死循环打满代理池，默认 30 次/分钟。"
            min={1}
            max={600}
            value={formRpm}
            onChange={(v) => setFormRpm(typeof v === "number" ? v : 30)}
            disabled={saving}
          />
          <MultiSelect
            label="允许调用的套餐"
            data={ALL_PLAN_OPTIONS}
            value={formPlans}
            onChange={setFormPlans}
            disabled={saving}
          />
          <Textarea
            label="备注（选填）"
            placeholder="可记录联系方式、结算单价约定（如 $0.10/单）等"
            minRows={2}
            value={formNote}
            onChange={(e) => setFormNote(e.currentTarget.value)}
            disabled={saving}
          />
          {formError && (
            <Alert color="red" radius="md" icon={<IconAlertCircle size={15} />}>
              {formError}
            </Alert>
          )}
          <Group justify="flex-end" gap="xs" pt={4}>
            <Button size="xs" variant="default" disabled={saving} onClick={() => setEditorMode(null)}>
              取消
            </Button>
            <Button size="xs" loading={saving} onClick={() => void handleSaveEditor()}>
              {editorMode === "create" ? "生成密钥" : "保存修改"}
            </Button>
          </Group>
        </Stack>
      </Modal>

      {/* 一次性明文密钥展示弹窗 */}
      <Modal
        opened={mintedSecret !== null}
        onClose={() => setMintedSecret(null)}
        title="请立即保存新生成的 API Key"
        size="lg"
        radius="lg"
        centered
      >
        {mintedSecret && (
          <Stack gap="md">
            <Alert color="orange" radius="md" icon={<IconAlertCircle size={16} />} title="此明文密钥仅展示这一次">
              {mintedSecret.warning}
            </Alert>
            <div className="order-detail-section" style={{ padding: 14 }}>
              <Text size="xs" c="dimmed" mb={6}>
                密钥名称：{mintedSecret.key.name}（ID: {mintedSecret.key.id}）
              </Text>
              <Group justify="space-between" gap="xs" wrap="nowrap">
                <Code fz={13} fw={700} style={{ wordBreak: "break-all", flex: 1, padding: "8px 10px" }}>
                  {mintedSecret.api_key}
                </Code>
                <CopyButton value={mintedSecret.api_key}>
                  {({ copied, copy }) => (
                    <Button size="xs" color={copied ? "teal" : "blue"} leftSection={copied ? <IconCheck size={14} /> : <IconCopy size={14} />} onClick={copy}>
                      {copied ? "已复制" : "复制密钥"}
                    </Button>
                  )}
                </CopyButton>
              </Group>
            </div>
            <div>
              <Group justify="space-between" mb={6}>
                <Text size="xs" fw={600}>快速测试连通性</Text>
                <CopyButton value={`curl -sS "${origin}/api/v1/ping" -H "Authorization: Bearer ${mintedSecret.api_key}"`}>
                  {({ copied, copy }) => (
                    <Button size="compact-xs" variant="subtle" onClick={copy}>
                      {copied ? "已复制" : "复制命令"}
                    </Button>
                  )}
                </CopyButton>
              </Group>
              <Code block fz={11.5}>
                {`curl -sS "${origin}/api/v1/ping" -H "Authorization: Bearer ${mintedSecret.api_key}"`}
              </Code>
            </div>
            <Group justify="flex-end">
              <Button size="xs" onClick={() => setMintedSecret(null)}>
                我已保存，关闭窗口
              </Button>
            </Group>
          </Stack>
        )}
      </Modal>

      {/* 吊销确认弹窗 */}
      <Modal
        opened={revokingKey !== null}
        onClose={() => { if (!busyId) setRevokingKey(null); }}
        title="确认吊销 API Key"
        size="sm"
        radius="lg"
        centered
      >
        {revokingKey && (
          <Stack gap="md">
            <Text size="sm">
              吊销 <strong>{revokingKey.name}</strong>（<Code>{revokingKey.key_prefix}…</Code>）后，该密钥将立即失效且无法恢复（历史订单统计仍会保留）。
            </Text>
            <Group justify="flex-end" gap="xs">
              <Button size="xs" variant="default" disabled={Boolean(busyId)} onClick={() => setRevokingKey(null)}>
                取消
              </Button>
              <Button size="xs" color="red" loading={busyId === revokingKey.id} onClick={() => void handleConfirmRevoke()}>
                确认吊销
              </Button>
            </Group>
          </Stack>
        )}
      </Modal>
    </Stack>
  );
}
