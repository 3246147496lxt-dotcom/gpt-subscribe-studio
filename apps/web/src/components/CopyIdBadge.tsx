import { ActionIcon, Code, CopyButton, Group, Tooltip } from "@mantine/core";
import { IconCheck, IconCopy } from "@tabler/icons-react";

export function CopyIdBadge({
  value,
  prefixLength = 10,
}: {
  value: string;
  prefixLength?: number;
}) {
  const shown = prefixLength > 0 && value.length > prefixLength ? `${value.slice(0, prefixLength)}…` : value;

  return (
    <CopyButton value={value} timeout={1500}>
      {({ copied, copy }) => (
        <Group gap={4} wrap="nowrap" display="inline-flex" align="center">
          <Tooltip label={value} openDelay={300}>
            <Code>{shown}</Code>
          </Tooltip>
          <Tooltip label={copied ? "已复制" : "复制完整编号"} withArrow>
            <ActionIcon
              size="xs"
              variant="subtle"
              color={copied ? "teal" : "gray"}
              onClick={(e) => {
                e.stopPropagation();
                copy();
              }}
              aria-label={`复制 ${value}`}
            >
              {copied ? <IconCheck size={12} /> : <IconCopy size={12} />}
            </ActionIcon>
          </Tooltip>
        </Group>
      )}
    </CopyButton>
  );
}
