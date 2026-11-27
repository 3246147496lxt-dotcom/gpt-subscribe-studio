import type { ReactNode } from "react";

export function AdminPageHeader({
  title,
  badge,
  subtitle,
  actions,
}: {
  title: string;
  badge?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="ops-hero">
      <div style={{ minWidth: 0 }}>
        <div className="ops-hero-title-row">
          <h2 className="ops-hero-title">{title}</h2>
          {badge}
        </div>
        {subtitle && <div className="ops-hero-sub">{subtitle}</div>}
      </div>
      {actions && <div className="ops-toolbar">{actions}</div>}
    </div>
  );
}
