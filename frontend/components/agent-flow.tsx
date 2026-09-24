"use client";

import { useMemo, useState } from "react";
import { Background, Controls, Edge, MarkerType, Node, ReactFlow } from "@xyflow/react";
import { RunEvent, RunStatus } from "../lib/types";
import { formatDate, formatDuration } from "../lib/format";
import { DetailDrawer, DrawerSection } from "./detail-drawer";

const AGENTS = [
  { id: "supervisor", label: "Supervisor", description: "路由与预算控制" },
  { id: "planner", label: "Planner", description: "生成任务 DAG" },
  { id: "researcher", label: "Researcher", description: "并行研究" },
  { id: "critic", label: "Critic", description: "证据审查" },
  { id: "synthesizer", label: "Synthesizer", description: "合成报告" },
  { id: "quality", label: "Quality", description: "质量门" },
];

function agentState(id: string, events: RunEvent[], runStatus: RunStatus) {
  const matches = events.filter((event) => `${event.agent ?? ""} ${event.node ?? ""}`.toLowerCase().includes(id));
  const last = matches.at(-1);
  if (last?.event_type === "node_failed") return "failed";
  if (last?.event_type === "node_started") return "running";
  if (last && ["node_succeeded", "task_completed", "quality_evaluated", "review_decided"].includes(last.event_type)) return "completed";
  if (["completed", "completed_with_warnings"].includes(runStatus) && matches.length) return "completed";
  return "waiting";
}

export function AgentFlow({ events, status }: { events: RunEvent[]; status: RunStatus }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { nodes, edges } = useMemo(() => {
    const states = Object.fromEntries(AGENTS.map((agent) => [agent.id, agentState(agent.id, events, status)]));
    const nextNodes: Node[] = AGENTS.map((agent, index) => ({
      id: agent.id,
      position: { x: index * 210, y: index % 2 ? 120 : 20 },
      data: { label: <div className="flowNodeLabel"><span className={`nodeState ${states[agent.id]}`} /> <div><strong>{agent.label}</strong><small>{agent.description}</small></div></div> },
      className: `flowNode node-${states[agent.id]}`,
      style: { width: 172 },
    }));
    const pairs = [["supervisor", "planner"], ["planner", "researcher"], ["researcher", "critic"], ["critic", "synthesizer"], ["synthesizer", "quality"]];
    const nextEdges: Edge[] = pairs.map(([source, target]) => ({
      id: `${source}-${target}`, source, target,
      animated: states[target] === "running",
      markerEnd: { type: MarkerType.ArrowClosed, color: "#9aa7b8" },
      style: { stroke: states[source] === "completed" ? "#4f67d8" : "#cbd2dc", strokeWidth: 1.5 },
    }));
    return { nodes: nextNodes, edges: nextEdges };
  }, [events, status]);

  return (
    <div className="flowCanvas agentFlowCanvas">
      <ReactFlow nodes={nodes} edges={edges} fitView fitViewOptions={{ padding: 0.18 }} minZoom={0.5} maxZoom={1.5} nodesDraggable={false} nodesConnectable={false} elementsSelectable onNodeClick={(_, node) => setSelectedId(node.id)}>
        <Background color="#dce1e9" gap={20} size={1} /><Controls showInteractive={false} />
      </ReactFlow>
      {selectedId && <AgentDrawer agentId={selectedId} events={events} onClose={() => setSelectedId(null)} />}
    </div>
  );
}

function AgentDrawer({ agentId, events, onClose }: { agentId: string; events: RunEvent[]; onClose: () => void }) {
  const agent = AGENTS.find((item) => item.id === agentId)!;
  const related = events.filter((event) => `${event.agent ?? ""} ${event.node ?? ""}`.toLowerCase().includes(agentId));
  const first = related[0];
  const last = related.at(-1);
  const decision = [...related].reverse().find((event) => event.data.decision || event.decision_reason);
  return <DetailDrawer title={agent.label} subtitle={agent.description} onClose={onClose}>
    <div className="drawerStats"><div><span>状态</span><strong>{last?.status ?? (last ? "已执行" : "等待")}</strong></div><div><span>最近耗时</span><strong>{formatDuration([...related].reverse().find((event) => event.duration_ms !== undefined)?.duration_ms)}</strong></div><div><span>事件</span><strong>{related.length}</strong></div></div>
    <DrawerSection title="输入 / 启动上下文">{first ? <><p>{first.message}</p><DataBlock value={first.data} /></> : <p className="drawerMuted">尚未收到该 Agent 的执行输入。</p>}</DrawerSection>
    <DrawerSection title="最近结果">{last ? <><p>{last.message}</p><small>{formatDate(last.created_at)} · #{last.sequence}</small><DataBlock value={last.data} /></> : <p className="drawerMuted">尚无结果。</p>}</DrawerSection>
    <DrawerSection title="审查与决策">{decision ? <><p>{decision.decision_reason ?? String(decision.data.decision_reason ?? decision.data.decision ?? "已记录")}</p><DataBlock value={decision.data} /></> : <p className="drawerMuted">该节点没有结构化审查决策。</p>}</DrawerSection>
  </DetailDrawer>;
}

function DataBlock({ value }: { value: Record<string, unknown> }) {
  return Object.keys(value).length ? <pre className="drawerJson">{JSON.stringify(value, null, 2)}</pre> : null;
}
