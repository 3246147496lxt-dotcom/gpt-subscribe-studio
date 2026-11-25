import {
  Alert, Badge, Button, Card, Checkbox, Code, Group, PasswordInput, Stack, Switch, Table, Text, TextInput, Title,
} from "@mantine/core";
import { IconBellRinging, IconCheck, IconRefresh, IconSend } from "@tabler/icons-react";
import { useCallback, useEffect, useState } from "react";

import {
  getWebhookSettings,
  saveWebhookSettings,
  testWebhook,
  type WebhookDelivery,
  type WebhookSettings,
} from "../api/webhooks";

const EVENT_LABELS: Record<string, string> = {
  succeeded: "充值成功（succeeded）",
  failed: "任务失败（failed）",
  unknown: "扣款未知并加锁（unknown）",
  requires_action: "需持卡人/本人验证（requires_action）",
  paid_pending_delivery: "已扣款待交付（paid_pending_delivery）",
  reconcile_resolved: "自动对账核销成功（reconcile_resolved）",
};

export function WebhookSettings() {
  const [settings, setSettings] = useState<WebhookSettings | null>(null);
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
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "加载通知配置失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

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
      setUrlInput("");
      setSecretInput("");
      setClearSecret(false);
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

  return (
    <Stack gap="lg">
      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="md">
          <Group justify="space-between" align="flex-start" wrap="wrap">
            <div>
              <Title order={2}>Webhook 事件告警设置</Title>
              <Text c="dimmed" size="sm" mt={4}>
                任务终态、需要 3DS/人工验证（requires_action）、账号加锁（unknown）与对账核销时异步推送脱敏通知（兼容通用 JSON、飞书、钉钉、Slack）。
              </Text>
            </div>
            <Button size="xs" variant="subtle" leftSection={<IconRefresh size={14} />} loading={loading} onClick={() => void load()}>
              刷新
            </Button>
          </Group>

          <Switch
            checked={enabled}
            onChange={(e) => setEnabled(e.currentTarget.checked)}
            label="启用 Webhook 异步告警"
            description="发送负载仅包含脱敏后的订单号、作业号、金额与状态摘要，绝不包含卡号、Session 或账单地址。"
          />

          <TextInput
            label="Webhook URL（仅支持 https://）"
            placeholder={settings?.config.has_url ? `已配置：${settings.config.url_masked}（留空保持不变）` : "https://hooks.example.com/services/..."}
            value={urlInput}
            onChange={(e) => setUrlInput(e.currentTarget.value)}
          />

          <Group grow align="end">
            <PasswordInput
              label="可选签名密钥（HMAC-SHA256）"
              placeholder={settings?.config.has_secret ? "已设置密钥（留空保持不变）" : "留空则不附带 X-GSS-Signature-256 签名头"}
              value={secretInput}
              onChange={(e) => { setSecretInput(e.currentTarget.value); setClearSecret(false); }}
            />
            {settings?.config.has_secret && (
              <Checkbox
                checked={clearSecret}
                onChange={(e) => setClearSecret(e.currentTarget.checked)}
                label="清除已保存的签名密钥"
              />
            )}
          </Group>

          <Checkbox.Group label="订阅告警事件" value={events} onChange={setEvents}>
            <Stack gap={6} mt="xs">
              {Object.entries(EVENT_LABELS).map(([key, label]) => (
                <Checkbox key={key} value={key} label={label} size="sm" />
              ))}
            </Stack>
          </Checkbox.Group>

          <Group justify="flex-end" gap="sm">
            <Button variant="default" leftSection={<IconSend size={15} />}
              loading={testing} disabled={!settings?.config.has_url || testing || saving}
              onClick={() => void handleTest()}>
              发送测试告警
            </Button>
            <Button leftSection={<IconBellRinging size={15} />} loading={saving} disabled={saving} onClick={() => void handleSave()}>
              保存设置
            </Button>
          </Group>

          {notice && <Alert color="green" icon={<IconCheck size={16} />}>{notice}</Alert>}
          {error && <Alert color="red" title="提示">{error}</Alert>}
        </Stack>
      </Card>

      <Card withBorder radius="lg" p="lg" className="workspace-panel">
        <Stack gap="sm">
          <Title order={4}>近期投递审计记录（最多保留 30 条）</Title>
          {deliveries.length === 0 ? (
            <Text size="sm" c="dimmed">暂无 Webhook 投递记录。</Text>
          ) : (
            <Table verticalSpacing="xs" highlightOnHover withTableBorder fz="xs">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>结果</Table.Th>
                  <Table.Th>事件</Table.Th>
                  <Table.Th>HTTP / 错误</Table.Th>
                  <Table.Th>耗时 / 尝试</Table.Th>
                  <Table.Th>时间</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {deliveries.map((d) => (
                  <Table.Tr key={d.id}>
                    <Table.Td>
                      <Badge size="xs" color={d.ok ? "green" : "red"} variant="light">
                        {d.ok ? "成功" : "失败"}
                      </Badge>
                    </Table.Td>
                    <Table.Td><Code>{d.event}</Code></Table.Td>
                    <Table.Td>{d.status_code ?? d.error ?? "-"}</Table.Td>
                    <Table.Td>{d.elapsed_ms} ms · {d.attempts} 次</Table.Td>
                    <Table.Td>{new Date(d.created_at).toLocaleString()}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
        </Stack>
      </Card>
    </Stack>
  );
}
