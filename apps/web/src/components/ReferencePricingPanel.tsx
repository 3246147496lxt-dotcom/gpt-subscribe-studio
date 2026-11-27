import {
  Alert,
  Button,
  Group,
  NumberInput,
  Paper,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import { IconAlertCircle, IconCheck, IconRefresh, IconWorld } from "@tabler/icons-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  getAdminPortalSite,
  refreshReferencePrices,
  updateLocalReferencePrice,
  type PortalSiteConfig,
} from "../api/adminOps";
import { isAbortError } from "../api/http";

type PlanMeta = {
  id: string;
  label: string;
  desc: string;
  baseUsd: number;
};

const PLAN_ROWS: PlanMeta[] = [
  { id: "go", label: "Go", desc: "轻量日常高频使用", baseUsd: 8 },
  { id: "plus", label: "Plus", desc: "个人进阶主力订阅", baseUsd: 20 },
  { id: "pro_lite", label: "Pro100", desc: "10x Plus 额度 · 专业研发", baseUsd: 100 },
  { id: "pro", label: "Pro200", desc: "20x Plus 额度 · 深度推理", baseUsd: 200 },
  { id: "pro_max", label: "Pro500", desc: "顶配算力 · Ultrafast 极速", baseUsd: 500 },
];

type RegionMeta = {
  code: string;
  flag: string;
  name: string;
  currency: string;
  taxLabel: string;
  zeroTax?: boolean;
};

const REGION_META_BY_CODE: Record<string, RegionMeta> = {
  PH: { code: "PH", flag: "🇵🇭", name: "菲律宾", currency: "PHP", taxLabel: "12% VAT" },
  EG: { code: "EG", flag: "🇪🇬", name: "埃及", currency: "EGP", taxLabel: "14% VAT" },
  CL: { code: "CL", flag: "🇨🇱", name: "智利", currency: "CLP", taxLabel: "19% IVA" },
  JP: { code: "JP", flag: "🇯🇵", name: "日本", currency: "JPY", taxLabel: "10% JCT" },
  US: { code: "US", flag: "🇺🇸", name: "美国", currency: "USD", taxLabel: "0% 免税州", zeroTax: true },
};

const CORE_LOW_PRICE_REGIONS = ["PH", "EG", "CL"] as const;

function formatFxSourceName(source?: string): string {
  if (!source) return "ER-API";
  if (source.includes("er-api")) return "ER-API";
  if (source.includes("jsdelivr")) return "Currency-API";
  if (source === "seed") return "本地种子汇率";
  return source;
}

function formatLocalAmount(amount: number, currency: string): string {
  const isZeroDecimal = currency === "CLP" || currency === "JPY" || Number.isInteger(amount);
  return amount.toLocaleString("en-US", {
    minimumFractionDigits: isZeroDecimal ? 0 : 2,
    maximumFractionDigits: isZeroDecimal ? 0 : 2,
  });
}

function formatRateValue(rate?: number): string {
  if (rate == null || !Number.isFinite(rate)) return "-";
  return rate.toFixed(6);
}

export function ReferencePricingPanel() {
  const [config, setConfig] = useState<PortalSiteConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshingFx, setRefreshingFx] = useState(false);
  const [savingPrice, setSavingPrice] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [editPlan, setEditPlan] = useState("plus");
  const [editCountry, setEditCountry] = useState("PH");
  const [selectedCell, setSelectedCell] = useState<string | null>(null);
  const [editAmount, setEditAmount] = useState<number | string>(982.14);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const cfg = await getAdminPortalSite(signal);
      if (!signal?.aborted) {
        setConfig(cfg);
      }
    } catch (exc) {
      if (isAbortError(exc)) return;
      setError(exc instanceof Error ? exc.message : "读取动态汇率与地区价格失败");
    } finally {
      if (!signal?.aborted) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    const cell = config?.reference_prices?.[editPlan]?.region_prices?.[editCountry];
    if (cell && Number.isFinite(cell.local_price)) {
      setEditAmount(cell.local_price);
    } else {
      setEditAmount("");
    }
  }, [config, editPlan, editCountry]);

  async function handleRefreshFx() {
    setRefreshingFx(true);
    setError(null);
    setNotice(null);
    try {
      const res = await refreshReferencePrices();
      setNotice(
        `实时汇率同步成功（${formatFxSourceName(res.status.fx_source)} · 1 USD = ${res.status.usd_cny_rate ?? "-"} CNY），已重新计算各套餐最优地区。`,
      );
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "刷新汇率失败");
    } finally {
      setRefreshingFx(false);
    }
  }

  async function handleSaveOverride() {
    const numeric = typeof editAmount === "number" ? editAmount : Number(editAmount);
    if (!Number.isFinite(numeric) || numeric <= 0) {
      setError("请输入大于 0 的免税本地价格。");
      return;
    }
    const meta = REGION_META_BY_CODE[editCountry];
    const currency = meta?.currency || "USD";
    setSavingPrice(true);
    setError(null);
    setNotice(null);
    try {
      await updateLocalReferencePrice({
        plan: editPlan,
        country: editCountry,
        currency,
        amount: numeric,
      });
      setNotice(`已校准 ${editPlan} 在 ${meta?.name || editCountry} 的免税本地价为 ${currency} ${formatLocalAmount(numeric, currency)}。`);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "更新本地价格失败");
    } finally {
      setSavingPrice(false);
    }
  }

  const refStatus = config?.reference_price_status;
  const refPrices = config?.reference_prices ?? {};
  const fxRates = config?.fx_rates ?? {};
  const fxSourceLabel = formatFxSourceName(refStatus?.fx_source);
  const fxDateLabel = refStatus?.fx_date || (config?.price_updated_at ? config.price_updated_at.slice(0, 5) : "-");
  const cnyRate = fxRates.CNY ?? config?.usd_cny_rate;
  const updatedFull = refStatus?.updated_full_label || config?.price_updated_at || "-";

  // 仅展示套餐里的低价结算地区：默认核心低价三区（PH / EG / CL），若某套餐因汇率波动选出其他非美低价区则自动并入。
  const discountRegions = useMemo<RegionMeta[]>(() => {
    const codes: string[] = [...CORE_LOW_PRICE_REGIONS];
    for (const plan of PLAN_ROWS) {
      const winnerCode = refPrices[plan.id]?.country;
      if (winnerCode && winnerCode !== "US" && !codes.includes(winnerCode) && REGION_META_BY_CODE[winnerCode]) {
        codes.push(winnerCode);
      }
    }
    return codes.map((c) => REGION_META_BY_CODE[c]).filter(Boolean);
  }, [refPrices]);

  const editRegionMeta = REGION_META_BY_CODE[editCountry];

  return (
    <Stack gap="md">
      {/* 页面标题（与 gptcharge.online/prices 一致） */}
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
        <div>
          <h1 className="pricing-page-title">地区比价</h1>
          <p className="pricing-page-desc">
            按当天参考汇率，把已启用的免税价折成美金，再换成人民币。
          </p>
        </div>
        <Group gap="xs">
          <Button
            size="xs"
            variant="light"
            leftSection={<IconRefresh size={14} />}
            loading={refreshingFx}
            onClick={() => void handleRefreshFx()}
          >
            刷新汇率
          </Button>
          <Button
            size="xs"
            variant="subtle"
            color="gray"
            loading={loading}
            onClick={() => void load()}
          >
            重新读取
          </Button>
        </Group>
      </Group>

      {error && <Alert color="red" icon={<IconAlertCircle size={16} />} title="操作异常">{error}</Alert>}
      {notice && <Alert color="teal" icon={<IconCheck size={16} />} withCloseButton onClose={() => setNotice(null)}>{notice}</Alert>}

      {/* 1:1 复刻主白卡（.board） */}
      <section className="pricing-board">
        {/* 头部说明栏（.board-head） */}
        <header className="pricing-board-head">
          <div className="pricing-board-title-row">
            <span className="pricing-board-mark" aria-hidden="true">
              <IconWorld size={19} stroke={1.8} />
            </span>
            <div>
              <h2 className="pricing-board-h2">套餐地区美金一览</h2>
              <p className="pricing-board-meta">
                {fxSourceLabel} · 公布日 {fxDateLabel} · 参考中间价，不是卡实扣。
                {cnyRate != null ? ` 1 USD = ${formatRateValue(cnyRate)} CNY。` : " "}
                绿色是该套餐里最低的一格。
              </p>
              <p className="pricing-board-updated">
                <span>上次更新 {updatedFull}</span>
                <span className={refStatus?.stale ? "pricing-board-stale" : "pricing-board-live"}>
                  {refStatus?.stale ? "· 缓存汇率" : "· 实时汇率"}
                </span>
              </p>
            </div>
          </div>
        </header>

        {/* 低价地区汇率与税率卡片（.region-row） */}
        <div className="pricing-region-row">
          {discountRegions.map((reg) => {
            const rate = fxRates[reg.currency];
            return (
              <article key={reg.code} className="pricing-region-card">
                <div className="pricing-region-head">
                  <span className="pricing-flag">{reg.flag}</span>
                  <span>{reg.name} {reg.code} · {reg.currency}</span>
                </div>
                <div className="pricing-region-pills">
                  <span className="pricing-pill-rate">
                    1 USD = {formatRateValue(rate)} {reg.currency}
                  </span>
                  <span className={`pricing-pill-vat${reg.zeroTax ? " pricing-pill-vat--zero" : ""}`}>
                    {reg.taxLabel}
                  </span>
                </div>
                <p className="pricing-region-source">
                  {fxSourceLabel} · 公布 {fxDateLabel}
                </p>
              </article>
            );
          })}
        </div>

        {/* 套餐 × 低价地区三层比价表（.price-table） */}
        <div className="pricing-scroll">
          <table className="pricing-matrix-table">
            <thead>
              <tr>
                <th className="pricing-matrix-plan-col">套餐档位</th>
                {discountRegions.map((reg) => (
                  <th key={reg.code}>
                    <div className="pricing-matrix-region-title">
                      <span className="pricing-flag">{reg.flag}</span>
                      <span>{reg.name}</span>
                    </div>
                    <small className="pricing-matrix-region-sub">
                      {reg.currency} · {reg.taxLabel}
                    </small>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {PLAN_ROWS.map((plan) => {
                const regionMap = refPrices[plan.id]?.region_prices ?? {};
                let bestCode: string | null = null;
                let bestUsd = Number.POSITIVE_INFINITY;
                for (const reg of discountRegions) {
                  const cell = regionMap[reg.code];
                  if (cell && Number.isFinite(cell.usd) && cell.usd < bestUsd) {
                    bestUsd = cell.usd;
                    bestCode = reg.code;
                  }
                }

                return (
                  <tr key={plan.id}>
                    <th className="pricing-matrix-plan-col" scope="row">
                      <div className="pricing-plan-line">
                        <span className={`pricing-plan-pill pricing-plan-pill--${plan.id}`}>
                          {plan.label}
                        </span>
                        <span className="pricing-cycle-pill">月付</span>
                      </div>
                      <p className="pricing-plan-desc">{plan.desc}</p>
                    </th>
                    {discountRegions.map((reg) => {
                      const cell = regionMap[reg.code];
                      const isBest = bestCode === reg.code && cell != null;
                      const isSelected = selectedCell === `${plan.id}:${reg.code}`;
                      return (
                        <td
                          key={reg.code}
                          className={[
                            "pricing-cell",
                            isBest ? "pricing-cell--best" : "",
                            isSelected ? "pricing-cell--selected" : "",
                          ].filter(Boolean).join(" ")}
                          title="点击选中并在下方校准该地区免税价"
                          onClick={() => {
                            setEditPlan(plan.id);
                            setEditCountry(reg.code);
                            setSelectedCell(`${plan.id}:${reg.code}`);
                          }}
                        >
                          {cell ? (
                            <>
                              <div className="pricing-usd-line">
                                <strong className="pricing-usd">${cell.usd.toFixed(2)}</strong>
                                {isBest && <span className="pricing-best-badge">★ 最优</span>}
                              </div>
                              <div className="pricing-cny">¥{cell.cny.toFixed(2)}</div>
                              <div className="pricing-local">
                                免税 {cell.currency} {formatLocalAmount(cell.local_price, cell.currency)}
                              </div>
                            </>
                          ) : (
                            <span className="pricing-closed">未开通</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {/* 独立后台校准条（不打扰上方纯净比价白卡） */}
      <Paper withBorder radius="lg" p="md" className="workspace-panel">
        <Stack gap="sm">
          <div>
            <Text fw={600} size="sm">手动校准单地区免税本地价</Text>
            <Text size="xs" c="dimmed">
              点击上方比价表任意格子即可带入对应套餐与地区；保存后立即重新折算并同步到门户。真实非促销结账也会自动回写校准。
            </Text>
          </div>
          <Group align="flex-end" wrap="wrap" gap="sm">
            <Select
              size="xs"
              w={190}
              label="套餐"
              value={editPlan}
              onChange={(v) => setEditPlan(v || "plus")}
              data={PLAN_ROWS.map((p) => ({
                value: p.id,
                label: `${p.label}（美区 $${p.baseUsd}）`,
              }))}
            />
            <Select
              size="xs"
              w={175}
              label="低价结算地区"
              value={editCountry}
              onChange={(v) => setEditCountry(v || "PH")}
              data={discountRegions.map((r) => ({
                value: r.code,
                label: `${r.flag} ${r.name} (${r.code}) · ${r.currency}`,
              }))}
            />
            <NumberInput
              size="xs"
              w={190}
              label={`免税本地金额（${editRegionMeta?.currency || "PHP"}）`}
              placeholder="输入不含税本地价"
              min={0.01}
              decimalScale={2}
              value={editAmount}
              onChange={setEditAmount}
            />
            <Button size="xs" loading={savingPrice} onClick={() => void handleSaveOverride()}>
              保存价格校准
            </Button>
          </Group>
        </Stack>
      </Paper>
    </Stack>
  );
}
