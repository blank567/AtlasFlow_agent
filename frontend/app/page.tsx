"use client";

import { FormEvent, useRef, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

const EVENT_TYPES = [
  "run_started",
  "node_started",
  "node_succeeded",
  "node_failed",
  "plan_created",
  "approval_required",
  "approval_resolved",
  "task_scheduled",
  "task_completed",
  "review_decided",
  "route_selected",
  "quality_evaluated",
  "run_completed",
  "run_degraded",
  "run_failed",
  "run_cancelled",
] as const;

type RunStatus =
  | "pending"
  | "running"
  | "waiting_approval"
  | "completed"
  | "completed_with_warnings"
  | "failed"
  | "cancelled";

type ResearchTask = {
  task_id: string;
  title: string;
  objective: string;
  success_criteria: string[];
  priority: number;
  dependencies: string[];
  plan_version: number;
};

type ResearchPlan = {
  plan_version: number;
  rationale: string;
  tasks: ResearchTask[];
};

type ResearchResult = {
  task_id: string;
  plan_version: number;
  summary: string;
  confidence: number;
  attempt: number;
  duration_ms: number;
};

type AgentError = {
  agent: string;
  code: string;
  message: string;
  task_id?: string;
  plan_version?: number;
};

type CritiqueDecision = {
  decision: "accept" | "supplement" | "replan";
  rationale: string;
  plan_version: number;
};

type QualityDecision = {
  decision: "accept" | "revise" | "replan";
  score: number;
  rationale: string;
  plan_version: number;
  draft_version: number;
};

type ExecutionMetrics = {
  total_tasks: number;
  successful_tasks: number;
  failed_tasks: number;
  peak_concurrency: number;
  plan_versions: number;
  supplement_rounds: number;
  replan_count: number;
  revision_count: number;
  model_calls: number;
  duration_ms?: number;
};

type RunEvent = {
  event_id: string;
  sequence: number;
  event_type: string;
  message: string;
  agent?: string;
  node?: string;
  task_id?: string;
  status?: string;
  created_at: string;
  data: Record<string, unknown>;
};

type Run = {
  id: string;
  query: string;
  status: RunStatus;
  plan?: ResearchPlan;
  plans: ResearchPlan[];
  research_results: ResearchResult[];
  critique_history: CritiqueDecision[];
  quality_history: QualityDecision[];
  errors: AgentError[];
  metrics: ExecutionMetrics;
  warnings: string[];
  report?: string;
  error?: string;
};

const EMPTY_METRICS: ExecutionMetrics = {
  total_tasks: 0,
  successful_tasks: 0,
  failed_tasks: 0,
  peak_concurrency: 0,
  plan_versions: 1,
  supplement_rounds: 0,
  replan_count: 0,
  revision_count: 0,
  model_calls: 0,
};

export default function Home() {
  const [query, setQuery] = useState("评估一个多 Agent 研究系统的架构、风险与改进方案");
  const [autoApprove, setAutoApprove] = useState(true);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editedPlan, setEditedPlan] = useState("");
  const source = useRef<EventSource | null>(null);

  async function refreshRun(runId: string) {
    const response = await fetch(`${API_URL}/runs/${runId}`);
    if (response.ok) {
      const current: Run = await response.json();
      setRun(current);
      if (current.status === "waiting_approval" || isTerminal(current.status)) {
        setBusy(false);
      }
    }
  }

  function connectEvents(runId: string) {
    const stream = new EventSource(`${API_URL}/runs/${runId}/events`);
    source.current = stream;
    EVENT_TYPES.forEach((type) => {
      stream.addEventListener(type, (message) => {
        const event = JSON.parse((message as MessageEvent).data) as RunEvent;
        setEvents((current) => [...current, event]);
        if (type === "approval_required") {
          window.setTimeout(() => void refreshRun(runId), 150);
        }
      });
    });
    stream.addEventListener("done", async () => {
      stream.close();
      await refreshRun(runId);
      setBusy(false);
    });
    stream.onerror = () => {
      stream.close();
      void refreshRun(runId);
      setBusy(false);
    };
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setEvents([]);
    setEditing(false);
    source.current?.close();

    try {
      const response = await fetch(`${API_URL}/runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, auto_approve: autoApprove }),
      });
      if (!response.ok) throw new Error(`创建任务失败：${response.status}`);
      const created: Run = await response.json();
      setRun(created);
      connectEvents(created.id);
    } catch (error) {
      setBusy(false);
      setRun({
        id: "local-error",
        query,
        status: "failed",
        plans: [],
        research_results: [],
        critique_history: [],
        quality_history: [],
        errors: [],
        metrics: EMPTY_METRICS,
        warnings: [],
        error: error instanceof Error ? error.message : "未知错误",
      });
    }
  }

  async function resolveApproval(action: "approve" | "edit" | "cancel") {
    if (!run) return;
    setApprovalBusy(true);
    try {
      const payload: Record<string, unknown> = { action };
      if (action === "edit") payload.edited_plan = JSON.parse(editedPlan);
      const response = await fetch(`${API_URL}/runs/${run.id}/approval`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`审批失败：${response.status} ${detail}`);
      }
      setRun(await response.json());
      setEditing(false);
      setBusy(true);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "审批失败");
    } finally {
      setApprovalBusy(false);
    }
  }

  function openEditor() {
    if (!run?.plan) return;
    setEditedPlan(JSON.stringify(run.plan, null, 2));
    setEditing(true);
  }

  const latestCritique = run?.critique_history.at(-1);
  const latestQuality = run?.quality_history.at(-1);

  return (
    <main>
      <header className="hero">
        <div className="eyebrow">AUDITABLE MULTI-AGENT RESEARCH</div>
        <h1>AtlasFlow</h1>
        <p>让规划、并行研究、审查、质量门和人工决策出现在同一条可恢复执行链上。</p>
      </header>

      <section className="composer panel">
        <form onSubmit={submit}>
          <label htmlFor="query">研究任务</label>
          <textarea id="query" value={query} onChange={(event) => setQuery(event.target.value)} />
          <button disabled={busy || query.trim().length < 3}>{busy ? "Agents 工作中…" : "开始研究"}</button>
          <label className="checkRow">
            <input type="checkbox" checked={autoApprove} onChange={(event) => setAutoApprove(event.target.checked)} />
            自动批准 Planner 生成的任务 DAG
          </label>
        </form>
      </section>

      {run?.status === "waiting_approval" && (
        <section className="panel approval">
          <div className="panelTitle"><span>计划等待人工审批</span><Status status={run.status} /></div>
          <p className="muted">批准后继续，编辑后会重新校验任务 ID、依赖和 DAG，取消则立即结束。</p>
          {editing && <textarea className="planEditor" value={editedPlan} onChange={(event) => setEditedPlan(event.target.value)} />}
          <div className="actions">
            <button disabled={approvalBusy} onClick={() => void resolveApproval("approve")}>批准</button>
            {!editing ? (
              <button className="secondary" disabled={approvalBusy} onClick={openEditor}>编辑计划</button>
            ) : (
              <button className="secondary" disabled={approvalBusy} onClick={() => void resolveApproval("edit")}>提交编辑</button>
            )}
            <button className="danger" disabled={approvalBusy} onClick={() => void resolveApproval("cancel")}>取消运行</button>
          </div>
        </section>
      )}

      <section className="grid">
        <article className="panel timeline">
          <div className="panelTitle"><span>执行轨迹</span><Status status={run?.status} /></div>
          {events.length === 0 ? (
            <p className="muted">创建任务后，这里会实时显示每个 Agent、路由选择与重试。</p>
          ) : (
            <ol>
              {events.map((item) => (
                <li key={item.event_id}>
                  <span className="dot" />
                  <div>
                    <strong>#{item.sequence} {item.agent ?? item.node ?? item.event_type}</strong>
                    <p>{item.message}{item.task_id ? ` · ${item.task_id}` : ""}</p>
                  </div>
                </li>
              ))}
            </ol>
          )}
        </article>

        <article className="panel metrics">
          <div className="panelTitle"><span>运行摘要</span></div>
          <div className="statGrid">
            <Metric label="计划任务" value={run?.plan?.tasks.length ?? 0} />
            <Metric label="成功研究" value={run?.research_results.length ?? 0} />
            <Metric label="峰值并发" value={run?.metrics.peak_concurrency ?? 0} />
            <Metric label="模型调用" value={run?.metrics.model_calls ?? 0} />
          </div>
          <h3>最新决策</h3>
          <div className="decisionStack">
            <p><span>Critic</span>{latestCritique ? `${latestCritique.decision} · ${latestCritique.rationale}` : "等待审查"}</p>
            <p><span>Quality</span>{latestQuality ? `${latestQuality.score}/100 · ${latestQuality.decision}` : "等待验收"}</p>
          </div>
          {run?.warnings.map((warning) => <p className="warning" key={warning}>{warning}</p>)}
          {run?.error && <p className="error">{run.error}</p>}
        </article>
      </section>

      <section className="panel planPanel">
        <div className="panelTitle">
          <span>任务 DAG</span>
          <small>{run?.plan ? `PLAN v${run.plan.plan_version}` : "WAITING"}</small>
        </div>
        {!run?.plan ? <p className="muted">Planner 尚未生成计划。</p> : (
          <div className="taskGrid">
            {run.plan.tasks.map((task) => {
              const result = run.research_results.find((item) => item.task_id === task.task_id && item.plan_version === task.plan_version);
              const failed = run.errors.find((item) => item.task_id === task.task_id && item.plan_version === task.plan_version && item.code === "research_failed");
              const state = result ? "完成" : failed ? "失败" : "等待";
              return (
                <article className="taskCard" key={`${task.plan_version}-${task.task_id}`}>
                  <div className="taskHeader"><strong>{task.task_id} · {task.title}</strong><span>{state}</span></div>
                  <p>{task.objective}</p>
                  <small>依赖：{task.dependencies.length ? task.dependencies.join(", ") : "无"} · 优先级 {task.priority}</small>
                  {result && <small>置信度 {Math.round(result.confidence * 100)}% · attempt {result.attempt} · {result.duration_ms}ms</small>}
                </article>
              );
            })}
          </div>
        )}
      </section>

      <section className="panel report">
        <div className="panelTitle"><span>研究报告</span><small>{run?.id ? `RUN ${run.id.slice(0, 8)}` : "WAITING"}</small></div>
        <pre>{run?.report ?? "报告将在 Synthesizer 与 QualityGate 完成后出现。"}</pre>
      </section>
    </main>
  );
}

function isTerminal(status: RunStatus) {
  return ["completed", "completed_with_warnings", "failed", "cancelled"].includes(status);
}

function Metric({ label, value }: { label: string; value: number }) {
  return <div className="metric"><strong>{value}</strong><span>{label}</span></div>;
}

function Status({ status }: { status?: RunStatus }) {
  const label = status ?? "idle";
  return <span className={`status ${label}`}>{label.toUpperCase()}</span>;
}
