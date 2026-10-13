"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { cancelRun, rerun } from "../lib/api";
import { compactId, formatDate, formatDuration } from "../lib/format";
import { PlanLineage, ResearchPlan, ResearchTask, RunEvent, RunRecord, TERMINAL_STATUSES } from "../lib/types";
import { useRunStream } from "../hooks/use-run-stream";
import { ApprovalPanel } from "./approval-panel";
import { ConfirmDialog } from "./confirm-dialog";
import { EventTimeline } from "./event-timeline";
import { ReportView } from "./report-view";
import { EmptyState, LoadingBlock, MetricCard, Panel, StatusBadge } from "./ui";

const AgentFlow = dynamic(() => import("./agent-flow").then((module) => module.AgentFlow), { ssr: false, loading: () => <LoadingBlock label="加载Agent拓扑" /> });
const TaskGraph = dynamic(() => import("./task-graph").then((module) => module.TaskGraph), { ssr: false, loading: () => <LoadingBlock label="加载任务依赖图" /> });

type Tab = "flow" | "tasks" | "events" | "reviews" | "report";

export function RunDetail({ runId }: { runId: string }) {
  const router = useRouter();
  const { run, events, streamState, error, refresh, setRun } = useRunStream(runId);
  const [selectedTab, setTab] = useState<Tab | null>(null);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState<boolean | null>(null);
  const [now, setNow] = useState<number | null>(null);
  const [copied, setCopied] = useState(false);
  const diagnostics = useRef<HTMLDetailsElement>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<"cancel" | "rerun" | null>(null);
  const visualRun = useMemo(() => run ? deriveRunFromEvents(run, events) : null, [events, run]);
  const agentLabel = useMemo(() => events.at(-1)?.agent ?? events.at(-1)?.node ?? "等待调度", [events]);

  useEffect(() => {
    setTab(null);
    setDiagnosticsOpen(null);
    setCopied(false);
  }, [runId]);

  useEffect(() => {
    if (!run?.started_at || TERMINAL_STATUSES.has(run.status)) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [run?.started_at, run?.status]);

  async function copyRunId() {
    try {
      await navigator.clipboard.writeText(runId);
      setCopied(true);
    } catch {
      setActionError("无法复制运行编号，请手动复制。");
    }
  }

  function showDiagnostics() {
    setDiagnosticsOpen(true);
    diagnostics.current?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start" });
  }

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

  if (!run && !error) return <main className="pageContent"><LoadingBlock label="加载运行快照" /></main>;
  if (!run) return <main className="pageContent"><Panel><EmptyState title="运行记录读取失败" description={error ?? "记录不存在或后端暂不可用。"} /><Link className="secondaryButton inlineButton" href="/runs">返回运行记录</Link></Panel></main>;
  const activeRun = visualRun ?? run;
  const latestQuality = activeRun.quality_history.at(-1);
  const isTerminal = TERMINAL_STATUSES.has(run.status);
  const tab = selectedTab ?? (run.status === "failed" ? "events" : isTerminal && run.report?.trim() ? "report" : "flow");
  const startedAt = run.started_at ? Date.parse(run.started_at) : NaN;
  const duration = isTerminal ? run.metrics.duration_ms : now !== null && Number.isFinite(startedAt) ? Math.max(0, now - startedAt) : undefined;
  const failedToolCalls = run.tool_calls.filter((call) => !call.success).length;
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
        <div><div className="runHeaderMeta"><StatusBadge status={run.status} /><span className={`streamIndicator ${streamState}`}><i />{streamState === "live" ? "实时连接" : streamState === "reconnecting" ? "正在重连" : streamState === "connecting" ? "连接中" : "快照模式"}</span></div><h1>{run.query}</h1><p><code title={run.id}>{compactId(run.id)}</code><button className="copyMini" type="button" aria-label="复制完整运行编号" onClick={() => void copyRunId()}>{copied ? "已复制" : "复制"}</button><span>{formatDate(run.created_at)}</span><span>知识库：{run.policy?.knowledge_mode === "off" ? "不使用" : run.policy?.knowledge_mode === "selected" ? run.policy.knowledge_space : "系统默认空间"}</span></p></div>
        <div className="headerActions"><button className="secondaryButton" type="button" onClick={showDiagnostics}>运行诊断</button>{!isTerminal && <button className="secondaryButton dangerText" disabled={actionBusy} onClick={() => setConfirmAction("cancel")}>取消运行</button>}<button className="primaryButton" disabled={actionBusy} onClick={() => setConfirmAction("rerun")}>重新运行</button></div>
      </header>
      {(error || actionError) && <div className="inlineAlert errorAlert"><strong>请求未完成</strong><span>{actionError ?? error}</span></div>}
      {run.status === "waiting_approval" && <ApprovalPanel run={run} onResolved={(value) => { setRun(value); void refresh(); }} />}

      <div className="runSummary">
        <MetricCard label="任务进度" value={`${run.metrics.successful_tasks} / ${run.metrics.total_tasks}`} note={isTerminal ? `${run.metrics.failed_tasks}个失败任务` : `当前节点${agentLabel}`} />
        <MetricCard label={isTerminal ? "运行时长" : "经过时间"} value={duration === undefined ? "未记录" : formatDuration(duration)} note={isTerminal ? "本次运行" : "从运行开始计算"} />
        <MetricCard label="质量评分" value={latestQuality?.score !== undefined ? `${latestQuality.score} / 100` : "未评估"} note={latestQuality?.decision ?? "报告生成后验收"} />
      </div>

      <Panel className="visualizationPanel">
        <div className="tabBar" role="tablist">
          {([['report', '研究报告'], ['flow', 'Agent流程'], ['tasks', 'TaskDAG'], ['events', `执行事件 ${events.length}`], ['reviews', '审查决策']] as [Tab, string][]).map(([value, label]) => <button type="button" role="tab" id={`run-tab-${value}`} aria-controls={`run-panel-${value}`} aria-selected={tab === value} className={tab === value ? "active" : ""} key={value} onClick={() => setTab(value)}>{label}</button>)}
        </div>
        <div className="tabContent" role="tabpanel" id={`run-panel-${tab}`} aria-labelledby={`run-tab-${tab}`}>
          {tab === "flow" && <><div className="tabIntro"><div><h2>Agent执行拓扑</h2><p>展示实际执行路径、补充任务、重新规划与稿件修订。</p></div><div className="graphLegend"><span><i className="legendDot running" />执行中</span><span><i className="legendDot completed" />已执行</span><span><i className="legendLine normal" />主线</span><span><i className="legendLine supplement" />补充</span><span><i className="legendLine replan" />重规划</span><span><i className="legendLine revise" />修订</span><span><i className="legendLine fallback" />决策改道</span></div></div><AgentFlow events={events} status={run.status} /></>}
          {tab === "tasks" && <><div className="tabIntro"><div><h2>任务依赖与计划演进</h2><p>基础计划、补充批次和历史版本可独立追溯。</p></div></div><TaskGraph run={activeRun} events={events} /></>}
          {tab === "events" && <div className="eventTab"><div className="tabIntro"><div><h2>事件时间线</h2><p>执行、工具调用与决策按实际发生顺序记录。</p></div><span className="panelHint">最新在前</span></div><EventTimeline events={events} /></div>}
          {tab === "reviews" && <ReviewHistory run={activeRun} />}
          {tab === "report" && <ReportView run={run} />}
        </div>
      </Panel>

      <details className="runDiagnostics" ref={diagnostics} open={diagnosticsOpen ?? (failedToolCalls > 0 || run.status === "failed")} onToggle={(event) => setDiagnosticsOpen(event.currentTarget.open)}>
        <summary className="diagnosticSummary"><span><strong>运行诊断</strong><small>工具证据、LangSmith追踪及Provider调用</small></span><span className="diagnosticCounts">{run.tool_calls.length}次工具调用{failedToolCalls > 0 ? `，${failedToolCalls}次失败` : ""}<span>{run.trace_segments.length}条追踪</span></span></summary>
        <div className="diagnosticBody">
      <Panel title="工具调用与来源" meta={<span className="panelHint">{run.tool_calls.length} 次调用 · {run.evidence.length} 条来源</span>} className="toolPanel runTools">
        {!run.tool_calls.length ? <div className="emptyInline">尚无工具调用；运行中的请求会先出现在事件时间线。</div> : <div className="toolCallList">{run.tool_calls.map((call) => {
          const sources = run.evidence.filter((item) => call.evidence_ids.includes(item.id));
          const navigationUrls = (call.navigation_urls?.length ? call.navigation_urls : call.navigation_url ? [call.navigation_url] : []).filter((url) => url.startsWith("https://uri.amap.com/"));
          return <article key={call.call_id}>
            <div className="toolCallHead"><strong>{call.tool_name}</strong><span className={`softBadge ${call.success ? "" : "amber"}`}>{call.success ? "已完成" : "失败"}</span><small>{call.agent}{call.task_id ? `·${call.task_id}` : ""}·{formatDuration(call.duration_ms)}</small></div>
            <p>{call.success ? call.summary ?? "结果摘要未记录" : call.error ?? call.summary ?? "错误详情未记录"}</p>
            {sources.length > 0 && <div className="toolSources">{sources.map((source) => source.uri?.startsWith("https://") || source.uri?.startsWith("http://") ? <a key={source.id} href={source.uri} target="_blank" rel="noreferrer">{source.title} ↗</a> : <span key={source.id}>{source.title}</span>)}</div>}
            {navigationUrls.map((url, index) => <a key={`${index}-${url}`} className="toolNavigation" href={url} target="_blank" rel="noreferrer">{navigationUrls.length > 1 ? `第 ${index + 1} 段导航` : "打开高德导航"} ↗</a>)}
          </article>;
        })}</div>}
      </Panel>

      <div className="detailBottomGrid">
        <Panel title="LangSmith追踪" meta={<span className="panelHint">{run.trace_segments.length}条记录</span>}>
          <p className="traceHelp">LangSmith提供模型输入、输出与耗时；执行路径图展示Agent分支与回环。</p>
          {!run.trace_segments.length ? <div className="emptyInline">尚未记录追踪片段。</div> : <div className="traceList">{run.trace_segments.map((trace, index) => {
            const segmentTokens = trace.provider_calls.map((call) => call.usage?.total_tokens).filter((value): value is number => typeof value === "number").reduce((sum, value) => sum + value, 0);
            return <article key={trace.id}>
              <span className="traceIndex">{String(index + 1).padStart(2, "0")}</span>
              <div><strong>{trace.name ?? `执行片段${index + 1}`}</strong><small>{formatDate(trace.started_at)}，{formatDuration(trace.duration_ms)}，{trace.provider_calls.length}次模型调用{segmentTokens ? `，${segmentTokens.toLocaleString()}Token` : ""}</small></div>
              <span className="softBadge">{trace.status ?? "未记录"}</span>
              <div className="traceActions"><button className="secondaryButton" type="button" onClick={() => { setTab("flow"); document.querySelector(".visualizationPanel")?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start" }); }}>查看执行路径图</button>{trace.url ? <a className="secondaryButton" href={trace.url} target="_blank" rel="noreferrer">LangSmith调用树 ↗</a> : <span className="unavailable">追踪链接未记录</span>}</div>
              {trace.provider_calls.length > 0 && <details className="providerDetails"><summary>Provider调用详情</summary><div>{trace.provider_calls.map((call) => <div className="providerCall" key={call.call_id}><span><strong>{call.operation ?? call.provider ?? "Provider调用"}</strong><small>{call.model ?? call.requested_model ?? "未记录"}</small></span><span>{formatDuration(call.duration_ms)}</span><span>{call.usage?.total_tokens !== undefined ? `${call.usage.total_tokens}Token` : "未记录"}</span><code title={call.request_id}>{call.request_id ? compactId(call.request_id) : "RequestID未记录"}</code><em className={`call-${call.status}`}>{call.status ?? "未记录"}</em></div>)}</div></details>}
            </article>;
          })}</div>}
        </Panel>
        <Panel title="调用统计">
          <dl className="counterList"><div><dt>计划版本数</dt><dd>{run.metrics.plan_versions}</dd></div><div><dt>补充轮次</dt><dd>{run.metrics.supplement_rounds}</dd></div><div><dt>重规划次数</dt><dd>{run.metrics.replan_count}</dd></div><div><dt>稿件修订次数</dt><dd>{run.metrics.revision_count}</dd></div><div><dt>失败任务</dt><dd>{run.metrics.failed_tasks}</dd></div><div><dt>工具调用</dt><dd>{run.metrics.tool_calls ?? run.tool_calls.length}</dd></div><div><dt>模型调用</dt><dd>{providerCalls.length || run.metrics.model_calls}</dd></div><div><dt>Token总数</dt><dd>{totalTokens !== undefined ? totalTokens.toLocaleString() : "未记录"}</dd></div><div><dt>Provider成本</dt><dd title="Provider返回的原始数值；未假定币种">{totalCost !== undefined ? `${totalCost.toFixed(6)}（原始值）` : "未记录"}</dd></div><div><dt>最新RequestID</dt><dd title={latestProviderCall?.request_id}>{latestProviderCall?.request_id ? compactId(latestProviderCall.request_id) : "未记录"}</dd></div><div><dt>实际模型</dt><dd title={latestProviderCall?.model}>{latestProviderCall?.model ?? "未记录"}</dd></div></dl>
        </Panel>
      </div>
        </div>
      </details>
      {confirmAction && <ConfirmDialog title={confirmAction === "cancel" ? "确认取消运行？" : "确认重新运行？"} description={confirmAction === "cancel" ? "运行将立即终止，已产生的事件与追踪记录仍保留。" : "将以原始参数创建新运行，模型调用可能产生费用。"} confirmLabel={confirmAction === "cancel" ? "确认取消" : "创建新运行"} cancelLabel="继续查看" tone={confirmAction === "cancel" ? "danger" : "default"} onCancel={() => setConfirmAction(null)} onConfirm={() => { if (confirmAction === "cancel") void handleCancel(); else void handleRerun(); }} />}
    </main>
  );
}

function ReviewHistory({ run }: { run: NonNullable<ReturnType<typeof useRunStream>["run"]> }) {
  if (!run.critique_history.length && !run.quality_history.length) return <EmptyState title="暂无审查决策" description="Critic审查与质量验收结论按计划版本归档。" />;
  return <div className="reviewColumns"><section><h2>Critic审查</h2>{run.critique_history.map((item, index) => <article className="reviewCard" key={`critic-${index}`}><div><span className={`decisionTag decision-${item.decision}`}>{item.decision}</span><small>计划v{item.plan_version}</small></div><p>{item.rationale}</p>{item.replan_reason && <code>重规划原因：{item.replan_reason}</code>}</article>)}</section><section><h2>质量验收</h2>{run.quality_history.map((item, index) => <article className="reviewCard" key={`quality-${index}`}><div><span className={`decisionTag decision-${item.decision}`}>{item.decision}</span><small>计划v{item.plan_version}，稿件v{item.draft_version}</small></div><strong>{item.score !== undefined ? `${item.score}/100` : "未评估"}</strong><p>{item.rationale}</p></article>)}</section></div>;
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
    capability_alternatives: Array.isArray(task.capability_alternatives) ? task.capability_alternatives.filter(Array.isArray).map((group) => group.filter((item: unknown): item is string => typeof item === "string")) : [],
    provenance: typeof task.provenance === "string" ? task.provenance : undefined,
  };
}
