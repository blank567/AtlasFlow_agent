// UI fixtures only: CDP intercepts every API and blocks other remote traffic.
// No fixture score is a real RAG or Agent quality measurement.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url), { WebSocket } = require("next/dist/compiled/ws");
const workspace = path.resolve(fileURLToPath(new URL("../../", import.meta.url)));
assert.match(workspace, /^E:/i);
const base = process.argv[2] ?? "http://127.0.0.1:3214";
const root = path.join(workspace, "build", "evaluation-v2-browser"); mkdirSync(root, { recursive: true });
const output = mkdtempSync(path.join(root, "check-"));
const binary = ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].find(existsSync);
assert.ok(binary);
const browser = spawn(binary, ["--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-breakpad", "--disable-crash-reporter", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${path.join(output, "profile")}`, "about:blank"], { windowsHide: true, env: { ...process.env, TEMP: "E:\\codex\\tmp", TMP: "E:\\codex\\tmp" }, stdio: ["ignore", "ignore", "pipe"] });
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, label) { for (let index = 0; index < 300; index++) { if (await check()) return; await wait(100); } throw new Error(label); }
async function connect(endpoint) {
  const socket = new WebSocket(endpoint); await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
  let sequence = 0; const pending = new Map(), events = new Map();
  socket.on("message", data => { const packet = JSON.parse(data); if (packet.id) { const callback = pending.get(packet.id); pending.delete(packet.id); if (callback) { clearTimeout(callback.timer); packet.error ? callback.reject(new Error(packet.error.message)) : callback.resolve(packet.result); } } else events.get(packet.method)?.(packet.params); });
  return { on(method, callback) { events.set(method, callback); }, send(method, params = {}) { return new Promise((resolve, reject) => { const id = ++sequence, timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 45000); pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params })); }); }, close() { socket.close(); } };
}

const now = "2026-10-15T08:00:00Z", hash = "e".repeat(64);
const space = { id: "qa-space", slug: "qa-space", name: "界面测试空间（非真实基线）", active_generation_id: "qa-gen", active_version_id: "qa-v1" };
const datasets = {
  rag: { dataset_id: "qa-rag-v2", kind: "rag", name: "RAG界面契约夹具", version: "2", space: space.slug, cases: [1, 2].map(index => ({ case_id: `R${index}`, query: `注意力机制证据定位的界面验证${index}`, gold_status: "verified", expected_sources: [{ source_id: "qa-source", page_number: 3, anchor: "人工标注的参考锚点，非真实基线" }], reference_facts: ["界面参考事实"], tags: ["fixture"] })) },
  agent: { dataset_id: "qa-agent-v2", kind: "agent", name: "Agent界面契约夹具", version: "2", cases: [1, 2, 9].map(index => ({ case_id: `A0${index}`, query: `Agent任务与审批契约界面验证${index}`, protocol: index === 9 ? "agent-contract-v2" : "agent-task-v2", gold_status: "draft", expected_facts: ["最终目标必须独立验收"], assertions: [{ assertion_id: "approval", kind: "approval_before_research", required: true, parameters: {} }], tags: ["fixture"] })) },
};
const budgets = { rag: { max_http_attempts: 60, max_elapsed_seconds: 1200, case_timeout_seconds: 90, concurrency: 2 }, agent: { max_http_attempts: 60, max_elapsed_seconds: 1200, case_timeout_seconds: 600, concurrency: 1 } };
Object.assign(datasets.rag.cases[0], { reference_status: "verified", reference_answer: "基于原文核对的界面参考答案", reference_version: "fixture-references-v2.1", reference_provenance: { verification_method: "primary_source_reading", supports: [{ source_id: "qa-source", page_number: 3 }] } });
const metric = (value, extra = {}) => ({ value, status: value === null ? "missing_gold" : "evaluated", unit: "ratio", direction: "higher", scoring_unit: "case", grader_id: "fixture", grader_version: "v2", evidence_refs: [], ...extra });
const variants = kind => kind === "rag" ? [{ variant_id: "baseline", name: "混合召回", retrieval_tuning: { rerank_enabled: false }, retrieval_limit: 5 }, { variant_id: "candidate", name: "混合、扩展与重排", retrieval_tuning: { rerank_enabled: true }, retrieval_limit: 5 }] : [{ variant_id: "baseline", name: "基线策略", policy: { knowledge_mode: "off", max_tool_calls_per_turn: 5, max_tool_calls_per_run: 20 }, workflow_version: "current", prompt_version: "current" }, { variant_id: "candidate", name: "候选策略", policy: { knowledge_mode: "off", max_tool_calls_per_turn: 5, max_tool_calls_per_run: 20 }, workflow_version: "current", prompt_version: "current" }];
const plan = kind => ({ kind, protocol: kind === "rag" ? "rag-retrieval-v2" : "agent-task-v2", dataset_id: datasets[kind].dataset_id, mode: "replay", case_ids: kind === "rag" ? ["R1", "R2"] : ["A01", "A02"], variants: variants(kind), budget: budgets[kind], repetitions: 1, input_refs: {}, judge_enabled: false, confirm_judge: false, confirm_live: false, langsmith_sync: false, allow_degraded: false, ...(kind === "rag" ? { space: space.slug, cutoffs: [1, 3, 5] } : {}) });
function resultRows(value) { return value.case_ids.flatMap((id, caseIndex) => value.variants.flatMap(variant => Array.from({ length: value.repetitions }, (_, index) => ({ case_id: id, variant_id: variant.variant_id, trial: index + 1, status: value.kind === "agent" && caseIndex === 1 ? "timed_out" : "completed", input_id: `fixture-${value.kind}-${id}-${variant.variant_id}-${index + 1}`, metrics: value.kind === "rag" ? { anchor_coverage_at_5: metric(variant.variant_id === "candidate" ? 1 : 0), source_coverage_at_5: metric(1), anchor_mrr_at_5: metric(0), anchor_hit_at_1: metric(false, { unit: "boolean" }), context_anchor_coverage_at_5: metric(null), retrieval_duration_ms: metric(1000, { unit: "ms", direction: "lower" }), ...(value.protocol === "rag-answer-v2" ? { answer_correctness: metric(null, { status: "grader_failed", reason: "fixture: judge failed without deleting retrieval metrics" }), faithfulness: metric(null), reference_fact_coverage: metric(null), citation_support: metric(null), citation_validity: metric(1) } : {}) } : { task_success: metric(null), task_outcome_score: metric(null), tool_failures: metric(0, { unit: "count", direction: "lower" }), known_tokens: metric(500, { unit: "tokens", direction: "lower" }), trajectory_compliance: metric(1), tool_policy_compliance: metric(null), execution_completed: metric(1), duration_ms: metric(1000, { unit: "ms", direction: "lower" }), known_cost: metric(0.2, { unit: "usd", direction: "lower" }), tool_executions: metric(1, { unit: "count", direction: "neutral" }) }, checks: value.kind === "agent" ? [{ check_id: "assertion:approval", dimension: "trajectory", kind: "approval_before_research", value: true, status: "evaluated", reason: "真实事件顺序的界面夹具", evidence_refs: ["event:2"] }] : [], stage_status: { execution: "completed", scoring: "completed", ...(value.judge_enabled ? { judge: "grader_failed" } : {}) }, elapsed_ms: 1000, error: value.kind === "agent" && caseIndex === 1 ? "单题超时（界面夹具），其他单元保留" : null, degraded: value.kind === "rag" && variant.variant_id === "candidate" && caseIndex === 1 ? ["fixture_degraded"] : [], provider_calls: [] })))); }
function summary(value, results) { return { planned: value.case_ids.length * value.variants.length * value.repetitions, completed: results.filter(item => item.status === "completed").length, failed: 0, timed_out: results.filter(item => item.status === "timed_out").length, grader_failed: value.judge_enabled ? 1 : 0, degraded: 0, variants: Object.fromEntries(value.variants.map(variant => { const rows = results.filter(item => item.variant_id === variant.variant_id), keys = [...new Set(rows.flatMap(item => Object.keys(item.metrics)))]; return [variant.variant_id, { planned: rows.length, completed: rows.filter(item => item.status === "completed").length, failed: 0, metrics: Object.fromEntries(keys.map(key => { const values = rows.map(item => item.metrics[key]).filter(item => item?.value !== null); return [key, { mean: values.length ? values.reduce((sum, item) => sum + Number(item.value), 0) / values.length : null, evaluated: values.length, planned: rows.length, unit: rows[0].metrics[key].unit, direction: rows[0].metrics[key].direction, scoring_unit: rows[0].metrics[key].scoring_unit }]; })) }]; })) }; }
function experiment(kind, id, configuration = plan(kind)) { const rows = resultRows(configuration); return { experiment_id: id, kind, protocol: configuration.protocol, plan: configuration, dataset_hash: hash, context: { snapshot: { manifest_hash: hash } }, created_at: now, status: "completed", results: rows, summary: summary(configuration, rows), budget_ledger: { http_attempts: 0, elapsed_seconds: 1, known_cost: null }, sync_status: "not_requested", stop_reason: null }; }
const experiments = { rag: [experiment("rag", "rag-seed-0"), experiment("rag", "rag-seed-1")], agent: [experiment("agent", "agent-seed-0"), experiment("agent", "agent-seed-1")] };
for (const kind of ["rag", "agent"]) experiments[kind][1].results[0].input_id = null;
// Dedicated chart fixtures cover a third group without altering submitted plans.
// Each plotted mean must come from the API's paired cohort, not the group summary.
for (const kind of ["rag", "agent"]) {
  const configuration = plan(kind);
  configuration.variants.push({ ...structuredClone(configuration.variants[1]), variant_id: "conservative", name: "保守候选组" });
  const value = experiment(kind, `${kind}-seed-0`, configuration);
  for (const row of value.results) {
    const candidate = row.variant_id === "candidate", conservative = row.variant_id === "conservative";
    row.status = "completed"; row.error = null;
    row.metrics.duration_ms = metric(candidate ? 600 : conservative ? 1200 : 1000, { unit: "ms", direction: "lower" });
    row.metrics.known_cost = metric(candidate ? 0.1 : conservative ? 0.4 : 0.2, { unit: kind === "agent" ? "USD" : "usd", direction: "lower" });
    row.metrics.tool_executions = metric(candidate ? 2 : 1, { unit: "count", direction: "neutral" });
    if (kind === "rag") {
      // R2 is excluded by one group. Its deliberately different values must not
      // leak into any group's shared-cohort mean (including the other groups).
      row.metrics.anchor_coverage_at_5 = metric(row.case_id === "R2" ? candidate ? 0.4 : conservative ? 0.75 : 0.8 : candidate ? 1 : conservative ? 0.25 : 0);
      row.metrics.ndcg_at_5 = metric(null, { reason: "未提供完整、经复核的相关性标注" });
      if (conservative) row.degraded = [];
    }
  }
  value.summary = summary(configuration, value.results);
  experiments[kind][0] = value;
}
const tools = [
  { name: "calculator", description: "确定性计算工具界面夹具", available: true, capabilities: ["calculator"] },
  { name: "web_search", description: "网页搜索工具界面夹具", available: true, capabilities: ["web_search"] },
  { name: "map_route", description: "地图工具界面夹具", available: false, unavailable_reason: "未配置地图密钥", capabilities: ["map_route"] },
];
function withinComparison(value, baselineId) {
  const baseline = value.plan.variants.find(item => item.variant_id === baselineId) ?? value.plan.variants[0];
  const comparisons = value.plan.variants.filter(item => item.variant_id !== baseline.variant_id).map(candidate => {
    const pairs = value.plan.case_ids.flatMap(id => Array.from({ length: value.plan.repetitions }, (_, index) => {
      const left = value.results.find(item => item.case_id === id && item.variant_id === baseline.variant_id && item.trial === index + 1), right = value.results.find(item => item.case_id === id && item.variant_id === candidate.variant_id && item.trial === index + 1);
      const metrics = Object.fromEntries([...new Set([...Object.keys(left?.metrics ?? {}), ...Object.keys(right?.metrics ?? {})])].map(key => {
        const a = left?.metrics[key], b = right?.metrics[key], reasons = [];
        if (left?.status !== "completed" || right?.status !== "completed") reasons.push("status_not_completed");
        if (left?.degraded.length || right?.degraded.length) reasons.push("degraded");
        if (!a || !b || a.value === null || b.value === null || a.status !== "evaluated" || b.status !== "evaluated") reasons.push("metric_not_evaluated");
        const delta = reasons.length ? null : Number(b.value) - Number(a.value), direction = b?.direction ?? "higher";
        return [key, { baseline: a?.value ?? null, candidate: b?.value ?? null, delta, status: reasons.length ? "excluded" : "paired", outcome: reasons.length ? null : direction === "neutral" ? "unclassified" : delta === 0 ? "tied" : (direction === "lower" ? delta < 0 : delta > 0) ? "improved" : "regressed", reasons }];
      })); return { case_id: id, trial: index + 1, baseline_status: left?.status ?? "not_run", candidate_status: right?.status ?? "not_run", metrics };
    }));
    const keys = [...new Set(pairs.flatMap(item => Object.keys(item.metrics)))];
    const metrics = Object.fromEntries(keys.map(key => { const rows = pairs.map(item => item.metrics[key]), paired = rows.filter(item => item?.status === "paired"), sample = value.results.find(item => item.metrics[key])?.metrics[key], count = field => paired.filter(item => item.outcome === field).length; return [key, { baseline_mean: paired.length ? paired.reduce((sum, item) => sum + Number(item.baseline), 0) / paired.length : null, candidate_mean: paired.length ? paired.reduce((sum, item) => sum + Number(item.candidate), 0) / paired.length : null, mean_delta: paired.length ? paired.reduce((sum, item) => sum + item.delta, 0) / paired.length : null, paired: paired.length, planned: pairs.length, not_comparable: pairs.length - paired.length, excluded_reasons: Object.fromEntries([...new Set(rows.flatMap(item => item?.reasons ?? []))].map(reason => [reason, rows.filter(item => item?.reasons.includes(reason)).length])), improved: count("improved"), tied: count("tied"), regressed: count("regressed"), unclassified: count("unclassified"), direction: sample?.direction ?? "higher", unit: sample?.unit ?? "ratio", scoring_unit: "case" }]; }));
    return { candidate_variant_id: candidate.variant_id, candidate_name: candidate.name, planned_pairs: pairs.length, metrics, pairs };
  });
  return { experiment_id: value.experiment_id, kind: value.kind, protocol: value.protocol, status: value.status, baseline_variant_id: baseline.variant_id, baseline_name: baseline.name, planned_pairs: comparisons.reduce((sum, item) => sum + item.planned_pairs, 0), comparisons, limitations: ["界面夹具，仅用于验证配对展示，不是真实质量结论。"] };
}
const search = { query: "注意力机制", hits: [{ chunk: { id: "qa-child", title: "参考论文界面夹具", page_number: 3, heading_path: ["Attention"], content: "实际返回child文本，定位分数为零但文献覆盖为一。", metadata: { source_id: "qa-source" } }, context_excerpt: "parent补充上下文与child定位分别评分。", lexical_score: 1, vector_score: 0.1, fusion_score: 0.01, rerank_score: null }], trace: { generation_id: "qa-gen", embedding_compatibility: "compatible", lexical_candidates: 10, vector_candidates: 10, fused_candidates: 20, rerank_applied: true, duration_ms: 1, degraded: [] } };
const legacy = { experiment_id: "legacy-fixture", created_at: now, status: "completed", plan: { dataset_id: "old-protocol", mode: "replay", case_ids: ["QA"], arms: [] }, results: [{ case_id: "QA", arm_id: "old", status: "completed", metrics: { source_recall_at_5: 1 }, degraded: [], invariant_failures: [] }] };
const recoveryRun = { id: "qa-recovery-v2", query: "恢复入口回归夹具", status: "interrupted", auto_approve: true, policy: { knowledge_mode: "off" }, created_at: now, updated_at: now, plan: null, plans: [], research_results: [], evidence: [], tool_calls: [], critique_history: [], quality_history: [], errors: [], warnings: [], route_history: [], trace_segments: [], metrics: { tool_calls: 0, model_calls: 0 }, recovery: { available: true, reason: "等待人工继续", checkpoint_id: "fixture-checkpoint", recovery_count: 0, blocked_by: null, unknown_calls: [] } };
let available = false, preflightDelay = 0, artifactDelay = 0, artifactError = false, experimentDelay = 0, deleteDelay = 0, deleteFailure = null, control, page;
const requests = [], failures = [], diagnostics = [];
function fixture(request) {
  const url = new URL(request.url), route = url.pathname.replace(/^\/api\/v1/, ""), body = request.postData ? JSON.parse(request.postData) : null;
  if (request.method === "OPTIONS") return { code: 204, body: "" };
  if (route === "/knowledge/spaces") return { body: [space] };
  if (route.endsWith("/versions")) return { body: [{ id: "qa-v1", version_number: 1, note: "界面固定版本" }] };
  if (route === "/evaluations") return { body: [legacy] };
  if (route === "/evaluations/legacy-fixture") return { body: legacy };
  if (route === "/runs/qa-recovery-v2") return { body: recoveryRun };
  if (route === "/runs/qa-recovery-v2/events") return { stream: true, body: "event: done\ndata: {}\n\n" };
  if (route === "/capabilities") return { body: [] };
  const match = route.match(/^\/evaluations\/(rag|agent)(\/.*)$/); assert.ok(match, `Unexpected API ${request.method} ${route}`); const [, kind, tail] = match;
  if (tail === "/datasets") return { body: [{ ...datasets[kind], dataset_hash: hash }] };
  if (tail === "/settings") return { body: { kind, budget: budgets[kind], protocols: kind === "rag" ? ["rag-retrieval-v2", "rag-answer-v2"] : ["agent-task-v2", "agent-contract-v2"], judge_available: true, langsmith_available: true, default_model: "fixture/model", models: [{ id: "fixture/model", name: "fixture/model", is_default: true }, { id: "~fixture/alternative", name: "测试模型备选", is_default: false }], judge_model: "fixture/model", judge_reuses_default_model: true, judge_limits: { max_tokens: 4096, max_reason_chars: 160 }, ...(kind === "rag" ? { answer_defaults: { prompt_version: "rag-answer-v2.1", max_tokens: 4096, max_context_chars: 12000 } } : { tools, default_tool_allowlist: ["calculator", "web_search"], recommended_candidate_tool_allowlist: ["calculator"] }) } };
  if (tail === "/inputs") { assert.equal(kind, "agent", "RAG may not load a frozen-input catalog"); return { body: [] }; }
  if (tail === "/preflight") {
    assert.equal(body.kind, kind); assert.equal("arms" in body, false);
    assert.equal(body.langsmith_sync, false, "Web experiments must not silently enable removed summary sync");
    assert.equal(body.allow_degraded, false, "Removing the switch must not permit degraded experiments");
    assert.equal(body.variants.every(item => kind === "rag" ? !item.policy && item.retrieval_tuning : !item.retrieval_tuning && item.policy), true);
    const errors = !available ? ["evaluation prerequisites missing: fixture"] : [], delay = preflightDelay; preflightDelay = 0;
    const refs = Object.fromEntries(body.case_ids.flatMap(id => body.variants.flatMap(variant => Array.from({ length: body.repetitions }, (_, index) => [`${id}:${variant.variant_id}:${index + 1}`, `frozen-${id}-${variant.variant_id}-${index + 1}`]))));
    return { delay, body: { ready: !errors.length, errors, warnings: ["仅验证界面，不构成真实质量基线"], expected_cells: body.case_ids.length * body.variants.length * body.repetitions, budget: body.budget, context: {}, dataset_hash: hash, resolved_input_refs: refs } };
  }
  if (tail === "/experiments" && request.method === "GET") return { body: experiments[kind] };
  if (tail === "/experiments" && request.method === "POST") {
    assert.equal(body.kind, kind); assert.equal(body.confirm_live, body.mode === "live"); assert.equal(body.confirm_judge, body.judge_enabled);
    assert.equal(body.langsmith_sync, false); assert.equal(body.allow_degraded, false);
    if (kind === "rag") { assert.equal(body.mode, "live"); assert.deepEqual(body.input_refs, {}); }
    if (body.mode === "replay") assert.ok(Object.keys(body.input_refs).length >= body.case_ids.length * body.variants.length * body.repetitions, "Preflight's resolved refs must be pinned");
    if (kind === "agent") { assert.equal(body.comparison_focus, "tools"); assert.deepEqual(body.variants[0].policy, body.variants[1].policy); assert.equal(body.variants[0].model, body.variants[1].model); assert.equal(body.variants[0].workflow_version, body.variants[1].workflow_version); assert.notDeepEqual(body.variants[0].tool_allowlist, body.variants[1].tool_allowlist); }
    const value = experiment(kind, `${kind}-created-${experiments[kind].length}`, body);
    if (body.mode === "live") value.status = "running";
    experiments[kind].unshift(value); return { body: value };
  }
  if (tail === "/compare") throw new Error("Removed paired-comparison panel must not send requests");
  if (/\/comparison$/.test(tail)) { const value = experiments[kind].find(item => tail.includes(item.experiment_id)); return { body: withinComparison(value, url.searchParams.get("baseline_variant_id")) }; }
  if (/\/cancel$/.test(tail)) { const value = experiments[kind].find(item => tail.includes(item.experiment_id)); value.status = "cancelled"; value.stop_reason = "cancelled"; return { body: value }; }
  const artifactMatch = tail.match(/^\/experiments\/([^/]+)\/artifacts\/([^/]+)$/);
  if (artifactMatch) {
    const value = experiments[kind].find(item => item.experiment_id === artifactMatch[1]), row = value.results.find(item => item.input_id === decodeURIComponent(artifactMatch[2])); assert.ok(row);
    const delay = artifactDelay; artifactDelay = 0;
    if (artifactError) { artifactError = false; return { code: 503, body: { detail: "夹具产物读取失败；评分记录仍可查看" } }; }
    return { delay, body: { input_id: row.input_id, kind, protocol: value.protocol, case_id: row.case_id, variant_id: row.variant_id, trial: row.trial, content: kind === "rag" ? value.protocol === "rag-answer-v2" ? { retrieval: search, answer: "## 夹具回答\n\n回答与引用分开检验。[E1]", citations: ["E1"] } : search : { id: "qa-run", status: "completed", report: "## 夹具报告\n\n流程结束，任务成功尚未评审。", route_history: [{ source: "planner", target: "approval", reason: "等待审批" }, { source: "approval", target: "researcher", reason: "批准后研究" }], tool_calls: [] }, events: kind === "agent" ? [{ event_id: "fixture-event", sequence: 2, event_type: "approval_approved", message: "批准后才开始研究（界面夹具）" }] : [], source_id_map: {} } };
  }
  if (/^\/experiments\/[^/]+$/.test(tail)) {
    const id = decodeURIComponent(tail.split("/").at(-1)), value = experiments[kind].find(item => item.experiment_id === id);
    if (request.method === "DELETE") {
      const delay = deleteDelay; deleteDelay = 0;
      if (deleteFailure) { const failure = deleteFailure; deleteFailure = null; return { delay, code: failure.code, body: { detail: failure.detail } }; }
      if (!value) return { code: 404, body: { detail: "实验不存在" } };
      if (["planned", "running"].includes(value.status)) return { code: 409, body: { detail: "请先停止实验" } };
      experiments[kind] = experiments[kind].filter(item => item.experiment_id !== id);
      return { delay, body: { experiment_id: id, kind, status: "deleted", deleted_at: now, artifacts_retained: true } };
    }
    const delay = experimentDelay; experimentDelay = 0;
    return value ? { delay, body: value } : { code: 404, body: { detail: "实验不存在" } };
  }
  throw new Error(`Unexpected API ${request.method} ${route}`);
}

try {
  let logs = ""; browser.stderr.on("data", data => { logs += data.toString(); }); await until(() => /DevTools listening on (ws:\/\/[^\s]+)/.test(logs), "Browser failed to start");
  const endpoint = logs.match(/DevTools listening on (ws:\/\/[^\s]+)/)[1]; control = await connect(endpoint); const { targetId } = await control.send("Target.createTarget", { url: "about:blank" });
  page = await connect(`ws://${new URL(endpoint).host}/devtools/page/${targetId}`); await page.send("Page.enable"); await page.send("Runtime.enable");
  page.on("Runtime.exceptionThrown", event => failures.push(event.exceptionDetails.exception?.description ?? event.exceptionDetails.text));
  page.on("Fetch.requestPaused", async event => {
    const url = new URL(event.request.url);
    if (!url.pathname.startsWith("/api/v1/")) { await page.send(url.origin === new URL(base).origin ? "Fetch.continueRequest" : "Fetch.failRequest", { requestId: event.requestId, ...(url.origin === new URL(base).origin ? {} : { errorReason: "BlockedByClient" }) }).catch(() => {}); return; }
    try { requests.push({ path: url.pathname, method: event.request.method, body: event.request.postData ? JSON.parse(event.request.postData) : null }); const value = fixture(event.request); if (value.delay) await wait(value.delay); const body = typeof value.body === "string" ? value.body : JSON.stringify(value.body); await page.send("Fetch.fulfillRequest", { requestId: event.requestId, responseCode: value.code ?? 200, responseHeaders: [{ name: "Content-Type", value: value.stream ? "text/event-stream" : "application/json" }, { name: "Access-Control-Allow-Origin", value: "*" }, { name: "Access-Control-Allow-Methods", value: "GET,POST,DELETE,OPTIONS" }, { name: "Access-Control-Allow-Headers", value: "content-type" }], body: Buffer.from(body).toString("base64") }); } catch (error) { failures.push(error.message); await page.send("Fetch.failRequest", { requestId: event.requestId, errorReason: "BlockedByClient" }).catch(() => {}); }
  });
  await page.send("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
  const evaluate = async expression => { const result = await page.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }); if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text); return result.result.value; };
  const click = async text => { assert.equal(await evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()===${JSON.stringify(text)});b?.click();return !!b})()`), true, `Missing button: ${text}`); await wait(100); };
  const fill = async (selector, value) => { await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)throw Error('Missing ${selector}');const p=e instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event(e instanceof HTMLSelectElement?'change':'input',{bubbles:true}));})()`); await wait(100); };
  const screenshot = async name => { await wait(150); writeFileSync(path.join(output, `${name}.png`), Buffer.from((await page.send("Page.captureScreenshot", { captureBeyondViewport: false })).data, "base64")); };
  const open = async (route, selector = ".evaluationV2") => { await page.send("Page.navigate", { url: base + route }); await until(() => evaluate(`!!document.querySelector(${JSON.stringify(selector)})`), "UI did not load"); await until(() => evaluate("Object.keys(document.querySelector('.brand')??{}).some(k=>k.startsWith('__reactFiber'))"), "UI not hydrated"); };
  const creations = kind => requests.filter(item => item.method === "POST" && item.path === `/api/v1/evaluations/${kind}/experiments`);
  const check = async ready => { await click("检查实验条件"); await until(() => evaluate(`!!document.querySelector('.evalPreflight.${ready ? "ready" : "blocked"}')`), "Preflight missing"); };
  const confirmTwice = () => evaluate("(()=>{const b=document.querySelector('.confirmDialog :is(.primaryButton,.dangerButton)');b.click();b.click()})()");
  const pickExperiment = async (kind, id) => {
    const index = experiments[kind].findIndex(item => item.experiment_id === id); assert.ok(index >= 0);
    await until(() => evaluate(`!!document.querySelectorAll('.experimentHistory .experimentRecordSelect')[${index}] && !document.querySelectorAll('.experimentHistory .experimentRecordSelect')[${index}].disabled`), "Wait for the current operation before selecting a record");
    await evaluate(`document.querySelectorAll('.experimentHistory .experimentRecordSelect')[${index}].click()`);
    await until(() => evaluate(`document.querySelector('.experimentResults .evalSectionHead p')?.textContent.includes(${JSON.stringify(id)})`), `Experiment not selected: ${id}`);
    await until(() => evaluate("!!document.querySelector('.metricComparisonChart')"), "Comparison chart missing");
  };
  const selectMetric = async (id, keyboard = false) => {
    const selector = `.withinExperimentComparison button[data-metric-id="${id}"]`;
    if (keyboard) {
      await page.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
      await page.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
    }
    assert.equal(await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)return false;${keyboard ? "e.focus()" : "e.click()"};return true})()`), true, `Metric button missing: ${id}`);
    if (keyboard) {
      assert.equal(await evaluate("document.activeElement.matches(':focus-visible')"), true, "Metric button needs visible keyboard focus");
      await page.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", text: "\r", unmodifiedText: "\r", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
      await page.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
    }
    await until(() => evaluate(`document.querySelector('.metricComparisonChart')?.dataset.metricId===${JSON.stringify(id)}`), `Chart metric did not update: ${id}`);
    assert.equal(await evaluate(`document.querySelector(${JSON.stringify(selector)}).getAttribute('aria-pressed')`), "true");
    assert.equal(await evaluate("document.querySelector('.metricPrimaryTable tr[data-selected=true]').dataset.overviewMetric"), id, "Overview selection and chart must stay synchronized");
  };
  const chartGroup = id => evaluate(`(()=>{const e=document.querySelector('.metricComparisonChart [data-variant-id="${id}"][data-value]');return e?{value:e.getAttribute('data-value'),bars:e.querySelectorAll('[data-chart-bar]').length,zero:e.querySelectorAll('[data-zero-marker]').length,text:e.textContent}:null})()`);
  const chartExplanation = () => evaluate("document.querySelector('.metricComparisonExplanation')?.textContent??''");
  const scrollChart = (selector = ".metricComparisonChart") => evaluate(`(()=>{document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'start'});if(innerWidth<700)window.scrollBy(0,-76)})()`);
  const key = async (value, code, keyCode, modifiers = 0) => { await page.send("Input.dispatchKeyEvent", { type: "keyDown", key: value, code, windowsVirtualKeyCode: keyCode, modifiers }); await page.send("Input.dispatchKeyEvent", { type: "keyUp", key: value, code, windowsVirtualKeyCode: keyCode, modifiers }); await wait(30); };
  const openDetails = async (index = 0) => {
    assert.equal(await evaluate(`(()=>{const e=document.querySelectorAll('.experimentResultCells button[aria-haspopup="dialog"]')[${index}];if(!e)return false;window.__qaDetailTrigger=e;window.__qaBodyOverflow=document.body.style.overflow;e.focus();e.click();return true})()`), true, "Per-case detail trigger missing");
    await until(() => evaluate("!!document.querySelector('dialog[data-evaluation-dialog][open]')"), "Native case-detail dialog not open");
    assert.equal(await evaluate("document.querySelector('[data-evaluation-dialog]').getAttribute('role')"), "dialog");
    assert.equal(await evaluate("document.querySelector('[data-evaluation-dialog]').getAttribute('aria-modal')"), "true");
    assert.equal(await evaluate("document.activeElement.closest('[data-evaluation-dialog]')!==null"), true, "Opening details must move focus inside");
    assert.equal(await evaluate("getComputedStyle(document.body).overflow"), "hidden", "Modal must lock background scrolling");
    assert.equal(await evaluate("[...document.querySelectorAll('.experimentCaseDetail')].every(e=>!!e.closest('[data-evaluation-dialog]'))"), true, "Case detail may not remain inline under the table");
  };
  const assertDetailsClosed = async () => {
    await until(() => evaluate("!document.querySelector('dialog[data-evaluation-dialog][open]')"), "Case-detail dialog did not close");
    assert.equal(await evaluate("document.body.style.overflow===window.__qaBodyOverflow"), true, "Closing must restore body scroll state");
    assert.equal(await evaluate("document.activeElement===window.__qaDetailTrigger"), true, "Closing must restore the original row trigger focus");
  };
  const closeDetails = async (method = "button") => {
    if (method === "escape") await key("Escape", "Escape", 27);
    else if (method === "backdrop") { const point = await evaluate("(()=>{const r=document.querySelector('[data-evaluation-dialog]').getBoundingClientRect();return {x:Math.max(1,r.left/2),y:Math.max(1,r.top/2)}})()"); await page.send("Input.dispatchMouseEvent", { type: "mousePressed", button: "left", clickCount: 1, ...point }); await page.send("Input.dispatchMouseEvent", { type: "mouseReleased", button: "left", clickCount: 1, ...point }); }
    else await click("关闭详情");
    await assertDetailsClosed();
  };
  const verifyModalSafety = async kind => {
    await openDetails(); await until(() => evaluate(kind === "rag" ? "!!document.querySelector('.experimentHit')" : "!!document.querySelector('.experimentAgentReport')"), "Dialog content not ready for focus-cycle checks");
    assert.equal(await evaluate("(()=>{document.querySelector('.experimentBudget input').focus();return !!document.activeElement.closest('[data-evaluation-dialog]')})()"), true, "Top-layer dialog must make background inputs inert");
    const scrollY = await evaluate("window.scrollY"); await page.send("Input.dispatchMouseEvent", { type: "mouseWheel", x: 3, y: 3, deltaX: 0, deltaY: 360 }); await wait(80); assert.equal(await evaluate("window.scrollY"), scrollY, "Wheel over background may not scroll the document");
    const focusables = await evaluate("(()=>{const d=document.querySelector('[data-evaluation-dialog]'),es=[...d.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex=\"-1\"])')].filter(e=>e.getClientRects().length);window.__qaDialogFirst=es[0];window.__qaDialogLast=es.at(-1);es[0].focus();return es.length})()"); assert.ok(focusables >= 1);
    await key("Tab", "Tab", 9, 8); assert.equal(await evaluate("document.activeElement===window.__qaDialogLast"), true, "Shift+Tab must wrap within details");
    await key("Tab", "Tab", 9); assert.equal(await evaluate("document.activeElement===window.__qaDialogFirst"), true, "Tab must wrap back to the first detail control");
    await screenshot(`${kind}-detail-dialog`); await closeDetails("escape");
    await openDetails(); await closeDetails("backdrop");
    artifactDelay = 700; await openDetails(); await closeDetails(); await wait(950);
    assert.equal(await evaluate("!!document.querySelector('dialog[data-evaluation-dialog][open]')"), false, "Late artifact response may not reopen a closed dialog");
    assert.equal(await evaluate("!!document.querySelector('.experimentCaseDetail')"), false, "Late artifact response may not restore inline details");
    artifactError = true; await openDetails(); await until(() => evaluate("document.querySelector('[data-evaluation-dialog]')?.textContent.includes('夹具产物读取失败')"), "Artifact load failure must remain visible inside the dialog");
    assert.equal(await evaluate("!!document.querySelector('[data-evaluation-dialog] .experimentMetricEvidence')"), true, "Artifact failure must preserve saved scores"); await closeDetails();
  };
  const verifyMissingInput = async kind => {
    await pickExperiment(kind, `${kind}-seed-1`); const before = requests.filter(item => /\/artifacts\//.test(item.path)).length;
    await openDetails(); await until(() => evaluate("document.querySelector('[data-evaluation-dialog]')?.textContent.includes('尚未保存产物')"), "Missing-input cases must still expose scores and close controls");
    assert.equal(requests.filter(item => /\/artifacts\//.test(item.path)).length, before, "Missing input must not request a fabricated artifact id"); await closeDetails();
  };

  await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await open("/evaluations/rag"); await until(() => evaluate("document.querySelectorAll('.experimentCases tbody tr').length===2"), "RAG dataset missing");
  assert.equal(await evaluate("[...document.querySelectorAll('label')].some(e=>/同步LangSmith摘要|允许降级诊断/.test(e.textContent))"), false, "Removed options must not render");
  assert.equal(await evaluate("document.querySelectorAll('.ragControls .ragSwitch').length"), 0, "Experiment parameters must not render redundant feature switches");
  assert.equal(await evaluate("document.querySelector('.ragControls').textContent.includes('由选中的对比配置决定')"), false);
  assert.equal(await evaluate("!!document.querySelector('#rag-eval-v2-query-expansion-limit')"), true, "Keep the adjustable expansion limit");
  assert.equal(await evaluate("!!document.querySelector('.experimentComparison,select[aria-label=基线实验],select[aria-label=候选实验]')"), false, "Cross-experiment paired comparison must be removed");
  assert.equal(await evaluate("document.querySelectorAll('[name=rag-protocol]').length"), 2);
  assert.equal(await evaluate("document.querySelectorAll('[name=experiment-mode],select[aria-label$=冻结输入]').length"), 0, "RAG replay and frozen-input controls must be removed");
  assert.equal(await evaluate("[...document.querySelectorAll('label')].some(e=>e.textContent.includes('独立Judge评审'))"), false, "Retrieval-only must not offer answer Judge");
  await check(false); assert.equal(creations("rag").length, 0); await screenshot("rag-preflight-blocked"); available = true; await check(true);
  await fill("#rag-eval-v2-rrf-k", "70"); assert.equal(await evaluate("!!document.querySelector('.evalPreflight')"), false, "Changed config must invalidate preflight");
  preflightDelay = 300; await click("检查实验条件"); await fill("#rag-eval-v2-rrf-k", "80"); await wait(500); assert.equal(await evaluate("!!document.querySelector('.evalPreflight')"), false, "Stale preflight must not enable execution");
  await check(true); await click("开始真实采集"); assert.equal(creations("rag").length, 0); await click("返回"); assert.equal(creations("rag").length, 0); await click("开始真实采集"); await confirmTwice();
  await until(() => creations("rag").length === 1, "RAG live run not submitted"); await until(() => evaluate("!!document.querySelector('.experimentResultCells')"), "RAG results missing");
  await until(() => evaluate("!!document.querySelector('.metricComparisonChart')"), "Multi-group comparison missing");
  assert.equal(creations("rag").length, 1); assert.deepEqual(creations("rag")[0].body.input_refs, {});
  assert.deepEqual(creations("rag")[0].body.cutoffs, [1, 5], "New web experiments must not produce redundant K=3/10 primary views");
  assert.equal(await evaluate("document.querySelectorAll('[data-overview-metric]').length"), 5, "Retrieval has exactly five primary metrics");
  assert.equal(await evaluate("!!document.querySelector('[data-overview-metric=source_coverage_at_5],[data-overview-metric=citation_validity]')"), false);
  assert.deepEqual(creations("rag")[0].body.variants.map(variant => [variant.variant_id, variant.retrieval_tuning.rerank_enabled, variant.retrieval_tuning.query_expansion_enabled]), [["baseline", false, false], ["candidate", true, true]], "Removing read-only switches must preserve each control's actual feature flags");
  await openDetails(); await until(() => evaluate("!!document.querySelector('.experimentHit')"), "Frozen RAG evidence missing");
  assert.equal(await evaluate("document.querySelector('.experimentCaseDetail').textContent.includes('parent补充上下文')"), true);
  assert.equal(await evaluate("document.querySelector('.experimentGold').textContent.includes('人工标注的参考锚点')"), true);
  assert.equal(await evaluate("document.querySelector('.experimentGold').textContent.includes('基于原文核对的界面参考答案')"), true, "Matching label versions must expose reference answers");
  assert.equal(await evaluate("document.querySelector('.experimentGold').textContent.includes('原文核验依据')"), true);
  await closeDetails();
  assert.equal(await evaluate("document.querySelector('.metricComparisonChart').dataset.paired"), "1", "Degraded case must stay outside every group's common cohort");
  assert.equal(await evaluate("document.querySelector('.metricComparisonChart .metricMultiCaseRow[data-pair-status=excluded]').textContent.includes('降级')"), true, "Excluded case reason must be visible");
  assert.equal(await evaluate("!!document.querySelector('.metricChartAudit,.experimentPairedSummary,.experimentPairedCases')"), false, "The redundant audit-data module must be removed");
  await evaluate("document.querySelector('.experimentResults').scrollIntoView({block:'start'})"); await screenshot("rag-retrieval-results");
  await verifyModalSafety("rag");
  await verifyMissingInput("rag");
  await evaluate("document.querySelectorAll('[name=rag-protocol]')[1].click()"); await wait(150); assert.equal(await evaluate("!!document.querySelector('.experimentGenerator')"), true);
  assert.equal(await evaluate("document.querySelector('input[aria-label=回答Token上限]').value"), "4096", "Answer budget must match server defaults");
  assert.match(await evaluate("document.querySelector('.experimentOptions').textContent"), /4096Token.*160字符/, "Configured Judge limits must be visible");
  await fill('input[aria-label="回答Token上限"]', "4097");
  assert.equal(await evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent==='检查实验条件').disabled"), true, "Out-of-range answer budget must block preflight");
  await fill('input[aria-label="回答Token上限"]', "4096");
  assert.equal(await evaluate("!!document.querySelector('input[aria-label=回答模型]')"), false, "Answer model must not be free text");
  assert.equal(await evaluate("document.querySelector('select[aria-label=回答模型]').options[0].textContent.includes('fixture/model')"), true, "Default model must be visible by name");
  await check(true); await fill('select[aria-label="回答模型"]', "~fixture/alternative");
  assert.equal(await evaluate("!!document.querySelector('.evalPreflight')"), false, "Changing the answer model must invalidate preflight");
  assert.equal(await evaluate("document.querySelector('.experimentOptions').textContent.includes('评审模型：fixture/model（复用主模型）')"), true, "Judge must show its actual configured model");
  assert.equal(await evaluate("(()=>{const controls=[...document.querySelectorAll('.experimentGenerator .evalFields select,.experimentGenerator .evalFields input')];return Math.abs(controls[0].getBoundingClientRect().top-controls[1].getBoundingClientRect().top)<=1})()"), true, "Answer model and context input must align despite helper text");
  await evaluate("document.querySelector('.experimentGenerator').scrollIntoView({block:'start'})"); await screenshot("rag-answer-model-options");
  await evaluate("[...document.querySelectorAll('.experimentOptions label')].find(e=>e.textContent.includes('独立Judge评审')).querySelector('input').click()"); await check(true); await click("开始真实采集");
  assert.equal(await evaluate("document.querySelector('[role=dialog]').textContent.includes('Judge将接收答案与证据')"), true); await confirmTwice(); await until(() => creations("rag").length === 2, "Answer experiment not submitted");
  assert.equal(creations("rag")[1].body.variants.every(variant => variant.generator.model === "~fixture/alternative"), true, "All RAG controls must share the selected answer model");
  assert.equal(creations("rag")[1].body.variants.every(variant => variant.generator.max_tokens === 4096 && variant.generator.prompt_version === "rag-answer-v2.1"), true, "Submit new answer budget and prompt together");
  await until(() => evaluate("document.querySelector('.experimentResults h2')?.textContent.includes('检索与回答')"), "Answer result selection not updated");
  assert.equal(await evaluate("document.querySelectorAll('[data-overview-metric]').length"), 8, "Answer protocol has exactly eight primary metrics");
  await openDetails(); await until(() => evaluate("!!document.querySelector('.experimentAnswer')"), "Answer artifact missing");
  assert.equal(await evaluate("document.querySelector('.experimentMetricEvidence').textContent.includes('评审失败')"), true, "Judge failure must remain explicit");
  assert.equal(await evaluate("document.querySelector('.experimentMetricEvidence').textContent.includes('0.000')"), true, "Judge failure must not hide retained retrieval zero");
  assert.equal(await evaluate("document.querySelectorAll('.experimentMetricEvidence > article').length"), 8, "Details must use the same limited primary set as the overview");
  assert.equal(await evaluate("document.querySelector('.experimentMetricEvidence').textContent.includes('引用合法性')"), false, "Raw diagnostics should not return as a second full metric list");
  await evaluate("document.querySelector('.experimentAnswer').scrollIntoView({block:'start'})"); await screenshot("rag-answer-detail"); await closeDetails();
  await pickExperiment("rag", "rag-seed-0"); await selectMetric("anchor_coverage_at_5");
  assert.equal((await chartGroup("baseline"))?.value, "0", "Measured zero must use an actual zero marker");
  assert.equal((await chartGroup("baseline"))?.bars, 0, "Zero may not receive a minimum visible bar");
  assert.equal((await chartGroup("baseline"))?.zero, 1);
  assert.equal((await chartGroup("candidate"))?.value, "1"); assert.equal((await chartGroup("conservative"))?.value, "0.25");
  assert.equal(await evaluate("document.querySelectorAll('.metricComparisonChart [data-variant-id][data-value]').length"), 3, "All three groups must appear on the same chart");
  assert.deepEqual(await evaluate("[document.querySelector('.metricComparisonChart').dataset.paired,document.querySelector('.metricComparisonChart').dataset.planned]"), ["1", "2"], "Chart must use the paired, non-degraded cohort");
  assert.equal(await evaluate("document.querySelector('[data-overview-metric=anchor_coverage_at_5]').dataset.common"), "1", "Overview must use the same all-group intersection as the chart");
  assert.equal(await evaluate("document.querySelector('[data-overview-metric=anchor_coverage_at_5] [data-overview-group=baseline]').dataset.value"), "0", "Overview must not use the larger unpaired summary mean");
  assert.match(await evaluate("document.querySelector('[data-overview-metric=anchor_coverage_at_5] .metricPrimaryGap').textContent"), /100\.0个百分点/, "Absolute percentage-point gaps must be immediately visible");
  assert.match(await chartExplanation(), /共同|相同/, "Chart explanation must describe the all-group common cohort");
  assert.match(await chartExplanation(), /降级/, "Excluded degraded pair must be explained next to the chart");
  await scrollChart(); await screenshot("rag-metric-comparison");
  assert.equal(await evaluate("!!document.querySelector('select[aria-label=\"图表候选组\"],select[aria-label=\"基线组\"]')"), false, "All-group comparison must not require choosing a baseline or one candidate");
  assert.equal(await evaluate("document.querySelectorAll('.metricMultiCaseRow[data-pair-status=common] [data-group-point]').length"), 3, "The common case must plot one observation for every group");
  assert.equal(await evaluate("document.querySelectorAll('.metricMultiCaseRow[data-pair-status=excluded] [data-group-point]').length"), 0, "Excluded cases cannot show fabricated zero observations");
  assert.equal(await evaluate("!!document.querySelector('[data-overview-metric=ndcg_at_5]')"), false, "Unlabelled nDCG must not occupy a primary slot");
  await selectMetric("context_anchor_coverage_at_5", true);
  assert.equal(await evaluate("document.querySelector('.metricComparisonChart').querySelectorAll('[data-chart-bar],[data-zero-marker]').length"), 0, "Missing metrics may not be plotted as zero");
  assert.match(await chartExplanation(), /缺评|未评测|无法|不可比较|没有/, "Missing metric chart needs a reason");

  await open("/evaluations/agent"); await until(() => evaluate("document.querySelectorAll('.experimentCases tbody tr').length===2"), "Agent scenarios missing");
  assert.equal(await evaluate("[...document.querySelectorAll('label')].some(e=>/同步LangSmith摘要|允许降级诊断/.test(e.textContent))"), false, "Agent must not retain a summary-sync switch");
  assert.equal(await evaluate("!!document.querySelector('.ragControls')"), false, "Agent must not use RAG controls");
  assert.equal(await evaluate("document.querySelector('input[aria-label=\"单题时限（秒）\"]').value"), "600");
  assert.equal(await evaluate("document.querySelector('.agentEvaluation').textContent.includes('Recall')"), false);
  await fill('input[aria-label="每个场景重复次数"]', "2");
  assert.equal(await evaluate("!!document.querySelector('input[aria-label=共同模型标识]')"), false, "Agent model must not be free text");
  assert.equal(await evaluate("document.querySelector('select[aria-label=共同执行模型]').options[0].textContent.includes('fixture/model')"), true);
  await fill('select[aria-label="共同执行模型"]', "~fixture/alternative");
  assert.equal(await evaluate("(()=>{const controls=[...document.querySelectorAll('.agentSharedSettings .evalFields select,.agentSharedSettings .evalFields input')];return Math.abs(controls[0].getBoundingClientRect().top-controls[1].getBoundingClientRect().top)<=1})()"), true, "Shared execution fields must align despite model helper text");
  await evaluate("document.querySelector('.agentSharedSettings').scrollIntoView({block:'start'})"); await screenshot("agent-model-options");
  await fill('input[aria-label="共同每轮工具额度"]', "4");
  assert.equal(await evaluate("document.querySelector('input[aria-label=\\\"基线工具组启用map_route\\\"]').disabled"), true);
  await evaluate("document.querySelectorAll('[name=experiment-mode]')[1].click()"); await check(true); await click("开始真实采集"); assert.equal(creations("agent").length, 0); await screenshot("agent-live-confirmation"); await confirmTwice();
  await until(() => creations("agent").length === 1, "Agent live not created"); await until(() => evaluate("!!document.querySelector('.experimentResults')"), "Agent results missing");
  await until(() => evaluate("!!document.querySelector('.metricComparisonChart')"), "Agent multi-group comparison missing");
  const agentPlan = creations("agent")[0].body; assert.equal(agentPlan.repetitions, 2); assert.equal(agentPlan.budget.case_timeout_seconds, 600); assert.equal("retrieval_tuning" in agentPlan.variants[0], false); assert.deepEqual(agentPlan.variants[1].tool_allowlist, ["calculator"]); assert.equal(agentPlan.variants[0].model, "~fixture/alternative"); assert.equal(agentPlan.variants[1].model, "~fixture/alternative"); assert.equal(agentPlan.variants[0].policy.max_tool_calls_per_turn, 4); assert.deepEqual(agentPlan.variants[0].policy, agentPlan.variants[1].policy);
  assert.equal(await evaluate("document.querySelector('.experimentResultCells').textContent.includes('单题超时')"), true);
  assert.equal(await evaluate("document.querySelector('.withinExperimentComparison').textContent.includes('未评测')"), true, "Missing task rubric cannot become success");
  await selectMetric("duration_ms");
  assert.equal(await evaluate("document.querySelector('.metricMultiCaseChart').textContent.includes('1.00秒')"), true, "Per-case latency must retain its unit");
  await selectMetric("known_cost");
  assert.equal(await evaluate("document.querySelector('.metricMultiCaseChart').textContent.includes('$0.2000')"), true, "Per-case cost must retain its unit");
  await openDetails(); await until(() => evaluate("!!document.querySelector('.experimentAgentReport')"), "Agent report missing"); assert.equal(await evaluate("document.querySelector('.experimentRoutes').textContent.includes('批准后才开始研究')"), true); await closeDetails();
  await verifyModalSafety("agent");
  await evaluate("document.querySelector('.experimentResults').scrollIntoView({block:'start'})"); await screenshot("agent-results");
  await click("停止实验"); assert.equal(requests.filter(item => /\/cancel$/.test(item.path) && item.method === "POST").length, 0); await click("返回"); await click("停止实验"); await confirmTwice(); await until(() => requests.filter(item => /\/cancel$/.test(item.path) && item.method === "POST").length === 1, "Cancel not sent once");
  assert.equal(await evaluate("!!document.querySelector('.experimentComparison,select[aria-label=基线实验],select[aria-label=候选实验]')"), false, "Agent paired-comparison panel must be removed");
  await verifyMissingInput("agent");
  await pickExperiment("agent", "agent-seed-0"); await selectMetric("duration_ms");
  assert.equal(await evaluate("document.querySelectorAll('[data-overview-metric]').length"), 8, "Agent task protocol uses eight independent primary metrics");
  assert.equal(await evaluate("!!document.querySelector('[data-overview-metric=execution_completed],[data-overview-metric=report_present]')"), false, "Completion is not a quality metric");
  assert.equal(await evaluate("document.querySelector('.metricComparisonChart').dataset.direction"), "lower");
  assert.equal(await evaluate("document.querySelector('.metricComparisonChart').dataset.unit"), "ms");
  assert.equal((await chartGroup("baseline"))?.value, "1000"); assert.equal((await chartGroup("candidate"))?.value, "600");
  assert.match(await chartExplanation(), /越低越好|最低/, "Latency explanation must identify low values as preferable, without inventing a baseline gain");
  assert.equal((await chartGroup("conservative"))?.value, "1200");
  assert.equal(await evaluate("document.querySelectorAll('.metricComparisonChart [data-variant-id][data-value]').length"), 3, "Latency must compare every group on the same unit axis");
  await selectMetric("known_cost", true);
  assert.match(await evaluate("document.querySelector('.metricComparisonChart').dataset.unit"), /^usd$/i);
  assert.equal((await chartGroup("candidate"))?.value, "0.1");
  assert.match(await evaluate("document.querySelector('.metricComparisonChart').textContent"), /\$0\.1000/, "Chart values need actual currency units");
  await selectMetric("tool_executions");
  assert.equal(await evaluate("document.querySelector('.metricComparisonChart').dataset.direction"), "neutral");
  assert.match(await chartExplanation(), /不按数值大小判定效果改善/, "Neutral tool counts may not imply improved quality");
  await selectMetric("task_success");
  assert.equal(await evaluate("document.querySelector('.metricComparisonChart').querySelectorAll('[data-chart-bar],[data-zero-marker]').length"), 0, "Unreviewed task success must stay unplotted");
  await selectMetric("duration_ms"); await scrollChart(); await screenshot("agent-metric-comparison");

  // Six control groups, matching the RAG comparison scale; these are UI fixtures.
  const sixPlan = plan("rag");
  sixPlan.variants = ["lexical", "semantic", "baseline", "reranked", "expanded", "candidate"].map((id, index) => ({ ...variants("rag")[index % 2], variant_id: id, name: ["关键词", "语义", "混合召回", "混合与重排", "混合与扩展", "组合配置"][index] }));
  const six = experiment("rag", "rag-six-groups", sixPlan);
  for (const row of six.results) {
    const index = sixPlan.variants.findIndex(group => group.variant_id === row.variant_id);
    const rank = (row.case_id === "R1" ? [2, 5, 4, 1, 4, 1] : [0, 0, 0, 1, 1, 1])[index];
    row.degraded = []; row.metrics.anchor_mrr_at_5 = metric(rank ? 1 / rank : 0);
    row.metrics.anchor_hit_at_1 = metric(rank === 1, { unit: "boolean" });
    row.metrics.anchor_coverage_at_5 = metric(rank ? 1 : 0);
    row.metrics.context_anchor_coverage_at_5 = metric(rank ? 1 : 0);
    row.metrics.retrieval_duration_ms = metric([450, 900, 1200, 1800, 2100, 2400][index], { unit: "ms", direction: "lower" });
  }
  six.summary = summary(sixPlan, six.results); experiments.rag.push(six);
  await open("/evaluations/rag"); await pickExperiment("rag", "rag-six-groups"); await selectMetric("anchor_mrr_at_5");
  assert.equal(await evaluate("document.querySelectorAll('.metricPrimaryTable thead th').length"), 8, "Six groups and two descriptive columns must all be present");
  assert.equal(await evaluate("document.querySelector('[data-overview-metric=anchor_mrr_at_5]').dataset.spread"), "0.9");
  assert.equal(await evaluate("[...document.querySelectorAll('[data-overview-metric=anchor_mrr_at_5] [data-overview-group]')].every(e=>document.querySelector('.metricComparisonChart [data-variant-id='+e.dataset.overviewGroup+']').dataset.value===e.dataset.value)"), true, "All six overview values must exactly equal the plotted common-cohort means");
  for (const width of [1440, 390]) {
    await page.send("Emulation.setDeviceMetricsOverride", { width, height: 1000, deviceScaleFactor: 1, mobile: width === 390 });
    await scrollChart(".metricPrimaryOverview");
    const bounds = await evaluate("(()=>{const e=document.querySelector('.metricPrimaryOverview'),r=e.getBoundingClientRect();return {left:r.left,right:r.right,document:document.documentElement.scrollWidth,viewport:innerWidth}})()");
    assert.ok(bounds.left >= 0 && bounds.right <= width + 1 && bounds.document <= width + 1, "Six groups must scroll inside their container, not the page");
    await screenshot(`rag-primary-six-${width}`);
    await evaluate("document.querySelector('.metricPrimaryOverview').scrollLeft=10000");
    assert.equal(await evaluate("(()=>{const e=document.querySelector('.metricPrimaryTable tbody th').getBoundingClientRect();return e.left>=0&&e.right<=innerWidth})()"), true, "Metric labels must remain visible while scrolling groups");
    await evaluate("document.querySelector('.metricPrimaryOverview').scrollLeft=0");
  }

  for (const view of [{ id: "desktop", width: 1440, height: 1000, theme: "light" }, { id: "mobile", width: 390, height: 844, theme: "light" }, { id: "dark", width: 1440, height: 1000, theme: "dark" }]) {
    const theme = await page.send("Page.addScriptToEvaluateOnNewDocument", { source: `localStorage.setItem('atlasflow.theme',${JSON.stringify(view.theme)})` });
    await page.send("Emulation.setDeviceMetricsOverride", { width: view.width, height: view.height, deviceScaleFactor: 1, mobile: view.id === "mobile" });
    for (const kind of ["rag", "agent"]) {
      await open(`/evaluations/${kind}`); await until(() => evaluate("document.querySelectorAll('.experimentCases tbody tr').length===2"), "Cases missing"); await wait(150);
      const dimensions = await evaluate("({width:document.documentElement.scrollWidth,viewport:innerWidth})"); assert.ok(dimensions.width <= view.width + 1, `${kind} ${view.id} overflows`); diagnostics.push({ kind, view: view.id, ...dimensions }); await screenshot(`${kind}-${view.id}`);
      await evaluate("document.querySelector('.experimentHistory').scrollIntoView({block:'start'})");
      const recordsFit = await evaluate("[...document.querySelectorAll('.experimentRecordDelete')].every(e=>{const r=e.getBoundingClientRect();return r.width>=32&&r.height>=40&&r.left>=0&&r.right<=innerWidth})");
      assert.equal(recordsFit, true, `${kind} ${view.id}: deletion must stay visible and usable`); await screenshot(`${kind}-records-${view.id}`);
      await pickExperiment(kind, `${kind}-seed-0`); await selectMetric(kind === "rag" ? "anchor_coverage_at_5" : "duration_ms"); await scrollChart();
      const chartDimensions = await evaluate("(()=>{const e=document.querySelector('.metricComparisonChart'),r=e.getBoundingClientRect();return {width:document.documentElement.scrollWidth,viewport:innerWidth,left:r.left,right:r.right,groups:e.querySelectorAll('[data-variant-id][data-value]').length,points:e.querySelectorAll('[data-group-point]').length}})()");
      assert.ok(chartDimensions.width <= view.width + 1, `${kind} ${view.id} chart overflows`); assert.ok(chartDimensions.left >= -1 && chartDimensions.right <= view.width + 1, `${kind} ${view.id} chart escapes viewport`); assert.equal(chartDimensions.groups, 3, "Every group must remain visible on every viewport"); assert.ok(chartDimensions.points >= 3, "Common-case observations must remain visible"); diagnostics.push({ kind, view: `${view.id}-chart`, ...chartDimensions }); await screenshot(`${kind}-metric-comparison-${view.id}`);
      if (view.id === "mobile") { await scrollChart(".metricMultiCaseChart"); await screenshot(`${kind}-metric-cases-mobile`); }
      await openDetails(); await until(() => evaluate(kind === "rag" ? "!!document.querySelector('.experimentHit')" : "!!document.querySelector('.experimentAgentReport')"), "Dialog artifact missing");
      const dialogDimensions = await evaluate("(()=>{const e=document.querySelector('[data-evaluation-dialog]'),r=e.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:e.scrollWidth,client:e.clientWidth,height:innerHeight}})()");
      assert.ok(dialogDimensions.left >= 0 && dialogDimensions.right <= view.width + 1, `${kind} ${view.id} dialog escapes viewport width`); assert.ok(dialogDimensions.top >= 0 && dialogDimensions.bottom <= view.height + 1, `${kind} ${view.id} dialog escapes viewport height`); assert.ok(dialogDimensions.width <= dialogDimensions.client + 1, `${kind} ${view.id} dialog content overflows`); diagnostics.push({ kind, view: `${view.id}-dialog`, ...dialogDimensions }); await screenshot(`${kind}-detail-dialog-${view.id}`); await closeDetails();
    }
    await page.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 }); await page.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 }); assert.equal(await evaluate("document.activeElement.matches(':focus-visible')"), true); await page.send("Page.removeScriptToEvaluateOnNewDocument", { identifier: theme.identifier });
  }
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  const contractPlan = plan("agent"); contractPlan.protocol = "agent-contract-v2"; contractPlan.case_ids = ["A09"];
  experiments.agent.push(experiment("agent", "agent-contract-profile", contractPlan));
  await open("/evaluations/agent"); await pickExperiment("agent", "agent-contract-profile");
  assert.equal(await evaluate("document.querySelectorAll('[data-overview-metric]').length"), 7, "Engineering contracts have seven primary metrics, not the task or RAG profile");
  assert.equal(await evaluate("!!document.querySelector('[data-overview-metric=task_success],[data-overview-metric=anchor_coverage_at_5]')"), false);
  experiments.rag.push({ ...experiment("rag", "rag-old-labels"), dataset_hash: "f".repeat(64) });
  await open("/evaluations/rag"); await pickExperiment("rag", "rag-old-labels"); await openDetails();
  await until(() => evaluate("document.querySelector('.experimentGold')?.textContent.includes('没有与本实验标签版本一致的参考标注')"), "Historical results must not show current dataset's labels");
  assert.equal(await evaluate("!!document.querySelector('.experimentMetricEvidence')"), true, "Historical saved scores must remain available");
  await screenshot("rag-historical-label-version"); await closeDetails();
  const deletions = () => requests.filter(item => item.method === "DELETE");
  const recordSelector = id => `.experimentRecordRow[data-experiment-id="${id}"]`;
  const recordExists = id => evaluate(`!!document.querySelector(${JSON.stringify(recordSelector(id))})`);
  const requestDelete = async id => {
    await evaluate(`document.querySelector(${JSON.stringify(`${recordSelector(id)} .experimentRecordDelete`)}).click()`);
    await until(() => evaluate("document.querySelectorAll('.confirmDialog').length===1 && document.querySelector('.confirmDialog h2')?.textContent==='确认删除实验记录？'"), "Exactly one deletion confirmation must open");
    assert.equal(await evaluate(`document.querySelector('.confirmDialog').textContent.includes(${JSON.stringify(id)})`), true, "Confirmation must identify the exact record");
  };
  for (const kind of ["rag", "agent"]) {
    experiments[kind].push({ ...experiment(kind, `${kind}-active-check`), status: "running" });
    await open(`/evaluations/${kind}`); await until(() => recordExists(`${kind}-seed-0`), "Experiment records missing for deletion QA");
    assert.equal(await evaluate(`document.querySelector(${JSON.stringify(`${recordSelector(`${kind}-active-check`)} .experimentRecordDelete`)}).disabled`), true, "Active records may not be deleted");
    await pickExperiment(kind, `${kind}-seed-0`);
    const target = `${kind}-seed-1`, before = deletions().length;
    await requestDelete(target); assert.equal(deletions().length, before, "Opening confirmation must not delete");
    assert.equal(await evaluate("document.activeElement.textContent"), "返回", "Danger confirmation must focus the safe action");
    await key("Escape", "Escape", 27); await until(() => evaluate("!document.querySelector('.confirmDialog')"), "Esc must dismiss deletion");
    await requestDelete(target); await click("返回"); assert.equal(deletions().length, before, "Cancelling must not send DELETE");
    assert.equal(await recordExists(target), true);
    for (const failure of [{ code: 409, detail: "实验仍在收尾，请稍后删除" }, { code: 500, detail: "删除失败，请重试" }]) {
      deleteFailure = failure;
      await requestDelete(target); await confirmTwice();
      await until(() => evaluate(`!!document.querySelector('[role=alert]')?.textContent.includes(${JSON.stringify(failure.detail)})`), "Deletion failure must remain actionable");
      assert.equal(await recordExists(target), true, "Failed DELETE must keep the record");
      assert.equal(await evaluate(`document.querySelector('.experimentResults .evalSectionHead p').textContent.includes('${kind}-seed-0')`), true, "Deleting another record must not change selection");
    }
    const beforeSuccess = deletions().length; deleteDelay = 700;
    await requestDelete(target); await screenshot(`${kind}-delete-confirmation`); await confirmTwice();
    await until(() => deletions().length === beforeSuccess + 1, "DELETE must be sent exactly once");
    assert.equal(await recordExists(target), true, "Do not optimistically remove a record before success");
    assert.equal(await evaluate("[...document.querySelectorAll('.experimentRecordDelete')].every(e=>e.disabled)"), true, "Pending deletion must block duplicate actions");
    await until(async () => !(await recordExists(target)), "Successful deletion must remove the row");
    assert.equal(deletions().length, beforeSuccess + 1);
    assert.equal(await evaluate(`document.querySelector('.experimentResults .evalSectionHead p').textContent.includes('${kind}-seed-0')`), true);
    assert.equal(await evaluate("document.activeElement===document.querySelector('.experimentHistory h2')"), true, "Deleted trigger focus must move to the records heading");
    // A stale GET captured before deletion may not restore the selected record.
    const selectedId = `${kind}-seed-0`; experimentDelay = 1400;
    await evaluate(`document.querySelector(${JSON.stringify(`${recordSelector(selectedId)} .experimentRecordSelect`)}).click()`);
    await requestDelete(selectedId); await confirmTwice();
    await until(async () => !(await recordExists(selectedId)), "Selected record must be removed"); await wait(1600);
    assert.equal(await evaluate("!!document.querySelector('.experimentResults,[data-evaluation-dialog]')"), false, "Deleted selection and late details must not return");
    await open(`/evaluations/${kind}`); await until(() => evaluate("!!document.querySelector('.experimentRecordRow')"), "Remaining records must reload");
    assert.equal(await recordExists(target), false); assert.equal(await recordExists(selectedId), false, "Deleted records must stay absent after reload");
    // Isolate the final-record empty state with one disposable fixture record.
    const finalId = `${kind}-last-record`; experiments[kind] = [experiment(kind, finalId)];
    await open(`/evaluations/${kind}`); await until(() => recordExists(finalId), "Last-record fixture missing"); await pickExperiment(kind, finalId);
    await requestDelete(finalId); await confirmTwice();
    await until(() => evaluate("!document.querySelector('.experimentRecordRow') && document.querySelector('.experimentHistory').textContent.includes('尚无实验记录')"), "Deleting the last record must show an empty state");
    assert.equal(await evaluate("!!document.querySelector('.experimentResults')"), false);
  }
  assert.equal(requests.filter(item => item.method === "POST" && item.path.endsWith("/compare")).length, 0, "Removed paired-comparison UI must issue no requests");
  await open("/knowledge/evaluations");
  await until(() => evaluate("location.pathname==='/evaluations/rag' && !!document.querySelector('.ragEvaluation')"), "Old evaluation URL must redirect to RAG");
  assert.equal(await evaluate("!!document.querySelector('.legacyEvaluation')"), false);
  assert.equal(await evaluate("document.querySelector('.experimentNavigation').textContent.includes('历史实验')"), false);
  assert.equal(await evaluate("!!document.querySelector('.experimentNavigation a[href=\"/knowledge/evaluations\"]')"), false);
  assert.equal(requests.filter(item => item.path === "/api/v1/evaluations" || item.path === "/api/v1/evaluations/legacy-fixture").length, 0, "Removed history must not query legacy records");
  await screenshot("history-entry-removed");
  await open("/runs/qa-recovery-v2", ".recoveryPanel"); assert.equal(await evaluate("[...document.querySelectorAll('button')].some(e=>e.textContent==='继续执行')"), true); await click("继续执行"); assert.equal(await evaluate("!!document.querySelector('[role=dialog]')"), true); await click("返回");
  assert.equal(requests.filter(item => item.path.endsWith("/resume") && item.method === "POST").length, 0, "Recovery must remain manual");
  assert.deepEqual(failures, []); writeFileSync(path.join(output, "diagnostics.json"), JSON.stringify({ fixture_only: true, no_real_model_calls: true, requests, diagnostics, failures }, null, 2));
  console.log(`Evaluation v2 browser QA passed. Artifacts: ${output}`);
} catch (error) { if (page) await page.send("Page.captureScreenshot").then(value => writeFileSync(path.join(output, "failure.png"), Buffer.from(value.data, "base64"))).catch(() => {}); writeFileSync(path.join(output, "diagnostics.json"), JSON.stringify({ fixture_only: true, requests, diagnostics, failures, error: error.message }, null, 2)); throw error; }
finally { page?.close(); if (control) { await control.send("Browser.close").catch(() => {}); control.close(); } browser.kill(); }
