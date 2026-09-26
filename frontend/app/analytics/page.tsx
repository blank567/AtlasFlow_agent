"use client";

import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { getAnalytics } from "../../lib/api";
import { formatDuration, formatPercent } from "../../lib/format";
import { AnalyticsData } from "../../lib/types";
import { EmptyState, LoadingBlock, MetricCard, PageHeader, Panel } from "../../components/ui";

const STATUS_COLORS: Record<string, string> = { pending: "#80949a", running: "#247d87", waiting_approval: "#b27a2f", completed: "#247a62", completed_with_warnings: "#b38834", failed: "#b84b59", cancelled: "#826d85" };
const DECISION_COLORS: Record<string, string> = { accept: "#247a62", supplement: "#8067a6", replan: "#bd7146", revise: "#ae843a" };

export default function AnalyticsPage() {
  const [range, setRange] = useState("30d");
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setData(null); setError(null);
    void getAnalytics(range).then(setData).catch((reason) => setError(reason instanceof Error ? reason.message : "统计数据读取失败"));
  }, [range]);

  return (
    <main className="pageContent">
      <PageHeader eyebrow="运行分析" title="从运行记录看系统表现" description="指标只根据本地持久化的真实 Run 计算，不补造缺失数据。" actions={<div className="segmented">{[["7d", "最近 7 天"], ["30d", "最近 30 天"], ["all", "全部"]].map(([value, label]) => <button key={value} className={range === value ? "active" : ""} onClick={() => setRange(value)}>{label}</button>)}</div>} />
      {error && <div className="inlineAlert errorAlert"><strong>Analytics 暂不可用</strong><span>{error}</span></div>}
      {!data ? error ? <Panel><EmptyState title="没有可显示的统计" description="确认后端 Analytics 接口与 SQLite 运行数据库已启动。" /></Panel> : <LoadingBlock /> : <AnalyticsContent data={data} />}
    </main>
  );
}

function AnalyticsContent({ data }: { data: AnalyticsData }) {
  const summary = data.summary;
  return <>
    <div className="analyticsHighlights">
      <MetricCard label="运行总数" value={summary.total_runs ?? "不可用"} note="筛选范围内" />
      <MetricCard label="成功率" value={formatPercent(summary.success_rate)} note="已完成 / 终态运行" />
      <MetricCard label="平均耗时" value={formatDuration(summary.avg_duration_ms)} note="已结束的运行" />
    </div>
    <div className="analyticsSecondary">
      <MetricCard label="模型调用" value={summary.total_model_calls ?? "不可用"} note="真实调用计数" />
      <MetricCard label="研究任务" value={summary.total_tasks ?? "不可用"} note={`${summary.successful_tasks ?? "—"} 成功 · ${summary.failed_tasks ?? "—"} 失败`} />
      <MetricCard label="任务成功率" value={formatPercent(summary.task_success_rate)} note="成功任务 / 已结束任务" />
      <MetricCard label="平均质量分" value={summary.avg_quality_score !== undefined ? `${summary.avg_quality_score.toFixed(1)}/100` : "不可用"} note="Quality Gate 真实评分" />
      <MetricCard label="补充研究" value={summary.total_supplement_rounds ?? "不可用"} note="累计补充轮次" />
      <MetricCard label="重规划" value={summary.total_replans ?? "不可用"} note="累计重规划次数" />
      <MetricCard label="报告修订" value={summary.total_revisions ?? "不可用"} note="累计修订次数" />
    </div>
    <div className="chartGrid">
      <Panel title="运行耗时趋势" meta={<span className="panelHint">平均耗时 / 日</span>} className="chartPanel wideChart">
        {!data.duration_trend.length ? <ChartEmpty /> : <div className="chartCanvas"><ResponsiveContainer width="100%" height="100%"><LineChart data={data.duration_trend} margin={{ top: 12, right: 14, left: 4, bottom: 0 }}><CartesianGrid stroke="#e8ebf0" vertical={false} /><XAxis dataKey="date" tick={{ fill: "#7c8797", fontSize: 11 }} axisLine={false} tickLine={false} /><YAxis tickFormatter={(value) => `${Math.round(value / 1000)}s`} tick={{ fill: "#7c8797", fontSize: 11 }} axisLine={false} tickLine={false} width={45} /><Tooltip formatter={(value) => [formatDuration(Number(value)), "平均耗时"]} contentStyle={{ borderRadius: 10, border: "1px solid #e1e5eb" }} /><Line type="monotone" dataKey="avg_duration_ms" stroke="#4f67d8" strokeWidth={2.5} dot={{ r: 3, fill: "#fff", strokeWidth: 2 }} activeDot={{ r: 5 }} /></LineChart></ResponsiveContainer></div>}
      </Panel>
      <Panel title="状态分布" meta={<span className="panelHint">Runs</span>} className="chartPanel">
        {!data.status_distribution.length ? <ChartEmpty /> : <div className="chartCanvas"><ResponsiveContainer width="100%" height="100%"><PieChart><Pie data={data.status_distribution} dataKey="count" nameKey="status" innerRadius={55} outerRadius={82} paddingAngle={3}>{data.status_distribution.map((item) => <Cell key={item.status} fill={STATUS_COLORS[item.status] ?? "#80949a"} />)}</Pie><Tooltip contentStyle={{ borderRadius: 8, border: "1px solid #d9e3e0" }} /></PieChart></ResponsiveContainer><div className="chartLegend">{data.status_distribution.map((item) => <span key={item.status}><i style={{ background: STATUS_COLORS[item.status] ?? "#80949a" }} />{item.status} <strong>{item.count}</strong></span>)}</div></div>}
      </Panel>
      <Panel title="审查决策" meta={<span className="panelHint">Critic + Quality</span>} className="chartPanel">
        {!data.decision_counts.length ? <ChartEmpty /> : <div className="chartCanvas"><ResponsiveContainer width="100%" height="100%"><BarChart data={data.decision_counts} layout="vertical" margin={{ left: 8, right: 22, top: 8, bottom: 8 }}><CartesianGrid stroke="#e3ebe8" horizontal={false} /><XAxis type="number" hide /><YAxis dataKey="decision" type="category" width={78} tick={{ fill: "#667b80", fontSize: 11 }} axisLine={false} tickLine={false} /><Tooltip cursor={{ fill: "#f4f7f5" }} contentStyle={{ borderRadius: 8, border: "1px solid #d9e3e0" }} /><Bar dataKey="count" radius={[0, 3, 3, 0]} maxBarSize={28}>{data.decision_counts.map((item) => <Cell key={item.decision} fill={DECISION_COLORS[item.decision.toLowerCase()] ?? "#547d86"} />)}</Bar></BarChart></ResponsiveContainer></div>}
      </Panel>
      <Panel title="计划版本分布" meta={<span className="panelHint">最终版本</span>} className="chartPanel wideChart">
        {!data.plan_versions.length ? <ChartEmpty /> : <div className="chartCanvas"><ResponsiveContainer width="100%" height="100%"><BarChart data={data.plan_versions} margin={{ left: 0, right: 8, top: 10, bottom: 0 }}><CartesianGrid stroke="#edf0f4" vertical={false} /><XAxis dataKey="version" tick={{ fill: "#647083", fontSize: 11 }} axisLine={false} tickLine={false} /><YAxis allowDecimals={false} tick={{ fill: "#647083", fontSize: 11 }} axisLine={false} tickLine={false} width={30} /><Tooltip cursor={{ fill: "#f5f6f8" }} contentStyle={{ borderRadius: 10, border: "1px solid #e1e5eb" }} /><Bar dataKey="count" fill="#24a36a" radius={[5, 5, 0, 0]} maxBarSize={44} /></BarChart></ResponsiveContainer></div>}
      </Panel>
    </div>
  </>;
}

function ChartEmpty() { return <div className="chartEmpty"><span>暂无足够数据</span><small>产生更多终态 Run 后会自动绘制。</small></div>; }
