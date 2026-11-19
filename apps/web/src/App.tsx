import { ActionIcon, Tabs, Tooltip, useComputedColorScheme, useMantineColorScheme } from "@mantine/core";
import {
  IconBolt, IconChevronDown, IconLogout, IconMoon, IconSun,
} from "@tabler/icons-react";
import { useCallback, useState } from "react";

import { loadHistory, recordOrder } from "./api/history";
import type { Order } from "./api/client";
import { HistoryTable } from "./components/HistoryTable";
import { ProxySettings } from "./components/ProxySettings";
import { OrderNew } from "./pages/OrderNew";
import { QuickPay } from "./pages/QuickPay";

const helpItems = [
  { title: "在哪里查看本次订阅的金额？", body: "取得报价后，在「订阅回执」查看金额和币种。" },
  { title: "怎样确认订阅是否已开通？", body: "在「订阅回执」看当前状态，已处理的订单可以在「开通记录」核对。" },
  { title: "开通记录保存在哪里？", body: "记录保存在当前浏览器，最多保留最近 50 笔。请使用同一浏览器查看。" },
];

export function App() {
  const [history, setHistory] = useState(loadHistory);
  const [view, setView] = useState<string | null>("order");
  const scheme = useComputedColorScheme("light");
  const { toggleColorScheme } = useMantineColorScheme();
  const record = useCallback((order: Order) => setHistory(recordOrder(order)), []);

  function navigate(next: string) {
    setView(next);
    window.scrollTo({ top: 0 });
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
            <button type="button" className="studio-brand" onClick={() => navigate("order")} aria-label="GPT 订阅工作台首页">
              <span className="brand-mark"><IconBolt size={22} aria-hidden="true" /></span>
              GPT 订阅工作台
            </button>
          </h1>
          <div className="header-tools">
            <Tooltip label={scheme === "dark" ? "浅色模式" : "深色模式"}>
              <ActionIcon variant="default" className="theme-toggle" onClick={toggleColorScheme} aria-label="切换主题">
                {scheme === "dark" ? <IconSun size={17} /> : <IconMoon size={17} />}
              </ActionIcon>
            </Tooltip>
            <button type="button" className="logout-button" onClick={() => void logout()}><IconLogout size={16} />退出</button>
          </div>
        </div>
      </header>

      <main className="studio-width studio-main">
        <Tabs value={view} onChange={setView} keepMounted className="workspace-tabs" classNames={{ list: "workspace-list", tab: "workspace-tab" }}>
          <Tabs.List aria-label="工作台功能">
            <Tabs.Tab value="quick">一键充值</Tabs.Tab>
            <Tabs.Tab value="order">分步调试</Tabs.Tab>
            <Tabs.Tab value="history">开通记录</Tabs.Tab>
            <Tabs.Tab value="proxy">代理设置</Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="quick"><QuickPay /></Tabs.Panel>
          <Tabs.Panel value="order"><OrderNew onRecord={record} /></Tabs.Panel>
          <Tabs.Panel value="history"><section className="workspace-panel"><h2>开通记录</h2><HistoryTable items={history} /></section></Tabs.Panel>
          <Tabs.Panel value="proxy"><section className="workspace-panel"><ProxySettings /></section></Tabs.Panel>
        </Tabs>

        <details className="studio-help">
          <summary>使用帮助 <IconChevronDown size={16} aria-hidden="true" /></summary>
          <dl className="help-items">
            {helpItems.map(({ title, body }) => (
              <div key={title}><dt>{title}</dt><dd>{body}</dd></div>
            ))}
          </dl>
        </details>
      </main>
    </div>
  );
}
