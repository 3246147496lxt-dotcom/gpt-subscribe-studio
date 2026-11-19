import type { BillingInput } from "../api/client";

function hasText(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

export function verifiedPayerBilling(
  verified: boolean | undefined, billing: Partial<BillingInput> | null | undefined,
): BillingInput | null {
  if (verified !== true || !billing) return null;
  const { name, email, line1, city, state, postal_code, country } = billing;
  if (!hasText(name) || !hasText(email) || !hasText(line1) || !hasText(city) || !hasText(postal_code)
      || !hasText(country) || !/^[A-Z]{2}$/.test(country) || typeof state !== "string"
      || (["US", "CA"].includes(country) && !state.trim())) return null;
  return { name, email, line1, city, state, postal_code, country };
}

// Render text, not disabled form controls: browser autofill and DOM edits must
// never become a source of the address sent to checkout or card tokenization.
export function FixedPayerBilling({ billing }: { billing: BillingInput }) {
  return (
    <section aria-label="付款方已核验账单（只读）">
      <strong>付款方已核验账单（只读）</strong>
      <dl data-testid="verified-payer-billing">
        <dt>付款方姓名</dt><dd>{billing.name}</dd>
        <dt>账单邮箱</dt><dd>{billing.email}</dd>
        <dt>街道地址</dt><dd>{billing.line1}</dd>
        <dt>城市</dt><dd>{billing.city}</dd>
        <dt>州 / 省</dt><dd>{billing.state}</dd>
        <dt>邮编</dt><dd>{billing.postal_code}</dd>
        <dt>账单国家</dt><dd>{billing.country}</dd>
      </dl>
    </section>
  );
}
