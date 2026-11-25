import React, { lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";

import { PortalApp } from "./portal/PortalApp";

const AdminShell = lazy(() => import("./AdminShell"));

function isAdminPath(): boolean {
  try {
    const path = window.location.pathname || "/";
    return path === "/admin" || path.startsWith("/admin/");
  } catch {
    return false;
  }
}

function RootRouter() {
  if (isAdminPath()) {
    return (
      <Suspense fallback={<div style={{ padding: 32, textAlign: "center", color: "#64748b" }}>正在加载管理工作台…</div>}>
        <AdminShell />
      </Suspense>
    );
  }
  return <PortalApp />;
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RootRouter />
  </React.StrictMode>,
);
