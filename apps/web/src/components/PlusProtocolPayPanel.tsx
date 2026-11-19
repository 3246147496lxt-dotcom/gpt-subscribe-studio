import { Alert, Badge, Button, Checkbox, Code, Group, Paper, PasswordInput, SimpleGrid, Stack, Text, TextInput } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import { CardExpiryInput } from "../card-payment/CardExpiryInput";
import {
  bindAndPayProtocol,
  formatAmount,
  getBindAndPayProtocol,
  type BindAndPayProtocolInput,
  type BindAndPayProtocolResult,
  type BillingInput,
  type ProtocolPayResult,
  type TeamCardPayload,
} from "../api/client";
import { PROTOCOL_STAGE_LABELS, protocolBillingError, protocolCardError, protocolIsPaid, protocolPostReady, protocolStateLabel } from "../card-payment/protocolBind";
import { FixedPayerBilling } from "./FixedPayerBilling";

interface Props {
  orderId: string;
  billing: BillingInput;
  amount?: string | null;
  currency?: string | null;
  quotationError?: string | null;
  disabled?: boolean;
  submissionDisabled?: boolean;
  onBusyChange?: (busy: boolean) => void;
  onDone?: (result: ProtocolPayResult) => void;
}

export function PlusProtocolPayPanel({
  orderId, billing, amount, currency, quotationError, disabled = false,
  submissionDisabled = false, onBusyChange, onDone,
}: Props) {
  const [card, setCard] = useState<TeamCardPayload>({ number: "", cvc: "", exp_month: "", exp_year: "" });
  const [result, setResult] = useState<BindAndPayProtocolResult | null>(null);
  const [work, setWork] = useState<"submit" | "query" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [needsQuery, setNeedsQuery] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [quoteConfirmed, setQuoteConfirmed] = useState(false);
  const busyRef = useRef(false);
  const epoch = useRef(0);
  const submittedDetails = useRef<Pick<BindAndPayProtocolInput, "card" | "billing"> | null>(null);
  const onBusyRef = useRef(onBusyChange);
  const onDoneRef = useRef(onDone);
  onBusyRef.current = onBusyChange;
  onDoneRef.current = onDone;

  useEffect(() => {
    epoch.current += 1;
    return () => { epoch.current += 1; onBusyRef.current?.(false); };
  }, [orderId]);

  const price = formatAmount(amount, currency);
  const hasQuote = amount != null && amount !== "" && Number.isFinite(Number(amount)) && Number(amount) >= 0 && Boolean(currency);
  const quoteChanged = result?.state === "pending" && result.stage === "checkout";
  const canSubmit = !disabled && !submissionDisabled && work === null && hasQuote && !needsQuery
    && protocolPostReady(result) && (!quoteChanged || quoteConfirmed);
  const fieldsDisabled = disabled || work !== null || submitted;

  function applyResult(out: BindAndPayProtocolResult) {
    setResult(out);
    setNeedsQuery(false);
    setQuoteConfirmed(false);
    if (out.state === "not_started") {
      setSubmitted(false);
      submittedDetails.current = null;
    }
    onDoneRef.current?.(out);
  }

  async function run(kind: "submit" | "query", action: (isCurrent: () => boolean) => Promise<void>) {
    if (disabled || busyRef.current) return;
    busyRef.current = true;
    const current = epoch.current;
    const isCurrent = () => epoch.current === current;
    setWork(kind);
    setError(null);
    onBusyRef.current?.(true);
    try {
      await action(isCurrent);
    } catch (err) {
      if (isCurrent()) setError(err instanceof Error ? err.message : "请求结果待核实，请查询协议状态。");
    } finally {
      if (isCurrent()) {
        busyRef.current = false;
        setWork(null);
        onBusyRef.current?.(false);
      }
    }
  }

  function submitProtocol() {
    if (!canSubmit) return;
    const details = submittedDetails.current || {
      card: { ...card, number: card.number.replace(/[\s-]/g, "") },
      billing: { ...billing },
    };
    const invalid = protocolCardError(details.card) || protocolBillingError(details.billing);
    if (invalid) { setError(invalid); return; }
    void run("submit", async (isCurrent) => {
      setNeedsQuery(true);
      setSubmitted(true);
      submittedDetails.current = details;
      const out = await bindAndPayProtocol(orderId, {
        ...details, expected_amount: String(amount), expected_currency: String(currency),
      });
      if (isCurrent()) applyResult(out);
    });
  }

  function queryProtocol() {
    void run("query", async (isCurrent) => {
      const out = await getBindAndPayProtocol(orderId);
      if (isCurrent()) applyResult(out);
    });
  }

  const changeCard = (field: keyof TeamCardPayload) => (event: React.ChangeEvent<HTMLInputElement>) => {
    const value = event.currentTarget.value;
    setCard((previous) => ({ ...previous, [field]: value }));
  };
  return (
    <Stack gap="md" maw={720} mx="auto" data-testid="plus-protocol-pay-panel"
      onKeyDownCapture={(event) => {
        if (event.key === "Enter" && event.target instanceof HTMLInputElement) {
          event.preventDefault();
          event.stopPropagation();
        }
      }}>
      <Paper withBorder p="lg" radius="md">
        <Group justify="space-between">
          <div>
            <Text size="sm" fw={600}>Plus 协议支付</Text>
            <Text size="xs" c="dimmed">应付金额</Text>
            <Text fz={30} fw={700}>{price || "待取得报价"}</Text>
          </div>
          <Badge variant="light">服务端协议</Badge>
        </Group>
        {quotationError && <Alert color="red" mt="sm">{quotationError}</Alert>}
      </Paper>
      <Text size="sm">核对已验证的付款方账单和应付金额后提交。服务端沿用当前订单 checkout，依次核算税费、创建 confirmation_tokens、完成 ChatGPT confirm 与 PaymentIntent confirm，再通过 GET checkout 核对 complete / paid 并查询订阅。</Text>
      <Text size="xs" c="dimmed">本单固定使用同一付款方的已核验账单，不能修改。付款确认后仍需等待订阅生效；结果待核实时仅查询状态，不重放付款请求。</Text>
      <Paper withBorder p="md" radius="md">
        <Stack gap="sm">
          <TextInput label="卡号" value={card.number} onChange={changeCard("number")} inputMode="numeric" autoComplete="cc-number" disabled={fieldsDisabled} data-testid="protocol-card-number" />
          <SimpleGrid cols={{ base: 1, xs: 2 }}>
            <CardExpiryInput month={card.exp_month} year={card.exp_year}
              onChange={(expiry) => setCard((previous) => ({ ...previous, ...expiry }))}
              disabled={fieldsDisabled} autoComplete="cc-exp" />
            <PasswordInput label="CVC" value={card.cvc} onChange={changeCard("cvc")} inputMode="numeric" maxLength={4} autoComplete="cc-csc" disabled={fieldsDisabled} />
          </SimpleGrid>
        </Stack>
      </Paper>
      <FixedPayerBilling billing={billing} />
      {quoteChanged && (
        <Alert color="blue" title="请确认当前应付金额">
          <Text size="sm">当前金额为 {price}。核对后再次显式提交。</Text>
          <Checkbox mt="sm" label={`我已确认应付金额 ${price || ""}`} checked={quoteConfirmed}
            disabled={disabled || work !== null} onChange={(event) => setQuoteConfirmed(event.currentTarget.checked)} />
        </Alert>
      )}
      {(needsQuery || result?.state === "unknown") && <Alert color="yellow">请求结果待核实，请使用「查询协议状态」。</Alert>}
      {result && (
        <Alert color={protocolIsPaid(result) ? "green" : result.state === "integration_error" || result.state === "request_rejected" ? "red" : result.state === "challenge_required" ? "yellow" : "blue"} role="status">
          <Stack gap="xs">
            <Group gap="sm"><Text fw={600}>阶段：{PROTOCOL_STAGE_LABELS[result.stage]}</Text><Code>{result.stage}</Code></Group>
            <Group gap="sm"><Text fw={600}>状态：{protocolStateLabel(result.state)}</Text><Code>{result.state}</Code></Group>
            {result.message && <Text size="sm">{result.message}</Text>}
            {result.remediation && <Text size="sm">{result.remediation}</Text>}
            {result.upstream_message && <Code block style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{result.upstream_message}</Code>}
            {result.upstream_status && <Text size="xs">Stripe HTTP：{result.upstream_status}{result.upstream_code ? ` · ${result.upstream_code}` : ""}</Text>}
            {result.next_action?.type && <Text size="sm">待处理动作：{result.next_action.type}</Text>}
            {result.code && <Text size="sm">code：{result.code}</Text>}
            {result.decline_code && <Text size="sm">decline_code：{result.decline_code}</Text>}
            {result.state === "challenge_required" && <Text size="sm">当前结果需要上游验证。查询协议状态可读取后续进展。</Text>}
            {(result.payment_status === "succeeded" || result.intent_status === "succeeded" || result.receipt?.payment_status === "succeeded") && (
              <Paper withBorder p="sm" radius="sm">
                <Stack gap={4}>
                  <Text fw={600}>扣款回执</Text>
                  <Text size="sm">实际扣款：{formatAmount(result.receipt?.amount ?? undefined, result.receipt?.currency ?? undefined) || price || "—"}</Text>
                  {(result.receipt?.tax_delta || result.tax_delta) && <Text size="sm">减免税额：{result.receipt?.tax_delta || result.tax_delta}</Text>}
                  {(result.receipt?.plan_type || result.plan_type) && <Text size="sm">账号套餐：{result.receipt?.plan_type || result.plan_type}</Text>}
                  {(result.receipt?.payment_intent_id || result.payment_intent_id) && <Text size="xs" c="dimmed">PaymentIntent：{result.receipt?.payment_intent_id || result.payment_intent_id}</Text>}
                  <Text size="xs" c="dimmed">{result.receipt?.summary || result.status_summary || result.message}</Text>
                </Stack>
              </Paper>
            )}
          </Stack>
        </Alert>
      )}
      <Group justify="space-between">
        <Button type="button" variant="default" loading={work === "query"} disabled={disabled || work !== null} onClick={queryProtocol}>查询协议状态</Button>
        <Button type="button" loading={work === "submit"} disabled={!canSubmit} onClick={submitProtocol}>协议支付{price ? ` ${price}` : ""}</Button>
      </Group>
      {work && <Text size="sm" role="status">{work === "query" ? "正在读取协议状态…" : "服务端正在处理本次请求…"}</Text>}
      {error && <Alert color="red" role="alert">{error}</Alert>}
    </Stack>
  );
}
