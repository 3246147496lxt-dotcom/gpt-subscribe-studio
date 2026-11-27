import assert from "node:assert/strict";
import test from "node:test";

import { isAbortError } from "../src/api/http";
import { buildOrderLedgerExportUrl } from "../src/api/ledger";
import type { QuickPayJob } from "../src/api/quickPay";
import { planLabel } from "../src/plans";
import { detectDuplicateDevices, parseBatchSessions } from "../src/utils/batchSessions";
import { detectCardBrand, formatCardNumber, isLuhnValid, stripCardDigits } from "../src/utils/cardFormat";
import { computeQuickPayFlow, describeFailureDetail } from "../src/utils/quickPayProgress";
import { inspectSession, maskEmail } from "../src/utils/sessionInspect";
import {
  autoRenewLabel,
  buildTimingBreakdown,
  deliveryStatusLabel,
  formatDurationClock,
  formatDurationMs,
  formatMonthDayTime,
  formatPreciseDuration,
  hasFailureReason,
  paymentStatusLabel,
  planPillTone,
  simpleOrderStatus,
  sourceLabel,
} from "../src/utils/orderLedgerFormat";

function makeJwt(claims: Record<string, unknown>): string {
  const encode = (obj: Record<string, unknown>) =>
    Buffer.from(JSON.stringify(obj), "utf-8").toString("base64url");
  return `${encode({ alg: "none", typ: "JWT" })}.${encode(claims)}.sig`;
}

test("inspectSession parses valid, expiring, expired, refresh-only and invalid inputs without echoing token", () => {
  const now = 1_750_000_000_000;
  const nowSec = Math.floor(now / 1000);

  assert.equal(inspectSession("   ", now).state, "empty");
  assert.equal(inspectSession("{broken", now).state, "invalid");
  assert.equal(inspectSession("[]", now).state, "invalid");

  const validJwt = makeJwt({
    exp: nowSec + 7200,
    "https://api.openai.com/profile": { email: "alice@example.com" },
    "https://api.openai.com/auth": { chatgpt_account_id: "acct_1234567890abcdef" },
  });
  const validRes = inspectSession(
    JSON.stringify({ accessToken: validJwt, refreshToken: "rt-demo-secret", cookies: "a=1" }),
    now,
  );
  assert.equal(validRes.state, "valid");
  assert.equal(validRes.maskedEmail, "al***@example.com");
  assert.equal(validRes.hasRefreshCredential, true);
  assert.equal(validRes.hasCookies, true);
  assert.ok(!JSON.stringify(validRes).includes(validJwt));
  assert.ok(!JSON.stringify(validRes).includes("rt-demo-secret"));

  const expiringJwt = makeJwt({ exp: nowSec + 120 });
  assert.equal(inspectSession(JSON.stringify({ accessToken: expiringJwt }), now).state, "expiring");

  const expiredJwt = makeJwt({ exp: nowSec - 600 });
  const expiredNoRefresh = inspectSession(JSON.stringify({ accessToken: expiredJwt }), now);
  assert.equal(expiredNoRefresh.state, "expired");
  assert.equal(expiredNoRefresh.hasRefreshCredential, false);

  const refreshOnly = inspectSession(JSON.stringify({ sessionToken: "st-demo-only" }), now);
  assert.equal(refreshOnly.state, "refresh_only");
  assert.equal(refreshOnly.hasRefreshCredential, true);

  const cookieRefreshOnly = inspectSession(
    JSON.stringify({ cookies: { "__Secure-next-auth.session-token": "st-cookie-only" } }),
    now,
  );
  assert.equal(cookieRefreshOnly.state, "refresh_only");
  assert.equal(cookieRefreshOnly.hasRefreshCredential, true);

  const jweHeader = Buffer.from(JSON.stringify({ alg: "dir", enc: "A256GCM" }), "utf-8").toString("base64url");
  const rawJweSt = inspectSession(`${jweHeader}..ivPart.cipherPart.tagPart`, now);
  assert.equal(rawJweSt.state, "refresh_only");
  assert.equal(rawJweSt.hasRefreshCredential, true);

  assert.equal(maskEmail("ab@example.com"), "a***@example.com");
  assert.equal(maskEmail("operator@example.com"), "op***@example.com");
});

test("cardFormat groups digits, detects brands and checks Luhn checksum", () => {
  const visaSample = ["4242", "4242", "4242", "4242"].join("");
  const badSample = ["4242", "4242", "4242", "4243"].join("");
  const amexSample = ["3782", "822463", "10005"].join("");

  assert.equal(formatCardNumber(visaSample), "4242 4242 4242 4242"); // release-check: ignore
  assert.equal(formatCardNumber(amexSample), "3782 822463 10005"); // release-check: ignore
  assert.equal(stripCardDigits("4242-4242 4242 4242"), visaSample); // release-check: ignore
  assert.equal(detectCardBrand(visaSample), "Visa");
  assert.equal(detectCardBrand(amexSample), "Amex");
  assert.equal(detectCardBrand("55551234"), "Mastercard");
  assert.equal(isLuhnValid(visaSample), true);
  assert.equal(isLuhnValid(badSample), false);
});

test("parseBatchSessions supports single JSON, blank lines, --- dividers and labeled prefixes", () => {
  assert.deepEqual(parseBatchSessions(""), []);

  const single = parseBatchSessions('{"accessToken":"tok1"}');
  assert.equal(single.length, 1);
  assert.equal(single[0].label, "账号 #1");

  const multi = parseBatchSessions([
    '主号|{"accessToken":"t1"}',
    "---",
    '备用号|{"accessToken":"t2"}',
    "",
    '{"accessToken":"t3"}',
  ].join("\n"));
  assert.equal(multi.length, 3);
  assert.equal(multi[0].label, "主号");
  assert.equal(multi[1].label, "备用号");
  assert.equal(multi[2].label, "账号 #3");
});

test("inspectSession and detectDuplicateDevices extract masked oai-did prefixes and flag shared browser devices", () => {
  const sharedDid = "9f1a2b3c-dead-4bee-8000-112233445566";
  const otherDid = "aa44bb55-1234-4abc-9999-abcdef012345";
  const s1 = JSON.stringify({ accessToken: "t1", deviceId: sharedDid });
  const s2 = JSON.stringify({ accessToken: "t2", cookies: { "oai-did": sharedDid } });
  const s3 = JSON.stringify({ accessToken: "t3", statsigContext: { deviceId: otherDid } });

  const info1 = inspectSession(s1);
  assert.equal(info1.deviceIdPrefix, "9f1a2b3c…");
  assert.ok(!JSON.stringify(info1).includes(sharedDid));

  const audit = detectDuplicateDevices([
    { label: "账号A", session: s1 },
    { label: "账号B", session: s2 },
    { label: "账号C", session: s3 },
  ]);
  assert.equal(audit.conflicts.length, 1);
  assert.equal(audit.conflicts[0].deviceIdPrefix, "9f1a2b3c…");
  assert.deepEqual(audit.conflicts[0].labels, ["账号A", "账号B"]);
  assert.deepEqual(audit.byIndex[0].duplicateLabels, ["账号B"]);
  assert.deepEqual(audit.byIndex[1].duplicateLabels, ["账号A"]);
  assert.deepEqual(audit.byIndex[2].duplicateLabels, []);
});

test("buildOrderLedgerExportUrl omits default 'all' filters and encodes active filters", () => {
  assert.equal(buildOrderLedgerExportUrl({ status: "all", plan: "all" }), "/api/orders/export.csv");
  assert.equal(buildOrderLedgerExportUrl({ status: "all", plan: "all", source: "all" }), "/api/orders/export.csv");
  const url = buildOrderLedgerExportUrl({
    status: "succeeded",
    plan: "plus",
    search: "ord-1",
    date_from: "2026-01-01",
    date_to: "2026-01-31",
  });
  assert.equal(
    url,
    "/api/orders/export.csv?status=succeeded&plan=plus&search=ord-1&date_from=2026-01-01&date_to=2026-01-31",
  );
  assert.equal(
    buildOrderLedgerExportUrl({ source: "portal", status: "failed" }),
    "/api/orders/export.csv?status=failed&source=portal",
  );
});

test("orderLedgerFormat renders Chinese operational labels and human durations", () => {
  assert.equal(sourceLabel("portal"), "用户门户");
  assert.equal(sourceLabel("quick_pay"), "后台一键");
  assert.equal(sourceLabel("step"), "分步调试");
  assert.equal(paymentStatusLabel("succeeded"), "已支付");
  assert.equal(paymentStatusLabel("unpaid"), "未支付");
  assert.equal(deliveryStatusLabel("verify_delayed"), "延迟核验");
  assert.equal(autoRenewLabel("manual_required"), "需人工关续费");
  assert.equal(formatDurationMs(null), null);
  assert.equal(formatDurationMs(420), "420ms");
  assert.equal(formatDurationMs(5120), "5.1s");
  assert.equal(formatDurationMs(125_000), "2m 5s");
  assert.equal(formatDurationClock(null), null);
  assert.equal(formatDurationClock(420), "00:01");
  assert.equal(formatDurationClock(48_000), "00:48");
  assert.equal(formatDurationClock(63_000), "01:03");
  assert.match(formatMonthDayTime("2026-11-24T11:40:00Z"), /^\d{2}\/\d{2} \d{2}:\d{2}$/);
  assert.deepEqual(simpleOrderStatus("delivered"), { label: "成功", tone: "success" });
  assert.deepEqual(simpleOrderStatus("rejected"), { label: "失败", tone: "failed" });
  assert.deepEqual(simpleOrderStatus("unknown"), { label: "待核对", tone: "warning" });
  assert.deepEqual(simpleOrderStatus("awaiting_payment"), { label: "处理中", tone: "pending" });
  assert.equal(hasFailureReason({ status: "delivered" }), false);
  assert.equal(hasFailureReason({ status: "awaiting_payment" }), false);
  assert.equal(hasFailureReason({ status: "rejected", failure_code: "unusual_activity" }), true);

  assert.equal(planLabel("go"), "Go");
  assert.equal(planLabel("plus"), "Plus");
  assert.equal(planLabel("pro_lite"), "Pro100");
  assert.equal(planLabel("pro"), "Pro200");
  assert.equal(planLabel("pro_max"), "Pro500");
  assert.equal(planLabel("team"), "Team");
  assert.equal(planPillTone("go"), "go");
  assert.equal(planPillTone("plus"), "plus");
  assert.equal(planPillTone("pro_lite"), "pro100");
  assert.equal(planPillTone("pro"), "pro200");
  assert.equal(planPillTone("pro_max"), "pro500");
  assert.equal(planPillTone("team"), "other");

  // Timing breakdown avoids double-counting create_order_ms + create_checkout_ms
  assert.equal(formatPreciseDuration(850), "850 ms");
  assert.equal(formatPreciseDuration(12910), "12.91s");
  const tb = buildTimingBreakdown({
    total_ms: 16800,
    auto_cancel_renew: true,
    timings_ms: {
      queue_ms: 5,
      preflight_ms: 20,
      create_checkout_ms: 12910,
      create_order_ms: 12935,
      payment_ms: 3100,
      cancel_renew_ms: 750,
      total_ms: 16800,
    },
  });
  assert.equal(tb.totalMs, 16800);
  const byKey = Object.fromEntries(tb.segments.map((s) => [s.key, s]));
  assert.equal(byKey.create_checkout_ms.ms, 12910);
  assert.equal(byKey.payment_ms.ms, 3100);
  assert.equal(byKey.cancel_renew_ms.ms, 750);
  assert.equal(byKey.other_ms, undefined);
});

test("isAbortError recognizes AbortError objects cleanly", () => {
  const err = new Error("aborted");
  err.name = "AbortError";
  assert.equal(isAbortError(err), true);
  assert.equal(isAbortError(new Error("other")), false);
  assert.equal(isAbortError(null), false);
});

test("computeQuickPayFlow uses only checkout / payment / cancel_renew steps and progresses monotonically", () => {
  // 1) autoCancelRenew=true -> 3 steps: 建单 / 支付 / 取消连续订阅
  const stages = [
    "queued",
    "preflight",
    "preflight_passed",
    "creating_order",
    "reissuing",
    "reissued",
    "browser_decode",
    "browser_decoded",
    "creating_checkout",
    "checkout_created",
    "confirming_payment",
    "verifying_delivery",
    "canceling_auto_renew",
    "auto_renew_canceled",
  ];

  let prevPct = 0;
  for (const stage of stages) {
    const isFinal = stage === "auto_renew_canceled";
    const job: QuickPayJob = {
      job_id: "job-mono",
      status: isFinal ? "succeeded" : stage === "queued" ? "queued" : "running",
      stage,
    };
    const flow = computeQuickPayFlow(job, {
      debugOnly: false,
      autoCancelRenew: true,
      highWaterPct: prevPct,
      stageElapsedMs: 800,
    });
    assert.deepEqual(flow.steps.map((s) => s.title), ["建单", "支付", "取消连续订阅"]);
    assert.ok(flow.percent >= prevPct, `stage ${stage} regressed from ${prevPct}% to ${flow.percent}%`);
    assert.ok(flow.currentActionText.length > 0);
    assert.ok(flow.etaText.length > 0);
    prevPct = flow.percent;
  }
  assert.equal(prevPct, 100);

  // 2) autoCancelRenew=false -> 2 steps: 建单 / 支付
  const twoStepFlow = computeQuickPayFlow(
    { job_id: "j-2", status: "running", stage: "confirming_payment" },
    { debugOnly: false, autoCancelRenew: false },
  );
  assert.deepEqual(twoStepFlow.steps.map((s) => s.title), ["建单", "支付"]);
  assert.equal(twoStepFlow.currentStepIndex, 2);

  // 3) debugOnly=true -> 1 step: 建单
  const oneStepFlow = computeQuickPayFlow(
    { job_id: "j-1", status: "running", stage: "creating_checkout" },
    { debugOnly: true, autoCancelRenew: true },
  );
  assert.deepEqual(oneStepFlow.steps.map((s) => s.title), ["建单"]);
  assert.equal(oneStepFlow.totalSteps, 1);
});

test("computeQuickPayFlow stops at the real failed step and describeFailureDetail provides concrete diagnostics", () => {
  const failedAtCheckout: QuickPayJob = {
    job_id: "job-fail-checkout",
    status: "failed",
    stage: "failed",
    failure_code: "checkout_upstream_error",
    message: "raw-secret-prose-never-echoed",
    diagnostics: {
      stage: "creating_checkout",
      upstream_status: 403,
      upstream_code: "unusual_activity",
      cf_ray: "abcdef0123456789-lax",
    },
    events: [
      { state: "running", stage: "preflight", message: "", created_at: "2026-11-25T00:00:00Z" },
      { state: "running", stage: "creating_checkout", message: "", created_at: "2026-11-25T00:00:03Z" },
      { state: "failed", stage: "failed", message: "", created_at: "2026-11-25T00:00:05Z" },
    ],
  };

  const flow = computeQuickPayFlow(failedAtCheckout, { debugOnly: false, autoCancelRenew: true });
  assert.equal(flow.color, "red");
  assert.equal(flow.currentStepIndex, 1);
  assert.equal(flow.currentStepTitle, "建单");
  assert.ok(flow.percent < 40 && flow.percent >= 20, `expected failure pct inside step 1, got ${flow.percent}`);
  assert.equal(flow.steps[0].status, "failed");
  assert.equal(flow.steps[1].status, "pending");
  assert.equal(flow.steps[2].status, "pending");

  const diag = describeFailureDetail(failedAtCheckout, false, true);
  assert.equal(diag.stepNumber, 1);
  assert.equal(diag.totalSteps, 3);
  assert.equal(diag.stepTitle, "建单");
  assert.equal(diag.substageCode, "creating_checkout");
  assert.equal(diag.upstreamStatus, 403);
  assert.equal(diag.upstreamCode, "unusual_activity");
  assert.equal(diag.traceId, "abcdef0123456789-lax");
  assert.ok(!JSON.stringify(diag).includes("raw-secret-prose-never-echoed"));

  // Payment failure stops at step 2 ("支付")
  const failedAtPay: QuickPayJob = {
    job_id: "job-fail-pay",
    status: "failed",
    stage: "failed",
    failure_code: "card_declined",
    diagnostics: { stage: "confirming_payment", upstream_status: 402, upstream_code: "card_declined" },
  };
  const payFlow = computeQuickPayFlow(failedAtPay, { debugOnly: false, autoCancelRenew: true });
  assert.equal(payFlow.currentStepIndex, 2);
  assert.equal(payFlow.currentStepTitle, "支付");
  assert.equal(payFlow.steps[0].status, "done");
  assert.equal(payFlow.steps[1].status, "failed");
  assert.equal(payFlow.steps[2].status, "pending");
});
