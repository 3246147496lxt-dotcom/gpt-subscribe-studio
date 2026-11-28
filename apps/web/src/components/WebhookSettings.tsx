import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Code,
  Divider,
  Group,
  NumberInput,
  PasswordInput,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
  Tooltip,
} from "@mantine/core";
import {
  IconBell,
  IconBellRinging,
  IconBrandTelegram,
  IconCheck,
  IconHistory,
  IconRefresh,
  IconSend,
  IconWebhook,
} from "@tabler/icons-react";
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
  failed: "任务 / 建单失败（failed）",
  unknown: "扣款未知并加锁（unknown）",
  requires_action: "需持卡人 / 3DS 验证（requires_action）",
  paid_pending_delivery: "已扣款待交付（paid_pending_delivery）",
  reconcile_resolved: "自动对账核销成功（reconcile_resolved）",
  manual_verdict: "人工判定已执行（manual_verdict）",
};

const CRITICAL_EVENTS = ["failed", "unknown", "requires_action", "paid_pending_delivery", "manual_verdict"];

export function WebhookSettings() {
  const [settings, setSettings] = useState<WebhookSettingsData | null>(null);

  // Telegram Bot channel state
  const [tgEnabled, setTgEnabled] = useState(false);
  const [tgTokenInput, setTgTokenInput] = useState("");
  const [tgClearToken, setTgClearToken] = useState(false);
  const [tgChatId, setTgChatId] = useState("");
  const [tgThreadId, setTgThreadId] = useState("");
  const [tgApiBase, setTgApiBase] = useState("https://api.telegram.org");
  const [tgSilent, setTgSilent] = useState(false);
  const [tgBotCommands, setTgBotCommands] = useState(true);
  const [tgDigestEnabled, setTgDigestEnabled] = useState(true);
  const [tgDigestHour, setTgDigestHour] = useState<number>(22);
  const [tgStreakThreshold, setTgStreakThreshold] = useState<number>(3);
  const [tgDiskWarnMb, setTgDiskWarnMb] = useState<number>(1024);

  // Generic Webhook channel state
  const [enabled, setEnabled] = useState(false);
  const [urlInput, setUrlInput] = useState("");
  const [secretInput, setSecretInput] = useState("");
  const [clearSecret, setClearSecret] = useState(false);

  // Shared subscribed events
  const [events, setEvents] = useState<string[]>(Object.keys(EVENT_LABELS));

  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testingChannel, setTestingChannel] = useState<null | "telegram" | "webhook">(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getWebhookSettings();
      setSettings(data);
      const cfg = data.config;
      setTgEnabled(Boolean(cfg.telegram_enabled));
      setTgTokenInput("");
      setTgClearToken(false);
      setTgChatId(cfg.telegram_chat_id || "");
      setTgThreadId(cfg.telegram_thread_id != null ? String(cfg.telegram_thread_id) : "");
      setTgApiBase(cfg.telegram_api_base || "https://api.telegram.org");
      setTgSilent(Boolean(cfg.telegram_silent));
      setTgBotCommands(cfg.telegram_bot_commands_enabled ?? true);
      setTgDigestEnabled(cfg.telegram_digest_enabled ?? true);
      setTgDigestHour(cfg.telegram_digest_hour ?? 22);
      setTgStreakThreshold(cfg.telegram_streak_threshold ?? 3);
      setTgDiskWarnMb(cfg.telegram_disk_warn_mb ?? 1024);

      setEnabled(Boolean(cfg.enabled));
      setUrlInput("");
      setSecretInput("");
      setClearSecret(false);

      setEvents(cfg.events);
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
    const cfg = settings.config;
    if (tgEnabled !== Boolean(cfg.telegram_enabled)) return true;
    if (tgTokenInput.trim() !== "") return true;
    if (tgClearToken) return true;
    if (tgChatId.trim() !== (cfg.telegram_chat_id || "")) return true;
    const prevThread = cfg.telegram_thread_id != null ? String(cfg.telegram_thread_id) : "";
    if (tgThreadId.trim() !== prevThread) return true;
    const prevBase = (cfg.telegram_api_base || "https://api.telegram.org").replace(/\/+$/, "");
    const currBase = (tgApiBase.trim() || "https://api.telegram.org").replace(/\/+$/, "");
    if (currBase !== prevBase) return true;
    if (tgSilent !== Boolean(cfg.telegram_silent)) return true;
    if (tgBotCommands !== (cfg.telegram_bot_commands_enabled ?? true)) return true;
    if (tgDigestEnabled !== (cfg.telegram_digest_enabled ?? true)) return true;
    if (tgDigestHour !== (cfg.telegram_digest_hour ?? 22)) return true;
    if (tgStreakThreshold !== (cfg.telegram_streak_threshold ?? 3)) return true;
    if (tgDiskWarnMb !== (cfg.telegram_disk_warn_mb ?? 1024)) return true;

    if (enabled !== Boolean(cfg.enabled)) return true;
    if (urlInput.trim() !== "") return true;
    if (secretInput.trim() !== "") return true;
    if (clearSecret) return true;

    const prevSorted = [...cfg.events].sort().join(",");
    const currSorted = [...events].sort().join(",");
    return prevSorted !== currSorted;
  }, [
    settings,
    tgEnabled,
    tgTokenInput,
    tgClearToken,
    tgChatId,
    tgThreadId,
    tgApiBase,
    tgSilent,
    tgBotCommands,
    tgDigestEnabled,
    tgDigestHour,
    tgStreakThreshold,
    tgDiskWarnMb,
    enabled,
    urlInput,
    secretInput,
    clearSecret,
    events,
  ]);

  function handleDiscard() {
    if (!settings) return;
    const cfg = settings.config;
    setTgEnabled(Boolean(cfg.telegram_enabled));
    setTgTokenInput("");
    setTgClearToken(false);
    setTgChatId(cfg.telegram_chat_id || "");
    setTgThreadId(cfg.telegram_thread_id != null ? String(cfg.telegram_thread_id) : "");
    setTgApiBase(cfg.telegram_api_base || "https://api.telegram.org");
    setTgSilent(Boolean(cfg.telegram_silent));
    setTgBotCommands(cfg.telegram_bot_commands_enabled ?? true);
    setTgDigestEnabled(cfg.telegram_digest_enabled ?? true);
    setTgDigestHour(cfg.telegram_digest_hour ?? 22);
    setTgStreakThreshold(cfg.telegram_streak_threshold ?? 3);
    setTgDiskWarnMb(cfg.telegram_disk_warn_mb ?? 1024);

    setEnabled(Boolean(cfg.enabled));
    setUrlInput("");
    setSecretInput("");
    setClearSecret(false);

    setEvents(cfg.events);
    setError(null);
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const payload: Parameters<typeof saveWebhookSettings>[0] = {
        enabled,
        events,
        clear_secret: clearSecret,
        telegram_enabled: tgEnabled,
        clear_telegram_token: tgClearToken,
        telegram_chat_id: tgChatId.trim(),
        telegram_api_base: tgApiBase.trim() || "https://api.telegram.org",
        telegram_silent: tgSilent,
        telegram_bot_commands_enabled: tgBotCommands,
        telegram_digest_enabled: tgDigestEnabled,
        telegram_digest_hour: tgDigestHour,
        telegram_streak_threshold: tgStreakThreshold,
        telegram_disk_warn_mb: tgDiskWarnMb,
      };
      if (urlInput.trim()) payload.url = urlInput.trim();
      if (secretInput.trim()) payload.secret = secretInput.trim();
      if (tgTokenInput.trim()) payload.telegram_bot_token = tgTokenInput.trim();
      if (tgThreadId.trim() === "") {
        payload.clear_telegram_thread_id = true;
      } else {
        payload.telegram_thread_id = tgThreadId.trim();
      }
      await saveWebhookSettings(payload);
      setNotice("告警通知配置已保存（权限 0600，绝不回显 Bot Token 与签名密钥）。");
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleTest(channel: "telegram" | "webhook") {
    setTestingChannel(channel);
    setError(null);
    setNotice(null);
    try {
      const res = await testWebhook(channel);
      const label = channel === "telegram" ? "Telegram" : "Webhook";
      if (res.ok) {
        setNotice(`${label} 测试消息发送成功（HTTP ${res.status_code} · ${res.elapsed_ms} ms）。`);
      } else {
        setError(`${label} 测试消息发送失败：${res.error || "未知错误"}`);
      }
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "发送测试失败");
    } finally {
      setTestingChannel(null);
    }
  }

  const deliveries: WebhookDelivery[] = settings?.deliveries ?? [];
  const tgActive = Boolean(settings?.config.telegram_enabled && settings?.config.telegram_configured);
  const whActive = Boolean(settings?.config.enabled && settings?.config.has_url);
  const activeLabels = [tgActive ? "Telegram" : null, whActive ? "Webhook" : null].filter(Boolean);
  const activeReady = activeLabels.length > 0;

  return (
    <Stack gap="md">
      <AdminPageHeader
        title="告警通知"
        badge={
          <span className={`ops-live-pill ${activeReady ? "ops-live-pill--ok" : "ops-live-pill--warn"}`}>
            <span className="ops-pulse-dot" aria-hidden="true" />
            {activeReady ? `${activeLabels.join(" + ")} 已启用` : "未启用"}
          </span>
        }
        subtitle="覆盖一键充值、自助门户、分步协议、自动对账与人工判定全入口，实时推送脱敏状态摘要（绝不含卡号、Session 或账单原文）。"
        actions={
          <>
            <Button
              size="xs"
              variant="default"
              leftSection={<IconBrandTelegram size={14} />}
              loading={testingChannel === "telegram"}
              disabled={!settings?.config.telegram_configured || testingChannel !== null || saving || isDirty}
              onClick={() => void handleTest("telegram")}
            >
              测试 Telegram
            </Button>
            <Button
              size="xs"
              variant="default"
              leftSection={<IconSend size={14} />}
              loading={testingChannel === "webhook"}
              disabled={!settings?.config.has_url || testingChannel !== null || saving || isDirty}
              onClick={() => void handleTest("webhook")}
            >
              测试 Webhook
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

      {/* Telegram Bot 推送卡片 */}
      <section className="ops-panel">
        <div className="ops-panel-head">
          <div className="ops-panel-title-wrap">
            <IconBrandTelegram size={16} stroke={1.9} />
            <h3 className="ops-panel-title">Telegram Bot 实时推送</h3>
            <Badge size="xs" color={tgActive ? "teal" : "gray"} variant="light">
              {tgActive ? "运行中" : settings?.config.telegram_configured ? "已配置未启用" : "未配置"}
            </Badge>
            {isDirty && <Badge size="xs" color="orange" variant="light">有未保存更改</Badge>}
          </div>
          <Button
            size="xs"
            variant="light"
            leftSection={<IconSend size={13} />}
            loading={testingChannel === "telegram"}
            disabled={!settings?.config.telegram_configured || testingChannel !== null || saving || isDirty}
            onClick={() => void handleTest("telegram")}
          >
            发送测试消息
          </Button>
        </div>

        <Stack gap="md">
          <Switch
            checked={tgEnabled}
            onChange={(e) => setTgEnabled(e.currentTarget.checked)}
            label="启用 Telegram Bot 异步告警"
            description="在 Telegram 中找 @BotFather 创建 Bot 获取 Token，私聊给 Bot 发一条消息后通过 @userinfobot 获取你的数字 Chat ID（群组填 -100 开头的 ID）。"
          />

          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
            <PasswordInput
              label="Bot Token"
              placeholder={
                settings?.config.telegram_has_token
                  ? `已配置：${settings.config.telegram_token_masked}（留空保持不变）`
                  : "123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
              }
              value={tgTokenInput}
              onChange={(e) => {
                setTgTokenInput(e.currentTarget.value);
                setTgClearToken(false);
              }}
            />
            <TextInput
              label="Chat ID（个人数字 ID / 群组 -100... / @频道）"
              placeholder="例如 123456789 或 -1001234567890"
              value={tgChatId}
              onChange={(e) => setTgChatId(e.currentTarget.value)}
            />
          </SimpleGrid>

          {settings?.config.telegram_has_token && (
            <Checkbox
              checked={tgClearToken}
              onChange={(e) => setTgClearToken(e.currentTarget.checked)}
              label="清除已保存的 Bot Token"
              size="xs"
            />
          )}

          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
            <TextInput
              label="群组话题 ID（可选，message_thread_id）"
              placeholder="普通私聊/群组留空；开启 Topics 的超级群填正整数"
              value={tgThreadId}
              onChange={(e) => setTgThreadId(e.currentTarget.value)}
            />
            <TextInput
              label="Bot API 地址（默认官方直连）"
              placeholder="https://api.telegram.org"
              value={tgApiBase}
              onChange={(e) => setTgApiBase(e.currentTarget.value)}
            />
          </SimpleGrid>

          <Checkbox
            checked={tgSilent}
            onChange={(e) => setTgSilent(e.currentTarget.checked)}
            label="静默推送（disable_notification，手机不响铃震动）"
            size="sm"
          />

          <Divider label="机器人交互指令、按钮操作与自动巡检预警" labelPosition="left" />

          <Stack gap="xs">
            <Switch
              checked={tgBotCommands}
              onChange={(e) => setTgBotCommands(e.currentTarget.checked)}
              label="启用斜杠指令与卡片操作按钮（/status /orders /order /locks /disk /digest）"
              description="严格仅响应上方 Chat ID 本人消息，支持一键「只读对账」与「重试关闭续费」，绝不触发扣款。"
            />
            <Switch
              checked={tgDigestEnabled}
              onChange={(e) => setTgDigestEnabled(e.currentTarget.checked)}
              label="启用每日运行简报（按北京时间定时汇总当日成单、成功率、失败原因与磁盘余量）"
            />
          </Stack>

          <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="md">
            <NumberInput
              label="每日简报时间（北京时间 0–23 点）"
              min={0}
              max={23}
              disabled={!tgDigestEnabled}
              value={tgDigestHour}
              onChange={(val) => setTgDigestHour(typeof val === "number" ? val : 22)}
            />
            <NumberInput
              label="连续失败预警阈值（单，0=关闭）"
              min={0}
              max={20}
              value={tgStreakThreshold}
              onChange={(val) => setTgStreakThreshold(typeof val === "number" ? val : 3)}
            />
            <NumberInput
              label="磁盘余量预警阈值（MB，0=关闭）"
              min={0}
              max={51200}
              step={256}
              value={tgDiskWarnMb}
              onChange={(val) => setTgDiskWarnMb(typeof val === "number" ? val : 1024)}
            />
          </SimpleGrid>
        </Stack>
      </section>

      {/* 共用订阅事件卡片 */}
      <section className="ops-panel">
        <div className="ops-panel-head">
          <div className="ops-panel-title-wrap">
            <IconBell size={16} stroke={1.9} />
            <h3 className="ops-panel-title">订阅告警事件（Telegram 与 Webhook 共用）</h3>
            <Badge size="xs" color="gray" variant="light">
              已选 {events.length}/{Object.keys(EVENT_LABELS).length}
            </Badge>
          </div>
          <Group gap="xs">
            <Button
              size="xs"
              variant="subtle"
              onClick={() => setEvents(Object.keys(EVENT_LABELS))}
            >
              全选
            </Button>
            <Button
              size="xs"
              variant="subtle"
              onClick={() => setEvents(CRITICAL_EVENTS)}
            >
              仅异常与人工
            </Button>
          </Group>
        </div>

        <Checkbox.Group value={events} onChange={setEvents}>
          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="xs">
            {Object.entries(EVENT_LABELS).map(([key, label]) => (
              <Checkbox key={key} value={key} label={label} size="sm" />
            ))}
          </SimpleGrid>
        </Checkbox.Group>
      </section>

      {/* 通用 Webhook 通道卡片 */}
      <section className="ops-panel">
        <div className="ops-panel-head">
          <div className="ops-panel-title-wrap">
            <IconWebhook size={16} stroke={1.9} />
            <h3 className="ops-panel-title">通用 Webhook 通道（可选，兼容飞书 / 钉钉 / Slack）</h3>
            <Badge size="xs" color={whActive ? "teal" : "gray"} variant="light">
              {whActive ? "运行中" : settings?.config.has_url ? "已配置未启用" : "未配置"}
            </Badge>
          </div>
        </div>

        <Stack gap="md">
          <Switch
            checked={enabled}
            onChange={(e) => setEnabled(e.currentTarget.checked)}
            label="启用通用 HTTPS Webhook 推送"
            description="可与 Telegram 同时启用；支持可选 HMAC-SHA256 签名头（X-GSS-Signature-256）。"
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
            <Badge size="xs" color="gray" variant="light">{deliveries.length} / 50 条</Badge>
          </div>
        </div>

        {deliveries.length === 0 ? (
          <Text size="sm" c="dimmed" py="md" ta="center">暂无告警投递记录。</Text>
        ) : (
          <Table.ScrollContainer minWidth={620}>
            <Table className="ledger-clean-table" highlightOnHover={false}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>结果</Table.Th>
                  <Table.Th>通道</Table.Th>
                  <Table.Th>事件</Table.Th>
                  <Table.Th>HTTP / 错误</Table.Th>
                  <Table.Th>耗时 / 尝试</Table.Th>
                  <Table.Th>投递时间</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {deliveries.map((d) => {
                  const tone = d.ok ? "success" : "failed";
                  const isTg = d.channel === "telegram";
                  return (
                    <Table.Tr key={d.id}>
                      <Table.Td>
                        <span className={`ledger-pill ledger-pill--status-${tone}`}>
                          <span className={`ledger-dot ledger-dot--${tone}`} />
                          {d.ok ? "成功" : "失败"}
                        </span>
                      </Table.Td>
                      <Table.Td>
                        <Badge size="xs" color={isTg ? "cyan" : "indigo"} variant="light">
                          {isTg ? "Telegram" : "Webhook"}
                        </Badge>
                      </Table.Td>
                      <Table.Td><Code>{d.event}</Code></Table.Td>
                      <Table.Td>{d.error ? `${d.error}` : (d.status_code ?? "-")}</Table.Td>
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
