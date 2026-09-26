"use client";

import { useMemo, useState } from "react";
import { Background, Controls, Edge, MarkerType, Node, Position, ReactFlow } from "@xyflow/react";
import { AgentFlowEdge, AgentRoute, buildAgentFlow } from "../lib/agent-flow-model";
import { RunEvent, RunStatus } from "../lib/types";
import { formatDate, formatDuration } from "../lib/format";
import { DetailDrawer, DrawerSection } from "./detail-drawer";

const ROUTE_COLORS = { normal: "#4f67d8", supplement: "#9a6dce", replan: "#d67a52", revise: "#d2a14b", fallback: "#7d8797" } as const;
const ROUTE_LABELS = { normal: "执行", supplement: "Supplement", replan: "Replan", revise: "Revise", fallback: "决策改道" } as const;

export function AgentFlow({ events, status }: { events: RunEvent[]; status: RunStatus }) {
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedRoute, setSelectedRoute] = useState<AgentRoute | null>(null);
  const flow = useMemo(() => buildAgentFlow(events, status), [events, status]);
  const nodes: Node[] = useMemo(() => flow.nodes.map((node) => ({
    id: node.id,
    position: node.position,
    sourcePosition: ["critic", "synthesizer", "quality_gate", "finalizer", "research_gate"].includes(node.id) ? Position.Left : node.id === "researcher" ? Position.Bottom : Position.Right,
    targetPosition: ["critic", "synthesizer", "quality_gate", "finalizer", "research_gate"].includes(node.id) ? Position.Right : node.id === "wave_join" ? Position.Top : Position.Left,
    data: { label: <div className="flowNodeLabel"><span className={`nodeState ${node.state}`} /><div><strong>{node.label}</strong><small>{node.description}</small></div>{node.visits > 1 && <em className="flowVisitCount">×{node.visits}</em>}</div> },
    className: `flowNode node-${node.state}`,
    style: { width: 178 },
  })), [flow.nodes]);
  const edges: Edge[] = useMemo(() => flow.edges.map((edge) => ({
    id: edge.id, source: edge.source, target: edge.target, type: "smoothstep",
    label: edge.kind === "normal" ? (edge.routes.length > 1 ? `×${edge.routes.length}` : undefined) : `${ROUTE_LABELS[edge.kind]}${edge.routes.length > 1 ? ` ×${edge.routes.length}` : ""}`,
    labelStyle: { fontSize: 10, fontWeight: 700, fill: ROUTE_COLORS[edge.kind] },
    labelBgStyle: { fill: "#fff", fillOpacity: 0.94 }, labelBgPadding: [6, 4],
    animated: !["completed", "completed_with_warnings", "failed", "cancelled"].includes(status) && flow.routes.at(-1)?.eventId === edge.routes.at(-1)?.eventId,
    markerEnd: { type: MarkerType.ArrowClosed, color: ROUTE_COLORS[edge.kind] },
    style: { stroke: ROUTE_COLORS[edge.kind], strokeWidth: edge.kind === "normal" ? 1.8 : 2.6, strokeDasharray: edge.kind === "normal" ? undefined : "5 4" },
  })), [flow.edges, flow.routes, status]);

  return <>
    <div className="flowCanvas agentFlowCanvas"><ReactFlow nodes={nodes} edges={edges} fitView fitViewOptions={{ padding: 0.12 }} minZoom={0.35} maxZoom={1.8} nodesDraggable={false} nodesConnectable={false} elementsSelectable onNodeClick={(_, node) => { setSelectedRoute(null); setSelectedNodeId(node.id); }} onEdgeClick={(_, edge) => { setSelectedNodeId(null); setSelectedRoute(flow.edges.find((item) => item.id === edge.id)?.routes.at(-1) ?? null); }}><Background color="#dce1e9" gap={20} size={1} /><Controls showInteractive={false} /></ReactFlow></div>
    <div className="routeHistory"><div className="routeHistoryHeader"><strong>实际执行路径</strong><span>{flow.routes.length} 次路由 · 按事件顺序</span></div>
      {flow.routes.length ? <ol className="routeHistoryList">{flow.routes.map((route) => {
        const kind = flow.edges.find((edge) => edge.routes.some((item) => item.eventId === route.eventId))?.kind ?? "normal";
        return <li key={route.eventId}><button type="button" onClick={() => { setSelectedNodeId(null); setSelectedRoute(route); }}><code>#{route.sequence}</code><span>{route.source} <b>→</b> {route.target}</span>{kind !== "normal" && <em className={`routeKind routeKind-${kind}`}>{ROUTE_LABELS[kind]}</em>}<small>Plan v{route.planVersion ?? "?"}</small></button></li>;
      })}</ol> : <p className="routeHistoryEmpty">尚无路由事件。Agent 开始执行后，这里会按真实事件显示分支与回环。</p>}</div>
    {selectedNodeId && <AgentDrawer agentId={selectedNodeId} label={flow.nodes.find((node) => node.id === selectedNodeId)?.label ?? selectedNodeId} description={flow.nodes.find((node) => node.id === selectedNodeId)?.description ?? ""} events={events} routes={flow.routes} onClose={() => setSelectedNodeId(null)} />}
    {selectedRoute && <RouteDrawer route={selectedRoute} grouped={flow.edges.find((edge) => edge.routes.some((item) => item.eventId === selectedRoute.eventId))} onClose={() => setSelectedRoute(null)} />}
  </>;
}

function AgentDrawer({ agentId, label, description, events, routes, onClose }: { agentId: string; label: string; description: string; events: RunEvent[]; routes: AgentRoute[]; onClose: () => void }) {
  const related = events.filter((event) => event.node === agentId || (!event.node && event.agent === agentId));
  const inbound = routes.filter((route) => route.target === agentId);
  const outbound = routes.filter((route) => route.source === agentId);
  const first = related[0]; const last = related.at(-1);
  const decision = [...related].reverse().find((event) => event.data.decision || event.decision_reason);
  return <DetailDrawer title={label} subtitle={description} onClose={onClose}>
    <div className="drawerStats"><div><span>进入</span><strong>{inbound.length}</strong></div><div><span>离开</span><strong>{outbound.length}</strong></div><div><span>事件</span><strong>{related.length}</strong></div></div>
    <DrawerSection title="输入 / 启动上下文">{first ? <><p>{first.message}</p><DataBlock value={first.data} /></> : <p className="drawerMuted">此路由节点尚无独立生命周期事件。</p>}</DrawerSection>
    <DrawerSection title="最近结果">{last ? <><p>{last.message}</p><small>{formatDate(last.created_at)} · #{last.sequence} · {formatDuration([...related].reverse().find((event) => event.duration_ms !== undefined)?.duration_ms)}</small><DataBlock value={last.data} /></> : <p className="drawerMuted">尚无结果。</p>}</DrawerSection>
    <DrawerSection title="审查与决策">{decision ? <><p>{decision.decision_reason ?? String(decision.data.decision_reason ?? decision.data.decision ?? "已记录")}</p><DataBlock value={decision.data} /></> : <p className="drawerMuted">该节点没有结构化审查决策。</p>}</DrawerSection>
  </DetailDrawer>;
}

function RouteDrawer({ route, grouped, onClose }: { route: AgentRoute; grouped?: AgentFlowEdge; onClose: () => void }) {
  return <DetailDrawer title={`${route.source} → ${route.target}`} subtitle={`路由事件 #${route.sequence}`} onClose={onClose}>
    <div className="drawerStats"><div><span>决策</span><strong>{route.decision ?? grouped?.kind ?? "执行"}</strong></div><div><span>Plan</span><strong>v{route.planVersion ?? "?"}</strong></div><div><span>该边次数</span><strong>{grouped?.routes.length ?? 1}</strong></div></div>
    <DrawerSection title="路由原因"><p>{route.reason ?? "后端未提供原因。"}</p></DrawerSection>
    <DrawerSection title="同一路径的历史">{grouped?.routes.map((item) => <p className="routeDrawerItem" key={item.eventId}>#{item.sequence} · Plan v{item.planVersion ?? "?"} · {item.decision ?? "执行"}{item.attempt ? ` · attempt ${item.attempt}` : ""}</p>)}</DrawerSection>
  </DetailDrawer>;
}

function DataBlock({ value }: { value: Record<string, unknown> }) { return Object.keys(value).length ? <pre className="drawerJson">{JSON.stringify(value, null, 2)}</pre> : null; }
