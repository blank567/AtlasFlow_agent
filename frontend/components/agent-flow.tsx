"use client";

import { useMemo, useState } from "react";
import { AgentFlowEdge, AgentRoute, FlowStageLink, buildAgentFlow, buildAgentOverview } from "../lib/agent-flow-model";
import { STAGE_PLACEMENT, feedbackDiagramPath, mainDiagramPath } from "../lib/agent-flow-diagram";
import { RunEvent, RunStatus } from "../lib/types";
import { formatDate, formatDuration } from "../lib/format";
import { DetailDrawer, DrawerSection } from "./detail-drawer";

const ROUTE_LABELS = { normal: "跨阶段跳转", supplement: "补充研究", replan: "重新规划", revise: "修订报告", fallback: "决策改道" } as const;
const STATE_LABELS = { waiting: "待执行", running: "执行中", completed: "已执行", failed: "执行失败" } as const;
const ROUTE_COLORS = { normal: "#4f67d8", supplement: "#8b69c6", replan: "#d67a52", revise: "#b97817", fallback: "#7d8797" } as const;

export function AgentFlow({ events, status }: { events: RunEvent[]; status: RunStatus }) {
  const [selectedStageId, setSelectedStageId] = useState<string | null>(null);
  const [selectedRoute, setSelectedRoute] = useState<AgentRoute | null>(null);
  const flow = useMemo(() => buildAgentFlow(events, status), [events, status]);
  const overview = useMemo(() => buildAgentOverview(flow), [flow]);
  const stageById = (id: string) => overview.stages.find((stage) => stage.id === id);
  const openRoute = (route: AgentRoute) => { setSelectedStageId(null); setSelectedRoute(route); };

  return <>
    <div className="agentFlowOverview">
      <div className="flowSectionHeading"><strong>阶段循环图</strong><span>箭头代表真实执行方向；外侧回线代表再次进入阶段，窄屏可左右滑动</span></div>
      <div className="flowDiagramScroll"><div className="flowDiagramCanvas">
        <svg className="flowDiagramPaths" viewBox="0 0 1000 480" preserveAspectRatio="none" aria-label="实际执行的阶段路径与回路">
          <defs>{Object.entries(ROUTE_COLORS).map(([kind, color]) => <marker key={kind} id={`flow-arrow-${kind}`} markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto" markerUnits="userSpaceOnUse"><path d="M 1 1 L 10 6 L 1 11 Z" fill={color} /></marker>)}</defs>
          {overview.mainLinks.map((link) => <DiagramEdge key={`main-${link.id}`} link={link} path={mainDiagramPath(link)} openRoute={openRoute} />)}
          {overview.feedbackLinks.map((link) => <DiagramEdge key={`loop-${link.id}`} link={link} path={feedbackDiagramPath(link)} openRoute={openRoute} />)}
        </svg>
        {overview.stages.map((stage, index) => <button key={stage.id} type="button" className={`flowStage flowStage-${stage.state}`} style={{ left: `${STAGE_PLACEMENT[stage.id].left}%`, top: STAGE_PLACEMENT[stage.id].top }} onClick={() => { setSelectedRoute(null); setSelectedStageId(stage.id); }} aria-label={`${stage.label}：${STATE_LABELS[stage.state]}，查看包含的节点`}>
            <span className="flowStageTop"><span className="flowStageNumber">{String(index + 1).padStart(2, "0")}</span><span className={`nodeState ${stage.state}`} /></span>
            <strong>{stage.label}</strong><small>{stage.description}</small><span className="flowStageStatus">{STATE_LABELS[stage.state]}</span>
          </button>)}
      </div></div>
      <p className="flowMobileHint">左右滑动查看完整循环图 →</p>
      <div className="flowFeedback"><div className="flowSectionHeading"><strong>反馈与跳转</strong><span>{overview.feedbackLinks.length ? "点击查看具体决策" : "本次暂无回路"}</span></div>
        {overview.feedbackLinks.length ? <div className="flowFeedbackList">{overview.feedbackLinks.map((link) => <button type="button" className={`flowFeedbackItem flowFeedback-${link.kind}`} key={link.id} onClick={() => openRoute(link.routes.at(-1)!)}>
          <span className="flowFeedbackKind">{ROUTE_LABELS[link.kind]}</span><strong>{stageById(link.source)?.label} <span>→</span> {stageById(link.target)?.label}</strong><small>{link.routes.length} 次 · 最近 #{link.routes.at(-1)?.sequence}</small>
        </button>)}</div> : <p className="flowFeedbackEmpty">本次尚无补充、重规划或修订；发生后将在循环图及这里显示。</p>}
      </div>
    </div>
    <details className="routeHistory"><summary className="routeHistoryHeader"><strong>完整节点路径</strong><span>{flow.routes.length} 次原始路由 · 展开查看每一步</span></summary>
      {flow.routes.length ? <ol className="routeHistoryList">{flow.routes.map((route) => {
        const kind = flow.edges.find((edge) => edge.routes.some((item) => item.eventId === route.eventId))?.kind ?? "normal";
        return <li key={route.eventId}><button type="button" onClick={() => openRoute(route)}><code>#{route.sequence}</code><span>{route.source} <b>→</b> {route.target}</span>{kind !== "normal" && <em className={`routeKind routeKind-${kind}`}>{ROUTE_LABELS[kind]}</em>}<small>Plan v{route.planVersion ?? "?"}</small></button></li>;
      })}</ol> : <p className="routeHistoryEmpty">尚无路由事件。Agent 开始执行后，这里会按真实事件显示分支与回环。</p>}</details>
    {selectedStageId && stageById(selectedStageId) && <AgentDrawer agentIds={stageById(selectedStageId)!.nodeIds} label={stageById(selectedStageId)!.label} description={stageById(selectedStageId)!.description} events={events} routes={flow.routes} onClose={() => setSelectedStageId(null)} />}
    {selectedRoute && <RouteDrawer route={selectedRoute} grouped={overview.mainLinks.concat(overview.feedbackLinks).find((link) => link.routes.some((item) => item.eventId === selectedRoute.eventId)) ?? flow.edges.find((edge) => edge.routes.some((item) => item.eventId === selectedRoute.eventId))} onClose={() => setSelectedRoute(null)} />}
  </>;
}

function DiagramEdge({ link, path, openRoute }: { link: FlowStageLink; path?: string; openRoute: (route: AgentRoute) => void }) {
  if (!path) return null;
  const latest = link.routes.at(-1);
  if (!latest) return null;
  const activate = () => openRoute(latest);
  return <g className="flowDiagramEdge" role="button" tabIndex={0} aria-label={`${link.source} 到 ${link.target}，${ROUTE_LABELS[link.kind]}，${link.routes.length} 次路由`} onClick={activate} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); } }}>
    <title>{`${link.source} → ${link.target} · ${ROUTE_LABELS[link.kind]} · ${link.routes.length} 次`}</title>
    <path d={path} fill="none" stroke={ROUTE_COLORS[link.kind]} strokeWidth={link.kind === "normal" ? 2.7 : 2.4} strokeDasharray={link.kind === "normal" ? undefined : "6 5"} strokeLinecap="round" strokeLinejoin="round" markerEnd={`url(#flow-arrow-${link.kind})`} vectorEffect="non-scaling-stroke" />
    <path d={path} fill="none" stroke="transparent" strokeWidth="18" pointerEvents="stroke" />
  </g>;
}

function AgentDrawer({ agentIds, label, description, events, routes, onClose }: { agentIds: readonly string[]; label: string; description: string; events: RunEvent[]; routes: AgentRoute[]; onClose: () => void }) {
  const related = events.filter((event) => agentIds.includes(event.node ?? event.agent ?? ""));
  const inbound = routes.filter((route) => agentIds.includes(route.target) && !agentIds.includes(route.source));
  const outbound = routes.filter((route) => agentIds.includes(route.source) && !agentIds.includes(route.target));
  const first = related[0]; const last = related.at(-1);
  const decision = [...related].reverse().find((event) => event.data.decision || event.decision_reason);
  return <DetailDrawer title={label} subtitle={description} onClose={onClose}>
    <div className="drawerStats"><div><span>进入</span><strong>{inbound.length}</strong></div><div><span>离开</span><strong>{outbound.length}</strong></div><div><span>事件</span><strong>{related.length}</strong></div></div>
    <DrawerSection title="包含的执行节点"><p>{agentIds.join(" → ")}</p></DrawerSection>
    <DrawerSection title="输入 / 启动上下文">{first ? <><p>{first.message}</p><DataBlock value={first.data} /></> : <p className="drawerMuted">此路由节点尚无独立生命周期事件。</p>}</DrawerSection>
    <DrawerSection title="最近结果">{last ? <><p>{last.message}</p><small>{formatDate(last.created_at)} · #{last.sequence} · {formatDuration([...related].reverse().find((event) => event.duration_ms !== undefined)?.duration_ms)}</small><DataBlock value={last.data} /></> : <p className="drawerMuted">尚无结果。</p>}</DrawerSection>
    <DrawerSection title="审查与决策">{decision ? <><p>{decision.decision_reason ?? String(decision.data.decision_reason ?? decision.data.decision ?? "已记录")}</p><DataBlock value={decision.data} /></> : <p className="drawerMuted">该节点没有结构化审查决策。</p>}</DrawerSection>
  </DetailDrawer>;
}

function RouteDrawer({ route, grouped, onClose }: { route: AgentRoute; grouped?: AgentFlowEdge | FlowStageLink; onClose: () => void }) {
  return <DetailDrawer title={`${route.source} → ${route.target}`} subtitle={`路由事件 #${route.sequence}`} onClose={onClose}>
    <div className="drawerStats"><div><span>决策</span><strong>{route.decision ?? grouped?.kind ?? "执行"}</strong></div><div><span>Plan</span><strong>v{route.planVersion ?? "?"}</strong></div><div><span>该边次数</span><strong>{grouped?.routes.length ?? 1}</strong></div></div>
    <DrawerSection title="路由原因"><p>{route.reason ?? "后端未提供原因。"}</p></DrawerSection>
    <DrawerSection title="同一路径的历史">{grouped?.routes.map((item) => <p className="routeDrawerItem" key={item.eventId}>#{item.sequence} · Plan v{item.planVersion ?? "?"} · {item.decision ?? "执行"}{item.attempt ? ` · attempt ${item.attempt}` : ""}</p>)}</DrawerSection>
  </DetailDrawer>;
}

function DataBlock({ value }: { value: Record<string, unknown> }) { return Object.keys(value).length ? <pre className="drawerJson">{JSON.stringify(value, null, 2)}</pre> : null; }
