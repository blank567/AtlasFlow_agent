import Link from "next/link";
import { ReactNode } from "react";
import { compactId, formatDate } from "../lib/format";
import { RunRecord, RunStatus } from "../lib/types";

const STATUS_LABELS: Record<string, string> = {
  pending: "等待中",
  running: "运行中",
  waiting_approval: "等待审批",
  completed: "已完成",
  completed_with_warnings: "完成但有警告",
  failed: "失败",
  cancelled: "已取消",
};

export function StatusBadge({ status }: { status?: RunStatus }) {
  const value = status ?? "pending";
  return <span className={`statusBadge status-${value}`}>{STATUS_LABELS[value] ?? value}</span>;
}

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description?: string; actions?: ReactNode }) {
  return (
    <header className="pageHeader">
      <div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h1>{title}</h1>{description && <p>{description}</p>}</div>
      {actions && <div className="headerActions">{actions}</div>}
    </header>
  );
}

export function Panel({ title, meta, children, className = "" }: { title?: string; meta?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel ${className}`}>
      {(title || meta) && <div className="panelHeader"><h2>{title}</h2>{meta}</div>}
      {children}
    </section>
  );
}

export function MetricCard({ label, value, note }: { label: string; value: ReactNode; note?: string }) {
  return <article className="metricCard"><span>{label}</span><strong>{value}</strong>{note && <small>{note}</small>}</article>;
}

export function EmptyState({ title, description }: { title: string; description: string }) {
  return <div className="emptyState"><span>◇</span><strong>{title}</strong><p>{description}</p></div>;
}

export function RunRow({ run, actions }: { run: RunRecord; actions?: ReactNode }) {
  return (
    <article className="runRow">
      <div className="runIdentity">
        <Link href={`/runs/${run.id}`} className="runQuery">{run.query || "未命名研究任务"}</Link>
        <div className="runMeta"><code title={run.id}>{compactId(run.id)}</code><span>创建于 {formatDate(run.created_at)}</span></div>
      </div>
      <div className="runStats"><span><strong>{run.metrics.total_tasks}</strong> 任务</span><span><strong>{run.metrics.model_calls}</strong> 调用</span></div>
      <StatusBadge status={run.status} />
      {actions && <div className="rowActions">{actions}</div>}
    </article>
  );
}

export function LoadingBlock({ label = "正在读取数据" }: { label?: string }) {
  return <div className="loadingBlock"><span className="spinner" />{label}</div>;
}
