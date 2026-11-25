import {
  Alert, Badge, Button, Card, Group, Loader, Paper, Progress, SimpleGrid, Stack, Text, ThemeIcon, Title,
} from "@mantine/core";
import {
  IconActivity, IconAlertTriangle, IconCheck, IconClock, IconExternalLink, IconShieldCheck, IconX,
} from "@tabler/icons-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import type { QuickPayJob } from "../../api/quickPay";
import {
  computeQuickPayFlow,
  formatSecondsBrief,
  resolveEffectiveStage,
  SUBSTAGE_DETAIL,
  type ComputedStep,
  type FlowStepId,
} from "../../utils/quickPayProgress";
import { CopyIdBadge } from "../CopyIdBadge";
import { isConfirmedRejection, isQuickPayTerminal, TIMING_LABEL } from "./constants";

const STEP_TAG_LABEL: Record<FlowStepId, { text: string; color: string }> = {
  checkout: { text: "建单", color: "blue" },
  payment: { text: "支付", color: "violet" },
  cancel_renew: { text: "取消续费", color: "teal" },
};

function formatStepTiming(ms?: number): string | null {
  if (ms === undefined || ms === null) return null;
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function StepIcon({ step }: { step: ComputedStep }) {
  if (step.status === "done") {
    return (
      <ThemeIcon size={24} radius="xl" color="green" variant="filled">
        <IconCheck size={14} />
      </ThemeIcon>
    );
  }
  if (step.status === "failed") {
    return (
      <ThemeIcon size={24} radius="xl" color="red" variant="filled">
        <IconX size={14} />
      </ThemeIcon>
    );
  }
  if (step.status === "paused") {
    return (
      <ThemeIcon size={24} radius="xl" color="orange" variant="filled">
        <IconAlertTriangle size={14} />
      </ThemeIcon>
    );
  }
  if (step.status === "active") {
    return (
      <ThemeIcon size={24} radius="xl" color="blue" variant="light">
        <Loader size={13} color="blue" />
      </ThemeIcon>
    );
  }
  return (
    <ThemeIcon size={24} radius="xl" color="gray" variant="light">
      <Text size="xs" fw={700}>{step.index}</Text>
    </ThemeIcon>
  );
}

export function QuickPayJobCard({
  job,
  locked,
  debugOnly,
  autoCancelRenew,
  streamMode,
  reconcilingJob,
  onActionReconcile,
  failureSlot,
}: {
  job: QuickPayJob;
  locked: boolean;
  debugOnly?: boolean;
  autoCancelRenew?: boolean;
  streamMode: "idle" | "sse" | "fallback";
  reconcilingJob: boolean;
  onActionReconcile: () => void;
  failureSlot: ReactNode;
}) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  const trackingRef = useRef<{
    jobId: string;
    startedAt: number;
    lastStage: string;
    stageEnteredAt: number;
    highWaterPct: number;
  }>({
    jobId: "",
    startedAt: Date.now(),
    lastStage: "",
    stageEnteredAt: Date.now(),
    highWaterPct: 0,
  });

  const effectiveStage = resolveEffectiveStage(job);
  const running = !isQuickPayTerminal(job) && job.status !== "requires_action";

  if (trackingRef.current.jobId !== job.job_id) {
    const firstEventMs = job.events?.[0]?.created_at ? Date.parse(job.events[0].created_at) : NaN;
    const baseStart = Number.isFinite(firstEventMs) ? firstEventMs : Date.now();
    const lastEventMs = job.events?.length
      ? Date.parse(job.events[job.events.length - 1].created_at)
      : NaN;
    trackingRef.current = {
      jobId: job.job_id,
      startedAt: baseStart,
      lastStage: effectiveStage,
      stageEnteredAt: Number.isFinite(lastEventMs) ? lastEventMs : Date.now(),
      highWaterPct: 0,
    };
  } else if (trackingRef.current.lastStage !== effectiveStage) {
    trackingRef.current.lastStage = effectiveStage;
    trackingRef.current.stageEnteredAt = Date.now();
  }

  useEffect(() => {
    if (!running) return;
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [running, job.job_id]);

  const elapsedMs = Math.max(0, nowMs - trackingRef.current.startedAt);
  const stageElapsedMs = Math.max(0, nowMs - trackingRef.current.stageEnteredAt);

  const flow = computeQuickPayFlow(job, {
    debugOnly,
    autoCancelRenew,
    highWaterPct: job.status === "failed" ? 0 : trackingRef.current.highWaterPct,
    elapsedMs,
    stageElapsedMs,
  });

  if (job.status !== "failed" && flow.percent > trackingRef.current.highWaterPct) {
    trackingRef.current.highWaterPct = flow.percent;
  }

  const headerTitle = job.status === "failed"
    ? (isConfirmedRejection(job, locked)
      ? `第 ${flow.currentStepIndex}/${flow.totalSteps} 步被拒 · ${flow.currentStepTitle}`
      : `第 ${flow.currentStepIndex}/${flow.totalSteps} 步失败 · ${flow.currentStepTitle}`)
    : job.status === "succeeded"
      ? (debugOnly || job.stage === "checkout_created" ? "建单已完成" : "充值完成")
      : `第 ${flow.currentStepIndex}/${flow.totalSteps} 步 · ${flow.currentStepTitle}`;

  const currentTag = STEP_TAG_LABEL[flow.currentStepId];

  return (
    <Card withBorder radius="lg" p="lg" className="workspace-panel">
      <Stack gap="md">
        {/* 顶部标题、进度百分比与耗时/ETA */}
        <Group justify="space-between" align="flex-start" wrap="wrap" gap="xs">
          <div>
            <Group gap="xs" align="center" wrap="wrap">
              <Title order={3}>{headerTitle}</Title>
              <Badge size="lg" variant="filled" color={flow.color}>
                {flow.percent}%
              </Badge>
            </Group>
            <Group gap="xs" mt={6} wrap="wrap">
              {flow.elapsedSeconds !== null && (
                <Badge size="sm" variant="light" color="gray" leftSection={<IconClock size={12} />}>
                  已耗时 {formatSecondsBrief(flow.elapsedSeconds)}
                </Badge>
              )}
              <Badge
                size="sm"
                variant="light"
                color={flow.color === "red" ? "red" : flow.color === "green" ? "green" : "blue"}
              >
                {flow.etaText}
              </Badge>
            </Group>
          </div>

          <Group gap="xs" wrap="wrap">
            {streamMode === "sse" && <Badge color="teal" variant="dot">SSE 实时推送</Badge>}
            {streamMode === "fallback" && <Badge color="gray" variant="light">快照补齐中</Badge>}
            {job.status === "queued" && job.queue_position != null && (
              <Badge color="cyan" variant="light">排队第 {job.queue_position} 位</Badge>
            )}
            <Badge color={flow.color} variant="outline">{job.status}</Badge>
          </Group>
        </Group>

        {/* 0-100% 业务总进度条 */}
        <Progress
          value={flow.percent}
          color={flow.color}
          size="lg"
          radius="xl"
          animated={flow.animated}
          striped={flow.animated}
        />

        {/* 实时执行状态条：醒目展示当前所处业务流程 + 正在干什么具体事情 */}
        <Paper
          withBorder
          radius="md"
          p="xs"
          style={{
            background: flow.color === "red"
              ? "rgba(250, 82, 82, 0.06)"
              : flow.color === "green"
                ? "rgba(64, 192, 87, 0.06)"
                : flow.color === "orange"
                  ? "rgba(253, 126, 20, 0.06)"
                  : "var(--studio-tint)",
          }}
        >
          <Group gap="xs" align="center" wrap="wrap">
            {running ? <Loader size={14} color="blue" /> : <IconActivity size={15} />}
            <Badge size="xs" variant="filled" color={currentTag.color}>
              {currentTag.text}
            </Badge>
            <Text size="xs" fw={600}>{flow.currentSubstageLabel}</Text>
            <Text size="xs" c="dimmed">· {flow.currentActionText}</Text>
          </Group>
        </Paper>

        {/* 精简核心业务大步骤：建单 -> 支付 -> 取消连续订阅（按实际模式动态 1~3 步） */}
        <SimpleGrid cols={{ base: 1, sm: flow.totalSteps }} spacing="xs">
          {flow.steps.map((step) => {
            const timing = formatStepTiming(step.elapsedMs);
            const borderColor = step.status === "failed"
              ? "var(--mantine-color-red-5)"
              : step.status === "active"
                ? "var(--studio-brand)"
                : step.status === "paused"
                  ? "var(--mantine-color-orange-5)"
                  : step.status === "done"
                    ? "var(--mantine-color-green-5)"
                    : undefined;
            const bgColor = step.status === "failed"
              ? "rgba(250, 82, 82, 0.06)"
              : step.status === "active"
                ? "var(--studio-tint)"
                : step.status === "paused"
                  ? "rgba(253, 126, 20, 0.06)"
                  : undefined;

            return (
              <Paper
                key={step.id}
                withBorder
                radius="md"
                p="sm"
                style={{
                  borderColor,
                  background: bgColor,
                  opacity: step.status === "pending" ? 0.65 : 1,
                }}
              >
                <Group gap="xs" align="flex-start" wrap="nowrap">
                  <StepIcon step={step} />
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <Group justify="space-between" gap={6} wrap="nowrap">
                      <Text size="sm" fw={650} truncate>
                        {step.index}. {step.title}
                      </Text>
                      {timing && (
                        <Badge size="xs" variant="light" color="gray" style={{ flexShrink: 0 }}>
                          {timing}
                        </Badge>
                      )}
                    </Group>
                    <Text
                      size="xs"
                      c={step.status === "failed" ? "red" : step.status === "paused" ? "orange.7" : "dimmed"}
                      mt={4}
                    >
                      {step.status === "failed"
                        ? `中断于：${step.substageLabel || step.title}`
                        : step.status === "active" || step.status === "paused"
                          ? (step.actionText || step.shortDesc)
                          : step.status === "done"
                            ? "已完成"
                            : step.shortDesc}
                    </Text>
                  </div>
                </Group>
              </Paper>
            );
          })}
        </SimpleGrid>

        {failureSlot}

        <Group gap="lg" wrap="wrap">
          <Group gap={4}>
            <Text size="sm">作业：</Text>
            <CopyIdBadge value={job.job_id} prefixLength={14} />
          </Group>
          {job.order_id && (
            <Group gap={4}>
              <Text size="sm">订单：</Text>
              <CopyIdBadge value={job.order_id} prefixLength={14} />
            </Group>
          )}
          {job.amount && (
            <Text size="sm">金额：<strong>{job.amount} {job.currency}</strong></Text>
          )}
          {job.purchase_attempt_id && (
            <Group gap={4}>
              <Text size="sm">尝试：</Text>
              <CopyIdBadge value={job.purchase_attempt_id} prefixLength={14} />
            </Group>
          )}
          {job.auto_renew_status === "canceled" && <Badge color="green">自动续费已关闭</Badge>}
          {job.auto_renew_status === "manual_required" && <Badge color="orange">自动续费待人工处理</Badge>}
        </Group>

        {job.timings_ms && Object.keys(job.timings_ms).length > 0 && (
          <Group gap="xs" wrap="wrap">
            {Object.entries(TIMING_LABEL).map(([key, label]) => {
              const value = job.timings_ms?.[key];
              return value === undefined ? null : (
                <Badge key={key} variant="light" color="gray">{label} {value} ms</Badge>
              );
            })}
          </Group>
        )}

        {job.status === "succeeded" && (
          <Alert color="green" icon={<IconCheck size={18} />}>
            {job.stage === "checkout_created" ? "仅建单完成，未提交付款。" : job.message || "充值成功，订阅已到账。"}
          </Alert>
        )}

        {job.status === "requires_action" && (
          <Alert color="orange" title="需要持卡人或本人在浏览器完成验证">
            <Stack gap="xs">
              <Text size="sm">
                {job.next_action?.message
                  || job.message
                  || "发卡行或上游要求完成 3DS / 官方确认；请勿重复扣款，验证完成后点击下方只读核验。"}
              </Text>
              <Group gap="sm">
                {job.next_action?.redirect_url && (
                  <Button
                    component="a"
                    href={job.next_action.redirect_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    referrerPolicy="no-referrer"
                    size="xs"
                    color="orange"
                    leftSection={<IconExternalLink size={14} />}
                  >
                    打开验证页面
                  </Button>
                )}
                <Button
                  size="xs"
                  variant="light"
                  color="blue"
                  loading={reconcilingJob}
                  leftSection={<IconShieldCheck size={14} />}
                  onClick={onActionReconcile}
                >
                  我已完成验证，只读核验结果
                </Button>
              </Group>
            </Stack>
          </Alert>
        )}

        {job.status === "paid_pending_delivery" && (
          <Alert color="orange" title="已扣款，等待订阅权益到账">
            付款已成功，正在等待上游同步订阅权益。后台自动对账器会持续只读复核，请保留订单号、切勿重复付款。
          </Alert>
        )}

        {job.status === "unknown" && (
          <Alert color="orange" title="扣款结果待核对（已自动加锁）" icon={<IconX size={18} />}>
            在支付阶段未收到明确结果，系统已自动锁定该账号防止重复扣款。请前往「资金锁与对账」点击「只读对账」或核对银行账单。
          </Alert>
        )}

        {/* 实时执行流水：逐条显示具体在哪个流程、干了什么事情 */}
        {job.events && job.events.length > 0 && (
          <Paper withBorder radius="md" p="xs">
            <Stack gap={6}>
              <Text size="xs" fw={600} c="dimmed">
                实时执行流水（最近 {Math.min(job.events.length, 12)} 步）
              </Text>
              {job.events.slice(-12).map((event, index) => {
                const isFailEvent = event.stage === "failed" || event.state === "failed";
                const meta = SUBSTAGE_DETAIL[event.stage];
                const stepId: FlowStepId = isFailEvent
                  ? flow.currentStepId
                  : (meta?.step ?? "checkout");
                const tag = STEP_TAG_LABEL[stepId];
                const title = isFailEvent
                  ? `中断于「${flow.currentStepTitle}」· ${flow.currentSubstageLabel}`
                  : (meta?.label || event.stage);
                const detail = isFailEvent
                  ? flow.currentActionText
                  : (meta?.action || (event.message && event.message !== event.stage ? event.message : ""));
                return (
                  <Group key={`${event.created_at}-${index}`} gap={6} wrap="wrap" align="center">
                    <Text size="xs" c="dimmed" style={{ fontVariantNumeric: "tabular-nums" }}>
                      {new Date(event.created_at).toLocaleTimeString()}
                    </Text>
                    <Badge size="xs" variant="light" color={isFailEvent ? "red" : tag.color}>
                      {tag.text}
                    </Badge>
                    <Text size="xs" fw={500} c={isFailEvent ? "red" : undefined}>
                      {title}
                    </Text>
                    {detail && (
                      <Text size="xs" c={isFailEvent ? "red" : "dimmed"}>
                        — {detail}
                      </Text>
                    )}
                  </Group>
                );
              })}
            </Stack>
          </Paper>
        )}
      </Stack>
    </Card>
  );
}
