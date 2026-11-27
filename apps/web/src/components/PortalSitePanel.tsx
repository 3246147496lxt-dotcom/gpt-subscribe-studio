import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Group,
  Paper,
  SimpleGrid,
  Skeleton,
  Stack,
  Text,
  Textarea,
  TextInput,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconArrowDown,
  IconArrowUp,
  IconCheck,
  IconExternalLink,
  IconLink,
  IconPlus,
  IconRefresh,
  IconSpeakerphone,
  IconTrash,
  IconWorld,
} from "@tabler/icons-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  getAdminPortalSite,
  updateAdminPortalSite,
  type PortalAnnouncement,
  type PortalSiteConfig,
} from "../api/adminOps";
import { isAbortError } from "../api/http";
import { AdminPageHeader } from "./AdminPageHeader";

function editableSnapshot(cfg: PortalSiteConfig | null): string {
  if (!cfg) return "";
  return JSON.stringify({
    brand_name: cfg.brand_name,
    brand_name_en: cfg.brand_name_en,
    footer_slogan: cfg.footer_slogan,
    footer_slogan_en: cfg.footer_slogan_en,
    hero_title: cfg.hero_title,
    hero_title_en: cfg.hero_title_en,
    hero_subtitle_1: cfg.hero_subtitle_1,
    hero_subtitle_1_en: cfg.hero_subtitle_1_en,
    hero_subtitle_2: cfg.hero_subtitle_2,
    hero_subtitle_2_en: cfg.hero_subtitle_2_en,
    card_shop_url: cfg.card_shop_url,
    card_shop_label: cfg.card_shop_label,
    card_shop_label_en: cfg.card_shop_label_en,
    support_url: cfg.support_url,
    support_label: cfg.support_label,
    support_label_en: cfg.support_label_en,
    support_note: cfg.support_note,
    support_note_en: cfg.support_note_en,
    price_note: cfg.price_note,
    price_note_en: cfg.price_note_en,
    announcements: cfg.announcements,
  });
}

export function PortalSitePanel() {
  const [form, setForm] = useState<PortalSiteConfig | null>(null);
  const [baseline, setBaseline] = useState<PortalSiteConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const cfg = await getAdminPortalSite(signal);
      if (!signal?.aborted) {
        setForm(cfg);
        setBaseline(cfg);
      }
    } catch (exc) {
      if (isAbortError(exc)) return;
      setError(exc instanceof Error ? exc.message : "读取门户站点配置失败");
    } finally {
      if (!signal?.aborted) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const isDirty = useMemo(
    () => Boolean(form && baseline && editableSnapshot(form) !== editableSnapshot(baseline)),
    [form, baseline],
  );

  function setField<K extends keyof PortalSiteConfig>(key: K, value: PortalSiteConfig[K]) {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
    setSavedMsg(null);
  }

  function updateAnnouncement(idx: number, patch: Partial<PortalAnnouncement>) {
    setForm((prev) => {
      if (!prev) return prev;
      const next = prev.announcements.map((item, i) => (i === idx ? { ...item, ...patch } : item));
      return { ...prev, announcements: next };
    });
    setSavedMsg(null);
  }

  function addAnnouncement() {
    setForm((prev) => {
      if (!prev) return prev;
      const nextId = String(Date.now());
      return {
        ...prev,
        announcements: [...prev.announcements, { id: nextId, text: "", text_en: "" }],
      };
    });
    setSavedMsg(null);
  }

  function removeAnnouncement(idx: number) {
    setForm((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        announcements: prev.announcements.filter((_, i) => i !== idx),
      };
    });
    setSavedMsg(null);
  }

  function moveAnnouncement(idx: number, delta: -1 | 1) {
    setForm((prev) => {
      if (!prev) return prev;
      const target = idx + delta;
      if (target < 0 || target >= prev.announcements.length) return prev;
      const next = [...prev.announcements];
      [next[idx], next[target]] = [next[target], next[idx]];
      return { ...prev, announcements: next };
    });
    setSavedMsg(null);
  }

  function handleDiscard() {
    if (!baseline) return;
    setForm(baseline);
    setError(null);
    setSavedMsg(null);
  }

  async function handleSave() {
    if (!form) return;
    setSaving(true);
    setError(null);
    setSavedMsg(null);
    try {
      const cleanedAnnouncements = form.announcements
        .filter((item) => item.text.trim())
        .map((item, i) => ({
          id: item.id || String(i + 1),
          text: item.text.trim(),
          text_en: (item.text_en || item.text).trim(),
        }));
      const updated = await updateAdminPortalSite({
        brand_name: form.brand_name,
        brand_name_en: form.brand_name_en,
        footer_slogan: form.footer_slogan,
        footer_slogan_en: form.footer_slogan_en,
        hero_title: form.hero_title,
        hero_title_en: form.hero_title_en,
        hero_subtitle_1: form.hero_subtitle_1,
        hero_subtitle_1_en: form.hero_subtitle_1_en,
        hero_subtitle_2: form.hero_subtitle_2,
        hero_subtitle_2_en: form.hero_subtitle_2_en,
        card_shop_url: form.card_shop_url,
        card_shop_label: form.card_shop_label,
        card_shop_label_en: form.card_shop_label_en,
        support_url: form.support_url,
        support_label: form.support_label,
        support_label_en: form.support_label_en,
        support_note: form.support_note,
        support_note_en: form.support_note_en,
        price_note: form.price_note,
        price_note_en: form.price_note_en,
        announcements: cleanedAnnouncements,
      });
      setForm(updated);
      setBaseline(updated);
      setSavedMsg("门户站点配置已保存，刷新用户端 `/` 页面立即生效。");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "保存门户配置失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Stack gap="md">
      <AdminPageHeader
        title="门户配置"
        badge={
          isDirty ? (
            <Badge size="sm" color="orange" variant="light">有未保存更改</Badge>
          ) : form ? (
            <Badge size="sm" color="teal" variant="light">已与线上同步</Badge>
          ) : null
        }
        subtitle="管理客户自助充值页（/）的品牌名称、主视觉文案、购卡/客服外链与顶部公告栏；保存于 $GSS_CONFIG_DIR/portal-site.json。"
        actions={
          <>
            <Button
              component="a"
              href="/"
              target="_blank"
              rel="noopener noreferrer"
              size="xs"
              variant="light"
              rightSection={<IconExternalLink size={14} />}
            >
              预览用户门户
            </Button>
            <Button
              size="xs"
              variant="default"
              leftSection={<IconRefresh size={14} />}
              loading={loading}
              onClick={() => void load()}
            >
              重新加载
            </Button>
            <Button
              size="xs"
              loading={saving}
              disabled={!form || !isDirty}
              onClick={() => void handleSave()}
            >
              保存配置
            </Button>
          </>
        }
      />

      {error && (
        <Alert color="red" radius="md" icon={<IconAlertCircle size={16} />} title="操作失败" withCloseButton onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      {savedMsg && (
        <Alert color="teal" radius="md" icon={<IconCheck size={16} />} withCloseButton onClose={() => setSavedMsg(null)}>
          {savedMsg}
        </Alert>
      )}

      {!form ? (
        <section className="ops-panel">
          <Stack gap="sm">
            <Skeleton height={28} width="35%" />
            <Skeleton height={90} />
            <Skeleton height={90} />
          </Stack>
        </section>
      ) : (
        <>
          {/* 卡片 1：品牌与主视觉文案 */}
          <section className="ops-panel">
            <div className="ops-panel-head">
              <div className="ops-panel-title-wrap">
                <IconWorld size={16} stroke={1.9} />
                <h3 className="ops-panel-title">品牌与主视觉文案</h3>
              </div>
              <span className="ops-panel-sub">支持中英文双语独立维护</span>
            </div>
            <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
              <TextInput
                label="品牌名（中文）"
                value={form.brand_name}
                onChange={(e) => setField("brand_name", e.currentTarget.value)}
              />
              <TextInput
                label="品牌名（English）"
                value={form.brand_name_en}
                onChange={(e) => setField("brand_name_en", e.currentTarget.value)}
              />
              <TextInput
                label="主标题（中文）"
                value={form.hero_title}
                onChange={(e) => setField("hero_title", e.currentTarget.value)}
              />
              <TextInput
                label="主标题（English）"
                value={form.hero_title_en}
                onChange={(e) => setField("hero_title_en", e.currentTarget.value)}
              />
              <TextInput
                label="副标题 1（中文）"
                value={form.hero_subtitle_1}
                onChange={(e) => setField("hero_subtitle_1", e.currentTarget.value)}
              />
              <TextInput
                label="副标题 1（English）"
                value={form.hero_subtitle_1_en}
                onChange={(e) => setField("hero_subtitle_1_en", e.currentTarget.value)}
              />
              <TextInput
                label="副标题 2（中文）"
                value={form.hero_subtitle_2}
                onChange={(e) => setField("hero_subtitle_2", e.currentTarget.value)}
              />
              <TextInput
                label="副标题 2（English）"
                value={form.hero_subtitle_2_en}
                onChange={(e) => setField("hero_subtitle_2_en", e.currentTarget.value)}
              />
              <TextInput
                label="页脚标语（中文）"
                value={form.footer_slogan}
                onChange={(e) => setField("footer_slogan", e.currentTarget.value)}
              />
              <TextInput
                label="页脚标语（English）"
                value={form.footer_slogan_en}
                onChange={(e) => setField("footer_slogan_en", e.currentTarget.value)}
              />
            </SimpleGrid>
          </section>

          {/* 卡片 2：购卡外链、客服与价格说明 */}
          <section className="ops-panel">
            <div className="ops-panel-head">
              <div className="ops-panel-title-wrap">
                <IconLink size={16} stroke={1.9} />
                <h3 className="ops-panel-title">购卡外链、客服与价格说明</h3>
              </div>
              <span className="ops-panel-sub">留空链接将自动隐藏对应入口</span>
            </div>
            <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
              <TextInput
                label="购卡 / 卡网链接（留空隐藏）"
                placeholder="https://example.com/cards"
                value={form.card_shop_url}
                onChange={(e) => setField("card_shop_url", e.currentTarget.value)}
              />
              <TextInput
                label="客服链接（留空仅显示说明）"
                placeholder="https://t.me/your_support"
                value={form.support_url}
                onChange={(e) => setField("support_url", e.currentTarget.value)}
              />
              <TextInput
                label="购卡按钮文案（中文）"
                value={form.card_shop_label}
                onChange={(e) => setField("card_shop_label", e.currentTarget.value)}
              />
              <TextInput
                label="购卡按钮文案（English）"
                value={form.card_shop_label_en}
                onChange={(e) => setField("card_shop_label_en", e.currentTarget.value)}
              />
              <TextInput
                label="客服按钮文案（中文）"
                value={form.support_label}
                onChange={(e) => setField("support_label", e.currentTarget.value)}
              />
              <TextInput
                label="客服按钮文案（English）"
                value={form.support_label_en}
                onChange={(e) => setField("support_label_en", e.currentTarget.value)}
              />
              <Textarea
                label="客服引导说明（中文）"
                autosize
                minRows={2}
                value={form.support_note}
                onChange={(e) => setField("support_note", e.currentTarget.value)}
              />
              <Textarea
                label="客服引导说明（English）"
                autosize
                minRows={2}
                value={form.support_note_en}
                onChange={(e) => setField("support_note_en", e.currentTarget.value)}
              />
              <Textarea
                label="参考价提示（中文）"
                autosize
                minRows={2}
                value={form.price_note}
                onChange={(e) => setField("price_note", e.currentTarget.value)}
              />
              <Textarea
                label="参考价提示（English）"
                autosize
                minRows={2}
                value={form.price_note_en}
                onChange={(e) => setField("price_note_en", e.currentTarget.value)}
              />
            </SimpleGrid>
          </section>

          {/* 卡片 3：顶部公告栏 */}
          <section className="ops-panel">
            <div className="ops-panel-head">
              <div className="ops-panel-title-wrap">
                <IconSpeakerphone size={16} stroke={1.9} />
                <h3 className="ops-panel-title">顶部公告栏</h3>
                <Badge size="xs" color="gray" variant="light">{form.announcements.length} 条</Badge>
              </div>
              <Button size="xs" variant="light" leftSection={<IconPlus size={14} />} onClick={addAnnouncement}>
                新增公告
              </Button>
            </div>

            {form.announcements.length === 0 ? (
              <Text size="sm" c="dimmed" py="md" ta="center">
                暂无公告，点击右上角「新增公告」添加。
              </Text>
            ) : (
              <Stack gap="sm">
                {form.announcements.map((item, idx) => (
                  <Paper key={`${item.id}-${idx}`} withBorder radius="md" p="sm">
                    <Stack gap="xs">
                      <Group justify="space-between" align="center">
                        <Text size="xs" fw={600} c="dimmed">公告 #{idx + 1}</Text>
                        <Group gap={4}>
                          <ActionIcon
                            size="sm"
                            variant="subtle"
                            disabled={idx === 0}
                            aria-label="上移公告"
                            onClick={() => moveAnnouncement(idx, -1)}
                          >
                            <IconArrowUp size={14} />
                          </ActionIcon>
                          <ActionIcon
                            size="sm"
                            variant="subtle"
                            disabled={idx === form.announcements.length - 1}
                            aria-label="下移公告"
                            onClick={() => moveAnnouncement(idx, 1)}
                          >
                            <IconArrowDown size={14} />
                          </ActionIcon>
                          <ActionIcon
                            size="sm"
                            variant="subtle"
                            color="red"
                            aria-label="删除公告"
                            onClick={() => removeAnnouncement(idx)}
                          >
                            <IconTrash size={14} />
                          </ActionIcon>
                        </Group>
                      </Group>
                      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="xs">
                        <TextInput
                          size="xs"
                          label="中文内容"
                          value={item.text}
                          onChange={(e) => updateAnnouncement(idx, { text: e.currentTarget.value })}
                        />
                        <TextInput
                          size="xs"
                          label="English"
                          value={item.text_en}
                          onChange={(e) => updateAnnouncement(idx, { text_en: e.currentTarget.value })}
                        />
                      </SimpleGrid>
                    </Stack>
                  </Paper>
                ))}
              </Stack>
            )}
          </section>

          {/* 吸底未保存更改条 */}
          {isDirty && (
            <div className="ops-sticky-save" role="status">
              <div className="ops-sticky-save-left">
                <span className="ops-dirty-dot" aria-hidden="true" />
                <span>门户站点配置有未保存的更改</span>
              </div>
              <Group gap="xs">
                <Button size="xs" variant="default" disabled={saving} onClick={handleDiscard}>
                  放弃更改
                </Button>
                <Button size="xs" loading={saving} onClick={() => void handleSave()}>
                  保存并生效
                </Button>
              </Group>
            </div>
          )}
        </>
      )}
    </Stack>
  );
}
