import { formatCardNumber, stripCardDigits } from "../utils/cardFormat";

export type ParsedCardInput = {
  number: string;
  expiry: string;
  cvc: string;
  matched: boolean;
};

/**
 * Parse a pasted card line like:
 * - "4242 4242 4242 4242 | 12/30 | 123"   release-check: ignore
 * - "4242424242424242 12/2030 123"        release-check: ignore
 * - "4242424242424242|12|2030|123"        release-check: ignore
 * - Multi-line card dump containing PAN, MM/YY, and CVC.
 */
export function parseCardPaste(raw: string): ParsedCardInput {
  const text = raw.trim();
  if (!text) return { number: "", expiry: "", cvc: "", matched: false };

  // Pattern 1: pipe/comma/tab/space delimited PAN + MM/YY(YY) + CVC
  const compact = text.replace(/[\r\n]+/g, " ");
  const panMatch = compact.match(/(?:\d[ -]?){12,19}/);
  if (!panMatch) return { number: "", expiry: "", cvc: "", matched: false };

  const panDigits = stripCardDigits(panMatch[0]);
  if (panDigits.length < 12 || panDigits.length > 19) {
    return { number: "", expiry: "", cvc: "", matched: false };
  }

  const afterPan = compact.slice((panMatch.index ?? 0) + panMatch[0].length);
  let expiry = "";
  let cvc = "";

  // MM/YY or MM/YYYY
  const expSlash = afterPan.match(/(?:^|[^\d])(0?[1-9]|1[0-2])\s*[/|-]\s*(20\d{2}|\d{2})(?:[^\d]|$)/);
  if (expSlash) {
    const mm = expSlash[1].padStart(2, "0");
    const yy = expSlash[2].slice(-2);
    expiry = `${mm}/${yy}`;
    const tail = afterPan.slice((expSlash.index ?? 0) + expSlash[0].length);
    const cvcMatch = tail.match(/(?:^|[^\d])(\d{3,4})(?:[^\d]|$)/);
    if (cvcMatch) cvc = cvcMatch[1];
  } else {
    // Separate tokens like | 12 | 2030 | 123
    const tokens = afterPan.split(/[^0-9]+/).filter(Boolean);
    if (tokens.length >= 2) {
      const m = Number(tokens[0]);
      if (m >= 1 && m <= 12 && (tokens[1].length === 2 || tokens[1].length === 4)) {
        expiry = `${String(m).padStart(2, "0")}/${tokens[1].slice(-2)}`;
        if (tokens[2] && (tokens[2].length === 3 || tokens[2].length === 4)) {
          cvc = tokens[2];
        }
      }
    }
  }

  const matched = Boolean(expiry || cvc);
  return {
    number: formatCardNumber(panDigits),
    expiry,
    cvc,
    matched,
  };
}

export function formatExpiryInput(raw: string): string {
  const digits = raw.replace(/\D/g, "").slice(0, 4);
  if (digits.length <= 2) return digits;
  return `${digits.slice(0, 2)}/${digits.slice(2)}`;
}

export function splitExpiry(expiry: string): { exp_month: string; exp_year: string } | null {
  const m = expiry.trim().match(/^(\d{2})\/(\d{2}|\d{4})$/);
  if (!m) return null;
  const month = Number(m[1]);
  if (month < 1 || month > 12) return null;
  const year = m[2].length === 2 ? `20${m[2]}` : m[2];
  return { exp_month: m[1], exp_year: year };
}
