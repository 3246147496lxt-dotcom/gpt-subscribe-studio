import { Alert, Button, Group, Paper, SegmentedControl, Select, Stack, Text } from "@mantine/core";
import { IconAlertCircle, IconCheck } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";

import {
  createOrder,
  checkPromoEligibility,
  getOrder,
  isHostedOrder,
  payOrder,
  prepareTeam,
  quoteTeam,
  pollTeam,
  verifyOrder,
  type BillingInput,
  type Order,
  type Plan,
  type PromoEligibility,
} from "../api/client";
import { forgetPurchase, openNonTeamPurchase, readPurchaseIntent } from "../api/purchaseIntent";
import { SUCCESS_STATUSES, isSettled } from "../api/history";
import { subscribeOrderUpdates, type OrderUpdateEvent } from "../api/events";
import { loadStripe, type StripeCardHandle } from "../card-payment/CardFields";
import { OrderSummary } from "../components/OrderSummary";
import { OrderProgress, type CheckoutClock, type ProgressActivity } from "../components/OrderProgress";
import { getOrderFlow } from "../orderFlow";
import { PlanPicker, type TeamValues } from "../components/PlanPicker";
import { SettlementRegion } from "../components/SettlementRegion";
import { ResultPanel } from "../components/ResultPanel";
import { SessionField } from "../components/SessionField";
import { verifiedPayerBilling } from "../components/FixedPayerBilling";
import { ProtocolPayPanel } from "../components/ProtocolPayPanel";
import { CheckoutLinkCard } from "../components/CheckoutLinkCard";
import { isValidCardExpiry } from "../card-payment/cardExpiry";
import { emptyTeamCard, type TeamCardDraft } from "../card-payment/TeamCardForm";

interface Props {
  onRecord: (order: Order) => void;
}

const requestActivities = {
  create: { label: "正在创建结账，等待建单回执" },
  restore: { label: "正在读取原订单，不会重新建单" },
  quote: { label: "正在读取应付金额", step: "quote" },
  verify: { label: "正在查询订阅状态", step: "subscription" },
  payment: { label: "正在处理付款请求", step: "payment" },
} satisfies Record<string, ProgressActivity>;

export function OrderNew({ onRecord }: Props) {
  const [plan, setPlan] = useState<Plan>("plus");
  const [country, setCountry] = useState("");
  const [exitCountry, setExitCountry] = useState("");
  const [session, setSession] = useState("");
  const [team, setTeam] = useState<TeamValues>({
    seats: 5,
    workspace: "Team Workspace",
    promoCode: "",
    interval: "month",
  });
  const [order, setOrder] = useState<Order | null>(null);
  const [incompleteCheckout, setIncompleteCheckout] = useState<Order | null>(null);
  const currentOrderId = useRef<string | null>(null);
  currentOrderId.current = order?.id || null;

  // Default the exit country to the proxy currently configured by the
  // operator. Billing still comes from the server-side verified US profile.
  useEffect(() => {
    let cancelled = false;
    fetch("/api/settings/proxies")
      .then((res) => res.json())
      .then((data) => {
        const region = String(data?.checkout?.region || "").toUpperCase();
        if (!cancelled && /^[A-Z]{2}$/.test(region)) {
          setExitCountry((current) => current || region);
        }
      })
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, []);
  const [result, setResult] = useState<Order | null>(null);
  const [readOnlyOrderId, setReadOnlyOrderId] = useState<string | null>(null);
  const [card, setCard] = useState<StripeCardHandle | null>(null);
  const [teamCard, setTeamCard] = useState<TeamCardDraft>(emptyTeamCard);
  const [teamBilling, setTeamBilling] = useState<BillingInput>({
    name: "", line1: "", city: "", state: "", postal_code: "", country: "US",
  });
  const [clock, setClock] = useState<CheckoutClock | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [promo, setPromo] = useState<PromoEligibility | null>(null);
  const [promoCampaignId, setPromoCampaignId] = useState("");
  const [promoBusy, setPromoBusy] = useState(false);
  const [promoError, setPromoError] = useState<string | null>(null);
  const [request, setRequest] = useState<keyof typeof requestActivities | null>(null);
  const busy = request !== null;
  const [protocolBusy, setProtocolBusy] = useState(false);
  // 共享支付容器；pmMode 决定协议提交或 Stripe 输入框。
  const payMode = "protocol" as const;
  // 取 pm_ 的方式：server = 服务端提交卡号；stripe = 浏览器 Stripe 输入框
  // 放在顶层，提链前就能选，选完带进支付面板
  const [pmMode, setPmMode] = useState<"server" | "stripe">("server");
  const hosted = order ? isHostedOrder(order) : plan === "team";
  const teamOrder = (order?.plan ?? plan) === "team";
  // Team 托管结账只要拿到 Stripe 公钥，就能和 Plus 一样站内填卡支付；
  // 没有公钥才退回「打开长链手动付款」。
  const linkOnly = Boolean(order && hosted && !order.publishable_key);
  const step = result ? 2 : order ? 1 : 0;
  const displayOrder = order && result?.id === order.id ? result : order ?? incompleteCheckout;
  const progress = getOrderFlow(displayOrder, clock !== null, error, plan);
  const finished = progress.tone === "complete" || progress.tone === "failed"
    || Boolean(displayOrder && isSettled(displayOrder.status)) || Boolean(incompleteCheckout && !order);
  const workingStep = progress.steps.find(item => item.state === "active");
  let activity: ProgressActivity | null = null;
  if (clock && !finished) {
    if (request) activity = requestActivities[request];
    else if (workingStep) activity = { label: workingStep.status, step: workingStep.id };
    else if (protocolBusy) activity = { label: "正在等待操作回执" };
  }

  useEffect(() => {
    if (!clock || clock.finishedAt !== null || !finished) return;
    setClock({ ...clock, finishedAt: Date.now() });
  }, [clock, finished]);

  // Unknown or inaccessible storage must never look like permission to start a new purchase.
  let purchaseKind: "pending" | "order" | "unavailable" | undefined;
  if (!teamOrder) {
    try { purchaseKind = readPurchaseIntent(window.sessionStorage)?.kind; }
    catch { purchaseKind = "unavailable"; }
  }

  function finish(next: Order) {
    setResult(next);
    onRecord(next);
  }

  async function recheckDelivery() {
    if (!result || busy) return;
    setRequest("verify");
    setError(null);
    try {
      const next = await verifyOrder(result.id, true);
      setResult(next);
      onRecord(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "核验失败，请稍后重试");
    } finally {
      setRequest(null);
    }
  }

  function reset() {
    if (busy || protocolBusy) return;
    if (!teamOrder && purchaseKind) {
      if (!window.confirm("开始另一笔购买？原订单不会被撤销。若付款结果不明，请先核对原单。")) return;
      try { forgetPurchase(window.sessionStorage); }
      catch { setError("无法重置当前标签页的购买标记，未开始新购买。"); return; }
    }
    setOrder(null);
    setIncompleteCheckout(null);
    setClock(null);
    setResult(null);
    setReadOnlyOrderId(null);
    setCard(null);
    setTeamCard(emptyTeamCard());
    setTeamBilling({ name: "", line1: "", city: "", state: "", postal_code: "", country: "US" });
    setSession("");
    setExitCountry("");
    setError(null);
    setPromo(null);
    setPromoCampaignId("");
    setPromoError(null);
    setProtocolBusy(false);
    const url = new URL(window.location.href);
    url.searchParams.delete("setup_order");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }

  // 用 SSE 实时订阅订单状态，替代轮询
  useEffect(() => {
    if (!result || isSettled(result.status)) return;
    
    const id = result.id;
    console.log(`[订单 ${id}] 开始 SSE 订阅`);
    
    const unsubscribe = subscribeOrderUpdates(id, async (event: OrderUpdateEvent) => {
      console.log(`[订单 ${id}] SSE 更新:`, event);
      
      // 收到状态更新后，重新获取完整订单数据
      try {
        const next = await getOrder(id);
        setResult(next);
        if (isSettled(next.status)) {
          onRecord(next);
          console.log(`[订单 ${id}] 已结算，停止订阅`);
        }
      } catch (err) {
        console.error(`[订单 ${id}] 获取最新状态失败:`, err);
      }
    });
    
    return () => {
      console.log(`[订单 ${id}] 取消 SSE 订阅`);
      unsubscribe();
    };
  }, [result, onRecord]);

  // 3DS2（use_stripe_sdk）没有跳转链接：用 PaymentIntent 的 client_secret 让 Stripe.js 弹出验证窗口，
  // 验证结束后反复查询订阅状态，直到生效或超时。
  const handledSecret = useRef("");
  useEffect(() => {
    const secret = result?.next_action?.client_secret;
    if (!result || result.id === readOnlyOrderId || result.plan !== "team" || result.status !== "requires_action" || !secret || !result.publishable_key) return;
    if (handledSecret.current === secret) return;
    handledSecret.current = secret;
    const id = result.id;
    const key = result.publishable_key;
    void (async () => {
      try {
        await loadStripe(result.plan === "team" ? "v3" : "basil");
        if (!window.Stripe) throw new Error("Stripe.js 不可用");
        const stripe = window.Stripe(key);
        if (!stripe.handleNextAction) throw new Error("Stripe.js 未提供验证方法，请打开官方结账页完成验证");
        const outcome = await stripe.handleNextAction({ clientSecret: secret });
        if (outcome?.error) throw new Error(outcome.error.message || "3DS 验证失败");
        // 3DS 验证通过即视为扣款成功：直接按已支付交付，订阅查询只作附带信息。
        const paid = outcome?.paymentIntent?.status === "succeeded";
        const hostedTeam = result.plan === "team";
        let latest = hostedTeam ? await pollTeam(id) : await verifyOrder(id, paid);
        for (let i = 0; i < 10 && !isSettled(latest.status) && (hostedTeam || !paid) && !latest.subscription?.active; i += 1) {
          setResult(latest);
          await new Promise((resolve) => window.setTimeout(resolve, 3000));
          latest = hostedTeam ? await pollTeam(id) : await verifyOrder(id);
        }
        setResult(latest);
        if (isSettled(latest.status)) onRecord(latest);
      } catch (err) {
        handledSecret.current = "";
        const message = err instanceof Error ? err.message : "3DS 验证失败";
        // Keep the same secret marked as handled; retry requires a new action, not a render loop.
        handledSecret.current = secret;
        setResult((prev) => (prev ? { ...prev, last_error: `${message}；可打开官方结账页完成验证后查询。` } : prev));
      }
    })();
  }, [result, onRecord, pmMode, readOnlyOrderId]);

  // 支付已提交但订阅还没确认（verifying）：自动重查，避免页面一直转圈。
  const verifyingId = result && result.plan !== "team" && (result.plan !== "plus" || pmMode === "stripe") && result.status === "verifying" ? result.id : "";
  useEffect(() => {
    if (!verifyingId) return;
    let stopped = false;
    void (async () => {
      for (let i = 0; i < 15 && !stopped; i += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 4000));
        if (stopped) return;
        try {
          const latest = verifyingId === readOnlyOrderId
            ? await getOrder(verifyingId)
            : await verifyOrder(verifyingId);
          if (stopped) return;
          setResult(latest);
          if (isSettled(latest.status)) {
            onRecord(latest);
            return;
          }
        } catch {
          /* keep retrying */
        }
      }
    })();
    return () => {
      stopped = true;
    };
  }, [verifyingId, onRecord, readOnlyOrderId]);

  const teamPendingId = result?.plan === "team" && ["paying", "requires_action", "unknown", "verifying"].includes(result.status) ? result.id : "";
  useEffect(() => {
    if (!teamPendingId) return;
    let stopped = false;
    let timer: number | undefined;
    const update = async () => {
      try {
        const latest = await pollTeam(teamPendingId);
        if (stopped) return;
        setResult(latest);
        if (isSettled(latest.status)) { onRecord(latest); return; }
      } catch { /* The confirm is never replayed after a poll failure. */ }
      if (!stopped) timer = window.setTimeout(update, 4000);
    };
    timer = window.setTimeout(update, 4000);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [teamPendingId, onRecord]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy || protocolBusy || order?.plan === "plus" || (order && order.id === readOnlyOrderId)) return;
    setError(null);
    setRequest(!order ? (purchaseKind === "order" ? "restore" : "create") : linkOnly ? "verify" : "payment");
    try {
      if (!order) {
        setIncompleteCheckout(null);
        setClock(null);
        if (purchaseKind === "unavailable") {
          setError("当前标签页无法读取购买标记，请先核对原单；未提交建单。");
          return;
        }
        if (purchaseKind !== "order" && !session.trim()) {
          setError("请粘贴 ChatGPT session。");
          return;
        }
        if (purchaseKind !== "order" && plan === "team" && !country) {
          setError("请先选择创建结账的地区。");
          return;
        }
        if (plan === "team" && (!Number.isInteger(team.seats) || team.seats < 2)) {
          setError("Team 需要填写席位，最少 2 人。");
          return;
        }
        setClock({ startedAt: Date.now(), finishedAt: null });
        const create = (key: string) => createOrder({
          ...(plan === "team"
            ? { plan, billing_country: country, exit_country: exitCountry || country }
            : { plan, exit_country: exitCountry || undefined }),
          session,
          promo_code: plan === "team" ? team.promoCode.trim() : "",
          promo_campaign_id: plan === "plus" ? promoCampaignId : "",
          workspace_name: plan === "team" ? team.workspace.trim() : "",
          seat_quantity: plan === "team" ? team.seats : undefined,
          price_interval: plan === "team" ? team.interval : undefined,
        }, key);
        const created = plan === "team"
          ? await create(crypto.randomUUID())
          : await openNonTeamPurchase(window.sessionStorage, create, getOrder);
        if (created.plan !== "team") {
          setPlan(created.plan as Plan);
          setCountry(created.billing_country);
          if (created.status !== "awaiting_payment" || (created.payment_status && created.payment_status !== "unpaid")) {
            // Restoring an attempted payment must not mount a card form or resume a 3DS submission.
            setReadOnlyOrderId(created.id);
            setOrder(created);
            setSession("");
            finish(created);
            return;
          }
        }
        if (created.plan !== "plus" && !created.publishable_key && !isHostedOrder(created)) {
          // Keep the returned receipt visible even if the checkout form cannot open.
          setIncompleteCheckout(created);
          throw new Error("结账已创建，但没有拿到 Stripe 公钥，无法加载安全输入框。");
        }
        setOrder(created);
        setSession("");
        if (plan === "team") {
          setRequest("quote");
          try {
            // 只要拿到 cs_ 就能 init 报价，与有没有 publishable_key 无关。
            // 全协议模式不需要 pk，所以这里不能拿 pk 当条件。
            setOrder(await quoteTeam(created.id));
          } catch (err) {
            setOrder({ ...created, quote_error: err instanceof Error ? err.message : "报价读取失败，本步骤未提交付款" });
          }
        }
        return;
      }
      if (linkOnly) {
        const checked = await verifyOrder(order.id);
        setOrder(checked);
        if (checked.subscription?.active || SUCCESS_STATUSES.includes(checked.status)) {
          finish(checked);
        } else {
          setError(checked.last_error || "还没开通。请先打开长链完成支付，再点查询。");
        }
        return;
      }
      if (!teamOrder) {
        setError("浏览器支付标识不能核验其账单归属，付款未提交。");
        return;
      }
      const billing = teamBilling;
      if (!billing || !billing.name.trim() || !billing.line1.trim() || !billing.city.trim() || !billing.postal_code.trim()) {
        setError("请填写完整账单地址（姓名、街道、城市、邮编）。");
        return;
      }
      if (teamOrder) {
        const draft = teamCard;
        if (!/^\d{12,19}$/.test(draft.number.replace(/[\s-]/g, "")) || !/^\d{3,4}$/.test(draft.cvc)
          || !isValidCardExpiry(draft)) {
          setError("请检查卡号、有效期（MM/YY）和 CVC。");
          return;
        }
      }
      if (teamOrder && billing.country === "US" && !billing.state.trim()) {
        setError("请填写账单地址的州。");
        return;
      }
      let teamConfirm;
      let token: string;
      if (teamOrder) {
        const prepared = await prepareTeam(order.id, billing);
        setOrder(prepared.order);
        if (order.amount == null || prepared.order.amount !== order.amount || prepared.order.currency !== order.currency) {
          setError("应付金额已按账单地址更新，请核对右侧金额后再次点击确认支付。本次尚未提交卡片或付款。");
          return;
        }
        // The service tokenizes the card with Stripe; no PaymentMethod is created in the browser.
        token = "";
        teamConfirm = {
          team_context_id: prepared.context.context_id,
          card: {
            number: teamCard.number.replace(/[\s-]/g, ""),
            cvc: teamCard.cvc,
            exp_month: teamCard.exp_month,
            exp_year: teamCard.exp_year,
          },
        };
        setTeamCard(emptyTeamCard());
      } else {
        token = await card!.tokenize(billing);
      }
      try {
        finish(await payOrder(order.id, token, billing, teamConfirm));
      } catch (err) {
        const message = err instanceof Error ? err.message : "开通失败";
        const latest = await getOrder(order.id).catch(() => order);
        finish({ ...latest, status: isSettled(latest.status) ? latest.status : "unknown", last_error: message });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "开通失败");
      if (teamOrder && order) {
        const latest = await getOrder(order.id).catch(() => null);
        if (latest) setOrder(latest);
      }
    } finally {
      setRequest(null);
    }
  }

  const errorAlert = error && step < 2 ? (
    <Alert mt="md" color="red" variant="light" icon={<IconAlertCircle size={18} />} role="alert">
      {error} <a className="flow-error-link" href="#order-progress">查看流程</a>
    </Alert>
  ) : null;
  // 全协议模式的出口：优先订单 exit_country，其次账单国
  const protocolExit = (order?.exit_country || order?.billing?.country || order?.billing_country || country || "").toUpperCase() || undefined;
  // 全协议模式在 step 1 由 ProtocolPayPanel 自带按钮，底部不再重复一个「确认支付」
  // 只有「协议模式 + 已建单」才由面板自带按钮；未建单时仍需要底部「生成结账」按钮
  const protocolStep = payMode === "protocol" && step === 1 && Boolean(order);
  const primaryAction = step < 2 && !protocolStep ? (
    <Group mt={teamOrder ? "xs" : "md"} justify="flex-end">
      <Button type="submit" size="md" loading={busy}
        disabled={Boolean(order && !linkOnly && !teamOrder && !card)} fullWidth={teamOrder}>
        {!order
          ? plan === "team" ? `生成 Team 结账链接（${team.seats} 席）` : purchaseKind === "order" ? "查看原订单" : "创建结账"
          : linkOnly ? "我已支付，查询订阅" : "确认支付"}
      </Button>
    </Group>
  ) : null;
  return (
    <form onSubmit={submit}>
      <div className={`order-layout${step === 1 && order && payMode === "protocol" ? " order-layout--split" : ""}`}>
        <Paper className="order-main">
          {step === 1 && (!protocolStep || (order?.plan !== "plus" && order?.checkout_url)) && (
            <h2 className="payment-stage-title">订单结账</h2>
          )}

          {order?.plan === "team" && order.checkout_url && order.id !== readOnlyOrderId && step >= 1 && (
            <CheckoutLinkCard url={order.checkout_url} />
          )}

          {step === 0 && (
            <Stack gap={teamOrder ? "md" : "lg"}>
              {purchaseKind && (
                <Alert color="yellow" title={purchaseKind === "order" ? "本标签页已有关联订单" : "当前购买尚未结束"}>
                  {purchaseKind === "order" ? "查看原订单无需再次填写 Session，不会重新建单或付款。"
                    : purchaseKind === "pending" ? "请沿用原来的 Session 和选项重试，系统会核对原单，不会自动付款。"
                    : "无法读取购买标记，建单已暂停。请先核对原单。"}
                  <Button type="button" variant="subtle" size="xs" disabled={busy} onClick={reset}>开始另一笔购买</Button>
                </Alert>
              )}
              <PlanPicker
                compact={teamOrder}
                plan={plan}
                onPlan={(next) => {
                  setPlan(next);
                  if (next === "team" && !country) setCountry("EG");
                  if (next !== "team") setTeamCard(emptyTeamCard());
                  if (next !== "plus") {
                    setPromo(null);
                    setPromoCampaignId("");
                    setPromoError(null);
                  }
                }}
                country={country}
                onCountry={setCountry}
                team={team}
                onTeam={setTeam}
                disabled={busy}
              />
              <SettlementRegion
                team={plan === "team"}
                value={exitCountry}
                onChange={setExitCountry}
                disabled={busy || Boolean(order)}
              />
              <SessionField value={session} onChange={setSession} team={plan === "team"} disabled={busy} />
              {teamOrder ? (
                <div>
                  <Text size="sm" fw={600} mb={6}>支付方式</Text>
                  <SegmentedControl
                    fullWidth
                    value={pmMode}
                    onChange={(value) => setPmMode(value as "server" | "stripe")}
                    disabled={busy || Boolean(order)}
                    data={[
                      { value: "server", label: "协议提交（服务端提交卡号）" },
                      { value: "stripe", label: "Stripe 输入框（浏览器换 pm_）" },
                    ]}
                  />
                  <Text size="xs" c="dimmed" mt={6}>
                    协议提交由服务端向 Stripe 换 pm_；Stripe 输入框让卡号只进 Stripe iframe。
                  </Text>
                </div>
              ) : (
                <Text size="sm">支付方式：由服务端提交卡号。</Text>
              )}
              {plan === "plus" && (
                <Paper withBorder p="md" radius="md">
                  <Stack gap="sm">
                    <Group justify="space-between" align="center">
                      <Text fw={600}>Plus 优惠资格</Text>
                      <Button
                        type="button"
                        variant="light"
                        size="xs"
                        loading={promoBusy}
                        disabled={busy || !session.trim()}
                        onClick={async () => {
                          setPromoBusy(true);
                          setPromoError(null);
                          try {
                            const result = await checkPromoEligibility(session.trim());
                            setPromo(result);
                            const firstPlus = result.campaigns.find((item) => item.key === "plus");
                            setPromoCampaignId(firstPlus?.campaign_id ?? "");
                          } catch (err) {
                            setPromo(null);
                            setPromoCampaignId("");
                            setPromoError(err instanceof Error ? err.message : "优惠资格查询失败");
                          } finally {
                            setPromoBusy(false);
                          }
                        }}
                      >
                        检测资格
                      </Button>
                    </Group>
                    {promoError && <Alert color="red">{promoError}</Alert>}
                    {promo?.plus_eligible && promo.campaigns.some((item) => item.key === "plus") && (
                      <Select
                        label="选择活动"
                        description="创建结账时会提交上游返回的 campaign id。"
                        value={promoCampaignId || null}
                        onChange={(value) => setPromoCampaignId(value ?? "")}
                        data={promo.campaigns
                          .filter((item) => item.key === "plus")
                          .map((item) => ({
                            value: item.campaign_id,
                            label: `${item.discount_percentage ?? 0}% off · ${item.duration_num_periods ?? "?"} ${item.duration_period ?? "month"} · ${item.title ?? item.campaign_id}`,
                          }))}
                      />
                    )}
                    {promo && (!promo.plus_eligible || !promo.campaigns.some((item) => item.key === "plus")) && (
                      <Text size="sm" c="dimmed">当前账号没有 Plus 活动资格，将按正价结账。</Text>
                    )}
                    {!promo && !promoError && (
                      <Text size="xs" c="dimmed">检测会通过服务端配置的代理读取账号活动；不会创建结账。</Text>
                    )}
                  </Stack>
                </Paper>
              )}
              <Text size="xs" c="dimmed">
                {plan === "plus"
                  ? pmMode === "server" ? "此模式的付款请求使用随机生成的设备标识。建议使用官方结账。" : "此方式不能核验付款方账单归属，无法提交。"
                  : "结账地区由上面选的地区决定。session 只在内存中使用，不写数据库。"}
              </Text>
            </Stack>
          )}

          {step === 1 && order && linkOnly && teamOrder && (
            <Stack gap="md">
              <Alert color="green" variant="light" icon={<IconCheck size={18} />} title="结账已创建">
                官方付款链接已生成（见上方卡片）。金额、折扣和税费以官方结账页为准，
                付款后回到本页点「我已支付，查询订阅」。
              </Alert>
              <Group>
                <Button type="button" variant="light" loading={busy} onClick={async () => {
                  setRequest("verify");
                  setError(null);
                  try {
                    const checked = await verifyOrder(order.id);
                    setOrder(checked);
                    if (checked.subscription?.active || SUCCESS_STATUSES.includes(checked.status)) finish(checked);
                    else setError(checked.last_error || "订阅尚未生效，请稍后再查询。");
                  } catch (err) {
                    setError(err instanceof Error ? err.message : "查询订阅失败");
                  } finally {
                    setRequest(null);
                  }
                }}>
                  我已支付，查询订阅
                </Button>
              </Group>
            </Stack>
          )}



          {step === 2 && result && (
            <ResultPanel
              order={result}
              protocolOnly={result.id === readOnlyOrderId || (result.plan === "plus" && pmMode === "server")}
              onReset={reset}
              onVerify={recheckDelivery}
              verifying={busy}
            />
          )}

          {errorAlert}
          {primaryAction}
        </Paper>

        {step === 1 && order && payMode === "protocol" && (
          <div className="order-payment">
            <Stack gap="md">
              <ProtocolPayPanel
                key={order.plan === "plus" ? order.id : `${order.id}:${order.checkout_url || ""}`}
                orderId={order.id}
                bindBeforePay={order.plan === "plus" && pmMode === "server"}
                browserPayment={order.plan === "plus" && pmMode === "stripe"}
                submissionDisabled={order.status !== "awaiting_payment"}
                teamOrder={teamOrder}
                billingVerified={order.billing_verified}
                sessionEmail={teamOrder ? order.session_email : null}
                billing={!teamOrder ? verifiedPayerBilling(order.billing_verified, order.billing) : order.billing ? {
                  name: order.billing.name ?? "",
                  email: order.session_email ?? "",
                  line1: order.billing.line1 ?? "",
                  city: order.billing.city ?? "",
                  state: order.billing.state ?? "",
                  postal_code: order.billing.postal_code ?? "",
                  country: order.billing.country ?? order.billing_country,
                } : null}
                pmMode={pmMode}
                onPmMode={setPmMode}
                requireSaveChoice={Boolean(order.promo_campaign_id)}
                country={order.billing_country || country}
                amount={order.amount}
                currency={order.currency}
                quotationError={order.quote_error}
                exitLabel={protocolExit}
                publishableKey={order.publishable_key}
                checkoutUrl={order.checkout_url}
                disabled={busy || (order.plan !== "plus" && order.status !== "awaiting_payment")}
                onBusyChange={setProtocolBusy}
                onRefreshQuote={!teamOrder ? undefined : async () => {
                  if (!order) return;
                  setRequest("quote");
                  setError(null);
                  try {
                    // 报价带账单地址才能算准税额；订单上已有就用它
                    const known = order.billing;
                    const complete = Boolean(
                      known?.name && known?.line1 && known?.city && known?.postal_code && known?.country,
                    );
                    setOrder(await quoteTeam(order.id, complete ? (known as BillingInput) : undefined));
                  } catch (err) {
                    setError(err instanceof Error ? err.message : "报价读取失败");
                  } finally {
                    setRequest(null);
                  }
                }}
                onDone={(out) => {
                  void (async () => {
                    const latest = out.order || await getOrder(order.id).catch(() => null);
                    if (!latest || latest.id !== currentOrderId.current) return;
                    setOrder((current) => current?.id === latest.id ? latest : current);
                    if (latest.subscription?.active || SUCCESS_STATUSES.includes(latest.status)
                      || ((latest.plan !== "plus" || pmMode === "stripe") && isSettled(latest.status))
                       || (latest.plan === "plus" && pmMode === "stripe" && latest.status !== "awaiting_payment")) finish(latest);
                  })();
                }}
              />
            </Stack>
          </div>
        )}

        <div className="order-side">
          <OrderSummary plan={plan} country={plan === "team" ? country : "由已核验账单确定"} order={displayOrder} seats={team.seats} progress={progress} />
          {teamOrder && order && step < 2 && order.status === "awaiting_payment" && (
            <Button variant="light" fullWidth mt="xs" loading={busy} type="button" onClick={async () => {
              setRequest("quote"); setError(null);
              try {
                const complete = Boolean(teamBilling.name.trim() && teamBilling.line1.trim()
                  && teamBilling.city.trim() && teamBilling.postal_code.trim());
                setOrder(await quoteTeam(order.id, complete ? teamBilling : undefined));
              } catch (err) { setError(err instanceof Error ? err.message : "报价读取失败"); }
              finally { setRequest(null); }
            }}>刷新应付金额</Button>
          )}
          {order && step < 2 && (
            <Stack gap="xs" mt="xs">
              <Text size="xs" c="dimmed">想改套餐或地区？点击「重新开始」。</Text>
              <Button type="button" variant="subtle" disabled={busy || protocolBusy} onClick={reset}>重新开始</Button>
            </Stack>
          )}
        </div>
      </div>
      <OrderProgress progress={progress} clock={clock} activity={activity} />
    </form>
  );
}
