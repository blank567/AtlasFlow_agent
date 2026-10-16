// Contract-only browser QA. API interception prevents all real model/database calls.
// node scripts/verify-evaluation-browser.mjs http://127.0.0.1:3212
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { WebSocket } = require("next/dist/compiled/ws");
const workspace = path.resolve(fileURLToPath(new URL("../../", import.meta.url)));
assert.match(workspace, /^E:/i);
const base = process.argv[2] ?? "http://127.0.0.1:3212";
const root = path.join(workspace, "build", "evaluation-browser"); mkdirSync(root, { recursive: true });
const output = mkdtempSync(path.join(root, "check-"));
const binary = ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].find(existsSync);
assert.ok(binary);
const browser = spawn(binary, ["--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-breakpad", "--disable-crash-reporter", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${path.join(output, "profile")}`, "about:blank"], { windowsHide: true, env: { ...process.env, TEMP: "E:\\codex\\tmp", TMP: "E:\\codex\\tmp" }, stdio: ["ignore", "ignore", "pipe"] });
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, message) { for (let i = 0; i < 200; i++) { if (await check()) return; await wait(100); } throw new Error(message); }
async function connect(endpoint) {
  const socket = new WebSocket(endpoint); await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
  let sequence = 0; const pending = new Map(), events = new Map();
  socket.on("message", data => { const packet = JSON.parse(data); if (packet.id) { const callback = pending.get(packet.id); pending.delete(packet.id); if (callback) { clearTimeout(callback.timer); packet.error ? callback.reject(new Error(packet.error.message)) : callback.resolve(packet.result); } } else events.get(packet.method)?.(packet.params); });
  return { on(method, callback) { events.set(method, callback); }, send(method, params = {}) { return new Promise((resolve, reject) => { const id = ++sequence, timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 30000); pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params })); }); }, close() { socket.close(); } };
}

const now = "2026-10-14T08:00:00Z", hash = "f".repeat(64);
const space = { id: "qa-space", slug: "qa-contracts", name: "界面契约验证空间", visibility: "private", active_generation_id: "qa-generation", active_version_id: "qa-v1", active_build_id: null, embedding_profile: "fixture", retrieval_profile: "fixture", created_at: now, updated_at: now };
const dataset = { dataset_id: "qa-interface-fixture", version: "1", name: "界面验证夹具：非真实效果基准", space: space.slug, corpus_manifest_hash: hash, source_content_hashes: {}, scoring_version: "source-and-anchor-v1", cases: [1, 2].map(i => ({ case_id: `QA${i}`, kind: "rag", query: `界面契约测试问题${i}：不得视为真实研究结论`, tags: ["fixture"], gold_status: "draft" })) };
const arms = [{ arm_id: "baseline", name: "混合召回", tuning: { query_expansion_enabled: false, rerank_enabled: false }, result_limit: 5 }, { arm_id: "candidate", name: "混合+中英扩展+重排", tuning: { query_expansion_enabled: true, rerank_enabled: true }, result_limit: 5 }];
const budget = { max_http_attempts: 60, max_elapsed_seconds: 1200, case_timeout_seconds: 90, concurrency: 2 };
const plan = { dataset_id: dataset.dataset_id, mode: "replay", case_ids: ["QA1", "QA2"], arms, space: space.slug, space_version_id: "qa-v1", input_refs: {}, confirm_live: false, judge_enabled: false, confirm_judge: false, langsmith_sync: false, budget };
const results = arms.map(arm => ({ case_id: "QA1", arm_id: arm.arm_id, kind: "rag", status: "completed", input_id: `fixture-${arm.arm_id}`, metrics: { source_recall_at_5: null, mrr: null, anchor_hit_rate: null, answer_correctness: null }, availability: { source_recall_at_5: "draft_gold" }, invariant_failures: [], error: null, replayed: true, elapsed_ms: 1, source_duration_ms: null, degraded: [], provider_calls: [] }));
const summary = mean => ({ expected_count: 4, result_count: 2, failed_count: 0, arms: { baseline: { case_count: 1, metrics: { evidence_count: { mean, evaluated_count: mean === null ? 0 : 1, case_count: 1 }, source_recall_at_5: { mean: null, evaluated_count: 0, case_count: 1 } }, provider_calls: 0, known_cost: null, cost_usage_coverage: null } } });
const experiments = [0, 1].map(index => ({ experiment_id: `qa-seed-${index}`, status: "completed", plan, dataset_hash: hash, comparison_key: hash, snapshot: { generation_id: `gen-${index}` }, created_at: now, ended_at: now, results, summary: summary(index === 0 ? 0 : null), budget_ledger: { http_attempts: 0, elapsed_seconds: 1 }, stop_reason: null, sync_status: "not_requested" }));
const frozenInputs = dataset.cases.flatMap(item => arms.map(arm => ({ input_id: `frozen-${item.case_id}-${arm.arm_id}`, case_id: item.case_id, arm_id: arm.arm_id, kind: "rag", provenance: "fixture", created_at: now })));
const run = { id: "qa-recovery", query: "界面契约验证：进程中断后的人工继续", status: "interrupted", auto_approve: true, policy: { knowledge_mode: "off", max_tool_calls_per_turn: 5, max_tool_calls_per_run: 20 }, created_at: now, updated_at: now, started_at: now, plan: null, plans: [], research_results: [], evidence: [], tool_calls: [], critique_history: [], quality_history: [], errors: [], warnings: [], events: [], route_history: [], trace_segments: [], metrics: { total_tasks: 2, successful_tasks: 1, failed_tasks: 0, peak_concurrency: 1, plan_versions: 1, supplement_rounds: 0, replan_count: 0, revision_count: 0, model_calls: 1, tool_calls: 1 }, recovery: { available: true, reason: "界面夹具：已保存进度，等待人工继续", checkpoint_id: "qa-checkpoint", recovery_count: 0, blocked_by: null, unknown_calls: [{ call_id: "qa-unknown", kind: "http", name: "knowledge_search", tool_budget_counted: true, usage: null }] } };
let available = false, preflightDelay = 0, resumeFailures = 1, control, page;
run.metrics.model_calls = 4;
run.recovery.provider_usage = { http_attempts: 8, provider_requests: 6, model_calls: 6, known_tokens: null, known_cost: null, token_usage_coverage: 0, cost_usage_coverage: 0 };
run.trace_segments = [{ id: "qa-unclosed-trace", name: "界面夹具：中断前尚未闭合的追踪", status: "running", provider_calls: [{ call_id: "qa-partial-call", operation: "fixture", status: "success", model: "qa-fixture", usage: { total_tokens: 0, cost: 0 } }] }];
const requests = [], failures = [], diagnostics = [];
function fixture(request) {
  const url = new URL(request.url), route = url.pathname.replace(/^\/api\/v1/, "");
  if (request.method === "OPTIONS") return { body: "", code: 204 };
  const body = request.postData ? JSON.parse(request.postData) : null;
  if (route === "/evaluations/datasets") return { body: [dataset] };
  if (route === "/evaluations/settings") return { body: { budget, judge_available: false } };
  if (route === "/evaluations/inputs") return { body: available ? frozenInputs : [] };
  if (route === "/knowledge/spaces") return { body: [space] };
  if (route.endsWith("/versions")) return { body: [{ id: "qa-v1", space_id: space.id, version_number: 1, generation_id: space.active_generation_id, documents: {}, note: "仅用于界面回归的固定版本", created_at: now }] };
  if (route === "/evaluations/preflight") {
    assert.equal(body.confirm_live, body.mode === "live"); assert.equal(body.judge_enabled, false);
    const delay = preflightDelay; preflightDelay = 0;
    const errors = body.mode === "replay" && !available ? ["frozen input missing: QA1:baseline"] : [];
    return { delay, body: { ready: !errors.length, errors, warnings: ["夹具只验证接口与交互，不构成效果评测"], selected_cases: body.case_ids.length, case_ids: body.case_ids, estimated_provider_requests: body.mode === "live" ? 6 : 0, snapshot: {}, dataset_hash: hash, comparison_key: hash, available_inputs: available ? frozenInputs : [], resolved_input_refs: available ? Object.fromEntries(frozenInputs.map(item => [`${item.case_id}:${item.arm_id}`, item.input_id])) : {} } };
  }
  if (route === "/evaluations" && request.method === "GET") return { body: experiments };
  if (route === "/evaluations" && request.method === "POST") {
    assert.ok(body.arms.length && body.case_ids.length);
    assert.ok(body.budget.max_http_attempts >= 1 && body.budget.max_http_attempts <= 5000);
    assert.equal(body.confirm_live, body.mode === "live");
    if (body.mode === "replay") assert.equal(body.input_refs["QA1:baseline"], "frozen-QA1-baseline", "Run must use the preflight's explicit resolved frozen inputs");
    const value = { ...experiments[0], experiment_id: `qa-created-${experiments.length}`, plan: body, status: body.mode === "live" ? "running" : "completed", results: body.mode === "live" ? [] : results, summary: summary(null) };
    experiments.unshift(value); return { body: value };
  }
  if (route === "/evaluations/compare") {
    assert.ok(["retrieval", "index"].includes(body.scope));
    if (body.scope === "retrieval") return { code: 409, body: { detail: "retrieval comparison requires the same index snapshot; select index scope explicitly" } };
    return { body: { baseline_id: body.baseline_id, candidate_id: body.candidate_id, comparison_key: hash, scope: body.scope, snapshots_differ: true, baseline: summary(0), candidate: summary(null), limitations: ["fixture; not a quality claim"] } };
  }
  if (/^\/evaluations\/[^/]+\/cancel$/.test(route)) { const item = experiments.find(item => route.includes(item.experiment_id)); assert.equal(item.status, "running"); item.status = "cancelled"; item.stop_reason = "cancelled"; return { body: item }; }
  if (/^\/evaluations\/[^/]+$/.test(route)) return { body: experiments.find(item => route.endsWith(item.experiment_id)) };
  if (route === "/runs/qa-recovery/events") return { stream: true, body: "event: done\ndata: {}\n\n" };
  if (route === "/runs/qa-recovery/resume") {
    assert.ok(body.request_id); assert.equal(body.expected_checkpoint_id, "qa-checkpoint"); assert.deepEqual(body.acknowledged_unknown_call_ids, ["qa-unknown"]);
    if (resumeFailures-- > 0) return { code: 503, body: { detail: "界面夹具：暂时不可用，请重试相同命令" } };
    run.status = "running"; run.recovery = { ...run.recovery, available: false, recovery_count: 1, unknown_calls: [] }; return { body: run };
  }
  if (route === "/runs/qa-recovery") return { body: run };
  if (route === "/runs/qa-blocked") return { body: { ...run, id: "qa-blocked", status: "interrupted", recovery: { ...run.recovery, available: false, blocked_by: "图版本不兼容，请新建运行", reason: "界面夹具：不兼容恢复应明确阻断" } } };
  if (route === "/runs/qa-blocked/events") return { stream: true, body: "event: done\ndata: {}\n\n" };
  if (route === "/capabilities") return { body: [] };
  throw new Error(`Unexpected QA API: ${request.method} ${route}`);
}

try {
  let logs = ""; browser.stderr.on("data", data => { logs += data.toString(); });
  await until(() => /DevTools listening on (ws:\/\/[^\s]+)/.test(logs), "Browser failed to start");
  const endpoint = logs.match(/DevTools listening on (ws:\/\/[^\s]+)/)[1]; control = await connect(endpoint);
  const { targetId } = await control.send("Target.createTarget", { url: "about:blank" });
  page = await connect(`ws://${new URL(endpoint).host}/devtools/page/${targetId}`); await page.send("Page.enable"); await page.send("Runtime.enable");
  page.on("Runtime.exceptionThrown", event => failures.push(event.exceptionDetails.exception?.description ?? event.exceptionDetails.text));
  page.on("Fetch.requestPaused", async event => {
    const url = new URL(event.request.url);
    if (!url.pathname.startsWith("/api/v1/")) { await page.send(url.origin === new URL(base).origin ? "Fetch.continueRequest" : "Fetch.failRequest", { requestId: event.requestId, ...(url.origin === new URL(base).origin ? {} : { errorReason: "BlockedByClient" }) }).catch(() => {}); return; }
    try { requests.push({ path: url.pathname, method: event.request.method, body: event.request.postData ? JSON.parse(event.request.postData) : null }); const value = fixture(event.request); if (value.delay) await wait(value.delay); const body = typeof value.body === "string" ? value.body : JSON.stringify(value.body); await page.send("Fetch.fulfillRequest", { requestId: event.requestId, responseCode: value.code ?? 200, responseHeaders: [{ name: "Content-Type", value: value.stream ? "text/event-stream" : "application/json" }, { name: "Access-Control-Allow-Origin", value: "*" }, { name: "Access-Control-Allow-Methods", value: "GET,POST,OPTIONS" }, { name: "Access-Control-Allow-Headers", value: "content-type" }], body: Buffer.from(body).toString("base64") }); } catch (error) { failures.push(error.message); await page.send("Fetch.failRequest", { requestId: event.requestId, errorReason: "BlockedByClient" }).catch(() => {}); }
  });
  await page.send("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
  const evaluate = async expression => { const result = await page.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }); if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text); return result.result.value; };
  const screenshot = async name => { await wait(250); writeFileSync(path.join(output, `${name}.png`), Buffer.from((await page.send("Page.captureScreenshot", { captureBeyondViewport: false })).data, "base64")); };
  const click = async text => { assert.equal(await evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()===${JSON.stringify(text)});b?.click();return !!b})()`), true, `Missing button: ${text}`); await wait(60); };
  const fill = async (selector, value) => { await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});const p=e instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event(e instanceof HTMLSelectElement?'change':'input',{bubbles:true}));})()`); await wait(100); };
  const creations = () => requests.filter(item => item.method === "POST" && item.path === "/api/v1/evaluations");
  const cancellations = () => requests.filter(item => item.method === "POST" && /\/evaluations\/[^/]+\/cancel$/.test(item.path));
  const check = async ready => { await click("检查实验条件"); await until(() => evaluate(`!!document.querySelector('.evalPreflight.${ready ? "ready" : "blocked"}')`), "Preflight did not render"); };
  const open = async route => { await page.send("Page.navigate", { url: base + route }); await until(() => evaluate("!!document.querySelector('.evaluationWorkspace,.recoveryPanel')"), "UI did not load"); await until(() => evaluate("Object.keys(document.querySelector('.brand')??{}).some(k=>k.startsWith('__reactFiber'))"), "UI not hydrated"); };

  await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await open("/knowledge/evaluations"); await until(() => evaluate("document.querySelectorAll('.evalCases tbody tr').length===2"), "Dataset did not load");
  assert.equal(await evaluate("document.querySelector('input[name=eval-mode]').checked"), true);
  await check(false); assert.equal(creations().length, 0); await screenshot("desktop-missing-frozen-inputs");
  available = true; await check(true);
  assert.equal(await evaluate("document.querySelectorAll('.evalCases select option').length>4"), true, "Preflight must refresh frozen input selectors");
  await fill("#eval-rrf-k", "80"); assert.equal(await evaluate("!!document.querySelector('.evalPreflight')"), false, "Changing parameters must invalidate preflight");
  preflightDelay = 300; await click("检查实验条件"); await fill("#eval-rrf-k", "90"); await wait(400);
  assert.equal(await evaluate("!!document.querySelector('.evalPreflight')"), false, "Stale preflight response must not enable launch");
  await fill("#eval-rrf-k", "60"); await check(true); await click("开始回放");
  await until(() => evaluate("!!document.querySelector('[role=dialog]')"), "Replay confirmation missing");
  assert.equal(creations().length, 0); assert.equal(await evaluate("document.activeElement.textContent"), "返回");
  await click("返回"); assert.equal(creations().length, 0); await click("开始回放");
  await evaluate("(()=>{const b=document.querySelector('.confirmDialog .primaryButton');b.click();b.click()})()");
  await until(() => creations().length === 1, "Replay not created"); await until(() => evaluate("!!document.querySelector('.evalResults')"), "Replay results missing");
  assert.equal(creations().length, 1); assert.equal(creations()[0].body.mode, "replay"); assert.equal(creations()[0].body.input_refs["QA1:baseline"], "frozen-QA1-baseline");
  assert.equal(await evaluate("document.querySelector('.evalResults tbody').textContent.includes('未评测')"), true, "Missing effect scores cannot become fake quality claims");
  await evaluate("document.querySelectorAll('input[name=eval-mode]')[1].click()"); await wait(100); await check(true);
  await fill("#eval-candidate-limit", "1"); assert.equal(await evaluate("document.querySelector('.evalLaunch .secondaryButton').disabled"), true, "Invalid rerank/candidate settings must block preflight");
  await click("恢复默认"); await check(true); await click("开始真实评测");
  assert.equal(creations().length, 1); await screenshot("desktop-live-confirmation");
  await evaluate("(()=>{const b=document.querySelector('.confirmDialog .primaryButton');b.click();b.click()})()");
  await until(() => creations().length === 2, "Live not created"); await until(() => evaluate("[...document.querySelectorAll('button')].some(b=>b.textContent==='停止评测')"), "Live cancellation missing");
  await click("停止评测"); assert.equal(cancellations().length, 0); await click("返回"); assert.equal(cancellations().length, 0);
  await click("停止评测"); await evaluate("(()=>{const b=document.querySelector('.confirmDialog .primaryButton');b.click();b.click()})()");
  await until(() => cancellations().length === 1, "Cancellation not sent"); await until(() => evaluate("document.querySelector('.evalResults')?.textContent.includes('已取消')"), "Cancellation not reflected");
  await fill(".evalCompare .evalFields select", "qa-seed-0"); await fill(".evalCompare .evalFields label:nth-child(2) select", "qa-seed-1");
  await click("比较结果"); await until(() => evaluate("document.querySelector('[role=alert]')?.textContent.includes('same index snapshot')"), "Cross-generation retrieval compare must be blocked");
  await fill(".evalCompareScope select", "index"); await click("比较结果");
  await until(() => evaluate("!!document.querySelector('.evalCompare table')"), "Index comparison missing");
  assert.equal(await evaluate("document.querySelector('.evalCompare tbody').textContent.includes('0.000')"), true, "A measured zero must not become missing data");
  assert.equal(await evaluate("document.querySelector('.evalCompare tbody').textContent.includes('未评测')"), true);
  await evaluate("document.querySelector('.evalCompare').scrollIntoView({block:'start'})"); await screenshot("desktop-comparison");

  for (const mode of [{ id: "desktop", width: 1440, height: 1000, theme: "light" }, { id: "mobile", width: 390, height: 844, theme: "light" }, { id: "dark", width: 1440, height: 1000, theme: "dark" }]) {
    const theme = await page.send("Page.addScriptToEvaluateOnNewDocument", { source: `localStorage.setItem('atlasflow.theme',${JSON.stringify(mode.theme)})` });
    await page.send("Emulation.setDeviceMetricsOverride", { width: mode.width, height: mode.height, deviceScaleFactor: 1, mobile: mode.id === "mobile" });
    await open("/knowledge/evaluations"); await until(() => evaluate("document.querySelectorAll('.evalCases tbody tr').length===2"), "Dataset missing"); await wait(200);
    const size = await evaluate("({viewport:innerWidth,width:document.documentElement.scrollWidth,theme:document.documentElement.dataset.theme})"); assert.ok(size.width <= mode.width + 1, `${mode.id} overflows`); diagnostics.push({ mode: mode.id, ...size });
    await screenshot(`${mode.id}-evaluation`); await page.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 }); await page.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
    assert.equal(await evaluate("document.activeElement.matches(':focus-visible')"), true, "Keyboard focus must be visible");
    await page.send("Page.removeScriptToEvaluateOnNewDocument", { identifier: theme.identifier });
  }
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  const light = await page.send("Page.addScriptToEvaluateOnNewDocument", { source: "localStorage.setItem('atlasflow.theme','light')" });
  await open("/runs/qa-recovery"); await screenshot("desktop-recovery-unknown");
  const counters = await evaluate("Object.fromEntries([...document.querySelectorAll('.counterList > div')].map(e=>[e.querySelector('dt').textContent,e.querySelector('dd').textContent]))");
  assert.equal(counters["模型调用"], "6"); assert.equal(counters["已知Token"], "未记录"); assert.equal(counters["Provider成本"], "未记录", "Unclosed partial Trace zeros must not replace ledger unknown usage");
  await page.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true }); await screenshot("mobile-recovery-unknown");
  assert.equal(await evaluate("document.documentElement.scrollWidth<=innerWidth"), true, "Recovery UI overflows on mobile");
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await evaluate("document.documentElement.dataset.theme='dark'"); await screenshot("dark-recovery-unknown"); await evaluate("document.documentElement.dataset.theme='light'");
  await click("继续执行");
  assert.equal(requests.filter(item => item.method === "POST" && item.path.endsWith("/resume")).length, 0);
  await evaluate("(()=>{const b=document.querySelector('.confirmDialog .primaryButton');b.click();b.click()})()");
  await until(() => evaluate("document.querySelector('.recoveryPanel [role=alert]')?.textContent.includes('相同命令')"), "Recovery error should remain actionable");
  await click("继续执行"); await click("确认继续"); await until(() => evaluate("!document.querySelector('.recoveryPanel')"), "Recovery panel should close when execution resumes");
  const resumed = requests.filter(item => item.method === "POST" && item.path.endsWith("/resume")); assert.equal(resumed.length, 2); assert.equal(resumed[0].body.request_id, resumed[1].body.request_id, "Retrying identical recovery command must retain its idempotency ID");
  await open("/runs/qa-blocked");
  assert.equal(await evaluate("document.querySelector('.recoveryHeading button').disabled"), true);
  assert.equal(await evaluate("document.querySelector('.recoveryPanel').textContent.includes('不兼容')"), true);
  await page.send("Page.removeScriptToEvaluateOnNewDocument", { identifier: light.identifier });
  assert.deepEqual(failures, []);
  writeFileSync(path.join(output, "diagnostics.json"), JSON.stringify({ fixture_only: true, no_quality_claim: true, requests, diagnostics, failures }, null, 2));
  console.log(`Evaluation/Recovery browser QA passed. Artifacts: ${output}`);
} catch (error) {
  if (page) await page.send("Page.captureScreenshot").then(value => writeFileSync(path.join(output, "failure.png"), Buffer.from(value.data, "base64"))).catch(() => {});
  writeFileSync(path.join(output, "diagnostics.json"), JSON.stringify({ fixture_only: true, requests, diagnostics, failures, error: error.message }, null, 2)); throw error;
} finally { page?.close(); if (control) { await control.send("Browser.close").catch(() => {}); control.close(); } browser.kill(); }
