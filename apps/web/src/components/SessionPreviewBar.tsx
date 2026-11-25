import { Badge, Button, Group, Text } from "@mantine/core";
import { IconTrash } from "@tabler/icons-react";
import { useMemo } from "react";

import { inspectSession } from "../utils/sessionInspect";

const STATE_BADGE: Record<string, { label: string; color: string }> = {
  valid: { label: "格式有效", color: "green" },
  expiring: { label: "即将过期", color: "orange" },
  expired: { label: "已过期", color: "red" },
  refresh_only: { label: "仅含刷新凭证", color: "yellow" },
  opaque: { label: "待服务端核验", color: "blue" },
  invalid: { label: "格式异常", color: "red" },
};

export function SessionPreviewBar({
  value,
  onClear,
  disabled,
}: {
  value: string;
  onClear?: () => void;
  disabled?: boolean;
}) {
  const info = useMemo(() => inspectSession(value), [value]);
  if (info.state === "empty") return null;

  const badge = STATE_BADGE[info.state] ?? { label: "已粘贴", color: "gray" };

  return (
    <Group justify="space-between" align="center" gap="xs" wrap="wrap" mt={4}>
      <Group gap={6} wrap="wrap">
        <Badge size="xs" color={badge.color} variant="light">{badge.label}</Badge>
        {info.maskedEmail && <Badge size="xs" color="gray" variant="outline">{info.maskedEmail}</Badge>}
        {info.hasRefreshCredential && <Badge size="xs" color="teal" variant="light">含刷新凭证</Badge>}
        {info.hasCookies && <Badge size="xs" color="cyan" variant="light">含 Cookies</Badge>}
        <Text size="xs" c={info.state === "invalid" || (info.state === "expired" && !info.hasRefreshCredential) ? "red" : "dimmed"}>
          {info.summary}
        </Text>
      </Group>
      {onClear && (
        <Button
          size="compact-xs"
          variant="subtle"
          color="gray"
          leftSection={<IconTrash size={12} />}
          disabled={disabled}
          onClick={onClear}
        >
          清空凭证
        </Button>
      )}
    </Group>
  );
}
