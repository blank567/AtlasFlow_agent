import type { RunEvent, RunStatus } from "./types";

export type FlowState = "waiting" | "running" | "completed" | "failed";
export type RouteKind = "normal" | "supplement" | "replan" | "revise" | "fallback";

export type AgentRoute = {
  eventId: string;
  sequence: number;
  source: string;
  target: string;
  decision?: string;
  reason?: string;
  planVersion?: number;
  attempt?: number;
};

export type AgentFlowNode = {
  id: string;
  label: string;
  description: string;
  position: { x: number; y: number };
  state: FlowState;
  visits: number;
};

export type AgentFlowEdge = {
  id: string;
  source: string;
  target: string;
  kind: RouteKind;
  routes: AgentRoute[];
};

export type FlowStage = {
  id: string;
  label: string;
  description: string;
  nodeIds: readonly string[];
  state: FlowState;
};

export type FlowStageLink = {
  id: string;
  source: string;
  target: string;
  kind: RouteKind;
  routes: AgentRoute[];
};

const STAGE_DEFINITIONS = [
  { id: "plan", label: "规划", description: "确定目标与计划", nodeIds: ["supervisor", "planner"] },
  { id: "approval", label: "审批", description: "确认执行方案", nodeIds: ["approval"] },
  { id: "research", label: "研究", description: "分派并汇集结果", nodeIds: ["schedule_wave", "researcher", "wave_join", "research_gate"] },
  { id: "review", label: "审查", description: "检查证据与结论", nodeIds: ["critic"] },
  { id: "report", label: "成稿", description: "合成并校验报告", nodeIds: ["synthesizer", "quality_gate"] },
  { id: "finish", label: "完成", description: "归档运行结果", nodeIds: ["finalizer"] },
] as const;

const STAGE_BY_NODE = new Map<string, string>(STAGE_DEFINITIONS.flatMap((stage) => stage.nodeIds.map((nodeId) => [nodeId, stage.id] as const)));

export function buildAgentOverview(flow: ReturnType<typeof buildAgentFlow>): {
  stages: FlowStage[];
  mainLinks: FlowStageLink[];
  feedbackLinks: FlowStageLink[];
} {
  const stages: FlowStage[] = STAGE_DEFINITIONS.map((stage) => {
    const members = flow.nodes.filter((node) => (stage.nodeIds as readonly string[]).includes(node.id));
    const state: FlowState = members.some((node) => node.state === "failed") ? "failed"
      : members.some((node) => node.state === "running") ? "running"
      : members.some((node) => node.state === "completed") ? "completed" : "waiting";
    return { ...stage, state };
  });
  const main = new Map<string, FlowStageLink>();
  const feedback = new Map<string, FlowStageLink>();
  for (const route of flow.routes) {
    const source = STAGE_BY_NODE.get(route.source);
    const target = STAGE_BY_NODE.get(route.target);
    if (!source || !target) continue;
    const kind = routeKind(route);
    if (source === target && kind === "normal") continue;
    const sourceIndex = STAGE_DEFINITIONS.findIndex((stage) => stage.id === source);
    const targetIndex = STAGE_DEFINITIONS.findIndex((stage) => stage.id === target);
    const isAdjacent = targetIndex === sourceIndex + 1;
    // The diagram describes where execution actually went. A policy-diverted
    // review is still an executed forward transition, even though its decision
    // also belongs in the feedback list for explanation.
    if (isAdjacent) {
      const id = `${source}:${target}`;
      const existing = main.get(id);
      if (existing) {
        existing.routes.push(route);
        if (kind === "normal") existing.kind = "normal";
      } else main.set(id, { id, source, target, kind, routes: [route] });
    }
    if (!isAdjacent || kind !== "normal") {
      const id = `${source}:${target}:${kind}`;
      const existing = feedback.get(id);
      if (existing) existing.routes.push(route);
      else feedback.set(id, { id, source, target, kind, routes: [route] });
    }
  }
  return {
    stages,
    mainLinks: [...main.values()],
    feedbackLinks: [...feedback.values()].sort((a, b) => a.routes[0].sequence - b.routes[0].sequence),
  };
}

const NODE_DEFINITIONS = [
  { id: "supervisor", label: "Supervisor", description: "入口与预算", position: { x: 0, y: 20 } },
  { id: "planner", label: "Planner", description: "初始计划 / Replan", position: { x: 220, y: 20 } },
  { id: "approval", label: "Approval", description: "结构化审批", position: { x: 440, y: 20 } },
  { id: "schedule_wave", label: "Scheduler", description: "任务波次 / Supplement", position: { x: 660, y: 20 } },
  { id: "researcher", label: "Researcher", description: "并行研究任务", position: { x: 880, y: 20 } },
  { id: "wave_join", label: "Wave Join", description: "汇合研究结果", position: { x: 880, y: 185 } },
  { id: "research_gate", label: "Research Gate", description: "证据充分性检查", position: { x: 880, y: 350 } },
  { id: "critic", label: "Critic", description: "Accept / 补充 / 重规划", position: { x: 660, y: 350 } },
  { id: "synthesizer", label: "Synthesizer", description: "报告合成", position: { x: 440, y: 350 } },
  { id: "quality_gate", label: "Quality Gate", description: "Accept / 修订 / 重规划", position: { x: 220, y: 350 } },
  { id: "finalizer", label: "Finalizer", description: "结束与归档", position: { x: 0, y: 350 } },
] as const;

const NODE_IDS = new Set<string>(NODE_DEFINITIONS.map((node) => node.id));

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function routeKind(route: AgentRoute): RouteKind {
  const decision = route.decision?.toLowerCase();
  const supplement = route.source === "critic" && route.target === "schedule_wave";
  const replan = ["critic", "research_gate", "quality_gate"].includes(route.source) && route.target === "planner";
  const revise = route.source === "quality_gate" && route.target === "synthesizer";
  // A review can request a replan while policy routes elsewhere (for example,
  // when the replan budget is exhausted). Never paint that edge as a replan.
  if (decision === "supplement") return supplement ? "supplement" : "fallback";
  if (decision === "replan") return replan ? "replan" : "fallback";
  if (decision === "revise") return revise ? "revise" : "fallback";
  if (supplement) return "supplement";
  if (replan) return "replan";
  if (revise) return "revise";
  return "normal";
}

export function buildAgentFlow(events: RunEvent[], status: RunStatus): {
  nodes: AgentFlowNode[];
  edges: AgentFlowEdge[];
  routes: AgentRoute[];
} {
  const ordered = [...events].sort((a, b) => a.sequence - b.sequence);
  const routes: AgentRoute[] = ordered.flatMap((event) => {
    if (event.event_type !== "route_selected") return [];
    const source = text(event.node) ?? text(event.data.from_node);
    const target = text(event.data.to_node);
    if (!source || !target || !NODE_IDS.has(source) || !NODE_IDS.has(target)) return [];
    return [{
      eventId: event.event_id,
      sequence: event.sequence,
      source,
      target,
      decision: text(event.data.decision),
      reason: event.decision_reason ?? text(event.data.decision_reason),
      planVersion: event.plan_version,
      attempt: event.attempt,
    }];
  });

  const grouped = new Map<string, AgentFlowEdge>();
  for (const route of routes) {
    const kind = routeKind(route);
    const id = `${route.source}:${route.target}:${kind}`;
    const existing = grouped.get(id);
    if (existing) existing.routes.push(route);
    else grouped.set(id, { id, source: route.source, target: route.target, kind, routes: [route] });
  }

  const terminal = ["completed", "completed_with_warnings", "failed", "cancelled"].includes(status);
  const nodes: AgentFlowNode[] = NODE_DEFINITIONS.map((definition) => {
    const lifecycle = ordered.filter((event) => event.node === definition.id && ["node_started", "node_succeeded", "node_failed"].includes(event.event_type));
    const starts = lifecycle.filter((event) => event.event_type === "node_started").length;
    const finished = lifecycle.filter((event) => ["node_succeeded", "node_failed"].includes(event.event_type)).length;
    const visits = Math.max(starts, routes.filter((route) => route.source === definition.id).length);
    const seen = visits > 0 || routes.some((route) => route.target === definition.id);
    const last = lifecycle.at(-1);
    let state: FlowState = "waiting";
    if (last?.event_type === "node_failed") state = "failed";
    else if (!terminal && starts > finished) state = "running";
    else if (seen) state = "completed";
    if (!terminal && routes.at(-1)?.target === definition.id && !routes.some((route) => route.source === definition.id && route.sequence > routes.at(-1)!.sequence)) {
      if (state !== "failed") state = "running";
    }
    return { ...definition, state, visits };
  });
  return { nodes, edges: [...grouped.values()], routes };
}
