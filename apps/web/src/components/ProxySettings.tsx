import {
  Alert,
  Badge,
  Button,
  Code,
  Group,
  Modal,
  Paper,
  PasswordInput,
  Stack,
  Switch,
  Text,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconCheck,
  IconNetwork,
  IconPlugConnected,
  IconRefresh,
  IconTrash,
} from "@tabler/icons-react";
import { useCallback, useEffect, useState } from "react";

import {
  clearProxy,
  getProxies,
  parseProxy,
  saveProxy,
  setProxyEnabled,
  testProxy,
  type ProxyInfo,
} from "../api/client";
import { AdminPageHeader } from "./AdminPageHeader";

function Facts({ info }: { info: ProxyInfo }) {
  return (
    <Group gap="xs" wrap="wrap">
      <Badge variant="light" color="gray" tt="none">
        {info.scheme}://{info.host}:{info.port}
      </Badge>
      <Badge variant="light" color={info.region ? "indigo" : "gray"} tt="none">
        地区 {info.region || "未检测到"}
      </Badge>
      <Badge variant="light" color={info.sid ? "indigo" : "gray"} tt="none">
        SID {info.sid || "未检测到"}
      </Badge>
      {info.has_username && (
        <Badge variant="light" color="teal" tt="none">
          已保存认证凭据
        </Badge>
      )}
    </Group>
  );
}

export function ProxySettings() {
  const [raw, setRaw] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [saved, setSaved] = useState<ProxyInfo | null>(null);
  const [preview, setPreview] = useState<ProxyInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [clearModalOpen, setClearModalOpen] = useState(false);
  const [message, setMessage] = useState<{ color: "red" | "green"; text: string } | null>(null);
  const [busy, setBusy] = useState<"parse" | "save" | "test" | "toggle" | "clear" | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const all = await getProxies();
      setSaved(all.checkout);
    } catch (err) {
      setSaved(null);
      setMessage({ color: "red", text: err instanceof Error ? err.message : "读取代理配置失败" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(
    kind: "parse" | "save" | "test" | "toggle" | "clear",
    action: () => Promise<void>,
  ) {
    setBusy(kind);
    setMessage(null);
    try {
      await action();
    } catch (err) {
      setMessage({ color: "red", text: err instanceof Error ? err.message : "操作失败" });
    } finally {
      setBusy(null);
    }
  }

  const configured = Boolean(saved?.host);

  return (
    <Stack gap="md">
      <AdminPageHeader
        title="代理网关"
        badge={
          <span
            className={`ops-live-pill ${
              configured ? (saved?.enabled ? "ops-live-pill--ok" : "ops-live-pill--warn") : "ops-live-pill--warn"
            }`}
          >
            <span className="ops-pulse-dot" aria-hidden="true" />
            {configured ? (saved?.enabled ? "已启用" : "已停用") : "未配置"}
          </span>
        }
        subtitle="自动解析代理账号中的地区与 SID 参数，每笔订单按目标地区分配独立随机 SID 并保持单笔出口一致。"
        actions={
          <>
            <Button
              size="xs"
              variant="default"
              leftSection={<IconPlugConnected size={14} />}
              disabled={!configured || !saved?.enabled || busy !== null}
              loading={busy === "test"}
              onClick={() =>
                void run("test", async () => {
                  const result = await testProxy();
                  setMessage({
                    color: "green",
                    text: `连接成功：出口 IP ${result.ip}，耗时 ${result.elapsed_ms} ms。请确认该 IP 属于所选地区。`,
                  });
                })
              }
            >
              测试连接
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

      {message && (
        <Alert
          color={message.color}
          radius="md"
          variant="light"
          withCloseButton
          onClose={() => setMessage(null)}
          icon={message.color === "green" ? <IconCheck size={16} /> : <IconAlertCircle size={16} />}
        >
          {message.text}
        </Alert>
      )}

      {/* 当前已保存网关卡 */}
      <section className="ops-panel">
        <div className="ops-panel-head">
          <div className="ops-panel-title-wrap">
            <IconNetwork size={16} stroke={1.9} />
            <h3 className="ops-panel-title">当前已保存网关</h3>
          </div>
          {configured && saved && (
            <Group gap="sm" align="center" wrap="nowrap">
              <Switch
                size="sm"
                label={saved.enabled ? "已启用" : "已停用"}
                checked={saved.enabled}
                disabled={busy !== null}
                onChange={(event) => {
                  const nextEnabled = event.currentTarget.checked;
                  void run("toggle", async () => {
                    const next = await setProxyEnabled(nextEnabled);
                    setSaved(next);
                    setMessage({
                      color: "green",
                      text: nextEnabled
                        ? "出口代理已启用，对后续新建订单生效。"
                        : "出口代理已停用（已保留网关地址与凭据，可随时重新开启）。",
                    });
                  });
                }}
              />
              <Button
                size="xs"
                variant="subtle"
                color="red"
                leftSection={<IconTrash size={13} />}
                disabled={busy !== null}
                onClick={() => setClearModalOpen(true)}
              >
                清空配置
              </Button>
            </Group>
          )}
        </div>

        {configured && saved ? (
          <Facts info={saved} />
        ) : (
          <Text size="sm" c="dimmed">
            尚未保存代理网关；可在下方粘贴代理串解析并保存。
          </Text>
        )}
      </section>

      {/* 添加 / 替换网关卡 */}
      <section className="ops-panel">
        <div className="ops-panel-head">
          <div className="ops-panel-title-wrap">
            <h3 className="ops-panel-title">{configured ? "替换代理网关" : "添加代理网关"}</h3>
          </div>
          <span className="ops-panel-sub">认证密码仅落盘于本地受保护配置，绝不明文回显</span>
        </div>

        <Stack gap="sm" maw={760}>
          <PasswordInput
            label="代理地址"
            description="一行即可。支持 http://账号:密码@主机:端口、账号:密码@主机:端口、主机:端口:账号:密码、socks5://…。账号需含地区和 SID 参数，例如 acct-region-US-sid-abc123。"
            placeholder="http://账号-region-US-sid-abc123:密码@gateway.example.com:3010"
            value={raw}
            onChange={(event) => {
              setRaw(event.currentTarget.value.trim());
              setPreview(null);
            }}
          />
          <Group>
            <Button
              size="xs"
              variant="light"
              loading={busy === "parse"}
              disabled={!raw || busy !== null}
              onClick={() =>
                void run("parse", async () => {
                  setPreview(await parseProxy(raw));
                })
              }
            >
              解析预览
            </Button>
          </Group>

          {preview && (
            <Paper withBorder p="md" radius="md">
              <Stack gap="sm">
                <Text fw={600} size="sm">解析结果</Text>
                <Facts info={preview} />
                {(!preview.has_region_param || !preview.has_sid_param) && (
                  <Alert color="yellow" variant="light" icon={<IconAlertCircle size={18} />}>
                    账号里没有检测到
                    {!preview.has_region_param && " 地区参数"}
                    {!preview.has_region_param && !preview.has_sid_param && "、"}
                    {!preview.has_sid_param && " SID 参数"}。保存后本站会在账号末尾追加 <Code>-region-XX</Code> / <Code>-sid-随机值</Code>，请确认你的代理供应商支持这个写法，否则请先在账号里加上对应参数。
                  </Alert>
                )}
                <Switch
                  label="保存后立即启用"
                  checked={enabled}
                  onChange={(event) => setEnabled(event.currentTarget.checked)}
                />
                <Group>
                  <Button
                    size="xs"
                    loading={busy === "save"}
                    disabled={busy !== null}
                    onClick={() =>
                      void run("save", async () => {
                        const next = await saveProxy(raw, enabled);
                        setSaved(next);
                        setPreview(null);
                        setRaw("");
                        setMessage({ color: "green", text: "代理配置已保存，对后续新建订单生效。" });
                      })
                    }
                  >
                    保存配置
                  </Button>
                </Group>
              </Stack>
            </Paper>
          )}
        </Stack>
      </section>

      <Modal
        opened={clearModalOpen}
        onClose={() => setClearModalOpen(false)}
        title="清空已保存的代理网关"
        size="sm"
        radius="lg"
        centered
      >
        <Stack gap="md">
          <Text size="sm" c="dimmed">
            确认清空提链与支付阶段的代理地址及认证凭据？清空后将不再通过代理网关转发请求，如需恢复需重新粘贴配置。
          </Text>
          <Group justify="flex-end" gap="xs">
            <Button size="xs" variant="default" onClick={() => setClearModalOpen(false)} disabled={busy === "clear"}>
              取消
            </Button>
            <Button
              size="xs"
              color="red"
              loading={busy === "clear"}
              onClick={() =>
                void run("clear", async () => {
                  const next = await clearProxy();
                  setSaved(next);
                  setClearModalOpen(false);
                  setMessage({ color: "green", text: "已清空代理网关配置并停用出口代理。" });
                })
              }
            >
              确认清空
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
