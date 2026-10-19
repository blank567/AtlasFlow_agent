// Disposable browser/API fixtures only. No live backend, LLM, or corpus requests.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { WebSocket } = require("next/dist/compiled/ws");
const workspace = path.resolve(fileURLToPath(new URL("../../", import.meta.url)));
assert.match(workspace, /^E:/i);
const base = process.argv[2] || "http://localhost:3217";
const root = path.join(workspace, "build", "conversation-browser"); mkdirSync(root, { recursive: true });
const output = mkdtempSync(path.join(root, "check-"));
const binary = ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].find(existsSync);
assert.ok(binary, "Chrome or Edge is required");
const browser = spawn(binary, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-breakpad", "--disable-crash-reporter", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${path.join(output, "profile")}`, "about:blank"], { windowsHide: true, env: { ...process.env, TEMP: "E:\\codex\\tmp", TMP: "E:\\codex\\tmp" }, stdio: ["ignore", "ignore", "pipe"] });
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(check, message) { for (let n = 0; n < 200; n++) { if (await check()) return; await wait(100); } throw Error(message); }
async function connect(endpoint) {
  const socket = new WebSocket(endpoint);
  await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
  let sequence = 0; const pending = new Map(), events = new Map();
  socket.on("message", (data) => { const packet = JSON.parse(data); if (packet.id) { const item = pending.get(packet.id); pending.delete(packet.id); if (item) { clearTimeout(item.timer); packet.error ? item.reject(Error(packet.error.message)) : item.resolve(packet.result); } } else events.get(packet.method)?.(packet.params); });
  return { on(method, callback) { events.set(method, callback); }, send(method, params = {}) { return new Promise((resolve, reject) => { const id = ++sequence; const timer = setTimeout(() => { pending.delete(id); reject(Error(`CDP timeout: ${method}`)); }, 30000); pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params })); }); }, close() { socket.close(); } };
}

const now = "2026-10-17T06:00:00Z";
const policy = { knowledge_mode: "off", knowledge_space: null, max_tool_calls_per_turn: 5, max_tool_calls_per_run: 20 };
let snapshot = { conversation: { id: "conversation-browser", title: "新会话", version: 0, policy, auto_approve: false, requirements: {}, current_report_run_id: null, active_turn_id: null, created_at: now, updated_at: now }, messages: [], turns: [], reports: [], active_run: null };
const runs = new Map(), accepted = new Map();
const requests = [], failures = [], diagnostics = [];
let nextAction = "clarify", forceConflict = false, loseResponse = false, routeExecutions = 0;
let conversationExists = true, forceDeleteConflict = false, forceDeleteFailure = false, loseDeleteResponse = false;
let page, control;
let navigationEpoch = 0;
const cancelledNetworkRequests = new Set();
const failedNetworkRequests = new Map();
const pendingInterceptions = new Set();
const networkRequestOrigins = new Map();
let activeLoaderId = null;
const copy = (value) => JSON.parse(JSON.stringify(value));
function message(role, kind, content, turnId, runId = null) {
  const item = { id: `message-${snapshot.messages.length + 1}`, conversation_id: snapshot.conversation.id, turn_id: turnId, sequence: snapshot.messages.length + 1, role, kind, content, run_id: runId, created_at: now };
  snapshot.messages.push(item); return item;
}
function receiveMessage(payload) {
  if (accepted.has(payload.request_id)) return;
  accepted.set(payload.request_id, copy(payload)); routeExecutions++;
  const id = `turn-${snapshot.turns.length + 1}`;
  const user = message("user", "text", payload.content, id);
  if (snapshot.conversation.active_turn_id) snapshot.turns.find((t) => t.id === snapshot.conversation.active_turn_id).status = "cancelled";
  const baseId = payload.base_run_id || snapshot.conversation.current_report_run_id;
  const turn = { id, conversation_id: snapshot.conversation.id, user_message_id: user.id, request_id: payload.request_id, status: nextAction === "routing" ? "routing" : nextAction === "clarify" ? "awaiting_clarification" : nextAction === "answer" ? "completed" : "awaiting_confirmation", action: nextAction, decision: { action: nextAction, resolved_request: payload.content, rationale: "浏览器夹具：依据本轮需求选择执行方式。", base_run_id: baseId }, requirements_snapshot: copy(snapshot.conversation.requirements), run_id: null, error: null, trace_segments: [] };
  snapshot.turns.push(turn);
  snapshot.conversation.active_turn_id = ["routing", "awaiting_confirmation"].includes(turn.status) ? id : null;
  snapshot.conversation.version++;
  if (nextAction === "clarify") message("assistant", "clarification", "为明确研究范围，请补充目的地、天数和预算。", id);
  else if (nextAction === "answer") message("assistant", "text", "仅根据已有报告解释，不进行新的工具查询。\n\n<script>window.__unsafe=true</script>\n\n[不安全链接](javascript:alert%281%29)", id, baseId);
  else if (nextAction !== "routing") {
    snapshot.conversation.requirements = { "目的地": { value: "北京", source_message_ids: [user.id], confirmed: false }, "天数": { value: "三天", source_message_ids: [user.id], confirmed: false } };
    turn.requirements_snapshot = copy(snapshot.conversation.requirements);
    message("assistant", "proposal", `${payload.content}\n\n${nextAction === "revise" ? "仅调整报告结构和表达，不调用研究工具。" : "核验当前需求后再启动研究，保留独立执行记录。"}`, id);
  }
}
function startRun(turn) {
  if (turn.run_id) return;
  const id = `conversation-run-${runs.size + 1}`;
  turn.run_id = id; turn.status = "waiting_approval";
  const tasks = [1, 2].map((i) => ({ task_id: `T${i}`, title: i === 1 ? "核验行程安排" : "分析交通与预算", objective: "此为界面测试任务，不是实际旅行建议。", dependencies: [], success_criteria: ["保留依据"], priority: 1, plan_version: 1 }));
  const run = { id, query: turn.decision.resolved_request, conversation_id: snapshot.conversation.id, conversation_turn_id: turn.id, execution_mode: turn.action === "revise" ? "report_revision" : "research", source_run_id: turn.decision.base_run_id, status: "waiting_approval", plan: { plan_version: 1, rationale: "本地浏览器夹具", tasks }, plans: [], policy, auto_approve: false, warnings: [], metrics: { total_tasks: 2, successful_tasks: 0, failed_tasks: 0, model_calls: 1, tool_calls: 0 }, events: [], tool_calls: [], evidence: [], quality_history: [], critique_history: [], errors: [], trace_segments: [], report: null, created_at: now, updated_at: now };
  runs.set(id, run); snapshot.active_run = run; snapshot.conversation.version++;
  for (const value of Object.values(snapshot.conversation.requirements)) value.confirmed = true;
}
function finishRun() {
  const run = snapshot.active_run; assert.ok(run);
  run.status = "completed";
  if (run.execution_mode === "report_revision") run.metrics.total_tasks = 0;
  run.metrics.successful_tasks = run.metrics.total_tasks;
  run.report = `## 摘要\n\n${runs.size === 1 ? "第一版" : "修订版"}报告。以下内容仅用于浏览器交互验收，不构成出行建议。\n\n## 分析\n\n行程安排与研究来源应一一对应，实际执行前核验信息时效。\n\n| 维度 | 本次说明 |\n| --- | --- |\n| 行程 | 以已确认需求为准 |\n| 交通 | 按正式地点核验 |\n\n## 结论\n\n对话与报告分别保存。\n\n## 局限\n\n这是离线界面夹具。\n\n## 参考文献\n\n未调用外部来源。`;
  const turn = snapshot.turns.find((t) => t.run_id === run.id); turn.status = "completed";
  snapshot.reports.push({ run_id: run.id, turn_id: turn.id, title: run.query, status: run.status, source_run_id: run.source_run_id, created_at: now });
  message("assistant", "report", "报告已生成，可阅读、下载或继续追问。", turn.id, run.id);
  snapshot.conversation.current_report_run_id = run.id; snapshot.conversation.active_turn_id = null; snapshot.active_run = null; snapshot.conversation.version++;
}

try {
  let logs = ""; browser.stderr.on("data", (chunk) => { logs += chunk.toString(); });
  await until(() => /DevTools listening on (ws:\/\/\S+)/.test(logs), "Browser did not start");
  const endpoint = new URL(logs.match(/DevTools listening on (ws:\/\/\S+)/)[1]); control = await connect(endpoint.href);
  mkdirSync(path.join(output, "downloads"), { recursive: true });
  await control.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: path.join(output, "downloads") });
  const { targetId } = await control.send("Target.createTarget", { url: "about:blank" }); page = await connect(`ws://${endpoint.host}/devtools/page/${targetId}`);
  await page.send("Page.enable"); await page.send("Runtime.enable"); await page.send("Network.enable");
  page.on("Runtime.exceptionThrown", (event) => failures.push(event.exceptionDetails.text));
  page.on("Page.frameNavigated", (event) => { if (!event.frame.parentId) activeLoaderId = event.frame.loaderId; });
  page.on("Network.requestWillBeSent", (event) => networkRequestOrigins.set(event.requestId, { epoch: navigationEpoch, loader: event.loaderId }));
  page.on("Network.loadingFailed", (event) => { failedNetworkRequests.set(event.requestId, { canceled: event.canceled, error: event.errorText }); if (event.canceled) cancelledNetworkRequests.add(event.requestId); });
  page.on("Fetch.requestPaused", (event) => { const requestEpoch = navigationEpoch; const pending = (async () => {
    const url = new URL(event.request.url), method = event.request.method;
    const payload = event.request.postData ? JSON.parse(event.request.postData) : null;
    requests.push({ method, path: url.pathname, payload });
    let value = {}, status = 200, stream = false, text = "";
    const prefix = "/api/v1/conversations";
    if (method === "OPTIONS") status = 204;
    else if (url.pathname === "/api/v1/knowledge/spaces") value = [];
    else if (url.pathname === prefix && method === "GET") value = { items: conversationExists && snapshot.messages.length ? [snapshot.conversation] : [], total: conversationExists && snapshot.messages.length ? 1 : 0, page: 1, page_size: 30 };
    else if (url.pathname === prefix && method === "POST") { snapshot.conversation.title = payload.title; snapshot.conversation.policy = payload.policy; value = snapshot.conversation; status = 201; }
    else if (url.pathname === `${prefix}/${snapshot.conversation.id}` && method === "DELETE") {
      assert.equal(payload.confirmation_conversation_id, snapshot.conversation.id);
      if (!conversationExists) { status = 404; value = { detail: "会话不存在" }; }
      else if (forceDeleteConflict) { forceDeleteConflict = false; snapshot.conversation.version++; status = 409; value = { detail: "会话已更新，请重新核对删除范围后确认" }; }
      else if (forceDeleteFailure) { forceDeleteFailure = false; snapshot.conversation.deleting = true; snapshot.conversation.version++; status = 503; value = { detail: "清理暂不可用" }; }
      else {
        assert.equal(payload.expected_version, snapshot.conversation.version);
        value = { conversation_id: snapshot.conversation.id, deleted_run_count: runs.size };
        conversationExists = false; runs.clear();
        if (loseDeleteResponse) { loseDeleteResponse = false; await page.send("Fetch.failRequest", { requestId: event.requestId, errorReason: "ConnectionReset" }); return; }
      }
    }
    else if (url.pathname === `${prefix}/${snapshot.conversation.id}`) { if (conversationExists) value = snapshot; else { status = 404; value = { detail: "会话不存在" }; } }
    else if (url.pathname.endsWith("/messages")) {
      if (forceConflict) { forceConflict = false; snapshot.conversation.version++; status = 409; value = { detail: "会话已更新，请刷新后重试；输入已保留" }; }
      else { receiveMessage(payload); value = snapshot; status = 202; if (loseResponse) { loseResponse = false; await page.send("Fetch.failRequest", { requestId: event.requestId, errorReason: "ConnectionReset" }); return; } }
    } else if (url.pathname.startsWith(prefix) && url.pathname.endsWith("/confirm")) { const turn = snapshot.turns.find((t) => t.id === payload.confirmation_turn_id); assert.ok(turn); startRun(turn); value = snapshot; status = 202; }
    else if (url.pathname.startsWith(prefix) && url.pathname.endsWith("/cancel")) { const turn = snapshot.turns.find((t) => t.id === payload.confirmation_turn_id); turn.status = "cancelled"; snapshot.conversation.active_turn_id = null; snapshot.active_run = null; snapshot.conversation.version++; message("assistant", "notice", "本轮已停止，输入与报告保留。", turn.id); value = snapshot; }
    else if (url.pathname.startsWith(prefix) && url.pathname.endsWith("/events")) { stream = true; text = conversationExists ? `retry: 200\nevent: conversation_snapshot\nid: ${snapshot.conversation.version}\ndata: ${JSON.stringify(snapshot)}\n\n` : `event: conversation_deleted\ndata: ${JSON.stringify({ conversation_id: snapshot.conversation.id })}\n\n`; }
    else if (/\/runs\/[^/]+\/approval$/.test(url.pathname)) { const id = url.pathname.split("/").at(-2); const run = runs.get(id); run.status = "running"; snapshot.turns.find((t) => t.run_id === id).status = "running"; snapshot.conversation.version++; value = run; status = 202; }
    else if (/\/runs\/[^/]+\/events$/.test(url.pathname)) { stream = true; text = "retry: 1000\nevent: heartbeat\ndata: {}\n\n"; }
    else if (/\/runs\/[^/]+$/.test(url.pathname)) value = runs.get(url.pathname.split("/").at(-1));
    else { failures.push(`Unexpected API: ${method} ${url.pathname}`); status = 404; value = { detail: "Unexpected fixture endpoint" }; }
    await page.send("Fetch.fulfillRequest", { requestId: event.requestId, responseCode: status, responseHeaders: [{ name: "Content-Type", value: stream ? "text/event-stream" : "application/json" }, { name: "Access-Control-Allow-Origin", value: "*" }, { name: "Access-Control-Allow-Headers", value: "*" }, { name: "Access-Control-Allow-Methods", value: "*" }], body: Buffer.from(stream ? text : JSON.stringify(value ?? {})).toString("base64") });
  })().catch(async (error) => {
    // Navigating away/closing an EventSource invalidates its paused Fetch ID.
    // Ignore only proven obsolete requests, never a live fixture/API failure.
    if (error.message.includes("Invalid InterceptionId")) {
      await wait(40); // Let the matching Network.loadingFailed event arrive.
      const origin = networkRequestOrigins.get(event.networkId);
      const staleLoader = origin?.loader && activeLoaderId && origin.loader !== activeLoaderId;
      if (requestEpoch !== navigationEpoch || origin?.epoch < navigationEpoch || staleLoader || cancelledNetworkRequests.has(event.networkId)) {
        diagnostics.push({ name: "obsolete-interception", path: new URL(event.request.url).pathname, stale_loader: !!staleLoader, cancelled: cancelledNetworkRequests.has(event.networkId) });
        return;
      }
    }
    diagnostics.push({ name: "interception-error", message: error.message, path: new URL(event.request.url).pathname, method: event.request.method, request_id: event.requestId, network_id: event.networkId, request_epoch: requestEpoch, current_epoch: navigationEpoch, origin: networkRequestOrigins.get(event.networkId), current_loader: activeLoaderId, network_failure: failedNetworkRequests.get(event.networkId) });
    failures.push(`${error.message} (${event.request.method} ${new URL(event.request.url).pathname})`);
  }); pendingInterceptions.add(pending); void pending.finally(() => pendingInterceptions.delete(pending)); });
  await page.send("Fetch.enable", { patterns: [{ urlPattern: "*/api/v1/*" }] });
  const evaluate = async (expression) => { const result = await page.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }); if (result.exceptionDetails) throw Error(result.exceptionDetails.text); return result.result.value; };
  const open = async (pathname) => { navigationEpoch++; await page.send("Page.navigate", { url: `${base}${pathname}` }); await until(() => evaluate("document.readyState==='complete'"), "Page failed to load"); };
  const click = async (label) => { await until(() => evaluate(`(()=>{const root=document.querySelector('[role=dialog]')||document;const el=[...root.querySelectorAll('button')].find(e=>e.textContent.trim()===${JSON.stringify(label)});if(!el||el.disabled)return false;el.click();return true})()`), `Missing enabled button: ${label}`); };
  const input = async (selector, value) => evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});const prototype=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(prototype,'value').set.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
  const postCount = (suffix) => requests.filter((r) => r.method === "POST" && r.path.endsWith(suffix)).length;
  const shot = async (name) => { const result = await page.send("Page.captureScreenshot", { captureBeyondViewport: true }); writeFileSync(path.join(output, `${name}.png`), Buffer.from(result.data, "base64")); };
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1500, height: 1050, deviceScaleFactor: 1, mobile: false });
  await open("/conversations"); await until(() => evaluate("!!document.querySelector('.conversationCreate')"), "Conversation list missing");
  await shot("conversation-list");
  await input(".conversationCreate input[maxlength='100']", "北京旅行研究与报告迭代"); await click("创建会话");
  await until(() => evaluate("!!document.querySelector('.conversationWelcome')"), "Empty conversation missing");
  const created = requests.find((r) => r.method === "POST" && r.path === "/api/v1/conversations");
  assert.equal(created.payload.auto_approve, false); assert.equal(created.payload.policy.knowledge_mode, "off");
  assert.equal(created.payload.policy.max_tool_calls_per_turn, 10); assert.equal(created.payload.policy.max_tool_calls_per_run, 40);
  await input("#conversation-message", "帮我做一份旅游报告"); await click("发送");
  await until(() => evaluate("document.querySelector('.conversationTranscript').textContent.includes('请补充目的地')"), "Clarification missing"); assert.equal(runs.size, 0);
  nextAction = "research"; await input("#conversation-message", "北京，三天，两个人，预算3000元"); await click("发送");
  await until(() => evaluate("!!document.querySelector('.conversationProposalActions')"), "Research proposal missing"); await click("确认执行"); assert.equal(postCount("/confirm"), 0); await click("返回"); assert.equal(postCount("/confirm"), 0);
  await click("确认执行"); await click("确认并执行");
  await until(() => evaluate("!!document.querySelector('.conversationPlan')"), "Approval plan missing");
  assert.equal(runs.size, 1); assert.equal(await evaluate("document.querySelector('#conversation-message').disabled"), true);
  await click("批准计划"); await click("返回"); assert.equal(postCount("/approval"), 0);
  await click("批准计划"); await click("批准并执行"); await until(() => runs.values().next().value.status === "running", "Approval did not execute");
  finishRun(); await until(() => evaluate("!!document.querySelector('.conversationReport .reportPaper')"), "Completed report missing");
  await shot("desktop-report");
  nextAction = "answer"; await input("#conversation-message", "为什么第二天这样安排"); await click("发送");
  await until(() => evaluate("document.querySelector('.conversationTranscript').textContent.includes('仅根据已有报告解释')"), "Report explanation missing");
  assert.equal(runs.size, 1); assert.equal(await evaluate("!!window.__unsafe"), false); assert.equal(await evaluate("!!document.querySelector('.conversationTranscript a[href^=\"javascript:\"]')"), false);
  nextAction = "revise"; await input("#conversation-message", "内容不变，改成正式报告"); await click("发送");
  await until(() => evaluate("document.querySelector('.conversationTranscript').textContent.includes('不调用研究工具')"), "Rewrite proposal missing"); await click("确认执行"); await click("确认并执行");
  await until(() => runs.size === 2, "Revision Run missing"); finishRun();
  await until(() => evaluate("document.querySelectorAll('select[aria-label=\"阅读报告版本\"] option').length===3"), "Report versions missing");
  await evaluate("(()=>{const e=document.querySelector('select[aria-label=\"阅读报告版本\"]');e.value='conversation-run-1';e.dispatchEvent(new Event('change',{bubbles:true}))})()");
  await until(() => evaluate("document.querySelector('.conversationReport .reportProse')?.textContent.includes('第一版')"), "Older report cannot be selected");
  await click("下载Markdown"); assert.equal(await evaluate("document.querySelector('.conversationReport').textContent.includes('E盘')"), true);
  await until(() => readdirSync(path.join(output, "downloads")).some((name) => name.endsWith(".md")), "Markdown download must complete on E:");
  // A known version conflict retains text. Retry uses a fresh request/version.
  forceConflict = true; nextAction = "clarify"; await input("#conversation-message", "保留这条尚未发送成功的消息"); await click("发送");
  await until(() => evaluate("document.querySelector('[role=alert]')?.textContent.includes('会话已更新')"), "Conflict explanation missing"); assert.equal(await evaluate("document.querySelector('#conversation-message').value"), "保留这条尚未发送成功的消息");
  await click("发送"); await until(() => evaluate("document.querySelector('#conversation-message').value===''"), "Retry did not succeed");
  // Unknown network outcome retries the exact request ID, not a second model turn.
  loseResponse = true; const beforeRoute = routeExecutions; await input("#conversation-message", "网络错误后保持幂等"); await click("发送");
  await until(() => evaluate("!!document.querySelector('[role=alert]')"), "Unknown network error missing"); await click("发送");
  await until(() => evaluate("document.querySelector('#conversation-message').value===''"), "Idempotent retry failed");
  const lastTwo = requests.filter((r) => r.method === "POST" && r.path.endsWith("/messages")).slice(-2); assert.equal(lastTwo[0].payload.request_id, lastTwo[1].payload.request_id); assert.equal(routeExecutions, beforeRoute + 1);
  nextAction = "routing"; await input("#conversation-message", "测试停止正在判断的请求"); await click("发送");
  await until(() => evaluate("!!document.querySelector('.conversationRouting')"), "Routing progress missing"); await click("停止本轮"); await click("返回"); assert.equal(postCount("/cancel"), 0);
  await click("停止本轮"); await click("停止本轮"); await until(() => evaluate("!document.querySelector('#conversation-message').disabled"), "Cancellation did not unlock composer"); assert.equal(postCount("/cancel"), 1);
  await open(`/conversations/${snapshot.conversation.id}`); await until(() => evaluate("document.querySelectorAll('.conversationMessage').length>5"), "Messages did not reload");
  for (const [name, width, height, dark] of [["desktop-final",1500,1050,false],["mobile",390,844,false],["mobile-dark",390,844,true]]) {
    await page.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 600 });
    await evaluate(`document.documentElement.dataset.theme='${dark ? "dark" : "light"}'`); await wait(200);
    const bounds = await evaluate("({width:innerWidth,body:document.documentElement.scrollWidth,send:!!document.querySelector('button[aria-label=\"发送消息\"]')})");
    assert.ok(bounds.body <= bounds.width + 2, `${name} horizontal overflow`); assert.ok(bounds.send);
    diagnostics.push({ name, ...bounds }); await shot(name);
  }
  await open("/runs/conversation-run-2"); await until(() => evaluate("!!document.querySelector('.runDetailPage')"), "Revision Run details missing");
  assert.equal(await evaluate("document.querySelector('.breadcrumb').textContent.includes('返回研究会话')"), true);
  await click("TaskDAG");
  assert.equal(await evaluate("document.querySelector('.tabContent').textContent.includes('本轮未执行研究任务')"), true);
  assert.equal(await evaluate("!!document.querySelector('.tabContent a[href=\"/runs/conversation-run-1\"]')"), true);
  await shot("revision-run-provenance");
  // Execution success and factual completeness remain separate in the TaskDAG.
  const firstRun = runs.get("conversation-run-1");
  firstRun.research_results = [{ task_id: "T1", plan_version: 1, summary: "已采纳可用部分，未核验范围仍保留。", findings: ["已核验的部分发现"], limitations: ["第二项仍缺少细节"], confidence: 0.9, reported_confidence: 0.9, criteria_coverage: 0.5, outcome: "partial", execution_accepted: true, assessments: [{ criterion_index: 1, status: "partial", finding_indices: [1], source_ids: [], reason: "已取得部分依据" }] }];
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1500, height: 1050, deviceScaleFactor: 1, mobile: false });
  await open("/runs/conversation-run-1"); await until(() => evaluate("!!document.querySelector('.runDetailPage')"), "Presentation Run missing"); await click("TaskDAG");
  await until(() => evaluate("!!document.querySelector('.taskNode.node-completed[data-id=\"T1\"]')"), "Accepted partial task is not completed");
  await evaluate("document.querySelector('.taskNode[data-id=\"T1\"]').click()");
  await until(() => evaluate("document.querySelector('.drawerStats')?.textContent.includes('完成·部分采纳')"), "Accepted partial task details missing");
  assert.equal(await evaluate("document.querySelector('.drawerStats').textContent.includes('90%')"), true);
  assert.equal(await evaluate("document.querySelector('.detailDrawer').textContent.includes('标准覆盖50%')"), true);
  assert.equal(await evaluate("document.querySelector('.drawerContent').textContent.includes('第二项仍缺少细节')"), true);
  await shot("accepted-partial-task");
  await evaluate("document.querySelector('button[aria-label=\"关闭详情\"]').click()");
  // A degraded review is a separate operational record, never a fake accept or
  // an extra copy of the later genuine Critic verdict.
  firstRun.critique_history = [{ decision: "accept", rationale: "后续真实审查通过", plan_version: 1, created_at: "2026-10-17T06:01:00Z" }];
  firstRun.quality_history = [{ decision: "accept", rationale: "独立质量验收通过", score: 65, plan_version: 1, draft_version: 1, created_at: "2026-10-17T06:02:00Z" }];
  firstRun.events = [
    { event_id: "review-degraded", run_id: firstRun.id, sequence: 1, event_type: "review_decided", node: "critic", status: "degraded", plan_version: 1, message: "Critic未形成有效决策，已转交独立质量验收", created_at: now, data: { decision: "review_degraded", decision_reason: "保留研究结果，交由QualityGate独立验收" } },
    { event_id: "review-genuine", run_id: firstRun.id, sequence: 2, event_type: "review_decided", node: "critic", status: "succeeded", plan_version: 1, message: "后续真实审查通过", created_at: "2026-10-17T06:01:00Z", data: { decision: "accept" } },
    { event_id: "review-diversion", run_id: firstRun.id, sequence: 3, event_type: "route_selected", node: "critic", plan_version: 1, message: "路由：critic→synthesizer", created_at: now, data: { from_node: "critic", to_node: "synthesizer", decision: "review_degraded" } },
  ];
  await open("/runs/conversation-run-1"); await until(() => evaluate("!!document.querySelector('.runDetailPage')"), "Degraded review fixture missing"); await click("审查决策");
  await until(() => evaluate("document.querySelector('.reviewColumns')?.textContent.includes('审查降级')"), "Degraded review is not labelled");
  assert.equal(await evaluate("document.querySelector('.runSummary').textContent.includes('任务完成') && document.querySelector('.runSummary').textContent.includes('1项部分采纳')"), true);
  assert.equal(await evaluate("document.querySelectorAll('.reviewColumns section:first-child .reviewCard').length"), 2);
  assert.equal(await evaluate("document.querySelector('.reviewColumns').textContent.includes('65/100')"), true);
  await shot("critic-degraded-reviewed");
  // Limited delivery remains readable/downloadable, without a fake quality pass.
  const beforeLimited = copy(firstRun);
  firstRun.status = "completed_with_warnings";
  firstRun.report = "## 摘要\n\n有限范围交付，未获得完整质量验收。\n\n## 研究发现\n\n仅保留已获取的可用材料。\n\n## 结论\n\n未核实事项不作为确定事实。\n\n## 局限\n\n审查服务暂不可用。\n\n## 参考文献\n\n本夹具未请求外部来源。";
  firstRun.warnings = ["审查服务暂不可用，已保留有限范围报告"];
  firstRun.quality_history = [];
  await open("/runs/conversation-run-1");
  await until(() => evaluate("document.querySelector('.reportProse')?.textContent.includes('有限范围交付')"), "Limited report is not readable");
  assert.equal(await evaluate("document.querySelector('.reportPaperHeader').textContent.includes('已通过验收')"), false);
  await click("下载HTML");
  await until(() => readdirSync(path.join(output, "downloads")).some((name) => name.endsWith(".html")), "Limited report HTML download must complete on E:");
  await shot("limited-delivery-report");
  Object.assign(firstRun, beforeLimited);
  // Delete is a separate button, never nested inside the navigation link.
  const savedSnapshot = copy(snapshot), savedRuns = copy([...runs]);
  const deleteCount = () => requests.filter((request) => request.method === "DELETE").length;
  await open("/conversations"); await until(() => evaluate("!!document.querySelector('.conversationRowItem')"), "Delete row missing");
  assert.equal(await evaluate("!!document.querySelector('.conversationDeleteButton').closest('a')"), false);
  await click("删除会话"); assert.equal(deleteCount(), 0);
  assert.equal(await evaluate("document.querySelector('[role=dialog]').textContent.includes('全部归属Run')"), true);
  await shot("delete-confirm"); await click("保留会话"); assert.equal(deleteCount(), 0); assert.equal(runs.size, 2);
  forceDeleteConflict = true; await click("删除会话"); await click("永久删除");
  await until(() => evaluate("document.querySelector('[role=alert]')?.textContent.includes('已更新')"), "Delete conflict missing");
  assert.equal(runs.size, 2); assert.equal(conversationExists, true);
  await until(() => evaluate("!!document.querySelector('.conversationDeleteButton')"), "Refreshed delete action missing");
  // A DELETE response can be lost after commit: read once, do not dispatch a second DELETE.
  loseDeleteResponse = true; await click("删除会话"); const beforeDelete = deleteCount(); await click("永久删除");
  await until(() => evaluate("document.querySelector('.conversationHistory').textContent.includes('尚无研究会话')"), "Deleted conversation remains listed");
  assert.equal(deleteCount(), beforeDelete + 1); assert.equal(runs.size, 0); assert.equal(conversationExists, false);
  assert.equal(await evaluate("document.querySelector('.conversationHistory').textContent.includes('清理0个')"), false);
  // Restore disposable fixtures only, then exercise detail-page failure/retry.
  snapshot = copy(savedSnapshot); conversationExists = true; for (const [id, run] of savedRuns) runs.set(id, copy(run));
  await open(`/conversations/${snapshot.conversation.id}`); await until(() => evaluate("!!document.querySelector('#conversation-message')"), "Detail for deletion missing");
  forceDeleteFailure = true; await click("删除会话"); await click("永久删除");
  await until(() => evaluate("document.querySelector('[role=alert]')?.textContent.includes('清理暂不可用')"), "Failed cleanup is not visible");
  await until(() => evaluate("!!document.querySelector('#conversation-message')?.disabled"), "Deletion intent must block new work");
  assert.equal(runs.size, 2); await click("重试删除"); await click("永久删除");
  await until(() => evaluate("!!document.querySelector('.conversationIndex')"), "Detail delete did not return to list"); assert.equal(runs.size, 0);
  // Deleting in a second tab closes the first tab's SSE and returns it to the list.
  snapshot = copy(savedSnapshot); conversationExists = true; for (const [id, run] of savedRuns) runs.set(id, copy(run));
  await open(`/conversations/${snapshot.conversation.id}`); await until(() => evaluate("!!document.querySelector('#conversation-message')"), "Cross-tab fixture missing");
  conversationExists = false; runs.clear();
  await until(() => evaluate("!!document.querySelector('.conversationIndex')"), "Deleted SSE did not leave stale detail");
  await shot("deleted-list");
  await Promise.all([...pendingInterceptions]);
  assert.deepEqual(failures, []);
  writeFileSync(path.join(output, "diagnostics.json"), JSON.stringify({ fixture_only: true, requests, routeExecutions, diagnostics, failures }, null, 2));
  console.log(`Conversation browser QA passed. Artifacts: ${output}`);
} catch (error) { if (page) await page.send("Page.captureScreenshot").then((value) => writeFileSync(path.join(output, "failure.png"), Buffer.from(value.data, "base64"))).catch(() => {}); writeFileSync(path.join(output, "diagnostics.json"), JSON.stringify({ fixture_only: true, requests, diagnostics, failures, error: error.message }, null, 2)); throw error; }
finally { page?.close(); if (control) { await control.send("Browser.close").catch(() => {}); control.close(); } browser.kill(); }
