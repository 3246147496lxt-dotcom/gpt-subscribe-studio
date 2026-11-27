import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Code,
  Group,
  PasswordInput,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
  Tooltip,
} from "@mantine/core";
import { IconBell, IconBellRinging, IconCheck, IconHistory, IconRefresh, IconSend } from "@tabler/icons-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  getWebhookSettings,
  saveWebhookSettings,
  testWebhook,
  type WebhookDelivery,
  type WebhookSettings as WebhookSettingsData,
} from "../api/webhooks";
import { formatMonthDayTime } from "../utils/orderLedgerFormat";
import { AdminPageHeader } from "./AdminPageHeader";

const EVENT_LABELS: Record<string, string> = {
  succeeded: "充值成功（succeeded）",
  failed: "任务失败（failed）",
  unknown: "扣款未知并加锁（unknown）",
  requires_action: "需持卡人/本人验证（requires_action）",
  paid_pending_delivery: "已扣款待交付（paid_pending_delivery）",
  reconcile_resolved: "自动对账核销成功（reconcile_resolved）",
};

export function WebhookSettings() {
  const [settings, setSettings] = useState<WebhookSettingsData | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [urlInput, setUrlInput] = useState("");
  const [secretInput, setSecretInput] = useState("");
  const [clearSecret, setClearSecret] = useState(false);
  const [events, setEvents] = useState<string[]>(Object.keys(EVENT_LABELS));
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getWebhookSettings();
      setSettings(data);
      setEnabled(data.config.enabled);
      setEvents(data.config.events);
      setUrlInput("");
      setSecretInput("");
      setClearSecret(false);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "加载通知配置失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const isDirty = useMemo(() => {
    if (!settings) return false;
    if (enabled !== settings.config.enabled) return true;
    if (urlInput.trim() !== "") return true;
    if (secretInput.trim() !== "") return true;
    if (clearSecret) return true;
    const prevSorted = [...settings.config.events].sort().join(",");
    const currSorted = [...events].sort().join(",");
    return prevSorted !== currSorted;
  }, [settings, enabled, urlInput, secretInput, clearSecret, events]);

  function handleDiscard() {
    if (!settings) return;
    setEnabled(settings.config.enabled);
    setEvents(settings.config.events);
    setUrlInput("");
    setSecretInput("");
    setClearSecret(false);
    setError(null);
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const payload: {
        enabled: boolean;
        url?: string;
        secret?: string;
        clear_secret?: boolean;
        events: string[];
      } = {
        enabled,
        events,
        clear_secret: clearSecret,
      };
      if (urlInput.trim()) payload.url = urlInput.trim();
      if (secretInput.trim()) payload.secret = secretInput.trim();
      await saveWebhookSettings(payload);
      setNotice("Webhook 通知配置已保存（权限 0600，绝不回显密钥）。");
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleTest() {
    setTesting(true);
    setError(null);
    setNotice(null);
    try {
      const res = await testWebhook();
      if (res.ok) {
        setNotice(`测试告警发送成功（HTTP ${res.status_code} · ${res.elapsed_ms} ms）。`);
      } else {
        setError(`测试告警发送失败：${res.error || "未知错误"}`);
      }
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "发送测试失败");
    } finally {
      setTesting(false);
    }
  }

  const deliveries: WebhookDelivery[] = settings?.deliveries ?? [];
  const activeReady = Boolean(settings?.config.enabled && settings?.config.has_url);

  return (
    <Stack gap="md">
      <AdminPageHeader
        title="告警通知"
        badge={
          <span className={`ops-live-pill ${activeReady ? "ops-live-pill--ok" : "ops-live-pill--warn"}`}>
            <span className="ops-pulse-dot" aria-hidden="true" />
            {activeReady ? "已启用推送" : "未启用"}
          </span>
        }
        subtitle="任务终态、需要 3DS 验证（requires_action）、账号加锁（unknown）与对账核销时异步推送脱敏通知，兼容飞书、钉钉、Slack 与通用 JSON。"
        actions={
          <>
            <Button
              size="xs"
              variant="default"
              leftSection={<IconSend size={14} />}
              loading={testing}
              disabled={!settings?.config.has_url || testing || saving}
              onClick={() => void handleTest()}
            >
              发送测试告警
            </Button>
            <Button
              size="xs"
              variant="default"
              leftSection={<IconRefresh size={14} />}
              loading={loading}
              onClick={() => void load()}
            >
              刷新
            </Button>
          </>
        }
      />

      {notice && (
        <Alert color="teal" radius="md" icon={<IconCheck size={16} />} withCloseButton onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}
      {error && (
        <Alert color="red" radius="md" title="提示" withCloseButton onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* 配置卡片 */}
      <section className="ops-panel">
        <div className="ops-panel-head">
          <div className="ops-panel-title-wrap">
            <IconBell size={16} stroke={1.9} />
            <h3 className="ops-panel-title">Webhook 通道与订阅事件</h3>
            {isDirty && <Badge size="xs" color="orange" variant="light">有未保存更改</Badge>}
          </div>
        </div>

        <Stack gap="md">
          <Switch
            checked={enabled}
            onChange={(e) => setEnabled(e.currentTarget.checked)}
            label="启用 Webhook 异步告警"
            description="发送负载仅包含脱敏后的订单号、作业号、金额与状态摘要，绝不包含卡号、Session 或账单地址。"
          />

          <TextInput
            label="Webhook URL（仅支持 https://）"
            placeholder={
              settings?.config.has_url
                ? `已配置：${settings.config.url_masked}（留空保持不变）`
                : "https://hooks.example.com/services/..."
            }
            value={urlInput}
            onChange={(e) => setUrlInput(e.currentTarget.value)}
          />

          <Group grow align="end" wrap="wrap">
            <PasswordInput
              label="可选签名密钥（HMAC-SHA256）"
              placeholder={
                settings?.config.has_secret
                  ? "已设置密钥（留空保持不变）"
                  : "留空则不附带 X-GSS-Signature-256 签名头"
              }
              value={secretInput}
              onChange={(e) => {
                setSecretInput(e.currentTarget.value);
                setClearSecret(false);
              }}
            />
            {settings?.config.has_secret && (
              <Checkbox
                checked={clearSecret}
                onChange={(e) => setClearSecret(e.currentTarget.checked)}
                label="清除已保存的签名密钥"
                pb={8}
              />
            )}
          </Group>

          <Checkbox.Group label="订阅告警事件" value={events} onChange={setEvents}>
            <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="xs" mt="xs">
              {Object.entries(EVENT_LABELS).map(([key, label]) => (
                <Checkbox key={key} value={key} label={label} size="sm" />
              ))}
            </SimpleGrid>
          </Checkbox.Group>

          {!isDirty && (
            <Group justify="flex-end">
              <Button
                size="xs"
                variant="light"
                leftSection={<IconBellRinging size={14} />}
                loading={saving}
                disabled={saving}
                onClick={() => void handleSave()}
              >
                保存设置
              </Button>
            </Group>
          )}
        </Stack>
      </section>

      {/* 投递历史卡片 */}
      <section className="ops-panel">
        <div className="ops-panel-head">
          <div className="ops-panel-title-wrap">
            <IconHistory size={16} stroke={1.9} />
            <h3 className="ops-panel-title">近期投递审计记录</h3>
            <Badge size="xs" color="gray" variant="light">{deliveries.length} / 30 条</Badge>
          </div>
        </div>

        {deliveries.length === 0 ? (
          <Text size="sm" c="dimmed" py="md" ta="center">暂无 Webhook 投递记录。</Text>
        ) : (
          <Table.ScrollContainer minWidth={560}>
            <Table className="ledger-clean-table" highlightOnHover={false}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>结果</Table.Th>
                  <Table.Th>事件</Table.Th>
                  <Table.Th>HTTP / 错误</Table.Th>
                  <Table.Th>耗时 / 尝试</Table.Th>
                  <Table.Th>投递时间</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {deliveries.map((d) => {
                  const tone = d.ok ? "success" : "failed";
                  return (
                    <Table.Tr key={d.id}>
                      <Table.Td>
                        <span className={`ledger-pill ledger-pill--status-${tone}`}>
                          <span className={`ledger-dot ledger-dot--${tone}`} />
                          {d.ok ? "成功" : "失败"}
                        </span>
                      </Table.Td>
                      <Table.Td><Code>{d.event}</Code></Table.Td>
                      <Table.Td>{d.status_code ?? d.error ?? "-"}</Table.Td>
                      <Table.Td><span className="ledger-time">{d.elapsed_ms} ms · {d.attempts} 次</span></Table.Td>
                      <Table.Td>
                        <Tooltip label={new Date(d.created_at).toLocaleString()} openDelay={250} withArrow>
                          <span className="ledger-time">{formatMonthDayTime(d.created_at)}</span>
                        </Tooltip>
                      </Table.Td>
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
      </section>

      {/* 吸底未保存更改条 */}
      {isDirty && (
        <div className="ops-sticky-save" role="status">
          <div className="ops-sticky-save-left">
            <span className="ops-dirty-dot" aria-hidden="true" />
            <span>告警通知配置有未保存的更改</span>
          </div>
          <Group gap="xs">
            <Button size="xs" variant="default" disabled={saving} onClick={handleDiscard}>
              放弃更改
            </Button>
            <Button
              size="xs"
              leftSection={<IconBellRinging size={14} />}
              loading={saving}
              onClick={() => void handleSave()}
            >
              保存设置
            </Button>
          </Group>
        </div>
      )}
    </Stack>
  );
}
