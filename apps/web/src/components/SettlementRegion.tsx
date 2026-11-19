import { Select } from "@mantine/core";
import { useEffect, useId, useRef, useState } from "react";

import { REGIONS, regionLabel } from "../plans";

const regionOptions = REGIONS.map(({ code, label }) => ({ value: code, label: `${label} (${code})` }));

interface Props {
  team: boolean;
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
}

export function SettlementRegion({ team, value, onChange, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const noteId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const editing = open && !disabled;
  const title = team ? "代理出口" : "结算地区";
  const fallback = team ? "跟随结账地区" : "沿用当前代理地区";

  useEffect(() => {
    if (editing) input.current?.focus();
  }, [editing]);

  return (
    <div className="settlement-region">
      <button
        ref={trigger}
        type="button"
        className="settlement-region__summary"
        disabled={disabled}
        aria-expanded={editing}
        aria-controls={panelId}
        aria-describedby={noteId}
        onClick={() => setOpen(!editing)}
      >
        <span className="settlement-region__title">{title}</span>
        <strong className="settlement-region__value">{value ? regionLabel(value) : fallback}</strong>
        <span className="settlement-region__action">{editing ? "收起" : "修改"}</span>
      </button>
      <div id={panelId} className="settlement-region__editor" hidden={!editing}>
        {editing && (
          <Select
            ref={input}
            aria-label={team ? "选择代理出口" : "选择结算地区（代理出口）"}
            aria-describedby={noteId}
            placeholder={fallback}
            searchable
            data={regionOptions}
            value={value || null}
            onChange={(next) => {
              onChange(next ?? "");
              setOpen(false);
              trigger.current?.focus();
            }}
          />
        )}
      </div>
      <p className="settlement-region__description" id={noteId}>
        {team
          ? "默认跟随结账地区；修改会影响代理出口和会话重签发地区，不更改账单国家。"
          : "会影响结算币种、代理出口和会话重签发地区；账单地址固定使用服务端已核验的美国免税地址，不随出口变化。"}
      </p>
    </div>
  );
}
