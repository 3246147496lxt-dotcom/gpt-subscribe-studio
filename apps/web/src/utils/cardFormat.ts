export function stripCardDigits(raw: string): string {
  return raw.replace(/\D/g, "").slice(0, 19);
}

export function formatCardNumber(raw: string): string {
  const digits = stripCardDigits(raw);
  if (/^3[47]/.test(digits)) {
    // Amex 4-6-5 grouping
    return [digits.slice(0, 4), digits.slice(4, 10), digits.slice(10, 15)]
      .filter(Boolean)
      .join(" ");
  }
  return digits.replace(/(\d{4})(?=\d)/g, "$1 ");
}

export function detectCardBrand(raw: string): string | null {
  const digits = stripCardDigits(raw);
  if (!digits) return null;
  if (/^4/.test(digits)) return "Visa";
  if (/^(5[1-5]|2[2-7])/.test(digits)) return "Mastercard";
  if (/^3[47]/.test(digits)) return "Amex";
  if (/^6(?:011|5)/.test(digits)) return "Discover";
  if (/^35/.test(digits)) return "JCB";
  if (/^62/.test(digits)) return "UnionPay";
  return null;
}

export function isLuhnValid(raw: string): boolean {
  const digits = stripCardDigits(raw);
  if (digits.length < 12 || digits.length > 19) return false;
  let sum = 0;
  let doubleNext = false;
  for (let i = digits.length - 1; i >= 0; i -= 1) {
    let d = digits.charCodeAt(i) - 48;
    if (doubleNext) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    sum += d;
    doubleNext = !doubleNext;
  }
  return sum % 10 === 0;
}
