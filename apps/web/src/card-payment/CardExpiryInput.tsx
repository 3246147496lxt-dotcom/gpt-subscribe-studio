import { TextInput } from "@mantine/core";
import { useState } from "react";

import { expiryFormatHint, isValidCardExpiry, parseCardExpiryInput, type CardExpiry } from "./cardExpiry";

interface Props {
  month: string;
  year: string;
  onChange: (expiry: CardExpiry) => void;
  disabled?: boolean;
  autoComplete?: "cc-exp" | "off";
}

export function CardExpiryInput({ month, year, onChange, disabled, autoComplete }: Props) {
  const [trailingSlash, setTrailingSlash] = useState(false);
  const [blurred, setBlurred] = useState(false);
  const showSlash = Boolean(year || (month && trailingSlash));
  const displayYear = year.length === 4 ? year.slice(-2) : year;
  const value = month + (showSlash ? "/" : "") + displayYear;
  const expiry = { exp_month: month, exp_year: year };

  return (
    <TextInput
      label="有效期"
      placeholder="MM/YY"
      inputMode="numeric"
      autoComplete={autoComplete}
      disabled={disabled}
      value={value}
      error={blurred && value && !isValidCardExpiry(expiry) ? expiryFormatHint : undefined}
      onFocus={() => setBlurred(false)}
      onBlur={() => {
        setBlurred(true);
        if (month.length === 1 && year && isValidCardExpiry(expiry)) {
          onChange({ ...expiry, exp_month: month.padStart(2, "0") });
        }
      }}
      onChange={(event) => {
        const text = event.currentTarget.value;
        const next = parseCardExpiryInput(text);
        if (!next) return;
        setTrailingSlash(text.trimEnd().endsWith("/"));
        onChange(next);
      }}
    />
  );
}
