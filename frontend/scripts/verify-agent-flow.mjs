import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import ts from "typescript";

function loadTypeScript(relativePath) {
  const source = readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), "utf8");
  const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const exports = {};
  new Function("exports", output)(exports);
  return exports;
}

const { buildAgentFlow, buildAgentOverview } = loadTypeScript("../lib/agent-flow-model.ts");
const { mainDiagramPath, feedbackDiagramPath } = loadTypeScript("../lib/agent-flow-diagram.ts");
const { eventReducer, initialEventState } = loadTypeScript("../lib/event-reducer.ts");

function route(sequence, source, target, decision) {
  return { event_id: `route-${sequence}`, sequence, event_type: "route_selected", node: source, agent: "supervisor", data: { to_node: target, decision }, plan_version: sequence < 10 ? 1 : 2, decision_reason: decision ?? "正常路由", message: "路由", created_at: "2026-01-01T00:00:00Z" };
}

const events = [
  route(1, "supervisor", "planner"), route(2, "planner", "approval"), route(3, "approval", "schedule_wave"),
  route(4, "schedule_wave", "researcher"), route(5, "researcher", "wave_join"), route(6, "wave_join", "schedule_wave"),
  route(7, "schedule_wave", "research_gate"), route(8, "research_gate", "critic"),
  route(9, "critic", "schedule_wave", "supplement"), route(10, "schedule_wave", "researcher"),
  route(11, "researcher", "wave_join"), route(12, "wave_join", "schedule_wave"),
  route(13, "schedule_wave", "research_gate"), route(14, "research_gate", "critic"),
  route(15, "critic", "planner", "replan"), route(16, "planner", "approval"),
  route(17, "approval", "schedule_wave"), route(18, "schedule_wave", "research_gate"),
  route(19, "research_gate", "critic"), route(20, "critic", "synthesizer", "accept"),
  route(21, "synthesizer", "quality_gate"), route(22, "quality_gate", "synthesizer", "revise"),
  route(23, "synthesizer", "quality_gate"), route(24, "quality_gate", "finalizer", "accept"),
];

const flow = buildAgentFlow([...events].reverse(), "completed");
assert.equal(flow.routes.length, 24);
assert.deepEqual(flow.routes.map((item) => item.sequence), events.map((item) => item.sequence));
assert.equal(flow.nodes.length, 11);
assert.equal(flow.edges.find((edge) => edge.kind === "supplement")?.source, "critic");
assert.equal(flow.edges.find((edge) => edge.kind === "replan")?.target, "planner");
assert.equal(flow.edges.find((edge) => edge.kind === "revise")?.target, "synthesizer");
assert.equal(flow.edges.find((edge) => edge.source === "planner" && edge.target === "approval")?.routes.length, 2);
assert.equal(buildAgentFlow([route(25, "critic", "synthesizer", "replan")], "completed").edges[0]?.kind, "fallback", "a requested replan is not an executed replan when policy diverts it");
assert.equal(buildAgentFlow([], "running").edges.length, 0, "do not draw routes that never happened");

const overview = buildAgentOverview(flow);
assert.deepEqual(overview.stages.map((stage) => stage.id), ["plan", "approval", "research", "review", "report", "finish"]);
assert.deepEqual(overview.mainLinks.map((link) => `${link.source}->${link.target}`), ["plan->approval", "approval->research", "research->review", "review->report", "report->finish"]);
assert.deepEqual(overview.feedbackLinks.map((link) => link.kind), ["supplement", "replan", "revise"]);
assert.equal(overview.feedbackLinks.find((link) => link.kind === "supplement")?.routes[0].sequence, 9);
assert.equal(overview.feedbackLinks.find((link) => link.kind === "replan")?.target, "plan");
assert.equal(overview.feedbackLinks.find((link) => link.kind === "revise")?.source, "report");
assert.equal(overview.mainLinks.find((link) => link.source === "plan")?.routes.length, 2, "repeated main transitions retain their counts");
assert.equal(buildAgentOverview(buildAgentFlow([], "running")).mainLinks.length, 0, "the overview must not invent an executed arrow");
const diverted = buildAgentOverview(buildAgentFlow([route(25, "critic", "synthesizer", "replan"), route(26, "quality_gate", "finalizer", "replan")], "completed"));
assert.deepEqual(diverted.mainLinks.map((link) => `${link.source}->${link.target}`), ["review->report", "report->finish"], "actual forward arrows remain visible even when the decision was diverted");
assert.ok(diverted.mainLinks.every(mainDiagramPath), "every actual forward transition has an arrow path");
assert.deepEqual(diverted.feedbackLinks.map((link) => link.kind), ["fallback", "fallback"], "diverted decisions also stay in feedback details");
assert.equal(feedbackDiagramPath(diverted.feedbackLinks[0]), undefined, "a forward diversion does not create a false loop");
assert.equal(buildAgentOverview(buildAgentFlow([route(26, "planner", "finalizer")], "completed")).feedbackLinks[0]?.target, "finish", "a skipped stage is shown as a direct jump");
assert.ok(overview.mainLinks.every(mainDiagramPath), "all five mainline arrows have diagram paths");
assert.ok(overview.feedbackLinks.every(feedbackDiagramPath), "the known supplement, replan, and revise loops have diagram paths");
const otherLoops = buildAgentOverview(buildAgentFlow([route(30, "research_gate", "planner", "replan"), route(31, "quality_gate", "planner", "replan")], "completed"));
assert.ok(otherLoops.feedbackLinks.every(feedbackDiagramPath), "research and quality replans also return visibly to planning");

const noisy = Array.from({ length: 1100 }, (_, index) => ({ event_id: `noise-${index}`, sequence: index + 100, event_type: "node_started", node: "researcher", data: {}, message: "event", created_at: "2026-01-01T00:00:00Z" }));
const retained = eventReducer(initialEventState, { type: "hydrate", events: [events[0], ...noisy] });
assert.equal(retained.items.filter((event) => event.event_type === "route_selected").length, 1, "keep early route events beyond the telemetry cap");
assert.equal(retained.lastSequence, 1199);
const live = eventReducer(initialEventState, { type: "append", event: route(1200, "critic", "planner", "replan") });
const afterStaleSnapshot = eventReducer(live, { type: "hydrate", events: [route(1, "supervisor", "planner")] });
assert.deepEqual(afterStaleSnapshot.items.map((event) => event.sequence), [1, 1200], "a stale snapshot must not erase a live route");

console.log("Agent flow route, loop, decision, and retention checks passed.");
