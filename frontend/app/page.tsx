"use client";

import { FormEvent, useRef, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

type RunEvent = {
  type: string;
  message: string;
  agent?: string;
  created_at: string;
  data: Record<string, unknown>;
};

type Run = {
  id: string;
  query: string;
  status: "pending" | "running" | "completed" | "failed";
  plan: string[];
  evidence: Array<{ id: string; title: string; score: number; metadata: Record<string, unknown> }>;
  tool_calls: Array<{ tool_name: string; success: boolean; duration_ms: number }>;
  report?: string;
  error?: string;
};

export default function Home() {
  const [query, setQuery] = useState("AtlasFlow 如何通过工具调用和 RAG 提升多 Agent 结果可信度？");
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const source = useRef<EventSource | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setEvents([]);
    source.current?.close();

    try {
      const response = await fetch(`${API_URL}/runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      if (!response.ok) throw new Error(`创建任务失败：${response.status}`);
      const created: Run = await response.json();
      setRun(created);

      const stream = new EventSource(`${API_URL}/runs/${created.id}/events`);
      source.current = stream;
      ["lifecycle", "agent", "error"].forEach((type) => {
        stream.addEventListener(type, (message) => {
          setEvents((current) => [...current, JSON.parse((message as MessageEvent).data)]);
        });
      });
      stream.addEventListener("done", async () => {
        stream.close();
        const finalResponse = await fetch(`${API_URL}/runs/${created.id}`);
        setRun(await finalResponse.json());
        setBusy(false);
      });
      stream.onerror = () => {
        stream.close();
        setBusy(false);
      };
    } catch (error) {
      setBusy(false);
      setRun({
        id: "local-error",
        query,
        status: "failed",
        plan: [],
        evidence: [],
        tool_calls: [],
        error: error instanceof Error ? error.message : "未知错误",
      });
    }
  }

  return (
    <main>
      <header className="hero">
        <div className="eyebrow">MULTI-AGENT RESEARCH SYSTEM</div>
        <h1>AtlasFlow</h1>
        <p>让计划、检索、工具调用、审查与引用都出现在同一条可观察执行链上。</p>
      </header>

      <section className="composer panel">
        <form onSubmit={submit}>
          <label htmlFor="query">研究任务</label>
          <textarea id="query" value={query} onChange={(event) => setQuery(event.target.value)} />
          <button disabled={busy || query.trim().length < 3}>{busy ? "Agents 工作中…" : "开始研究"}</button>
        </form>
      </section>

      <section className="grid">
        <article className="panel timeline">
          <div className="panelTitle"><span>执行轨迹</span><Status status={run?.status} /></div>
          {events.length === 0 ? (
            <p className="muted">创建任务后，这里会实时显示 Supervisor、工具和各 Agent 的事件。</p>
          ) : (
            <ol>
              {events.map((item, index) => (
                <li key={`${item.created_at}-${index}`}>
                  <span className="dot" />
                  <div><strong>{item.agent ?? item.type}</strong><p>{item.message}</p></div>
                </li>
              ))}
            </ol>
          )}
        </article>

        <article className="panel metrics">
          <div className="panelTitle"><span>运行摘要</span></div>
          <div className="statGrid">
            <Metric label="计划步骤" value={run?.plan.length ?? 0} />
            <Metric label="工具调用" value={run?.tool_calls.length ?? 0} />
            <Metric label="证据数量" value={run?.evidence.length ?? 0} />
          </div>
          <h3>工具调用</h3>
          <div className="chips">
            {run?.tool_calls.map((call, index) => (
              <span className="chip" key={`${call.tool_name}-${index}`}>{call.tool_name} · {call.duration_ms}ms</span>
            )) ?? <span className="muted">尚无调用</span>}
          </div>
          {run?.error && <p className="error">{run.error}</p>}
        </article>
      </section>

      <section className="panel report">
        <div className="panelTitle"><span>研究报告</span><small>{run?.id ? `RUN ${run.id.slice(0, 8)}` : "WAITING"}</small></div>
        <pre>{run?.report ?? "报告将在 Critic 完成质量检查后出现。"}</pre>
      </section>
    </main>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return <div className="metric"><strong>{value}</strong><span>{label}</span></div>;
}

function Status({ status }: { status?: Run["status"] }) {
  const label = status ?? "idle";
  return <span className={`status ${label}`}>{label.toUpperCase()}</span>;
}

