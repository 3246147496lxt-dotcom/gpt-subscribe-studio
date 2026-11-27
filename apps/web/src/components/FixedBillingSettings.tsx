import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Divider,
  Group,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import {
  IconAlertTriangle,
  IconCheck,
  IconDeviceFloppy,
  IconMapPin,
  IconRefresh,
  IconSparkles,
  IconTrash,
} from "@tabler/icons-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  deleteFixedBillingSettings,
  getFixedBillingSettings,
  saveFixedBillingSettings,
  type FixedBillingAddress,
  type FixedBillingSettings as FixedBillingSnapshot,
  type FixedBillingSource,
} from "../api/fixedBilling";

const FALLBACK_STATES = [
  { value: "OR", label: "Oregon (OR · 俄勒冈州)" },
  { value: "DE", label: "Delaware (DE · 特拉华州)" },
  { value: "MT", label: "Montana (MT · 蒙大拿州)" },
  { value: "NH", label: "New Hampshire (NH · 新罕布什尔州)" },
  { value: "AK", label: "Alaska (AK · 阿拉斯加州)" },
];

const EMPTY_FORM: FixedBillingAddress = {
  name: "",
  email: "",
  line1: "",
  city: "",
  state: "OR",
  postal_code: "",
  country: "US",
};

const SOURCE_META: Record<FixedBillingSource, { label: string; color: string }> = {
  managed: { label: "后台托管文件", color: "teal" },
  env_fixed: { label: "环境变量指定", color: "blue" },
  legacy_env: { label: "旧版核验文件", color: "grape" },
  none: { label: "尚未配置", color: "gray" },
};

function formatTimestamp(iso?: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

export function FixedBillingSettings({ onNavigate }: { onNavigate?: (tab: string) => void }) {
  const [snapshot, setSnapshot] = useState<FixedBillingSnapshot | null>(null);
  const [form, setForm] = useState<FixedBillingAddress>(EMPTY_FORM);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const syncSnapshot = useCallback((data: FixedBillingSnapshot) => {
    setSnapshot(data);
    if (data.address) {
      setForm({
        name: data.address.name || "",
        email: data.address.email || "",
        line1: data.address.line1 || "",
        city: data.address.city || "",
        state: data.address.state || "OR",
        postal_code: data.address.postal_code || "",
        country: "US",
      });
    } else {
      setForm((prev) => ({
        ...EMPTY_FORM,
        state: prev.state || "OR",
      }));
    }
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getFixedBillingSettings();
      syncSnapshot(data);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "加载固定账单配置失败");
    } finally {
      setLoading(false);
    }
  }, [syncSnapshot]);

  useEffect(() => {
    void load();
  }, [load]);

  const stateOptions = useMemo(
    () => (snapshot?.tax_free_states?.length ? snapshot.tax_free_states : FALLBACK_STATES),
    [snapshot],
  );

  function updateField<K extends keyof FixedBillingAddress>(key: K, value: FixedBillingAddress[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function handleFillSample() {
    const sample = snapshot?.default_sample;
    if (!sample) return;
    setForm({
      name: sample.name,
      email: sample.email,
      line1: sample.line1,
      city: sample.city,
      state: sample.state,
      postal_code: sample.postal_code,
      country: "US",
    });
    setError(null);
    setNotice("已填入美国俄勒冈州（OR）免税示例地址，请按需修改后点击「保存并生效」。");
  }

  async function handleSave() {
    const payload: FixedBillingAddress = {
      name: form.name.trim(),
      email: form.email.trim(),
      line1: form.line1.trim(),
      city: form.city.trim(),
      state: form.state.trim().toUpperCase(),
      postal_code: form.postal_code.trim(),
      country: "US",
    };
    if (!payload.name || !payload.email || !payload.line1 || !payload.city || !payload.state || !payload.postal_code) {
      setError("请完整填写付款方姓名、账单邮箱、街道地址、城市、免税州与邮编。");
      setNotice(null);
      return;
    }

    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const data = await saveFixedBillingSettings(payload);
      syncSnapshot(data);
      setNotice(
        `美国固定免税账单已保存并立即生效（写入 ${data.storage_label}，权限 0600，核验指纹 ${data.verification_id || "-"}）。`,
      );
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "保存固定账单失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    setDeleting(true);
    setError(null);
    setNotice(null);
    try {
      const data = await deleteFixedBillingSettings();
      syncSnapshot(data);
      setNotice("已清除已保存的固定免税账单文件。未配置有效账单前，真实下单与前置体检将被安全拦截。");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "清除固定账单失败");
    } finally {
      setDeleting(false);
    }
  }

  const sourceInfo = SOURCE_META[snapshot?.source ?? "none"] ?? SOURCE_META.none;
  const statusBadge = snapshot?.valid
    ? { color: "green", label: `已就绪 · 美国免税州 (${snapshot.address?.state || "US"})` }
    : snapshot?.configured
      ? { color: "red", label: "配置异常（已安全拦截）" }
      : { color: "orange", label: "未配置" };

  return (
    <Stack gap="lg">
      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="md">
          <Group justify="space-between" align="flex-start" wrap="wrap">
            <div>
              <Group gap="xs" align="center">
                <Title order={2}>美国固定免税账单</Title>
                <Badge color={statusBadge.color} variant="light">
                  {statusBadge.label}
                </Badge>
                <Badge color={sourceInfo.color} variant="outline" size="sm">
                  {sourceInfo.label}
                </Badge>
              </Group>
              <Text c="dimmed" size="sm" mt={6}>
                所有非 Team 订单（Plus / Go / Pro 家族）统一共用这一份美国免税州账单地址；代理出口与结算币种仍可按订单自由切换。保存后以 <Code>0600</Code> 权限原子落盘并即时生效，无需重启服务。
              </Text>
            </div>
            <Button
              size="xs"
              variant="subtle"
              leftSection={<IconRefresh size={14} />}
              loading={loading}
              onClick={() => void load()}
            >
              刷新
            </Button>
          </Group>

          {snapshot?.error && (
            <Alert color="red" icon={<IconAlertTriangle size={16} />} title="当前账单配置异常">
              {snapshot.error}
            </Alert>
          )}

          {snapshot && !snapshot.configured && !snapshot.error && (
            <Alert color="yellow" icon={<IconAlertTriangle size={16} />} title="尚未配置固定免税账单">
              真实模式下未配置固定账单时，系统会出于合规与风控安全直接拦截新结账（<Code>BILLING_PROFILE_UNAVAILABLE</Code>）。请填写下方美国免税州地址并保存。
            </Alert>
          )}

          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
            <TextInput
              label="付款方姓名（Name）"
              placeholder="例如：Daniel Mercer"
              required
              value={form.name}
              onChange={(e) => updateField("name", e.currentTarget.value)}
            />
            <TextInput
              label="账单邮箱（Email）"
              placeholder="例如：billing@example.com"
              required
              value={form.email}
              onChange={(e) => updateField("email", e.currentTarget.value)}
            />
            <TextInput
              label="街道地址（Line 1）"
              placeholder="例如：1425 SW Morrison Street"
              required
              value={form.line1}
              onChange={(e) => updateField("line1", e.currentTarget.value)}
            />
            <TextInput
              label="城市（City）"
              placeholder="例如：Portland"
              required
              value={form.city}
              onChange={(e) => updateField("city", e.currentTarget.value)}
            />
            <Select
              label="美国免税州（State）"
              description="仅允许美国 5 个无州级销售税的免税州"
              required
              allowDeselect={false}
              data={stateOptions}
              value={form.state}
              onChange={(val) => updateField("state", val || "OR")}
            />
            <TextInput
              label="邮政编码（Postal Code / ZIP）"
              placeholder="例如：97205"
              required
              value={form.postal_code}
              onChange={(e) => updateField("postal_code", e.currentTarget.value)}
            />
          </SimpleGrid>

          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
            <TextInput
              label="账单国家（Country）"
              value="US · 美国（固定不可更改）"
              readOnly
              disabled
            />
            <Stack gap={4} justify="flex-end">
              <Text size="sm" fw={500}>出口与账单解耦模式</Text>
              <Group gap="xs" wrap="wrap">
                <Badge
                  color={snapshot?.strict_exit_match ? "orange" : "teal"}
                  variant="light"
                >
                  {snapshot?.strict_exit_match
                    ? "严格模式：出口必须为 US"
                    : "自由出口：任意出口国 + 固定 US 免税账单"}
                </Badge>
                {onNavigate && (
                  <Button
                    size="compact-xs"
                    variant="subtle"
                    onClick={() => onNavigate("flags")}
                  >
                    在策略开关中调整
                  </Button>
                )}
              </Group>
            </Stack>
          </SimpleGrid>

          <Group justify="space-between" align="center" wrap="wrap" pt={4}>
            <Group gap="xs">
              <Button
                variant="light"
                size="sm"
                leftSection={<IconSparkles size={15} />}
                disabled={saving || deleting}
                onClick={handleFillSample}
              >
                填入俄勒冈示例地址
              </Button>
              {snapshot?.can_delete && (
                <Button
                  variant="subtle"
                  color="red"
                  size="sm"
                  leftSection={<IconTrash size={15} />}
                  loading={deleting}
                  disabled={saving || deleting}
                  onClick={() => void handleDelete()}
                >
                  清空已保存账单
                </Button>
              )}
            </Group>
            <Button
              leftSection={<IconDeviceFloppy size={15} />}
              loading={saving}
              disabled={saving || deleting}
              onClick={() => void handleSave()}
            >
              保存并生效
            </Button>
          </Group>

          {notice && <Alert color="green" icon={<IconCheck size={16} />}>{notice}</Alert>}
          {error && <Alert color="red" title="校验或保存失败">{error}</Alert>}
        </Stack>
      </Card>

      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="sm">
          <Group gap="xs">
            <IconMapPin size={18} />
            <Title order={4}>当前生效口径与审计摘要</Title>
          </Group>
          <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="md">
            <div>
              <Text size="xs" c="dimmed">存储位置</Text>
              <Code mt={4}>{snapshot?.storage_label || "config/fixed-billing.json"}</Code>
            </div>
            <div>
              <Text size="xs" c="dimmed">核验指纹（SHA-256 前 16 位）</Text>
              <Code mt={4}>{snapshot?.verification_id || "-"}</Code>
            </div>
            <div>
              <Text size="xs" c="dimmed">最后更新时间</Text>
              <Text size="sm" mt={4}>{formatTimestamp(snapshot?.updated_at)}</Text>
            </div>
          </SimpleGrid>
          <Divider my={4} />
          <Text size="xs" c="dimmed">
            协议口径：建单 <Code>POST /payments/checkout</Code> 按所选出口国与出口币种发起；随后在算税 <Code>POST /payments/checkout/taxes</Code> 与 Stripe 确认令牌中统一提交此份美国免税州账单，避免触发跨州消费税或订单漂移。
          </Text>
        </Stack>
      </Card>
    </Stack>
  );
}
