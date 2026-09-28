"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { cancelRun, rerun } from "../lib/api";
import { compactId, formatDate, formatDuration } from "../lib/format";
import { PlanLineage, ResearchPlan, ResearchTask, RunEvent, RunRecord, TERMINAL_STATUSES } from "../lib/types";
import { useRunStream } from "../hooks/use-run-stream";
import { ApprovalPanel } from "./approval-panel";
import { ConfirmDialog } from "./confirm-dialog";
import { EventTimeline } from "./event-timeline";
import { ReportView } from "./report-view";
import { EmptyState, LoadingBlock, MetricCard, Panel, StatusBadge } from "./ui";

const AgentFlow = dynamic(() => import("./agent-flow").then((module) => module.AgentFlow), { ssr: false, loading: () => <LoadingBlock label="加载 Agent 拓扑" /> });
const TaskGraph = dynamic(() => import("./task-graph").then((module) => module.TaskGraph), { ssr: false, loading: () => <LoadingBlock label="加载任务 DAG" /> });

type Tab = "flow" | "tasks" | "events" | "reviews" | "report";

export function RunDetail({ runId }: { runId: string }) {
  const router = useRouter();
  const { run, events, streamState, error, refresh, setRun } = useRunStream(runId);
  const [tab, setTab] = useState<Tab>("flow");
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<"cancel" | "rerun" | null>(null);
  const visualRun = useMemo(() => run ? deriveRunFromEvents(run, events) : null, [events, run]);
  const agentLabel = useMemo(() => events.at(-1)?.agent ?? events.at(-1)?.node ?? "等待调度", [events]);

  async function handleCancel() {
    if (!run) return;
    setConfirmAction(null);
    setActionBusy(true); setActionError(null);
    try { setRun(await cancelRun(run.id)); await refresh(); } catch (reason) { setActionError(reason instanceof Error ? reason.message : "取消失败"); } finally { setActionBusy(false); }
  }

  async function handleRerun() {
    if (!run) return;
    setConfirmAction(null);
    setActionBusy(true); setActionError(null);
    try { const created = await rerun(run.id); router.push(`/runs/${created.id}`); } catch (reason) { setActionError(reason instanceof Error ? reason.message : "重新运行失败"); setActionBusy(false); }
  }

  if (!run && !error) return <main className="pageContent"><LoadingBlock label="加载 Run 快照" /></main>;
  if (!run) return <main className="pageContent"><Panel><EmptyState title="无法读取这个 Run" description={error ?? "记录不存在或后端暂不可用。"} /><Link className="secondaryButton inlineButton" href="/runs">返回运行记录</Link></Panel></main>;
  const activeRun = visualRun ?? run;
  const latestQuality = activeRun.quality_history.at(-1);
  const isTerminal = TERMINAL_STATUSES.has(run.status);
  const duration = run.metrics.duration_ms;
  const providerCalls = run.trace_segments.flatMap((segment) => segment.provider_calls);
  const tokenValues = providerCalls.map((call) => call.usage?.total_tokens).filter((value): value is number => typeof value === "number");
  const totalTokens = tokenValues.length ? tokenValues.reduce((sum, value) => sum + value, 0) : undefined;
  const costValues = providerCalls.map((call) => call.usage?.cost).filter((value): value is number => typeof value === "number");
  const totalCost = costValues.length ? costValues.reduce((sum, value) => sum + value, 0) : undefined;
  const latestProviderCall = providerCalls.at(-1);

  return (
    <main className="pageContent runDetailPage">
      <nav className="breadcrumb"><Link href="/runs">运行记录</Link><span>/</span><code>{compactId(run.id)}</code></nav>
      <header className="runHeader">
        <div><div className="runHeaderMeta"><StatusBadge status={run.status} /><span className={`streamIndicator ${streamState}`}><i />{streamState === "live" ? "实时连接" : streamState === "reconnecting" ? "正在重连" : streamState === "connecting" ? "连接中" : "快照模式"}</span></div><h1>{run.query}</h1><p><code title={run.id}>{run.id}</code><button className="copyMini" onClick={() => void navigator.clipboard.writeText(run.id)}>复制</button><span>创建于 {formatDate(run.created_at)}</span></p></div>
        <div className="headerActions">{!isTerminal && <button className="secondaryButton dangerText" disabled={actionBusy} onClick={() => setConfirmAction("cancel")}>取消运行</button>}<button className="primaryButton" disabled={actionBusy} onClick={() => setConfirmAction("rerun")}>重新运行</button></div>
      </header>
      {(error || actionError) && <div className="inlineAlert errorAlert"><strong>部分数据暂不可用</strong><span>{actionError ?? error}</span></div>}
      {run.status === "waiting_approval" && <ApprovalPanel run={run} onResolved={(value) => { setRun(value); void refresh(); }} />}

      <div className="metricStrip">
        <MetricCard label="当前 Agent" value={agentLabel} note={`事件 #${events.at(-1)?.sequence ?? 0}`} />
        <MetricCard label="任务完成" value={`${run.metrics.successful_tasks}/${run.metrics.total_tasks}`} note={`峰值并发 ${run.metrics.peak_concurrency}`} />
        <MetricCard label="运行耗时" value={formatDuration(duration)} note={isTerminal ? "最终值" : "实时更新"} />
        <MetricCard label="质量评分" value={latestQuality?.score !== undefined ? `${latestQuality.score}/100` : "不可用"} note={latestQuality?.decision ?? "等待 Quality"} />
        <MetricCard label="模型调用" value={providerCalls.length || run.metrics.model_calls} note={totalTokens !== undefined ? `${totalTokens.toLocaleString()} tokens` : "Token 不可用"} />
      </div>

      <Panel className="visualizationPanel">
        <div className="tabBar" role="tablist">
          {([['flow', 'Agent 流程'], ['tasks', 'Task DAG'], ['events', `实时事件 ${events.length}`], ['reviews', '审查记录'], ['report', '研究报告']] as [Tab, string][]).map(([value, label]) => <button role="tab" aria-selected={tab === value} className={tab === value ? "active" : ""} key={value} onClick={() => setTab(value)}>{label}</button>)}
        </div>
        <div className="tabContent">
          {tab === "flow" && <><div className="tabIntro"><div><h2>Agent 执行拓扑</h2><p>两行折返显示实际执行方向，外侧回线显示补充、重规划和修订；所有原始路由仍可在下方展开。</p></div><div className="graphLegend"><span><i className="legendDot running" />执行中</span><span><i className="legendDot completed" />已执行</span><span><i className="legendLine normal" />主线</span><span><i className="legendLine supplement" />补充</span><span><i className="legendLine replan" />重规划</span><span><i className="legendLine revise" />修订</span><span><i className="legendLine fallback" />决策改道</span></div></div><AgentFlow events={events} status={run.status} /></>}
          {tab === "tasks" && <><div className="tabIntro"><div><h2>任务依赖与计划版本</h2><p>基础计划与补充任务分开记录，版本切换不会抹去历史。</p></div></div><TaskGraph run={activeRun} events={events} /></>}
          {tab === "events" && <div className="eventTab"><div className="tabIntro"><div><h2>事件时间线</h2><p>SSE 增量更新、事件去重，并通过 RunRecord 快照校准。</p></div><span className="panelHint">最新在前</span></div><EventTimeline events={events} /></div>}
          {tab === "reviews" && <ReviewHistory run={activeRun} />}
          {tab === "report" && <ReportView run={run} />}
        </div>
      </Panel>

      <Panel title="工具调用与来源" meta={<span className="panelHint">{run.tool_calls.length} 次调用 · {run.evidence.length} 条来源</span>} className="toolPanel">
        {!run.tool_calls.length ? <div className="emptyInline">尚无工具调用；运行中的请求会先出现在事件时间线。</div> : <div className="toolCallList">{run.tool_calls.map((call) => {
          const sources = run.evidence.filter((item) => call.evidence_ids.includes(item.id));
          const navigationUrls = (call.navigation_urls?.length ? call.navigation_urls : call.navigation_url ? [call.navigation_url] : []).filter((url) => url.startsWith("https://uri.amap.com/"));
          return <article key={call.call_id}>
            <div className="toolCallHead"><strong>{call.tool_name}</strong><span className={`softBadge ${call.success ? "" : "amber"}`}>{call.success ? "已完成" : "失败"}</span><small>{call.agent}{call.task_id ? ` · ${call.task_id}` : ""} · {formatDuration(call.duration_ms)}</small></div>
            <p>{call.summary ?? call.error ?? "无结果摘要"}</p>
            {sources.length > 0 && <div className="toolSources">{sources.map((source) => source.uri?.startsWith("https://") || source.uri?.startsWith("http://") ? <a key={source.id} href={source.uri} target="_blank" rel="noreferrer">{source.title} ↗</a> : <span key={source.id}>{source.title}</span>)}</div>}
            {navigationUrls.map((url, index) => <a key={`${index}-${url}`} className="toolNavigation" href={url} target="_blank" rel="noreferrer">{navigationUrls.length > 1 ? `第 ${index + 1} 段导航` : "打开高德导航"} ↗</a>)}
          </article>;
        })}</div>}
      </Panel>

      <div className="detailBottomGrid">
        <Panel title="LangSmith 追踪片段" meta={<span className="panelHint">{run.trace_segments.length} 条记录</span>}>
          <p className="traceHelp">LangSmith 链接打开嵌套调用树与耗时详情；Agent 的分支、补充任务和回环请看本站的执行路径图。两者展示的是不同层次。</p>
          {!run.trace_segments.length ? <div className="emptyInline">后端尚未返回 Trace Segment。主流程不受观测服务状态影响。</div> : <div className="traceList">{run.trace_segments.map((trace, index) => {
            const segmentTokens = trace.provider_calls.map((call) => call.usage?.total_tokens).filter((value): value is number => typeof value === "number").reduce((sum, value) => sum + value, 0);
            return <article key={trace.id}><span className="traceIndex">{String(index + 1).padStart(2, "0")}</span><div><strong>{trace.name ?? `执行片段 ${index + 1}`}</strong><small>{formatDate(trace.started_at)} · {formatDuration(trace.duration_ms)} · {trace.provider_calls.length} calls{segmentTokens ? ` · ${segmentTokens.toLocaleString()} tokens` : ""}</small></div><span className="softBadge">{trace.status ?? "unknown"}</span><div className="traceActions"><button className="secondaryButton" type="button" onClick={() => { setTab("flow"); document.querySelector(".visualizationPanel")?.scrollIntoView({ behavior: "smooth", block: "start" }); }}>查看执行路径图</button>{trace.url ? <a className="secondaryButton" href={trace.url} target="_blank" rel="noreferrer">LangSmith 调用树 ↗</a> : <span className="unavailable">Trace 链接不可用</span>}</div>{trace.provider_calls.length > 0 && <details className="providerDetails"><summary>查看 Provider calls</summary><div>{trace.provider_calls.map((call) => <div className="providerCall" key={call.call_id}><span><strong>{call.operation ?? call.provider ?? "provider call"}</strong><small>{call.model ?? call.requested_model ?? "模型不可用"}</small></span><span>{formatDuration(call.duration_ms)}</span><span>{call.usage?.total_tokens !== undefined ? `${call.usage.total_tokens} tokens` : "Token 不可用"}</span><code title={call.request_id}>{call.request_id ? compactId(call.request_id) : "Request ID 不可用"}</code><em className={`call-${call.status}`}>{call.status ?? "unknown"}</em></div>)}</div></details>}</article>;
          })}</div>}
        </Panel>
        <Panel title="运行计数器">
          <dl className="counterList"><div><dt>Plan 版本</dt><dd>{run.metrics.plan_versions}</dd></div><div><dt>Supplement</dt><dd>{run.metrics.supplement_rounds}</dd></div><div><dt>Replan</dt><dd>{run.metrics.replan_count}</dd></div><div><dt>Revision</dt><dd>{run.metrics.revision_count}</dd></div><div><dt>失败任务</dt><dd>{run.metrics.failed_tasks}</dd></div><div><dt>工具调用</dt><dd>{run.metrics.tool_calls ?? run.tool_calls.length}</dd></div><div><dt>Provider 成本</dt><dd title="Provider 返回的原始数值；未假定币种">{totalCost !== undefined ? `${totalCost.toFixed(6)}（原始值）` : "成本不可用"}</dd></div><div><dt>最新 Request ID</dt><dd title={latestProviderCall?.request_id}>{latestProviderCall?.request_id ? compactId(latestProviderCall.request_id) : "不可用"}</dd></div><div><dt>实际模型</dt><dd title={latestProviderCall?.model}>{latestProviderCall?.model ?? "不可用"}</dd></div></dl>
        </Panel>
      </div>
      {confirmAction && <ConfirmDialog title={confirmAction === "cancel" ? "确认取消运行？" : "确认重新运行？"} description={confirmAction === "cancel" ? "运行将立即停止；已有的事件和追踪记录仍会保留。" : "系统会用相同参数创建新运行，再次调用模型可能产生费用。"} confirmLabel={confirmAction === "cancel" ? "确认取消" : "创建新运行"} cancelLabel="继续查看" tone={confirmAction === "cancel" ? "danger" : "default"} onCancel={() => setConfirmAction(null)} onConfirm={() => { if (confirmAction === "cancel") void handleCancel(); else void handleRerun(); }} />}
    </main>
  );
}

function ReviewHistory({ run }: { run: NonNullable<ReturnType<typeof useRunStream>["run"]> }) {
  if (!run.critique_history.length && !run.quality_history.length) return <EmptyState title="尚无审查决策" description="Critic 与 Quality 的结构化输出会按版本保留。" />;
  return <div className="reviewColumns"><section><h2>Critic</h2>{run.critique_history.map((item, index) => <article className="reviewCard" key={`critic-${index}`}><div><span className={`decisionTag decision-${item.decision}`}>{item.decision}</span><small>Plan v{item.plan_version}</small></div><p>{item.rationale}</p>{item.replan_reason && <code>reason: {item.replan_reason}</code>}</article>)}</section><section><h2>Quality Gate</h2>{run.quality_history.map((item, index) => <article className="reviewCard" key={`quality-${index}`}><div><span className={`decisionTag decision-${item.decision}`}>{item.decision}</span><small>Plan v{item.plan_version} · Draft v{item.draft_version}</small></div><strong>{item.score !== undefined ? `${item.score}/100` : "评分不可用"}</strong><p>{item.rationale}</p></article>)}</section></div>;
}

function deriveRunFromEvents(run: RunRecord, events: RunEvent[]): RunRecord {
  let next = run;
  const planEvent = [...events].reverse().find((event) => event.event_type === "plan_created" || event.data.active_plan || event.data.plan);
  if (planEvent) {
    const rawCandidate = asObject(planEvent.data.active_plan ?? planEvent.data.plan);
    const rawTasks = Array.isArray(rawCandidate.tasks) ? rawCandidate.tasks : Array.isArray(planEvent.data.tasks) ? planEvent.data.tasks : [];
    const tasks = rawTasks.map(parseLiveTask).filter((task): task is ResearchTask => task !== null);
    if (tasks.length) {
      const version = typeof rawCandidate.plan_version === "number" ? rawCandidate.plan_version : planEvent.plan_version ?? tasks[0].plan_version ?? 1;
      const candidate: ResearchPlan = { plan_version: version, rationale: typeof rawCandidate.rationale === "string" ? rawCandidate.rationale : "由实时事件恢复的活动计划", tasks };
      if (!run.plan || version > run.plan.plan_version || (version === run.plan.plan_version && tasks.length > run.plan.tasks.length)) {
        const plans = [...run.plans.filter((item) => item.plan_version !== version), candidate].sort((a, b) => a.plan_version - b.plan_version);
        next = { ...next, plan: candidate, plans };
      }
    }
  }
  const lineageEvent = [...events].reverse().find((event) => asObject(event.data.plan_lineage).base_plan);
  if (lineageEvent) {
    const rawLineage = asObject(lineageEvent.data.plan_lineage);
    if (typeof rawLineage.plan_version === "number" && Array.isArray(rawLineage.supplements)) {
      next = { ...next, plan_lineage: rawLineage as PlanLineage };
    }
  }
  const reviewEvents = events.filter((event) => event.event_type === "review_decided");
  if (reviewEvents.length > next.critique_history.length) {
    const missing = reviewEvents.slice(next.critique_history.length).map((event) => ({
      decision: String(event.data.decision ?? "unknown"),
      rationale: event.decision_reason ?? String(event.data.decision_reason ?? event.message),
      plan_version: event.plan_version ?? next.plan?.plan_version ?? 1,
      replan_reason: typeof event.data.replan_reason === "string" ? event.data.replan_reason : null,
      created_at: event.created_at,
    }));
    next = { ...next, critique_history: [...next.critique_history, ...missing] };
  }
  const qualityEvents = events.filter((event) => event.event_type === "quality_evaluated");
  if (qualityEvents.length > next.quality_history.length) {
    const missing = qualityEvents.slice(next.quality_history.length).map((event) => ({
      decision: String(event.data.decision ?? "unknown"),
      score: typeof event.data.score === "number" ? event.data.score : undefined,
      rationale: event.decision_reason ?? String(event.data.rationale ?? event.message),
      plan_version: event.plan_version ?? next.plan?.plan_version ?? 1,
      draft_version: typeof event.data.draft_version === "number" ? event.data.draft_version : 1,
      created_at: event.created_at,
    }));
    next = { ...next, quality_history: [...next.quality_history, ...missing] };
  }
  return next;
}

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function parseLiveTask(value: unknown): ResearchTask | null {
  const task = asObject(value);
  if (typeof task.task_id !== "string" || typeof task.title !== "string") return null;
  return {
    task_id: task.task_id,
    title: task.title,
    objective: typeof task.objective === "string" ? task.objective : "目标尚未写入快照",
    success_criteria: Array.isArray(task.success_criteria) ? task.success_criteria.filter((item): item is string => typeof item === "string") : [],
    priority: typeof task.priority === "number" ? task.priority : 0,
    dependencies: Array.isArray(task.dependencies) ? task.dependencies.filter((item): item is string => typeof item === "string") : [],
    plan_version: typeof task.plan_version === "number" ? task.plan_version : 1,
    requires_fresh_data: task.requires_fresh_data === true,
    required_capabilities: Array.isArray(task.required_capabilities) ? task.required_capabilities.filter((item): item is string => typeof item === "string") : [],
    provenance: typeof task.provenance === "string" ? task.provenance : undefined,
  };
}
