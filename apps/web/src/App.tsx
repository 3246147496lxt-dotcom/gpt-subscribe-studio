import {
  ActionIcon,
  AppShell,
  Badge,
  Burger,
  Center,
  Divider,
  Group,
  Loader,
  Modal,
  NavLink,
  ScrollArea,
  Stack,
  Text,
  Tooltip,
  useComputedColorScheme,
  useMantineColorScheme,
} from "@mantine/core";
import {
  IconActivity,
  IconAdjustments,
  IconBell,
  IconBolt,
  IconChecklist,
  IconCode,
  IconCurrencyDollar,
  IconExternalLink,
  IconHelpCircle,
  IconLayoutDashboard,
  IconListDetails,
  IconLockOpen,
  IconLogout,
  IconMapPin,
  IconMoon,
  IconNetwork,
  IconSun,
  IconUserCircle,
  IconWorld,
} from "@tabler/icons-react";
import { lazy, Suspense, useCallback, useEffect, useState, type ReactNode } from "react";

import { getHealth, type HealthSnapshot } from "./api/adminOps";
import type { Order } from "./api/client";
import { loadHistory, recordOrder } from "./api/history";
import { getPaymentLocks } from "./api/ledger";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { OpsOverviewPanel } from "./components/OpsOverviewPanel";

const SystemDiagnosticsPanel = lazy(() =>
  import("./components/SystemDiagnosticsModal").then((m) => ({ default: m.SystemDiagnosticsPanel })),
);
const AccountInspectorPanel = lazy(() =>
  import("./components/AccountDetailModal").then((m) => ({ default: m.AccountInspectorPanel })),
);
const PortalSitePanel = lazy(() =>
  import("./components/PortalSitePanel").then((m) => ({ default: m.PortalSitePanel })),
);
const ReferencePricingPanel = lazy(() =>
  import("./components/ReferencePricingPanel").then((m) => ({ default: m.ReferencePricingPanel })),
);
const BatchPreflightPanel = lazy(() =>
  import("./components/BatchPreflightPanel").then((m) => ({ default: m.BatchPreflightPanel })),
);
const LocksPanel = lazy(() =>
  import("./components/LocksPanel").then((m) => ({ default: m.LocksPanel })),
);
const ProxySettings = lazy(() =>
  import("./components/ProxySettings").then((m) => ({ default: m.ProxySettings })),
);
const FixedBillingSettings = lazy(() =>
  import("./components/FixedBillingSettings").then((m) => ({ default: m.FixedBillingSettings })),
);
const RuntimeFlagsPanel = lazy(() =>
  import("./components/RuntimeFlagsPanel").then((m) => ({ default: m.RuntimeFlagsPanel })),
);
const ServerLedgerPanel = lazy(() =>
  import("./components/ServerLedgerPanel").then((m) => ({ default: m.ServerLedgerPanel })),
);
const WebhookSettings = lazy(() =>
  import("./components/WebhookSettings").then((m) => ({ default: m.WebhookSettings })),
);
const QuickPay = lazy(() =>
  import("./pages/QuickPay").then((m) => ({ default: m.QuickPay })),
);
const OrderNew = lazy(() =>
  import("./pages/OrderNew").then((m) => ({ default: m.OrderNew })),
);

type NavTab =
  | "overview"
  | "diagnostics"
  | "history"
  | "locks"
  | "account"
  | "portal-site"
  | "pricing"
  | "proxy"
  | "billing"
  | "flags"
  | "webhooks"
  | "batch"
  | "quick"
  | "order";

const VALID_TABS = new Set<string>([
  "overview",
  "diagnostics",
  "history",
  "locks",
  "account",
  "portal-site",
  "pricing",
  "proxy",
  "billing",
  "flags",
  "webhooks",
  "batch",
  "quick",
  "order",
]);

type NavItemDef = {
  id: NavTab;
  label: string;
  icon: ReactNode;
};

type NavGroupDef = {
  title: string;
  items: NavItemDef[];
};

const NAV_GROUPS: NavGroupDef[] = [
  {
    title: "监控",
    items: [
      { id: "overview", label: "运维总览", icon: <IconLayoutDashboard size={16} stroke={1.8} /> },
      { id: "diagnostics", label: "系统自检", icon: <IconActivity size={16} stroke={1.8} /> },
    ],
  },
  {
    title: "业务",
    items: [
      { id: "history", label: "订单记录", icon: <IconListDetails size={16} stroke={1.8} /> },
      { id: "locks", label: "资金对账", icon: <IconLockOpen size={16} stroke={1.8} /> },
      { id: "account", label: "账号排查", icon: <IconUserCircle size={16} stroke={1.8} /> },
    ],
  },
  {
    title: "运营",
    items: [
      { id: "portal-site", label: "门户配置", icon: <IconWorld size={16} stroke={1.8} /> },
      { id: "pricing", label: "地区比价", icon: <IconCurrencyDollar size={16} stroke={1.8} /> },
    ],
  },
  {
    title: "系统",
    items: [
      { id: "proxy", label: "代理网关", icon: <IconNetwork size={16} stroke={1.8} /> },
      { id: "billing", label: "固定账单", icon: <IconMapPin size={16} stroke={1.8} /> },
      { id: "flags", label: "策略开关", icon: <IconAdjustments size={16} stroke={1.8} /> },
      { id: "webhooks", label: "告警通知", icon: <IconBell size={16} stroke={1.8} /> },
    ],
  },
  {
    title: "工具",
    items: [
      { id: "batch", label: "批量体检", icon: <IconChecklist size={16} stroke={1.8} /> },
      { id: "quick", label: "内部代充", icon: <IconBolt size={16} stroke={1.8} /> },
      { id: "order", label: "分步调试", icon: <IconCode size={16} stroke={1.8} /> },
    ],
  },
];

const TAB_LABEL_MAP: Record<NavTab, string> = NAV_GROUPS.reduce(
  (acc, group) => {
    for (const item of group.items) {
      acc[item.id] = item.label;
    }
    return acc;
  },
  {} as Record<NavTab, string>,
);

function readTabFromUrl(): NavTab {
  try {
    const tab = new URLSearchParams(window.location.search).get("tab") || "";
    return VALID_TABS.has(tab) ? (tab as NavTab) : "overview";
  } catch {
    return "overview";
  }
}

function writeTabToUrl(tab: NavTab) {
  try {
    const url = new URL(window.location.href);
    if (tab === "overview") {
      url.searchParams.delete("tab");
    } else {
      url.searchParams.set("tab", tab);
    }
    const nextHref = `${url.pathname}${url.search}${url.hash}`;
    const currHref = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    if (nextHref !== currHref) {
      window.history.pushState({ tab }, "", nextHref);
    }
  } catch {
    // ignore history API failures in restricted environments
  }
}

function shouldShowLogout(): boolean {
  try {
    const host = window.location.hostname;
    const isLocalLoopback = host === "127.0.0.1" || host === "localhost" || host === "::1";
    return !isLocalLoopback || Boolean(document.cookie.trim());
  } catch {
    return false;
  }
}

const helpItems = [
  { title: "在哪里查看订单和结算币种？", body: "在左侧「业务 → 订单记录」服务端账本中按状态、套餐、日期或订单号查询，并支持导出脱敏 CSV。" },
  { title: "出现已付款待交付或 unknown 怎么办？", body: "后台只读对账器会自动按阶梯退避复核；也可以在「资金对账」手动触发只读对账，或在核实未扣款后人工安全解锁。" },
  { title: "如何修改前台门户文案、公告或参考价？", body: "在「运营」分组下的「门户配置」和「地区比价」中直接调整并保存，刷新用户端 `/` 即可生效。" },
];

function PanelFallback() {
  return (
    <Center py="xl">
      <Loader size="sm" />
    </Center>
  );
}

export function App() {
  const [history, setHistory] = useState(loadHistory);
  const [view, setView] = useState<NavTab>(() => readTabFromUrl());
  const [visitedTabs, setVisitedTabs] = useState<Record<string, boolean>>(() => {
    const initial = readTabFromUrl();
    return { overview: true, [initial]: true };
  });
  const [navOpened, setNavOpened] = useState(false);
  const [helpOpened, setHelpOpened] = useState(false);
  const [heldLocks, setHeldLocks] = useState(0);
  const [lockRevision, setLockRevision] = useState(0);
  const [health, setHealth] = useState<HealthSnapshot | null>(null);
  const [showLogout] = useState<boolean>(() => shouldShowLogout());

  const scheme = useComputedColorScheme("light");
  const { toggleColorScheme } = useMantineColorScheme();
  const record = useCallback((order: Order) => setHistory(recordOrder(order)), []);

  useEffect(() => {
    const label = TAB_LABEL_MAP[view] || "运维总览";
    document.title = `${label} · GPT 运维后台`;
  }, [view]);

  useEffect(() => {
    const themeMeta = document.querySelector('meta[name="theme-color"]');
    if (themeMeta) {
      themeMeta.setAttribute("content", scheme === "dark" ? "#101922" : "#f3f8fc");
    }
    const schemeMeta = document.querySelector('meta[name="color-scheme"]');
    if (schemeMeta) {
      schemeMeta.setAttribute("content", scheme);
    }
    document.documentElement.style.colorScheme = scheme;
  }, [scheme]);

  useEffect(() => {
    const onPopState = () => {
      const next = readTabFromUrl();
      setView(next);
      setVisitedTabs((prev) => (prev[next] ? prev : { ...prev, [next]: true }));
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const refreshHeaderTelemetry = useCallback(async (signal?: AbortSignal) => {
    try {
      const [locksRes, healthRes] = await Promise.all([
        getPaymentLocks({ state: "held", limit: 1 }, signal),
        getHealth(signal),
      ]);
      if (signal?.aborted) return;
      setHeldLocks(locksRes.counts?.held ?? 0);
      setHealth(healthRes);
    } catch {
      // ignore transient telemetry errors in header
    }
  }, []);

  const notifyLocksChanged = useCallback(() => {
    setLockRevision((rev) => rev + 1);
    void refreshHeaderTelemetry();
  }, [refreshHeaderTelemetry]);

  useEffect(() => {
    let activeController: AbortController | null = null;
    const poll = () => {
      if (document.visibilityState !== "visible") return;
      activeController?.abort();
      activeController = new AbortController();
      void refreshHeaderTelemetry(activeController.signal);
    };
    poll();
    const timer = window.setInterval(poll, 25_000);
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        poll();
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      activeController?.abort();
    };
  }, [refreshHeaderTelemetry]);

  const navigate = useCallback((next: string) => {
    const target: NavTab = VALID_TABS.has(next) ? (next as NavTab) : "overview";
    setView(target);
    setVisitedTabs((prev) => (prev[target] ? prev : { ...prev, [target]: true }));
    setNavOpened(false);
    writeTabToUrl(target);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  async function logout() {
    await fetch("/logout", { method: "POST" });
    window.location.href = "/login";
  }

  return (
    <AppShell
      className="studio-app"
      header={{ height: 56 }}
      navbar={{ width: 176, breakpoint: "md", collapsed: { mobile: !navOpened } }}
      padding={0}
    >
      <AppShell.Header className="admin-header">
        <div className="admin-header-inner">
          <Group gap="sm" wrap="nowrap">
            <Burger
              opened={navOpened}
              onClick={() => setNavOpened((o) => !o)}
              hiddenFrom="md"
              size="sm"
              aria-label="切换侧边导航"
            />
            <h1 className="studio-identity">
              <button
                type="button"
                className="studio-brand"
                onClick={() => navigate("overview")}
                aria-label="GPT 运维管理后台首页"
              >
                <span className="brand-mark"><IconBolt size={20} aria-hidden="true" /></span>
                <span>GPT 运维管理后台</span>
              </button>
            </h1>
          </Group>

          <Group gap="xs" wrap="nowrap" className="header-tools">
            {health && (
              <Group gap={6} visibleFrom="sm" wrap="nowrap">
                <Badge
                  size="sm"
                  variant="light"
                  color={health.queue.draining ? "orange" : health.fake_adapters ? "yellow" : "green"}
                  style={{ cursor: "pointer" }}
                  onClick={() => navigate("diagnostics")}
                >
                  {health.queue.draining
                    ? "排空中"
                    : health.fake_adapters
                      ? "离线模拟"
                      : "服务正常"}
                </Badge>
                <Badge
                  size="sm"
                  variant="light"
                  color="blue"
                  style={{ cursor: "pointer" }}
                  onClick={() => navigate("overview")}
                >
                  队列 {health.queue.running}/{health.queue.max_concurrency}
                  {health.queue.queued > 0 ? ` · 排队 ${health.queue.queued}` : ""}
                </Badge>
              </Group>
            )}

            {heldLocks > 0 && (
              <Badge
                size="sm"
                color="red"
                variant="filled"
                style={{ cursor: "pointer" }}
                onClick={() => navigate("locks")}
              >
                活跃锁 {heldLocks}
              </Badge>
            )}

            <Tooltip label="运维指引">
              <ActionIcon
                variant="default"
                className="theme-toggle"
                onClick={() => setHelpOpened(true)}
                aria-label="打开运维指引"
              >
                <IconHelpCircle size={17} />
              </ActionIcon>
            </Tooltip>

            <Tooltip label="打开用户端充值门户">
              <ActionIcon
                component="a"
                href="/"
                target="_blank"
                rel="noopener noreferrer"
                variant="default"
                className="theme-toggle"
                aria-label="打开用户端充值门户"
              >
                <IconExternalLink size={17} />
              </ActionIcon>
            </Tooltip>

            <Tooltip label={scheme === "dark" ? "浅色模式" : "深色模式"}>
              <ActionIcon
                variant="default"
                className="theme-toggle"
                onClick={toggleColorScheme}
                aria-label="切换主题"
              >
                {scheme === "dark" ? <IconSun size={17} /> : <IconMoon size={17} />}
              </ActionIcon>
            </Tooltip>

            {showLogout && (
              <button type="button" className="logout-button" onClick={() => void logout()}>
                <IconLogout size={16} />退出
              </button>
            )}
          </Group>
        </div>
      </AppShell.Header>

      <AppShell.Navbar className="admin-navbar" p={8}>
        <AppShell.Section grow component={ScrollArea} scrollbarSize={4}>
          <div className="admin-nav-groups">
            {NAV_GROUPS.map((group) => (
              <div key={group.title} className="admin-nav-group">
                <div className="admin-nav-group-label">{group.title}</div>
                <Stack gap={2}>
                  {group.items.map((item) => (
                    <NavLink
                      key={item.id}
                      label={item.label}
                      leftSection={item.icon}
                      rightSection={
                        item.id === "locks" && heldLocks > 0 ? (
                          <Badge size="xs" color="red" variant="filled">{heldLocks}</Badge>
                        ) : undefined
                      }
                      active={view === item.id}
                      onClick={() => navigate(item.id)}
                      className="admin-nav-item"
                    />
                  ))}
                </Stack>
              </div>
            ))}
          </div>
        </AppShell.Section>

        <AppShell.Section pt={8}>
          <Divider mb={8} color="var(--studio-line)" />
          <Group justify="space-between" px={8} pb={2} gap={4} wrap="nowrap">
            <Text fz={11} c="dimmed" truncate>
              {showLogout ? "鉴权模式" : "本地模式"}
            </Text>
            <Text fz={11} c="dimmed">v{health?.version || "0.2.0"}</Text>
          </Group>
        </AppShell.Section>
      </AppShell.Navbar>

      <AppShell.Main className="admin-main">
        <div className="admin-content">
          <ErrorBoundary title="管理后台发生异常">
            <div style={{ display: view === "overview" ? "block" : "none" }}>
              <ErrorBoundary title="运维总览加载失败">
                <OpsOverviewPanel
                  active={view === "overview"}
                  onNavigate={navigate}
                  onHealthUpdate={setHealth}
                  onLocksChanged={notifyLocksChanged}
                />
              </ErrorBoundary>
            </div>

            {visitedTabs.diagnostics && (
              <div style={{ display: view === "diagnostics" ? "block" : "none" }}>
                <ErrorBoundary title="系统自检加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <SystemDiagnosticsPanel active={view === "diagnostics"} onNavigate={navigate} />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}

            {visitedTabs.history && (
              <div style={{ display: view === "history" ? "block" : "none" }}>
                <ErrorBoundary title="订单记录加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <ServerLedgerPanel
                      active={view === "history"}
                      localHistory={history}
                      onLocksChanged={notifyLocksChanged}
                    />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}

            {visitedTabs.locks && (
              <div style={{ display: view === "locks" ? "block" : "none" }}>
                <ErrorBoundary title="资金对账加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <LocksPanel
                      active={view === "locks"}
                      onCountChange={setHeldLocks}
                      onLocksChanged={notifyLocksChanged}
                    />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}

            {visitedTabs.account && (
              <div style={{ display: view === "account" ? "block" : "none" }}>
                <ErrorBoundary title="账号与账单排查加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <AccountInspectorPanel />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}

            {visitedTabs["portal-site"] && (
              <div style={{ display: view === "portal-site" ? "block" : "none" }}>
                <ErrorBoundary title="门户站点配置加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <PortalSitePanel />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}

            {visitedTabs.pricing && (
              <div style={{ display: view === "pricing" ? "block" : "none" }}>
                <ErrorBoundary title="地区比价加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <ReferencePricingPanel />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}

            {visitedTabs.proxy && (
              <div style={{ display: view === "proxy" ? "block" : "none" }}>
                <ErrorBoundary title="代理网关加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <ProxySettings />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}

            {visitedTabs.billing && (
              <div style={{ display: view === "billing" ? "block" : "none" }}>
                <ErrorBoundary title="固定账单加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <FixedBillingSettings onNavigate={navigate} />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}

            {visitedTabs.flags && (
              <div style={{ display: view === "flags" ? "block" : "none" }}>
                <ErrorBoundary title="运行策略开关加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <RuntimeFlagsPanel />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}

            {visitedTabs.webhooks && (
              <div style={{ display: view === "webhooks" ? "block" : "none" }}>
                <ErrorBoundary title="告警通知加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <WebhookSettings />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}

            {visitedTabs.batch && (
              <div style={{ display: view === "batch" ? "block" : "none" }}>
                <ErrorBoundary title="批量体检与入队加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <BatchPreflightPanel />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}

            {visitedTabs.quick && (
              <div style={{ display: view === "quick" ? "block" : "none" }}>
                <ErrorBoundary title="内部代充面板加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <QuickPay lockRevision={lockRevision} onLocksChanged={notifyLocksChanged} />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}

            {visitedTabs.order && (
              <div style={{ display: view === "order" ? "block" : "none" }}>
                <ErrorBoundary title="协议分步调试面板加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <OrderNew onRecord={record} />
                  </Suspense>
                </ErrorBoundary>
              </div>
            )}
          </ErrorBoundary>
        </div>
      </AppShell.Main>

      <Modal
        opened={helpOpened}
        onClose={() => setHelpOpened(false)}
        title="运维指引"
        size="md"
        radius="lg"
        padding="lg"
        centered
      >
        <Stack gap="sm">
          {helpItems.map(({ title, body }) => (
            <div key={title} className="admin-help-item">
              <div className="admin-help-item-title">{title}</div>
              <div className="admin-help-item-body">{body}</div>
            </div>
          ))}
        </Stack>
      </Modal>
    </AppShell>
  );
}
