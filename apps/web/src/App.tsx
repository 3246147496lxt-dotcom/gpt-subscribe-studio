import { ActionIcon, Badge, Center, Group, Loader, Tabs, Tooltip, useComputedColorScheme, useMantineColorScheme } from "@mantine/core";
import {
  IconActivity, IconBolt, IconChevronDown, IconLogout, IconMoon, IconSun, IconUserCircle,
} from "@tabler/icons-react";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";

import type { Order } from "./api/client";
import { loadHistory, recordOrder } from "./api/history";
import { getPaymentLocks } from "./api/ledger";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { QuickPay } from "./pages/QuickPay";

const AccountDetailModal = lazy(() =>
  import("./components/AccountDetailModal").then((m) => ({ default: m.AccountDetailModal })),
);
const SystemDiagnosticsModal = lazy(() =>
  import("./components/SystemDiagnosticsModal").then((m) => ({ default: m.SystemDiagnosticsModal })),
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
const RuntimeFlagsPanel = lazy(() =>
  import("./components/RuntimeFlagsPanel").then((m) => ({ default: m.RuntimeFlagsPanel })),
);
const ServerLedgerPanel = lazy(() =>
  import("./components/ServerLedgerPanel").then((m) => ({ default: m.ServerLedgerPanel })),
);
const WebhookSettings = lazy(() =>
  import("./components/WebhookSettings").then((m) => ({ default: m.WebhookSettings })),
);
const OrderNew = lazy(() =>
  import("./pages/OrderNew").then((m) => ({ default: m.OrderNew })),
);

const VALID_TABS = new Set(["quick", "batch", "order", "history", "locks", "proxy", "flags", "webhooks"]);

function readTabFromUrl(): string {
  try {
    const tab = new URLSearchParams(window.location.search).get("tab") || "";
    return VALID_TABS.has(tab) ? tab : "quick";
  } catch {
    return "quick";
  }
}

function writeTabToUrl(tab: string) {
  try {
    const url = new URL(window.location.href);
    if (tab === "quick") {
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
    // Show logout only when deployed behind auth gateway (non-loopback) or when a session cookie exists.
    return !isLocalLoopback || Boolean(document.cookie.trim());
  } catch {
    return false;
  }
}

const helpItems = [
  { title: "在哪里查看本次订阅的金额？", body: "取得报价后，在「订阅回执」或「开通记录」服务端账本中查看金额和币种。" },
  { title: "出现已付款待交付或 unknown 怎么办？", body: "后台只读对账器会按阶梯退避自动复核；也可以在「资金锁与对账」标签页手动触发只读对账或在核对账单后安全解锁。" },
  { title: "开通记录保存在哪里？", body: "订单主记录与审计事件均持久化在服务端 SQLite（不含敏感凭证），更换浏览器也不会丢失，并支持按条件导出脱敏 CSV。" },
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
  const [view, setView] = useState<string>(() => readTabFromUrl());
  const [visitedTabs, setVisitedTabs] = useState<Record<string, boolean>>(() => {
    const initial = readTabFromUrl();
    return { quick: true, [initial]: true };
  });
  const [accountOpen, setAccountOpen] = useState(false);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [heldLocks, setHeldLocks] = useState(0);
  const [lockRevision, setLockRevision] = useState(0);
  const [showLogout] = useState<boolean>(() => shouldShowLogout());

  const scheme = useComputedColorScheme("light");
  const { toggleColorScheme } = useMantineColorScheme();
  const record = useCallback((order: Order) => setHistory(recordOrder(order)), []);

  // Keep browser chrome theme-color synced with light/dark mode.
  useEffect(() => {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      meta.setAttribute("content", scheme === "dark" ? "#101922" : "#f3f8fc");
    }
  }, [scheme]);

  // Sync tab selection when user navigates browser Back/Forward.
  useEffect(() => {
    const onPopState = () => {
      const next = readTabFromUrl();
      setView(next);
      setVisitedTabs((prev) => (prev[next] ? prev : { ...prev, [next]: true }));
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const refreshLockBadge = useCallback(async () => {
    try {
      const res = await getPaymentLocks({ state: "held", limit: 1 });
      setHeldLocks(res.counts?.held ?? 0);
    } catch {
      // ignore badge fetch errors
    }
  }, []);

  const notifyLocksChanged = useCallback(() => {
    setLockRevision((rev) => rev + 1);
    void refreshLockBadge();
  }, [refreshLockBadge]);

  useEffect(() => {
    void refreshLockBadge();
  }, [refreshLockBadge]);

  function handleViewChange(next: string | null) {
    const target = next && VALID_TABS.has(next) ? next : "quick";
    setView(target);
    setVisitedTabs((prev) => (prev[target] ? prev : { ...prev, [target]: true }));
    writeTabToUrl(target);
  }

  function navigate(next: string) {
    handleViewChange(next);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function logout() {
    await fetch("/logout", { method: "POST" });
    window.location.href = "/login";
  }

  return (
    <div className="studio-app">
      <header className="studio-header">
        <div className="studio-width header-inner">
          <h1 className="studio-identity">
            <button type="button" className="studio-brand" onClick={() => navigate("quick")} aria-label="GPT 订阅工作台首页">
              <span className="brand-mark"><IconBolt size={22} aria-hidden="true" /></span>
              GPT 订阅工作台
            </button>
          </h1>
          <div className="header-tools">
            <Tooltip label="系统自检与维护">
              <ActionIcon variant="default" className="theme-toggle" onClick={() => setDiagnosticsOpen(true)} aria-label="系统自检与维护">
                <IconActivity size={18} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label="账号详情">
              <ActionIcon variant="default" className="theme-toggle" onClick={() => setAccountOpen(true)} aria-label="查看账号详情">
                <IconUserCircle size={18} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label={scheme === "dark" ? "浅色模式" : "深色模式"}>
              <ActionIcon variant="default" className="theme-toggle" onClick={toggleColorScheme} aria-label="切换主题">
                {scheme === "dark" ? <IconSun size={17} /> : <IconMoon size={17} />}
              </ActionIcon>
            </Tooltip>
            {showLogout && (
              <button type="button" className="logout-button" onClick={() => void logout()}>
                <IconLogout size={16} />退出
              </button>
            )}
          </div>
        </div>
      </header>

      <main className="studio-width studio-main">
        <ErrorBoundary title="工作台发生异常">
          <Tabs value={view} onChange={handleViewChange} keepMounted className="workspace-tabs" classNames={{ list: "workspace-list", tab: "workspace-tab" }}>
            <Tabs.List aria-label="工作台功能">
              <Tabs.Tab value="quick">一键充值</Tabs.Tab>
              <Tabs.Tab value="batch">批量体检</Tabs.Tab>
              <Tabs.Tab value="order">分步调试</Tabs.Tab>
              <Tabs.Tab value="history">开通记录</Tabs.Tab>
              <Tabs.Tab value="locks">
                <Group gap={6} wrap="nowrap">
                  <span>资金锁与对账</span>
                  {heldLocks > 0 && <Badge size="xs" color="red" variant="filled">{heldLocks}</Badge>}
                </Group>
              </Tabs.Tab>
              <Tabs.Tab value="proxy">代理设置</Tabs.Tab>
              <Tabs.Tab value="flags">策略开关</Tabs.Tab>
              <Tabs.Tab value="webhooks">通知设置</Tabs.Tab>
            </Tabs.List>

            <Tabs.Panel value="quick">
              <ErrorBoundary title="一键充值面板加载失败">
                <QuickPay lockRevision={lockRevision} onLocksChanged={notifyLocksChanged} />
              </ErrorBoundary>
            </Tabs.Panel>

            <Tabs.Panel value="batch">
              {visitedTabs.batch && (
                <ErrorBoundary title="批量体检面板加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <BatchPreflightPanel />
                  </Suspense>
                </ErrorBoundary>
              )}
            </Tabs.Panel>

            <Tabs.Panel value="order">
              {visitedTabs.order && (
                <ErrorBoundary title="分步调试面板加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <OrderNew onRecord={record} />
                  </Suspense>
                </ErrorBoundary>
              )}
            </Tabs.Panel>

            <Tabs.Panel value="history">
              {visitedTabs.history && (
                <ErrorBoundary title="服务端账本加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <ServerLedgerPanel localHistory={history} onLocksChanged={notifyLocksChanged} />
                  </Suspense>
                </ErrorBoundary>
              )}
            </Tabs.Panel>

            <Tabs.Panel value="locks">
              {visitedTabs.locks && (
                <ErrorBoundary title="资金锁与对账台加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <LocksPanel onCountChange={setHeldLocks} onLocksChanged={notifyLocksChanged} />
                  </Suspense>
                </ErrorBoundary>
              )}
            </Tabs.Panel>

            <Tabs.Panel value="proxy">
              {visitedTabs.proxy && (
                <ErrorBoundary title="代理设置面板加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <section className="workspace-panel"><ProxySettings /></section>
                  </Suspense>
                </ErrorBoundary>
              )}
            </Tabs.Panel>

            <Tabs.Panel value="flags">
              {visitedTabs.flags && (
                <ErrorBoundary title="策略开关面板加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <RuntimeFlagsPanel />
                  </Suspense>
                </ErrorBoundary>
              )}
            </Tabs.Panel>

            <Tabs.Panel value="webhooks">
              {visitedTabs.webhooks && (
                <ErrorBoundary title="通知设置面板加载失败">
                  <Suspense fallback={<PanelFallback />}>
                    <WebhookSettings />
                  </Suspense>
                </ErrorBoundary>
              )}
            </Tabs.Panel>
          </Tabs>
        </ErrorBoundary>

        <details className="studio-help">
          <summary>使用帮助 <IconChevronDown size={16} aria-hidden="true" /></summary>
          <dl className="help-items">
            {helpItems.map(({ title, body }) => (
              <div key={title}><dt>{title}</dt><dd>{body}</dd></div>
            ))}
          </dl>
        </details>
      </main>

      {accountOpen && (
        <ErrorBoundary title="账号详情弹窗加载失败">
          <Suspense fallback={null}>
            <AccountDetailModal opened={accountOpen} onClose={() => setAccountOpen(false)} />
          </Suspense>
        </ErrorBoundary>
      )}

      {diagnosticsOpen && (
        <ErrorBoundary title="系统自检弹窗加载失败">
          <Suspense fallback={null}>
            <SystemDiagnosticsModal opened={diagnosticsOpen} onClose={() => setDiagnosticsOpen(false)} />
          </Suspense>
        </ErrorBoundary>
      )}
    </div>
  );
}
