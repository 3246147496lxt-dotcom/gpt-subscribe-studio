import { ActionIcon, Group, Text, Tooltip } from "@mantine/core";
import { IconCheck, IconCopy } from "@tabler/icons-react";
import { Fragment, useState } from "react";

const JSON_TOKEN_RE =
  /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g;

function highlightJson(text: string) {
  const parts: Array<{ kind: string; value: string }> = [];
  let lastIndex = 0;
  for (const match of text.matchAll(JSON_TOKEN_RE)) {
    const idx = match.index ?? 0;
    if (idx > lastIndex) {
      parts.push({ kind: "plain", value: text.slice(lastIndex, idx) });
    }
    const token = match[0];
    if (token.startsWith('"')) {
      if (token.endsWith(":")) {
        parts.push({ kind: "key", value: token.slice(0, -1) });
        parts.push({ kind: "plain", value: ":" });
      } else if (token === '"[REDACTED]"') {
        parts.push({ kind: "redacted", value: token });
      } else {
        parts.push({ kind: "string", value: token });
      }
    } else if (token === "true" || token === "false") {
      parts.push({ kind: "bool", value: token });
    } else if (token === "null") {
      parts.push({ kind: "null", value: token });
    } else {
      parts.push({ kind: "number", value: token });
    }
    lastIndex = idx + token.length;
  }
  if (lastIndex < text.length) {
    parts.push({ kind: "plain", value: text.slice(lastIndex) });
  }
  return parts;
}

export function JsonCodeBlock({
  label,
  value,
}: {
  label: string;
  value: unknown;
}) {
  const [copied, setCopied] = useState(false);
  const formatted = JSON.stringify(value, null, 2);
  const tokens = highlightJson(formatted);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(formatted);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable in restricted context
    }
  }

  return (
    <div className="failure-json-section">
      <Group justify="space-between" align="center" mb={6}>
        <Text size="xs" fw={700} className="failure-block-label" mb={0}>
          {label}
        </Text>
        <Tooltip label={copied ? "已复制" : `复制${label}`} withArrow>
          <ActionIcon
            size="sm"
            variant="subtle"
            color={copied ? "teal" : "gray"}
            aria-label={`复制${label}`}
            onClick={() => void handleCopy()}
          >
            {copied ? <IconCheck size={13} /> : <IconCopy size={13} />}
          </ActionIcon>
        </Tooltip>
      </Group>
      <pre className="failure-code-block">
        {tokens.map((tok, i) =>
          tok.kind === "plain" ? (
            <Fragment key={i}>{tok.value}</Fragment>
          ) : (
            <span key={i} className={`json-tok--${tok.kind}`}>
              {tok.value}
            </span>
          ),
        )}
      </pre>
    </div>
  );
}
