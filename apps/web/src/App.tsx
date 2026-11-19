import { ActionIcon, Tabs, Tooltip, useComputedColorScheme, useMantineColorScheme } from "@mantine/core";
import {
  IconAdjustmentsHorizontal, IconArrowRight, IconBolt, IconBook2, IconBrandOpenai,
  IconCircleCheck, IconHistory, IconLogout, IconMoon, IconSpeakerphone, IconSun,
} from "@tabler/icons-react";
import { useCallback, useState } from "react";

import { loadHistory, recordOrder } from "./api/history";
import type { Order } from "./api/client";
import { HistoryTable } from "./components/HistoryTable";
import { ProxySettings } from "./components/ProxySettings";
import { OrderNew } from "./pages/OrderNew";

const helpItems = [
  { title: "在哪里查看本次订阅的金额？", body: "选择套餐和地区后创建结账。取得报价后，「订阅回执」会显示应付金额和币种，最终金额以结账页为准。" },
  { title: "怎样确认订阅是否已开通？", body: "在「订阅回执」查看当前状态。订单处理结束后，也可以打开「开通记录」核对结果。" },
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
          <button type="button" className="studio-brand" onClick={() => navigate("order")} aria-label="GPT 订阅工作台首页">
            <span className="brand-mark"><IconBolt size={22} aria-hidden="true" /></span>
            GPT 订阅工作台
          </button>
          <nav className="header-nav" aria-label="主导航">
            <button type="button" aria-current={view === "order" ? "page" : undefined} onClick={() => navigate("order")}>订阅开通</button>
            <button type="button" aria-current={view === "history" ? "page" : undefined} onClick={() => navigate("history")}>我的记录</button>
            <a href="#help">帮助</a>
          </nav>
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
        <section className="studio-hero" aria-labelledby="page-title">
          <h1 id="page-title">开通你的 ChatGPT 订阅</h1>
          <p>从个人套餐到团队席位，选择适合你的订阅。<br />在这里查看结算金额，跟进每一笔订单。</p>
          <ul className="hero-features" aria-label="订阅功能">
            <li><IconCircleCheck />多地区结算</li>
            <li><IconCircleCheck />个人与团队套餐</li>
            <li><IconCircleCheck />订阅进度查询</li>
          </ul>
        </section>
        <aside className="studio-notice" aria-label="结算提示">
          <span><IconSpeakerphone size={17} />结算提示</span>
          <p>不同地区的价格与币种可能不同，实际应付金额以本次结账报价为准。</p>
        </aside>

        <Tabs value={view} onChange={setView} keepMounted className="workspace-tabs" classNames={{ list: "workspace-list", tab: "workspace-tab", tabLabel: "workspace-tab-label" }}>
          <Tabs.List aria-label="工作台功能">
            <Tabs.Tab value="order"><IconBrandOpenai className="workspace-icon" /><strong>ChatGPT</strong><span className="workspace-note">订阅开通</span></Tabs.Tab>
            <Tabs.Tab value="history"><IconHistory className="workspace-icon history-icon" /><strong>开通记录</strong><span className="workspace-note">{history.length} 条记录</span></Tabs.Tab>
            <Tabs.Tab value="proxy"><IconAdjustmentsHorizontal className="workspace-icon settings-icon" /><strong>代理设置</strong><IconArrowRight className="workspace-arrow" size={16} /></Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="order"><OrderNew onRecord={record} /></Tabs.Panel>
          <Tabs.Panel value="history"><section className="workspace-panel"><h2>开通记录</h2><HistoryTable items={history} /></section></Tabs.Panel>
          <Tabs.Panel value="proxy"><section className="workspace-panel"><ProxySettings /></section></Tabs.Panel>
        </Tabs>

        <section id="help" className="studio-help" aria-labelledby="help-title">
          <h2 id="help-title">需要一点帮助？</h2>
          <div className="help-items">
            {helpItems.map(({ title, body }) => (
              <details key={title}><summary>{title}</summary><p>{body}</p></details>
            ))}
          </div>
        </section>
      </main>
      <footer className="studio-footer"><div className="studio-width footer-inner">
        <span>GPT 订阅工作台</span><a href="#help"><IconBook2 size={15} />使用帮助</a>
      </div></footer>
    </div>
  );
}
