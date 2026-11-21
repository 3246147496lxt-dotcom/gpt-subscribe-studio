import assert from "node:assert/strict";
import test from "node:test";
import { MantineProvider } from "@mantine/core";
import { renderToStaticMarkup } from "react-dom/server";
import { QuickPayFailure } from "../src/pages/QuickPay";
import { startQuickPay, type QuickPayInput, type QuickPayJob } from "../src/api/quickPay";

const job: QuickPayJob = {
  job_id: "local-job", order_id: "local-order", status: "failed", stage: "failed",
  failure_code: "checkout_upstream_error", checkout_outcome: "rejected_before_payment",
  message: "private-upstream-marker",
};
function notice(diff: Partial<QuickPayJob> = {}, locked = false) {
  return renderToStaticMarkup(<MantineProvider>
    <QuickPayFailure job={{ ...job, ...diff }} locked={locked} />
  </MantineProvider>);
}

test("manual route is independent: fixed official URL, no context, no form or retry", () => {
  const html = notice();
  assert.match(html, /未创建结账单，未提交付款/);
  const links = [...html.matchAll(/<a\b[^>]*>/g)].map(m => m[0]);
  assert.equal(links.length, 1);
  assert.match(links[0], /href="https:\/\/chatgpt\.com\/"/);
  assert.match(links[0], /rel="noopener noreferrer"/);
  assert.match(links[0], /referrerPolicy="no-referrer"/i);
  assert.match(html, /独立操作/);
  assert.doesNotMatch(html, /<form\b|<button\b|private-upstream-marker|local-job|local-order/);
});

test("legacy, locked, confirmed, ambiguous and contradictory statuses never suggest another payment", () => {
  const event = (stage: string) => [{ state: "running", stage, message: "", created_at: "" }];
  for (const diff of [
    { checkout_outcome: undefined }, { status: "unknown" }, { status: "running" },
    { status: "succeeded" }, { order_id: null }, { stage: "confirming_payment" },
    { failure_code: "payment_unknown_state" }, { failure_code: "card_declined" }, { failure_code: null },
    { lock_id: "lock" }, { events: event("checkout_created") }, { events: event("verifying_delivery") },
    { events: event("confirming_payment") },
  ] as Partial<QuickPayJob>[]) {
    assert.doesNotMatch(notice(diff), /chatgpt\.com|未创建结账单，未提交付款/);
  }
  assert.doesNotMatch(notice({}, true), /chatgpt\.com|未创建结账单，未提交付款/);
  for (const failure_code of ["already_subscribed", "subscription_limit"])
    assert.doesNotMatch(notice({ failure_code }), /href="https:\/\/chatgpt\.com/);
});

test("checkout-only sends no card; invalid combinations stop before transport", async () => {
  const input: QuickPayInput = {
    plan: "plus", session: "synthetic", debug_checkout_only: true, auto_cancel_renew: false,
    mode: { account_source: "customer_session", card_source: "customer", billing_mode: "standard", risk_acknowledged: false },
  };
  const orig = globalThis.fetch;
  const messages: Record<string, unknown>[] = [];
  globalThis.fetch = async (url, init) => {
    assert.equal(url, "/api/payments/quick-pay");
    messages.push(JSON.parse(String(init?.body)));
    return new Response(JSON.stringify({ job_id: "synth", status: "queued" }), { status: 202 });
  };
  try {
    await startQuickPay(input, "test-id");
    assert.equal(messages.length, 1);
    assert.ok(!("card" in messages[0]));
    for (const invalid of [{ ...input, card: null }, { ...input, card: { number: "private-marker" } },
                          { ...input, debug_checkout_only: false }])
      await assert.rejects(startQuickPay(invalid as QuickPayInput, "test-id"), /仅建单|必须提供/);
    assert.equal(messages.length, 1);
    await startQuickPay({ ...input, debug_checkout_only: false,
      card: { number: "synthetic", cvc: "fixture", exp_month: "00", exp_year: "0000" } }, "test-pay");
    assert.equal(messages.length, 2);
    assert.ok("card" in messages[1]);
  } finally {
    globalThis.fetch = orig;
  }
});
