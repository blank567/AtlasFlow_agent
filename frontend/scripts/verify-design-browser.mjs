// node scripts/verify-design-browser.mjs http://localhost:3210 [--baseline] [--only=home,knowledge]
// All API responses are local fixtures. Profiles, screenshots and diagnostics stay on E:.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { WebSocket } = require("next/dist/compiled/ws");
const workspace = path.resolve(fileURLToPath(new URL("../../", import.meta.url)));
assert.match(workspace, /^E:/i, "QA artifacts must stay on E:");
const args = process.argv.slice(2);
const base = args.find((arg) => /^https?:/.test(arg)) ?? "http://localhost:3210";
const baseline = args.includes("--baseline");
const selection = args.find((arg) => arg.startsWith("--only="))?.slice(7).split(",");
const root = path.join(workspace, "build", "design-browser");
mkdirSync(root, { recursive: true });
const output = mkdtempSync(path.join(root, baseline ? "baseline-" : "check-"));
const binary = ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].find(existsSync);
assert.ok(binary, "Chrome or Edge is required");
const browser = spawn(binary, ["--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-breakpad", "--disable-crash-reporter", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${path.join(output, "profile")}`, "about:blank"], {
  windowsHide: true, env: { ...process.env, TEMP: "E:\\codex\\tmp", TMP: "E:\\codex\\tmp" }, stdio: ["ignore", "ignore", "pipe"],
});
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(check, message, limit = 300) {
  for (let attempt = 0; attempt < limit; attempt++) { if (await check()) return; await wait(100); }
  throw new Error(message);
}
async function connect(endpoint) {
  const socket = new WebSocket(endpoint);
  await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
  let sequence = 0;
  const pending = new Map(), events = new Map();
  socket.on("message", (data) => {
    const packet = JSON.parse(data);
    if (packet.id) {
      const callback = pending.get(packet.id); pending.delete(packet.id);
      if (callback) { clearTimeout(callback.timer); packet.error ? callback.reject(new Error(packet.error.message)) : callback.resolve(packet.result); }
    } else events.get(packet.method)?.(packet.params);
  });
  return {
    on(method, callback) { events.set(method, callback); },
    send(method, params = {}) { return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 30_000);
      pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }));
    }); },
    close() { socket.close(); },
  };
}

const now = "2026-10-13T03:00:00Z";
function event(sequence, type, node, data = {}, message = "研究执行记录") {
  return { event_id: `event-${sequence}`, sequence, event_type: type, node, agent: node, plan_version: 1, data, message, created_at: now };
}
const task = (id, title, dependencies = []) => ({ task_id: id, title, objective: `核实${title}的依据与适用边界`, success_criteria: ["给出原始来源", "说明局限"], priority: 1, dependencies, required_capabilities: ["knowledge_search"], capability_alternatives: [], requires_fresh_data: false, provenance: "initial", plan_version: 1 });
const plan = { plan_version: 1, rationale: "先建立理论依据，再对工程权衡进行比较。", tasks: [task("T1", "解析注意力机制"), task("T2", "比较推理优化", ["T1"]), task("T3", "整理工程建议", ["T2"])] };
const routePairs = [["supervisor", "planner"], ["planner", "approval"], ["approval", "schedule_wave"], ["schedule_wave", "researcher"], ["researcher", "wave_join"], ["wave_join", "research_gate"], ["research_gate", "critic"], ["critic", "schedule_wave", "supplement"], ["schedule_wave", "researcher"], ["researcher", "critic"], ["critic", "planner", "replan"], ["planner", "approval"], ["approval", "researcher"], ["researcher", "critic"], ["critic", "synthesizer", "accept"], ["synthesizer", "quality_gate"], ["quality_gate", "finalizer", "accept"]];
const routeEvents = routePairs.map(([source, to_node, decision], index) => event(index + 2, "route_selected", source, { to_node, decision }, `路由：${source} → ${to_node}`));
const report = `## 研究摘要\n\n本报告比较 **注意力机制、KV Cache 与推理优化** 的工程关系。以下内容是浏览器展示测试语料，不是模型生成结果。\n\n## 方法与证据\n\n采用原始论文和技术文档进行交叉核验，保留原文定位与检索来源。[1]\n\n### 核心对比\n\n| 方法 | 关注点 | 工程取舍 |\n| --- | --- | --- |\n| FlashAttention | 减少显存读写 | 依赖硬件和算子支持 |\n| PagedAttention | 管理 KV Cache | 增加调度复杂度 |\n| RAG | 引入外部证据 | 依赖检索质量 |\n\n## 分析与建议\n\n先测量延迟、吞吐和显存，再选择适合目标负载的优化方法。检索结果应保留文档版本和原文位置，以便追溯。\n\n> 本页仅验证阅读、导航与下载，不代表实时研究结论。\n\n### 风险与局限\n\n不同系统的测试条件并不一致，不能直接比较未经校准的指标。\n\n## 参考文献\n\n[1] Vaswani et al. Attention Is All You Need. 2017. [论文原文](https://arxiv.org/abs/1706.03762)。`;
const runBase = { id: "design-completed", query: "注意力机制与大模型推理优化的工程权衡", status: "completed", auto_approve: true, policy: { knowledge_mode: "selected", knowledge_space: "llm-engineering-v1", max_tool_calls_per_turn: 5, max_tool_calls_per_run: 20 }, created_at: now, updated_at: now, started_at: "2026-10-13T02:54:00Z", completed_at: now, plan, plans: [plan], research_results: plan.tasks.map((item) => ({ task_id: item.task_id, plan_version: 1, summary: "已完成来源核验与工程分析。", confidence: 0.87, duration_ms: 32000 })), tool_calls: [{ call_id: "tool-1", tool_name: "knowledge_search", agent: "researcher", task_id: "T1", arguments: { query: "attention mechanism" }, success: true, duration_ms: 1280, evidence_ids: ["evidence-1"], summary: "找到原始论文中的注意力定义。", created_at: now }], evidence: [{ id: "evidence-1", source_id: "source-attention", title: "Attention Is All You Need", content: "注意力机制的原始定义与数学形式。", uri: "https://arxiv.org/abs/1706.03762" }], critique_history: [{ decision: "accept", rationale: "证据覆盖问题，保留硬件与评测局限。", plan_version: 1 }], quality_history: [{ decision: "accept", score: 91, rationale: "结论有来源，分析清楚。", plan_version: 1, draft_version: 1 }], errors: [], warnings: [], report, metrics: { total_tasks: 3, successful_tasks: 3, failed_tasks: 0, peak_concurrency: 2, plan_versions: 2, supplement_rounds: 1, replan_count: 1, revision_count: 0, model_calls: 9, tool_calls: 1, duration_ms: 360000 }, events: [event(1, "run_started", "system", {}, "任务开始执行"), ...routeEvents, event(19, "run_completed", "finalizer", {}, "研究已完成")], trace_segments: [{ id: "trace-1", name: "atlasflow.workflow.initial", url: "https://smith.langchain.com/", status: "completed", started_at: "2026-10-13T02:54:00Z", duration_ms: 360000, provider_calls: [{ call_id: "provider-1", operation: "planning", requested_model: "configured-model", model: "configured-model", status: "success", duration_ms: 2400, request_id: "qa-local-provider-id", usage: { total_tokens: 1520 } }] }] };
const runRunning = { ...runBase, id: "design-running", status: "running", report: undefined, completed_at: undefined, quality_history: [], research_results: [], critique_history: [], tool_calls: [], trace_segments: [], metrics: { ...runBase.metrics, successful_tasks: 1, duration_ms: undefined }, events: [event(1, "run_started", "system"), ...routeEvents.slice(0, 10), event(20, "node_started", "researcher", {}, "Researcher 正在补充研究")] };
const streamTools = [event(21, "tool_requested", "researcher", { tool_name: "knowledge_search" }, "QA SSE：请求知识检索"), event(22, "tool_succeeded", "researcher", { tool_name: "knowledge_search" }, "QA SSE：检索工具已完成"), event(23, "tool_failed", "researcher", { tool_name: "web_fetch", error: "QA：网页暂不可用" }, "QA SSE：工具失败"), event(24, "tool_budget_exhausted", "researcher", {}, "QA SSE：工具预算已达到上限")];
const runFailed = { ...runRunning, id: "design-failed", status: "failed", completed_at: now, errors: [{ agent: "planner", code: "PROVIDER_ERROR", message: "服务暂不可用，请稍后重试。" }], error: "服务暂不可用，请稍后重试。", metrics: { ...runRunning.metrics, duration_ms: 1100 }, events: [event(1, "run_started", "system"), event(2, "node_started", "planner", {}, "Planner 正在创建计划"), event(3, "node_failed", "planner", { error: "QA：模型服务暂不可用" }, "Planner 执行失败"), event(4, "run_failed", "system", {}, "运行已结束")] };
const runApproval = { ...runRunning, id: "design-approval", status: "waiting_approval", auto_approve: false, events: [event(1, "run_started", "system"), ...routeEvents.slice(0, 2), event(4, "approval_required", "approval", {}, "计划等待人工审批")] };
const runs = [runBase, runRunning, runFailed, runApproval];
const space = { id: "qa-space", slug: "llm-engineering-v1", name: "LLM 工程知识库", description: "原始论文与工程文档，用于验证知识库界面。", visibility: "private", embedding_profile: "default-embedding-v1", retrieval_profile: "default-hybrid-v1", active_generation_id: "qa-generation-v3", active_version_id: "qa-version-3", active_build_id: null, created_at: now, updated_at: now };
const documents = ["Attention Is All You Need", "FlashAttention", "PagedAttention / vLLM", "Retrieval-Augmented Generation: A Survey"].map((title, index) => ({ id: `qa-doc-${index}`, space_id: space.id, source_id: `arxiv:qa-${index}`, title, status: "active", current_version_id: `qa-doc-v3-${index}`, metadata: {}, created_at: now, updated_at: now }));
const versions = [2, 3].map((number) => ({ id: `qa-version-${number}`, space_id: space.id, version_number: number, generation_id: `qa-generation-v${number}`, documents: Object.fromEntries(documents.map((doc, i) => [doc.id, { version_id: `qa-doc-v${number}-${i}`, status: "active" }])), note: number === 3 ? "统一章节解析与父子切块；双路召回融合。" : "首次整理原始论文与工程文档。", created_at: now }));
const jobs = documents.map((document, index) => ({ id: `qa-job-${index}`, space_id: space.id, document_id: document.id, version_id: document.current_version_id, status: index === 3 ? "failed_terminal" : "completed", stage: index === 3 ? "embedding" : "ready", attempt: 1, progress: index === 3 ? 0.6 : 1, error_message: index === 3 ? "QA：Embedding 服务暂时不可用" : null, created_at: now, updated_at: now }));
const searchResult = { query: "注意力机制如何计算相关性？", decision: { status: "sufficient", reasons: ["原始论文段落覆盖问题的主要内容。"], coverage: 0.9, evidence_count: 3, best_relevance: 0.92, score_basis: "rerank" }, hits: ["Scaled Dot-Product Attention", "Multi-Head Attention", "Position-wise Feed-Forward Networks"].map((title, i) => ({ chunk: { id: `qa-child-${i}`, title: "Attention Is All You Need", content: `The attention function maps a query and a set of key-value pairs to an output. ${title} provides a structured representation.`, heading_path: ["3 Model Architecture", title], page_number: i + 3, metadata: { canonical_uri: "https://arxiv.org/abs/1706.03762", section_role: "body" } }, context_excerpt: "The Transformer follows this overall architecture using stacked self-attention and point-wise, fully connected layers.", lexical_score: 12.5 - i * 2, vector_score: 0.84 - i * 0.08, fusion_score: 0.032 - i * 0.003, rerank_score: 0.92 - i * 0.08 })), trace: { profile_id: "default-hybrid-v1", generation_id: space.active_generation_id, lexical_candidates: 40, vector_candidates: 40, fused_candidates: 40, rerank_candidates: 20, rerank_requested: true, rerank_applied: true, query_expansion_requested: true, query_expansion_applied: true, query_expansion_method: "bilingual-terms-v1", query_variants: ["注意力机制如何计算相关性？", "注意力机制如何计算相关性？ attention mechanism"], matched_terms: ["attention"], score_scope: "best_query_variant", applied_parameters: { lexical_weight: 1, vector_weight: 1, rrf_k: 60, candidate_limit: 40, rerank_limit: 20, query_expansion_enabled: true, query_expansion_limit: 2 }, degraded: [], duration_ms: 1490 } };
function fixture(request) {
  const url = new URL(request.url), route = url.pathname.replace(/^\/api\/v1/, "");
  if (request.method === "OPTIONS") return { body: "", code: 204 };
  if (route === "/knowledge/search/debug") return { body: searchResult };
  if (request.method !== "GET") throw new Error(`Unexpected write in read-only QA: ${request.method} ${route}`);
  if (route.endsWith("/events")) return { stream: true, body: (route.includes("design-running") ? streamTools.map((item) => `id: ${item.sequence}\nevent: ${item.event_type}\ndata: ${JSON.stringify(item)}\n\n`).join("") : "") + 'event: done\ndata: {}\n\n' };
  if (route === "/runs") return { body: { items: runs, total: runs.length, page: 1, page_size: 10, pages: 1 } };
  if (route.startsWith("/runs/")) return { body: runs.find((item) => item.id === route.split("/")[2]) ?? runBase };
  if (route === "/knowledge/spaces") return { body: [space] };
  if (route.endsWith("/snapshot")) return { body: { schema_version: "search-snapshot-v1", space_id: space.id, space_slug: space.slug, space_version_id: space.active_version_id, generation_id: space.active_generation_id, identity_status: "verified", manifest_hash: "f".repeat(64), documents: Object.fromEntries(documents.map(doc => [doc.id, { version_id: doc.current_version_id, status: doc.status }])), build_spec: { parser_version: "structured-parser-v3", chunker_version: "parent-child-v2", child_tokens: 350, parent_tokens: 1200, embedding: { model: "qa-fixture-embedding", observed_dimensions: 16 } } } };
  if (route.endsWith("/documents")) return { body: documents };
  if (route.endsWith("/versions")) return { body: versions };
  if (route.endsWith("/builds")) return { body: [] };
  if (route === "/knowledge/jobs") return { body: jobs };
  if (route === "/capabilities") return { body: [{ id: "knowledge_search", description: "检索已发布的知识空间", supports_fresh_data: false, available: true, tools: ["knowledge_search"], unavailable_reasons: [] }] };
  if (route === "/analytics") return { body: { range: "30d", summary: { total_runs: 4, success_rate: 50, avg_duration_ms: 180500, total_model_calls: 18, total_tasks: 12, task_success_rate: 75, successful_tasks: 9, failed_tasks: 3, avg_quality_score: null, total_supplement_rounds: 2, total_replans: 1, total_revisions: 1 }, status_distribution: [{ status: "completed", count: 1 }, { status: "running", count: 1 }, { status: "failed", count: 1 }, { status: "waiting_approval", count: 1 }], duration_trend: [{ date: "10-08", avg_duration_ms: 100000 }, { date: "10-09", avg_duration_ms: 180000 }, { date: "10-10", avg_duration_ms: 95000 }, { date: "10-11", avg_duration_ms: 240000 }, { date: "10-12", avg_duration_ms: 180000 }], decision_counts: [{ decision: "accept", count: 7 }, { decision: "supplement", count: 2 }, { decision: "replan", count: 1 }, { decision: "revise", count: 1 }], plan_versions: [{ version: 1, count: 3 }, { version: 2, count: 1 }] } };
  if (route === "/settings/status") return { body: { app_version: "0.6.0", database: { path: "E:\\codex\\agent\\data\\atlasflow.db", persistent: true, size_bytes: 524288 }, langsmith: { state: "ready", configured: true, enabled: true, project: "atlasflow", endpoint: "https://api.smith.langchain.com", trace_content: "metadata", connection_status: "not_checked" }, provider: { configured: true, name: "openrouter", model: "configured-model" } } };
  throw new Error(`Missing fixture: ${request.method} ${route}`);
}

const scenarios = [
  { id: "home", route: "/", ready: ".createForm" },
  { id: "runs", route: "/runs", ready: ".runList" },
  { id: "report", route: "/runs/design-completed", ready: ".runDetailPage", tab: "report" },
  { id: "flow", route: "/runs/design-running", ready: ".runDetailPage", tab: "flow" },
  { id: "tasks", route: "/runs/design-completed", ready: ".runDetailPage", tab: "tasks" },
  { id: "reviews", route: "/runs/design-completed", ready: ".runDetailPage", tab: "reviews" },
  { id: "failed", route: "/runs/design-failed", ready: ".runDetailPage", tab: "events" },
  { id: "approval", route: "/runs/design-approval", ready: ".approvalPanel" },
  { id: "knowledge", route: "/knowledge", ready: ".knowledgeRows" },
  { id: "lab", route: "/knowledge/lab", ready: ".ragLab", search: true },
  { id: "jobs", route: "/knowledge/jobs", ready: ".jobLedger" },
  { id: "analytics", route: "/analytics", ready: ".analyticsHighlights" },
  { id: "settings", route: "/settings", ready: ".settingsList" },
].filter((item) => !selection || selection.includes(item.id));
const modes = [{ id: "desktop", width: 1440, height: 1000, mobile: false, theme: "light" }, { id: "mobile", width: 390, height: 844, mobile: true, theme: "light" }, { id: "dark", width: 1440, height: 1000, mobile: false, theme: "dark" }];
// Evaluate real computed colors, including transparent ancestor surfaces.
// Disabled controls and icon-only buttons are intentionally excluded from text contrast.
function auditControlContrast() {
  const parse = (value) => {
    const parts = value.match(/[\d.]+/g)?.map(Number) ?? [];
    // color-mix() resolves to color(srgb ...) in Chromium, whose channels are
    // 0..1; rgb()/rgba() channels are 0..255. Treating them alike hides failures.
    const channels = parts.slice(0, 3).map((channel) => value.startsWith("color(srgb") ? channel * 255 : channel);
    return [...channels, parts[3] ?? 1];
  };
  const blend = (top, base) => top.slice(0, 3).map((value, index) => value * top[3] + base[index] * (1 - top[3]));
  const background = (element) => {
    const ancestors = []; for (let current = element; current; current = current.parentElement) ancestors.push(current);
    return ancestors.reverse().reduce((base, current) => blend(parse(getComputedStyle(current).backgroundColor), base), [255, 255, 255]);
  };
  const luminance = (color) => color.map((value) => { const channel = value / 255; return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4; }).reduce((total, channel, index) => total + channel * [.2126, .7152, .0722][index], 0);
  const issues = [];
  const check = (element, color, label) => {
    const style = getComputedStyle(element), base = background(element), foreground = blend(parse(color), base);
    const values = [luminance(base), luminance(foreground)].sort((a, b) => b - a), ratio = (values[0] + .05) / (values[1] + .05);
    const large = parseFloat(style.fontSize) >= 24 || parseFloat(style.fontSize) >= 18.66 && Number(style.fontWeight) >= 700;
    if (ratio + .02 < (large ? 3 : 4.5)) issues.push({ label, ratio: Number(ratio.toFixed(2)), color, background: base.map(Math.round) });
  };
  for (const element of document.querySelectorAll('button, a.primaryButton, a.secondaryButton, input, select, textarea, .eventTypeLabel')) {
    const style = getComputedStyle(element), rect = element.getBoundingClientRect();
    if (element.matches(':disabled') || rect.width < 5 || rect.height < 5 || style.visibility === 'hidden' || Number(style.opacity) < 1) continue;
    if (element instanceof HTMLInputElement && ['checkbox', 'radio', 'file', 'hidden'].includes(element.type)) continue;
    const label = element.getAttribute('aria-label') || element.value || element.textContent?.trim() || element.placeholder || (element.matches('input,select,textarea') ? element.id : "");
    if (!label) continue;
    check(element, style.color, label.slice(0, 50));
    if (element.matches('input[placeholder],textarea[placeholder]')) check(element, getComputedStyle(element, '::placeholder').color, `placeholder:${element.id || element.placeholder.slice(0, 30)}`);
  }
  return issues;
}
function eventColorSnapshot() {
  return [...document.querySelectorAll('.eventTimeline > li')].map((row) => {
    const raw = row.style.getPropertyValue('--event-color');
    const probe = document.createElement('span'); probe.style.color = raw; document.body.append(probe);
    const expected = getComputedStyle(probe).color; probe.remove();
    const label = row.querySelector('.eventTypeLabel'), dot = row.querySelector('.eventIcon span');
    const style = getComputedStyle(label);
    return { type: label.title, expected, dot: getComputedStyle(dot).backgroundColor, text: style.color, background: style.backgroundColor };
  });
}
let control, page;
const failures = [], diagnostics = [], requests = [];
let currentScenario = "startup";
try {
  let logs = "";
  browser.stderr.on("data", (data) => { logs += data.toString(); });
  await until(() => /DevTools listening on (ws:\/\/[^\s]+)/.test(logs), "Browser failed to start");
  const endpoint = logs.match(/DevTools listening on (ws:\/\/[^\s]+)/)[1];
  control = await connect(endpoint);
  const { targetId } = await control.send("Target.createTarget", { url: "about:blank" });
  page = await connect(`ws://${new URL(endpoint).host}/devtools/page/${targetId}`);
  await page.send("Page.enable"); await page.send("Runtime.enable");
  page.on("Runtime.exceptionThrown", (event) => failures.push({ scenario: currentScenario, kind: "exception", error: event.exceptionDetails.exception?.description ?? event.exceptionDetails.text }));
  page.on("Runtime.consoleAPICalled", (event) => { if (event.type === "error") failures.push({ scenario: currentScenario, kind: "console", error: event.args.map((item) => item.value ?? item.description).join(" ") }); });
  page.on("Fetch.requestPaused", (event) => {
    const request = event.request;
    if (!new URL(request.url).pathname.startsWith("/api/v1/")) {
      void page.send(new URL(request.url).origin === new URL(base).origin ? "Fetch.continueRequest" : "Fetch.failRequest", { requestId: event.requestId, ...(new URL(request.url).origin === new URL(base).origin ? {} : { errorReason: "BlockedByClient" }) }).catch(() => {});
      return;
    }
    try {
      requests.push({ scenario: currentScenario, method: request.method, path: new URL(request.url).pathname });
      const response = fixture(request);
      const body = typeof response.body === "string" ? response.body : JSON.stringify(response.body);
      void page.send("Fetch.fulfillRequest", { requestId: event.requestId, responseCode: response.code ?? 200, responseHeaders: [{ name: "Content-Type", value: response.stream ? "text/event-stream" : "application/json" }, { name: "Access-Control-Allow-Origin", value: "*" }, { name: "Access-Control-Allow-Methods", value: "GET, POST, PUT, OPTIONS" }, { name: "Access-Control-Allow-Headers", value: "content-type" }], body: Buffer.from(body).toString("base64") }).catch((error) => failures.push({ scenario: currentScenario, error: error.message }));
    } catch (error) { failures.push({ scenario: currentScenario, error: error.message }); void page.send("Fetch.failRequest", { requestId: event.requestId, errorReason: "BlockedByClient" }); }
  });
  await page.send("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
  await page.send("Page.addScriptToEvaluateOnNewDocument", { source: "window.__qaListeners=[];const original=EventSource.prototype.addEventListener;EventSource.prototype.addEventListener=function(type,...args){window.__qaListeners.push(type);return original.call(this,type,...args)};window.print=()=>{};" });
  const evaluate = async (expression) => {
    const result = await page.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const capture = async (name) => writeFileSync(path.join(output, `${name}.png`), Buffer.from((await page.send("Page.captureScreenshot", { captureBeyondViewport: false })).data, "base64"));
  for (const mode of modes) {
    await page.send("Emulation.setDeviceMetricsOverride", { width: mode.width, height: mode.height, deviceScaleFactor: 1, mobile: mode.mobile });
    await page.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: mode.theme }] });
    const themeScript = await page.send("Page.addScriptToEvaluateOnNewDocument", { source: `try{localStorage.setItem('atlasflow.theme',${JSON.stringify(mode.theme)})}catch{}` });
    for (const scenario of scenarios) {
      currentScenario = `${mode.id}-${scenario.id}`;
      console.log(`Capturing ${currentScenario}`);
      try {
        await page.send("Page.navigate", { url: `${base}${scenario.route}` });
        await until(() => evaluate(`!!document.querySelector(${JSON.stringify(scenario.ready)})`), `${currentScenario}: content did not load`);
        // Wait for React to hydrate the client shell before setting the test theme.
        // Mutating SSR <html> attributes before hydration creates a test-induced warning.
        await until(() => evaluate("Object.keys(document.querySelector('.brand') ?? {}).some(key=>key.startsWith('__reactFiber'))"), "Client shell did not hydrate");
        if (baseline) await evaluate(`document.documentElement.dataset.theme=${JSON.stringify(mode.theme)};`);
        else await until(() => evaluate(`document.documentElement.dataset.theme===${JSON.stringify(mode.theme)}`), "Saved theme was not applied by the shell");
        if (scenario.tab) {
          const oldLabels = { report: "报告", flow: "Agent流程", events: "事件", tasks: "TaskDAG", reviews: "审查决策" };
          const tab = await evaluate(`(()=>{const tab=document.querySelector('#run-tab-${scenario.tab}')??[...document.querySelectorAll('[role=tab]')].find(e=>e.textContent.replace(/\\s/g,'').includes(${JSON.stringify(oldLabels[scenario.tab])}));tab?.click();return Boolean(tab)})()`);
          assert.ok(tab, `${currentScenario}: requested tab is missing`);
        }
        if (scenario.search) {
          await evaluate("(()=>{const input=document.querySelector('#rag-query');const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;setter.call(input,'注意力机制如何计算相关性？');input.dispatchEvent(new Event('input',{bubbles:true}));})()");
          await wait(100);
          await evaluate("document.querySelector('.ragQueryForm').requestSubmit()");
          await until(() => evaluate("!!document.querySelector('.ragHitList')"), "RAG fixture results did not render");
          if (!baseline) {
            assert.equal(await evaluate("document.querySelectorAll('.ragQueryExpansion li').length"), 2, "Bilingual query variants must be directly visible across themes and viewports");
            assert.equal(await evaluate("document.querySelector('.ragQueryExpansion li > span').textContent"), "原始问题", "The original query must lead the actual variants");
          }
        }
        await wait(350);
        if (scenario.id === "failed" && !baseline) {
          const eventColors = await evaluate(`(${eventColorSnapshot.toString()})()`);
          assert.ok(eventColors.length >= 4, "Fixture must exercise several event types");
          for (const item of eventColors) {
            assert.equal(item.dot, item.expected, `${item.type}: timeline dot lost its type color`);
            assert.notEqual(item.background, "rgba(0, 0, 0, 0)", `${item.type}: event label lost its type background`);
          }
          assert.ok(new Set(eventColors.map((item) => item.dot)).size >= 4, "Event dots must not collapse to one neutral color");
          assert.ok(new Set(eventColors.map((item) => item.text)).size >= 4, "Event labels must retain distinct type colors");
          diagnostics.push({ scenario: currentScenario, eventColors });
        }
        if (scenario.id === "tasks") await until(() => evaluate("document.querySelectorAll('.react-flow__node').length === 3"), "Task dependency graph did not render");
        if (scenario.id === "flow") {
          await until(() => evaluate("!!document.querySelector('.flowStage')"), "Agent topology did not finish loading");
          const listeners = await evaluate("window.__qaListeners");
          for (const type of ["tool_requested", "tool_succeeded", "tool_failed", "tool_budget_exhausted"]) assert.ok(listeners.includes(type), `Actual EventSource listener missing: ${type}`);
          await evaluate("(document.querySelector('#run-tab-events')??[...document.querySelectorAll('[role=tab]')].find(e=>e.textContent.includes('事件'))).click()");
          await until(() => evaluate("document.querySelector('.eventTimeline')?.textContent.includes('QA SSE：工具预算已达到上限')"), "Actual named tool SSE events did not render");
          await evaluate("(document.querySelector('#run-tab-flow')??[...document.querySelectorAll('[role=tab]')].find(e=>e.textContent.replace(/\\s/g,'').includes('Agent流程'))).click()");
          await until(() => evaluate("!!document.querySelector('.flowStage')"), "Agent topology did not render after switching tabs");
        }
        const dimensions = await evaluate("({viewport:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth,title:document.querySelector('h1')?.textContent,theme:document.documentElement.dataset.theme})");
        diagnostics.push({ scenario: currentScenario, ...dimensions });
        if (!baseline) {
          const contrastIssues = await evaluate(`(${auditControlContrast.toString()})()`);
          diagnostics.push({ scenario: currentScenario, contrastIssues });
          for (const issue of contrastIssues) failures.push({ scenario: currentScenario, kind: "contrast", ...issue });
        }
        if (dimensions.document > mode.width + 1) failures.push({ scenario: currentScenario, kind: "overflow", error: `${dimensions.document}px document exceeds ${mode.width}px viewport` });
        await evaluate("window.scrollTo(0,0)");
        await capture(currentScenario);
        if (!baseline && mode.id === "desktop" && scenario.id === "knowledge") {
          await evaluate("document.querySelector('.knowledgeVersionHistoryButton').click()");
          await until(() => evaluate("!!document.querySelector('.knowledgeVersionDrawer')"), "Version history did not open");
          assert.equal(await evaluate("document.querySelector('.knowledgeVersionDrawer .knowledgeSnapshotAction button').disabled"), true, "An already-saved current index must not allow duplicate snapshots");
          assert.equal(await evaluate("document.querySelector('.knowledgeVersionCurrent').closest('.spaceVersionRow').querySelector('.dangerText').disabled"), true, "Current version deletion must be disabled");
          await wait(250);
          await capture("desktop-version-history");
          await evaluate("document.querySelector('.knowledgeVersionStatus').closest('.spaceVersionRow').querySelector('.dangerText').click()");
          await until(() => evaluate("!!document.querySelector('.confirmDialog-danger')"), "Historical deletion must require explicit confirmation");
          await wait(250);
          await capture("desktop-version-delete-confirmation");
          assert.equal(requests.filter((item) => item.method === "DELETE").length, 0, "Opening delete confirmation must not delete a version");
          await evaluate("document.querySelector('.confirmDialog .secondaryButton').click()");
          await until(() => evaluate("!document.querySelector('[role=dialog]')"), "Delete cancellation did not close the confirmation");
        }
        if (scenario.id === "report") {
          await until(() => evaluate("!!document.querySelector('.reportWorkspace')"), "Report reader did not render");
          await evaluate("document.querySelector('.reportWorkspace').scrollIntoView({block:'start'})");
          await wait(100);
          await capture(`${currentScenario}-reader`);
          if (mode.id === "desktop" && await evaluate("!!document.querySelector('.runDiagnostics')")) {
            await evaluate("document.querySelector('.runDiagnostics > summary').click();document.querySelector('.runDiagnostics').scrollIntoView({block:'start'})");
            await wait(100);
            assert.equal(await evaluate("document.querySelector('.runDiagnostics').open"), true, "Run diagnostics must expand");
            await capture("desktop-run-diagnostics");
          }
        }
        await evaluate("window.scrollTo(0,0);document.activeElement?.blur()");
        await page.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
        await page.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
        const focus = await evaluate("(()=>{const e=document.activeElement,s=getComputedStyle(e);return {tag:e.tagName,text:e.textContent.slice(0,60),visible:e.matches(':focus-visible'),outline:s.outlineStyle,shadow:s.boxShadow}})()");
        diagnostics.push({ scenario: currentScenario, focus });
        if (!focus.visible || (focus.outline === "none" && focus.shadow === "none")) failures.push({ scenario: currentScenario, kind: "focus", error: "Keyboard focus has no visible indicator", focus });
        if (mode.id === "desktop" && scenario.id === "flow") {
          await evaluate("const button=document.querySelector('.flowStage');button.focus();button.click()");
          await until(() => evaluate("!!document.querySelector('[role=dialog]')"), "Flow node diagnostic did not open");
          await capture("desktop-flow-detail");
          await page.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
          await page.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
          assert.equal(await evaluate("document.activeElement.classList.contains('flowStage')"), true, "Node drawer must restore keyboard focus");
          await evaluate("const edge=document.querySelector('.flowDiagramEdge');edge.focus();edge.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))");
          await until(() => evaluate("!!document.querySelector('[role=dialog]')"), "Keyboard route activation did not open details");
          await page.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
          await page.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
          assert.equal(await evaluate("document.activeElement.classList.contains('flowDiagramEdge')"), true, "Route drawer must restore SVG keyboard focus");
        }
        if (mode.id === "desktop" && scenario.id === "approval") {
          await until(() => evaluate("!!document.querySelector('.approvalCatalog')"), "Capability status did not render");
          await evaluate("document.querySelector('.approvalCatalog button').click()");
          await until(() => evaluate("!!document.querySelector('.capabilityCatalog')"), "Explicit capability catalog button did not open details");
          await capture("desktop-approval-capabilities");
          await page.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
          await page.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
          await evaluate("[...document.querySelectorAll('.approvalActions button')].find(e=>e.textContent.includes('批准')).click()");
          await until(() => evaluate("!!document.querySelector('[role=dialog]')"), "Approval confirmation did not open");
          await capture("desktop-approval-confirmation");
          assert.equal(requests.filter((item) => item.path.endsWith("/approval")).length, 0, "Confirmation must not submit approval prematurely");
        }
      } catch (error) { failures.push({ scenario: currentScenario, kind: "check", error: error.message }); await capture(`${currentScenario}-failure`).catch(() => {}); }
    }
    await page.send("Page.removeScriptToEvaluateOnNewDocument", { identifier: themeScript.identifier });
  }
  writeFileSync(path.join(output, "diagnostics.json"), JSON.stringify({ baseline, diagnostics, failures, requests }, null, 2));
  console.log(`${baseline ? "Baseline" : "Design"} browser artifacts: ${output}`);
  if (!baseline) assert.deepEqual(failures, [], "Browser checks found failures; see diagnostics.json and screenshots");
  else if (failures.length) console.log(`Baseline recorded ${failures.length} existing issue(s).`);
} finally {
  page?.close();
  if (control) { await control.send("Browser.close").catch(() => {}); control.close(); }
  browser.kill();
}
