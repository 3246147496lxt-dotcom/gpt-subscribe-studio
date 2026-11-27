import { Alert, Badge, Button, Group, Modal, Stack, Text } from "@mantine/core";
import { IconAlertCircle } from "@tabler/icons-react";

import type { OrderFailureDetail, OrderLedgerItem } from "../api/ledger";
import { planLabel } from "../plans";
import { planPillTone } from "../utils/orderLedgerFormat";
import { CopyIdBadge } from "./CopyIdBadge";
import { JsonCodeBlock } from "./JsonCodeBlock";
import { FAILURE_LABEL } from "./quick-pay/constants";

export function OrderFailureDetailModal({
  order,
  detail,
  loading,
  fetchError,
  onClose,
  onOpenAuditDetail,
}: {
  order: OrderLedgerItem | null;
  detail: OrderFailureDetail | null;
  loading: boolean;
  fetchError: string | null;
  onClose: () => void;
  onOpenAuditDetail?: (order: OrderLedgerItem) => void;
}) {
  return (
    <Modal
      opened={order !== null}
      onClose={onClose}
      title={
        <Group gap="xs" align="center" wrap="nowrap">
          <span className="failure-modal-icon" aria-hidden="true">
            <IconAlertCircle size={17} stroke={2} />
          </span>
          <Text fw={700} size="md">{detail?.title || "开通失败详情"}</Text>
          {order && (
            <span className={`ledger-pill ledger-pill--plan ledger-pill--plan-${planPillTone(order.plan)}`}>
              {planLabel(order.plan)}
            </span>
          )}
        </Group>
      }
      size="lg"
      radius="lg"
      padding="lg"
      centered
    >
      {order && (
        <Stack gap="md">
          <div className="failure-summary-banner">
            <div className="failure-detail-summary">
              {loading
                ? "正在加载结构化失败诊断…"
                : detail?.summary
                  || order.last_error
                  || (order.failure_code ? FAILURE_LABEL[order.failure_code] : null)
                  || "该订单未成功完成，请结合下方请求与响应排查。"}
            </div>
            <div className="failure-detail-meta">
              <span className="failure-meta-chip">
                错误码 <code>{detail?.error_code || (order.failure_code || "UNKNOWN").toUpperCase()}</code>
              </span>
              <span className="failure-meta-chip">
                阶段 <code>{detail?.stage || order.diagnostics?.stage || order.job_stage || "-"}</code>
              </span>
              {detail?.cf_ray && (
                <span className="failure-meta-chip">
                  CF-Ray <code>{detail.cf_ray}</code>
                </span>
              )}
              {detail?.request_id && (
                <span className="failure-meta-chip">
                  Request-ID <code>{detail.request_id}</code>
                </span>
              )}
            </div>
          </div>

          {fetchError && (
            <Alert color="red" title="加载接口诊断失败">
              <Text size="xs">{fetchError}</Text>
            </Alert>
          )}

          {!loading && detail?.exchange && (
            <div className="failure-exchange-card">
              <div className="failure-exchange-header">
                <div className="failure-exchange-head">
                  <span className="failure-exchange-title">{detail.exchange.title}</span>
                  {detail.exchange.http_status != null && (
                    <Badge
                      size="sm"
                      radius="sm"
                      variant="light"
                      color={
                        detail.exchange.http_status >= 500
                          ? "red"
                          : detail.exchange.http_status >= 400
                            ? "orange"
                            : "teal"
                      }
                    >
                      HTTP {detail.exchange.http_status}
                    </Badge>
                  )}
                </div>
                {(detail.exchange.method || detail.exchange.url) && (
                  <div className="failure-exchange-url-row">
                    {detail.exchange.method && (
                      <span className="failure-method-tag">{detail.exchange.method}</span>
                    )}
                    {detail.exchange.url && (
                      <span className="failure-exchange-url">{detail.exchange.url}</span>
                    )}
                  </div>
                )}
              </div>

              <Stack gap="sm" className="failure-exchange-body">
                {detail.exchange.request != null && (
                  <JsonCodeBlock label="请求参数" value={detail.exchange.request} />
                )}

                {detail.exchange.response != null && (
                  <JsonCodeBlock label="响应内容" value={detail.exchange.response} />
                )}

                {detail.exchange.captured === false && (
                  <Text size="xs" c="dimmed">
                    注：该笔为旧版历史记录，当前展示为已落库的非敏感摘要；新产生的失败订单将自动记录完整脱敏后的上游请求参数与响应内容。
                  </Text>
                )}
              </Stack>
            </div>
          )}

          <Group justify="space-between" align="center" wrap="wrap" gap="xs" pt={4}>
            <Group gap={6}>
              <Text size="xs" c="dimmed">订单编号</Text>
              <CopyIdBadge value={order.id} prefixLength={14} />
            </Group>
            <Group gap="xs">
              {onOpenAuditDetail && (
                <Button size="xs" variant="light" onClick={() => onOpenAuditDetail(order)}>
                  查看完整审计详情
                </Button>
              )}
              <Button size="xs" variant="default" onClick={onClose}>
                关闭
              </Button>
            </Group>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}
