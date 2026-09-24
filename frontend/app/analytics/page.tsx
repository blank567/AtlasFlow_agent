"use client";

import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { getAnalytics } from "../../lib/api";
import { formatDuration, formatPercent } from "../../lib/format";
import { AnalyticsData } from "../../lib/types";
import { EmptyState, LoadingBlock, MetricCard, PageHeader, Panel } from "../../components/ui";

const COLORS = ["#4f67d8", "#24a36a", "#e7a43a", "#de6262", "#8692a5", "#8b69c6"];

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
      <PageHeader eyebrow="LOCAL ANALYTICS" title="统计分析" description="只展示由本地持久化 Run 快照计算出的真实指标。" actions={<div className="segmented">{[["7d", "最近 7 天"], ["30d", "最近 30 天"], ["all", "全部"]].map(([value, label]) => <button key={value} className={range === value ? "active" : ""} onClick={() => setRange(value)}>{label}</button>)}</div>} />
      {error && <div className="inlineAlert errorAlert"><strong>Analytics 暂不可用</strong><span>{error}</span></div>}
      {!data ? error ? <Panel><EmptyState title="没有可显示的统计" description="确认后端 Analytics 接口与 SQLite 运行数据库已启动。" /></Panel> : <LoadingBlock /> : <AnalyticsContent data={data} />}
    </main>
  );
}

function AnalyticsContent({ data }: { data: AnalyticsData }) {
  const summary = data.summary;
  return <>
    <div className="analyticsMetrics">
      <MetricCard label="运行总数" value={summary.total_runs ?? "不可用"} note="筛选范围内" />
      <MetricCard label="成功率" value={formatPercent(summary.success_rate)} note="Completed / terminal" />
      <MetricCard label="平均耗时" value={formatDuration(summary.avg_duration_ms)} note="仅已终止 Run" />
      <MetricCard label="模型调用" value={summary.total_model_calls ?? "不可用"} note="真实调用计数" />
      <MetricCard label="研究任务" value={summary.total_tasks ?? "不可用"} note={`${summary.successful_tasks ?? "—"} 成功 · ${summary.failed_tasks ?? "—"} 失败`} />
      <MetricCard label="任务成功率" value={formatPercent(summary.task_success_rate)} note="成功 Task / 终态 Task" />
      <MetricCard label="平均质量分" value={summary.avg_quality_score !== undefined ? `${summary.avg_quality_score.toFixed(1)}/100` : "不可用"} note="Quality Gate 真实评分" />
      <MetricCard label="Supplement" value={summary.total_supplement_rounds ?? "不可用"} note="累计补充轮次" />
      <MetricCard label="Replan" value={summary.total_replans ?? "不可用"} note="累计重规划次数" />
      <MetricCard label="Revision" value={summary.total_revisions ?? "不可用"} note="累计报告修订次数" />
    </div>
    <div className="chartGrid">
      <Panel title="运行耗时趋势" meta={<span className="panelHint">平均耗时 / 日</span>} className="chartPanel wideChart">
        {!data.duration_trend.length ? <ChartEmpty /> : <div className="chartCanvas"><ResponsiveContainer width="100%" height="100%"><LineChart data={data.duration_trend} margin={{ top: 12, right: 14, left: 4, bottom: 0 }}><CartesianGrid stroke="#e8ebf0" vertical={false} /><XAxis dataKey="date" tick={{ fill: "#7c8797", fontSize: 11 }} axisLine={false} tickLine={false} /><YAxis tickFormatter={(value) => `${Math.round(value / 1000)}s`} tick={{ fill: "#7c8797", fontSize: 11 }} axisLine={false} tickLine={false} width={45} /><Tooltip formatter={(value) => [formatDuration(Number(value)), "平均耗时"]} contentStyle={{ borderRadius: 10, border: "1px solid #e1e5eb" }} /><Line type="monotone" dataKey="avg_duration_ms" stroke="#4f67d8" strokeWidth={2.5} dot={{ r: 3, fill: "#fff", strokeWidth: 2 }} activeDot={{ r: 5 }} /></LineChart></ResponsiveContainer></div>}
      </Panel>
      <Panel title="状态分布" meta={<span className="panelHint">Runs</span>} className="chartPanel">
        {!data.status_distribution.length ? <ChartEmpty /> : <div className="chartCanvas"><ResponsiveContainer width="100%" height="100%"><PieChart><Pie data={data.status_distribution} dataKey="count" nameKey="status" innerRadius={55} outerRadius={82} paddingAngle={3}>{data.status_distribution.map((item, index) => <Cell key={item.status} fill={COLORS[index % COLORS.length]} />)}</Pie><Tooltip contentStyle={{ borderRadius: 10, border: "1px solid #e1e5eb" }} /></PieChart></ResponsiveContainer><div className="chartLegend">{data.status_distribution.map((item, index) => <span key={item.status}><i style={{ background: COLORS[index % COLORS.length] }} />{item.status} <strong>{item.count}</strong></span>)}</div></div>}
      </Panel>
      <Panel title="审查决策" meta={<span className="panelHint">Critic + Quality</span>} className="chartPanel">
        {!data.decision_counts.length ? <ChartEmpty /> : <div className="chartCanvas"><ResponsiveContainer width="100%" height="100%"><BarChart data={data.decision_counts} layout="vertical" margin={{ left: 8, right: 22, top: 8, bottom: 8 }}><CartesianGrid stroke="#edf0f4" horizontal={false} /><XAxis type="number" hide /><YAxis dataKey="decision" type="category" width={78} tick={{ fill: "#647083", fontSize: 11 }} axisLine={false} tickLine={false} /><Tooltip cursor={{ fill: "#f5f6f8" }} contentStyle={{ borderRadius: 10, border: "1px solid #e1e5eb" }} /><Bar dataKey="count" fill="#4f67d8" radius={[0, 5, 5, 0]} maxBarSize={28} /></BarChart></ResponsiveContainer></div>}
      </Panel>
      <Panel title="Plan 版本分布" meta={<span className="panelHint">最终版本</span>} className="chartPanel wideChart">
        {!data.plan_versions.length ? <ChartEmpty /> : <div className="chartCanvas"><ResponsiveContainer width="100%" height="100%"><BarChart data={data.plan_versions} margin={{ left: 0, right: 8, top: 10, bottom: 0 }}><CartesianGrid stroke="#edf0f4" vertical={false} /><XAxis dataKey="version" tick={{ fill: "#647083", fontSize: 11 }} axisLine={false} tickLine={false} /><YAxis allowDecimals={false} tick={{ fill: "#647083", fontSize: 11 }} axisLine={false} tickLine={false} width={30} /><Tooltip cursor={{ fill: "#f5f6f8" }} contentStyle={{ borderRadius: 10, border: "1px solid #e1e5eb" }} /><Bar dataKey="count" fill="#24a36a" radius={[5, 5, 0, 0]} maxBarSize={44} /></BarChart></ResponsiveContainer></div>}
      </Panel>
    </div>
  </>;
}

function ChartEmpty() { return <div className="chartEmpty"><span>暂无足够数据</span><small>产生更多终态 Run 后会自动绘制。</small></div>; }
