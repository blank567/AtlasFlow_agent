"use client";

import { useEffect, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { getAnalytics } from "../../lib/api";
import { formatDuration, formatPercent } from "../../lib/format";
import { AnalyticsData } from "../../lib/types";
import { EmptyState, LoadingBlock, MetricCard, PageHeader, Panel } from "../../components/ui";

const STATUS_LABELS: Record<string,string> = { pending: "待执行", running: "执行中", waiting_approval: "待审批", completed: "已完成", completed_with_warnings: "完成且有警告", failed: "执行失败", cancelled: "已取消" };
const STATUS_COLORS: Record<string,string> = { pending: "var(--muted)", running: "var(--primary)", waiting_approval: "var(--amber)", completed: "var(--green)", completed_with_warnings: "var(--amber)", failed: "var(--red)", cancelled: "var(--muted)" };
const DECISION_LABELS: Record<string,string> = { accept: "接受", supplement: "补充研究", replan: "重新规划", revise: "报告修订" };

export default function AnalyticsPage() {
  const [range, setRange] = useState("30d");
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let current = true;
    setData(null); setError(null);
    void getAnalytics(range).then((value) => { if (current) setData(value); }).catch((reason) => { if (current) setError(reason instanceof Error ? reason.message : "运行分析数据读取失败"); });
    return () => { current = false; };
  }, [range, revision]);
  return <main className="pageContent analyticsPage">
    <PageHeader title="统计分析" description="执行效率、研究质量与协作决策的量化视图。" actions={<div className="segmented" aria-label="统计时间范围">{[["7d","近7天"],["30d","近30天"],["all","全部记录"]].map(([value,label]) => <button type="button" key={value} aria-pressed={range === value} className={range === value ? "active" : ""} onClick={() => setRange(value)}>{label}</button>)}</div>} />
    {error ? <Panel><div className="inlineAlert errorAlert" role="alert"><strong>读取失败</strong><span>{error}</span></div><button type="button" className="secondaryButton" onClick={() => setRevision((value) => value + 1)}>重新加载</button></Panel> : !data ? <LoadingBlock label="正在汇总运行数据" /> : <AnalyticsContent data={data} />}
  </main>;
}

function AnalyticsContent({ data }: { data: AnalyticsData }) {
  const summary = data.summary;
  const quality = typeof summary.avg_quality_score === "number" && Number.isFinite(summary.avg_quality_score) ? summary.avg_quality_score.toFixed(1) : "未评估";
  const statusMax = Math.max(1, ...data.status_distribution.map((item) => item.count));
  const decisionMax = Math.max(1, ...data.decision_counts.map((item) => item.count));
  return <>
    <div className="analyticsHighlights">
      <MetricCard label="运行总数" value={summary.total_runs ?? "未记录"} note="当前统计范围" />
      <MetricCard label="运行成功率" value={formatPercent(summary.success_rate)} note="已完成运行占终态比例" />
      <MetricCard label="平均耗时" value={formatDuration(summary.avg_duration_ms)} note="已结束运行" />
    </div>
    {summary.total_runs === 0 && <Panel><EmptyState title="尚无运行记录" description="完成研究任务后，可在此查看执行效率与质量表现。" /></Panel>}
    <div className="analyticsPrimary">
      <Panel title="执行耗时" meta={<span className="panelHint">每日平均值</span>} className="chartPanel">
        {!data.duration_trend.length ? <ChartEmpty /> : <div className="chartCanvas"><ResponsiveContainer width="100%" height="100%"><LineChart data={data.duration_trend} margin={{top:20,right:14,left:0,bottom:8}}>
          <CartesianGrid stroke="var(--line)" vertical={false} />
          <XAxis dataKey="date" tick={{fill:"var(--muted)",fontSize:11}} axisLine={false} tickLine={false} />
          <YAxis tickFormatter={(value) => Math.round(Number(value) / 1000) + "s"} tick={{fill:"var(--muted)",fontSize:11}} axisLine={false} tickLine={false} width={42} />
          <Tooltip formatter={(value) => [formatDuration(Number(value)),"平均耗时"]} contentStyle={{background:"var(--surface)",color:"var(--ink)",borderRadius:8,border:"1px solid var(--line)"}} labelStyle={{color:"var(--ink)"}} />
          <Line type="monotone" dataKey="avg_duration_ms" stroke="var(--primary)" strokeWidth={2} dot={{r:3,fill:"var(--surface)",strokeWidth:2}} activeDot={{r:5}} isAnimationActive={false} />
        </LineChart></ResponsiveContainer></div>}
      </Panel>
      <Panel title="运行状态" meta={<span className="panelHint">按实际记录汇总</span>}>
        {!data.status_distribution.length ? <ChartEmpty /> : <ul className="analyticsDistribution">{data.status_distribution.map((item) => <li key={item.status}><span>{STATUS_LABELS[item.status] ?? item.status}</span><strong>{item.count}次</strong><div className="distributionBar" aria-hidden="true"><i style={{width:Math.max(0,item.count / statusMax * 100) + "%",background:STATUS_COLORS[item.status] ?? "var(--primary)"}} /></div></li>)}</ul>}
      </Panel>
    </div>
    <div className="analyticsDetail">
      <Panel title="研究质量与资源">
        <dl className="analyticsDefinition">
          <div><dt>模型调用</dt><dd>{summary.total_model_calls ?? "未记录"}</dd></div>
          <div><dt>研究任务</dt><dd>{summary.total_tasks ?? "未记录"}</dd></div>
          <div><dt>任务完成</dt><dd>{summary.successful_tasks ?? "未记录"}成功 / {summary.failed_tasks ?? "未记录"}失败</dd></div>
          <div><dt>任务成功率</dt><dd>{formatPercent(summary.task_success_rate)}</dd></div>
          <div><dt>平均质量评分</dt><dd>{quality}{quality !== "未评估" && <span>/100</span>}</dd></div>
        </dl>
      </Panel>
      <Panel title="协作决策">
        <div className="analyticsDecisionTotals"><span>补充研究<strong>{summary.total_supplement_rounds ?? "未记录"}</strong></span><span>重新规划<strong>{summary.total_replans ?? "未记录"}</strong></span><span>报告修订<strong>{summary.total_revisions ?? "未记录"}</strong></span></div>
        {data.decision_counts.length > 0 && <ul className="analyticsDistribution">{data.decision_counts.map((item) => <li key={item.decision}><span>{DECISION_LABELS[item.decision.toLowerCase()] ?? item.decision}</span><strong>{item.count}次</strong><div className="distributionBar" aria-hidden="true"><i style={{width:Math.max(0,item.count / decisionMax * 100) + "%"}} /></div></li>)}</ul>}
        <section className="analyticsVersionDetails" aria-label="计划版本分布"><h3>计划版本分布</h3>{!data.plan_versions.length ? <p className="panelHint">尚无计划版本记录。</p> : <dl className="analyticsVersionChips">{data.plan_versions.map((item) => <div key={item.version}><dt>版本{item.version}</dt><dd>{item.count}次</dd></div>)}</dl>}</section>
      </Panel>
    </div>
  </>;
}

function ChartEmpty() { return <div className="chartEmpty"><span>样本不足</span><small>完成运行后生成统计视图。</small></div>; }
