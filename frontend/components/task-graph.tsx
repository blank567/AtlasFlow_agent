"use client";

import { useEffect, useMemo, useState } from "react";
import { Background, Controls, Edge, MarkerType, Node, ReactFlow } from "@xyflow/react";
import { ResearchTask, RunEvent, RunRecord, SupplementBatch } from "../lib/types";
import { formatDuration } from "../lib/format";
import { DetailDrawer, DrawerSection } from "./detail-drawer";

export function TaskGraph({ run, events }: { run: RunRecord; events: RunEvent[] }) {
  const allPlans = run.plans.length ? run.plans : run.plan ? [run.plan] : [];
  const [selectedVersion, setSelectedVersion] = useState(run.plan?.plan_version ?? allPlans.at(-1)?.plan_version ?? 1);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  useEffect(() => { if (run.plan?.plan_version) setSelectedVersion(run.plan.plan_version); }, [run.plan?.plan_version]);
  const plan = allPlans.find((item) => item.plan_version === selectedVersion) ?? run.plan ?? allPlans.at(-1);

  const { nodes, edges } = useMemo(() => {
    if (!plan) return { nodes: [] as Node[], edges: [] as Edge[] };
    const levels = new Map<string, number>();
    const level = (taskId: string, seen = new Set<string>()): number => {
      if (levels.has(taskId)) return levels.get(taskId)!;
      if (seen.has(taskId)) return 0;
      seen.add(taskId);
      const task = plan.tasks.find((item) => item.task_id === taskId);
      const value = !task?.dependencies.length ? 0 : Math.max(...task.dependencies.map((dep) => level(dep, seen))) + 1;
      levels.set(taskId, value);
      return value;
    };
    plan.tasks.forEach((task) => level(task.task_id));
    const counters = new Map<number, number>();
    const nextNodes: Node[] = plan.tasks.map((task) => {
      const xLevel = levels.get(task.task_id) ?? 0;
      const row = counters.get(xLevel) ?? 0;
      counters.set(xLevel, row + 1);
      const result = run.research_results.find((item) => item.task_id === task.task_id && item.plan_version === task.plan_version);
      const failed = run.errors.some((item) => item.task_id === task.task_id && item.plan_version === task.plan_version);
      const liveEvent = [...events].reverse().find((event) => event.task_id === task.task_id && event.plan_version === task.plan_version);
      const liveConfidence = typeof liveEvent?.data.confidence === "number" ? liveEvent.data.confidence : undefined;
      const state = result || liveEvent?.status === "succeeded" ? "completed" : failed || liveEvent?.status === "failed" ? "failed" : liveEvent?.status === "running" || liveEvent?.status === "scheduled" ? "running" : "waiting";
      return {
        id: task.task_id,
        position: { x: xLevel * 280, y: row * 150 },
        data: { label: <div className="taskNodeLabel"><div><code>{task.task_id}</code><span className={`nodeState ${state}`} /></div><strong>{task.title}</strong><small>{task.objective}</small>{(result?.confidence ?? liveConfidence) !== undefined && <em>置信度 {Math.round((result?.confidence ?? liveConfidence ?? 0) * 100)}%</em>}</div> },
        className: `taskNode node-${state}`,
        style: { width: 230 },
      };
    });
    const nextEdges: Edge[] = plan.tasks.flatMap((task) => task.dependencies.map((dependency) => ({
      id: `${dependency}-${task.task_id}`, source: dependency, target: task.task_id,
      markerEnd: { type: MarkerType.ArrowClosed, color: "#98a4b5" }, style: { stroke: "#aeb8c6", strokeWidth: 1.4 },
    })));
    return { nodes: nextNodes, edges: nextEdges };
  }, [events, plan, run.errors, run.research_results]);

  if (!plan) return <div className="emptyInline">Planner 尚未生成任务 DAG。</div>;
  const supplementIds = new Set(run.plan_lineage?.supplements?.flatMap(supplementTaskIds) ?? []);
  return (
    <div className="taskGraphLayout">
      <aside className="planLineage">
        <span className="sectionLabel">Plan lineage</span>
        {allPlans.map((item) => <button key={item.plan_version} className={item.plan_version === plan.plan_version ? "active" : ""} onClick={() => setSelectedVersion(item.plan_version)}><span>v{item.plan_version}</span><small>{item.tasks.length} tasks</small></button>)}
        {!!run.plan_lineage?.supplements?.length && <div className="supplementList"><strong>增量补充</strong>{run.plan_lineage.supplements.map((batch) => { const ids = supplementTaskIds(batch); return <p key={`${batch.round}-${ids.join("-")}`}><span>Round {batch.round}</span>{ids.join(", ")}</p>; })}</div>}
      </aside>
      <div className="taskCanvasWrap">
        <div className="graphLegend"><span><i className="legendDot initial" />初始任务</span><span><i className="legendDot supplement" />补充任务</span></div>
        <div className="flowCanvas taskFlowCanvas">
          <ReactFlow nodes={nodes.map((node) => ({ ...node, className: `${node.className} ${supplementIds.has(node.id) ? "supplementNode" : ""}` }))} edges={edges} fitView fitViewOptions={{ padding: 0.24 }} minZoom={0.4} maxZoom={1.5} nodesDraggable={false} nodesConnectable={false} onNodeClick={(_, node) => setSelectedTaskId(node.id)}>
            <Background color="#dce1e9" gap={20} size={1} /><Controls showInteractive={false} />
          </ReactFlow>
          {selectedTaskId && <TaskDrawer task={plan.tasks.find((item) => item.task_id === selectedTaskId)} run={run} events={events} onClose={() => setSelectedTaskId(null)} />}
        </div>
      </div>
    </div>
  );
}

function TaskDrawer({ task, run, events, onClose }: { task?: ResearchTask; run: RunRecord; events: RunEvent[]; onClose: () => void }) {
  if (!task) return null;
  const taskEvents = events.filter((event) => event.task_id === task.task_id && event.plan_version === task.plan_version);
  const lastEvent = taskEvents.at(-1);
  const result = [...run.research_results].reverse().find((item) => item.task_id === task.task_id && item.plan_version === task.plan_version);
  const error = [...run.errors].reverse().find((item) => item.task_id === task.task_id && item.plan_version === task.plan_version);
  const completedEvent = [...taskEvents].reverse().find((event) => event.event_type === "task_completed");
  const liveResult = asObject(completedEvent?.data.result);
  const liveConfidence = typeof liveResult.confidence === "number" ? liveResult.confidence : completedEvent?.data.confidence;
  const summary = result?.summary ?? (typeof liveResult.summary === "string" ? liveResult.summary : undefined);
  const liveError = typeof completedEvent?.data.error === "string" ? completedEvent.data.error : undefined;
  return <DetailDrawer title={`${task.task_id} · ${task.title}`} subtitle={`Plan v${task.plan_version}`} onClose={onClose}>
    <div className="drawerStats"><div><span>状态</span><strong>{result || completedEvent?.status === "succeeded" ? "完成" : error || completedEvent?.status === "failed" ? "失败" : lastEvent?.status ?? "等待"}</strong></div><div><span>置信度</span><strong>{typeof result?.confidence === "number" ? `${Math.round(result.confidence * 100)}%` : typeof liveConfidence === "number" ? `${Math.round(liveConfidence * 100)}%` : "不可用"}</strong></div><div><span>耗时</span><strong>{formatDuration(result?.duration_ms ?? completedEvent?.duration_ms ?? [...taskEvents].reverse().find((event) => event.duration_ms !== undefined)?.duration_ms)}</strong></div></div>
    <DrawerSection title="任务输入"><dl className="drawerDefinition"><div><dt>研究目标</dt><dd>{task.objective}</dd></div><div><dt>依赖</dt><dd>{task.dependencies.length ? task.dependencies.join(", ") : "无"}</dd></div><div><dt>优先级</dt><dd>{task.priority}</dd></div></dl></DrawerSection>
    <DrawerSection title="成功标准"><ul className="criteriaList">{task.success_criteria.map((criterion) => <li key={criterion}>{criterion}</li>)}</ul></DrawerSection>
    <DrawerSection title="研究结果">{summary ? <p>{summary}</p> : <p className="drawerMuted">结果尚未写入 RunRecord。</p>}{error && <p className="drawerError">{error.code} · {error.message}</p>}{!error && liveError && <p className="drawerError">{liveError}</p>}</DrawerSection>
    <DrawerSection title="实时事件"><ol className="drawerEvents">{taskEvents.map((event) => <li key={event.event_id}><span>#{event.sequence}</span>{event.message}</li>)}</ol></DrawerSection>
  </DetailDrawer>;
}

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function supplementTaskIds(batch: SupplementBatch): string[] {
  if (Array.isArray(batch.task_ids)) return batch.task_ids;
  return batch.tasks.map((task) => task.task_id);
}
