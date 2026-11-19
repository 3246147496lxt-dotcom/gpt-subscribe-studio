export interface CardExpiry {
  exp_month: string;
  exp_year: string;
}

export const expiryFormatHint = "有效期请按 MM/YY 填写，例如 11/28，月份为 01 至 12。";

export function isValidCardExpiry({ exp_month, exp_year }: CardExpiry) {
  return /^(?:0?[1-9]|1[0-2])$/.test(exp_month) && /^(?:\d{2}|\d{4})$/.test(exp_year);
}

// Keep partial values editable. Mobile numeric keyboards can omit the slash.
export function parseCardExpiryInput(text: string): CardExpiry | null {
  const value = text.replace(/\s/g, "");
  const parts = /^(\d{0,2})\/(\d{0,4})$/.exec(value);
  if (parts) return { exp_month: parts[1], exp_year: parts[2] };
  if (!/^\d{0,6}$/.test(value)) return null;
  return { exp_month: value.slice(0, 2), exp_year: value.slice(2) };
}
