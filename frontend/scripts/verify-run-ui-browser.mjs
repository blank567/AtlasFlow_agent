// Browser-only fixtures verify interaction without sending model or database writes.
// Run: node scripts/verify-run-ui-browser.mjs http://localhost:3210
// Font evidence only: append --inspect-fonts-only (no production writes or downloads).
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync } from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
const require = createRequire(import.meta.url);
const { WebSocket } = require("next/dist/compiled/ws");
const workspace = path.resolve(fileURLToPath(new URL("../../", import.meta.url)));
assert.match(workspace, /^E:/i, "Browser profiles must remain on E:");
const artifacts = path.join(workspace, "build", "run-ui-browser");
mkdirSync(artifacts, { recursive: true });
const profile = mkdtempSync(path.join(artifacts, "check-"));
const binary = ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].find(existsSync);
assert.ok(binary, "Chrome or Edge is required");
const browser = spawn(binary, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-breakpad", "--disable-crash-reporter", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { windowsHide: true, env: { ...process.env, TEMP: "E:\\codex\\tmp", TMP: "E:\\codex\\tmp" }, stdio: ["ignore", "ignore", "pipe"] });
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(check, message) {
  for (let attempt = 0; attempt < 150; attempt++) { if (await check()) return; await wait(100); }
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
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}${params.url ? ` (${params.url})` : ""}`)); }, method === "Page.navigate" ? 45_000 : 15_000);
      pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }));
    }); },
    close() { socket.close(); },
  };
}
const fixture = {
  id: "run-ui-check", query: "浏览器交互校验", status: "completed", report: "## 研究结论\n\n仅用于验证页面行为。", created_at: "2026-10-13T01:00:00Z", started_at: new Date(Date.now() - 60_000).toISOString(),
  metrics: { total_tasks: 2, successful_tasks: 2, failed_tasks: 0, duration_ms: 9000, model_calls: 3 },
  tool_calls: [{ call_id: "call-1", tool_name: "map_route", success: true, agent: "researcher", evidence_ids: ["source-1"], navigation_url: "https://uri.amap.com/navigation?from=fixture&to=fixture", duration_ms: 200, summary: "导航链接校验" }],
  evidence: [{ id: "source-1", title: "来源校验", uri: "https://example.com/source" }],
  trace_segments: [{ id: "trace-1", name: "atlasflow.workflow.initial", url: "https://smith.langchain.com/test-trace", status: "completed", provider_calls: [] }],
};
const knowledgeSpace = { id: "space-ui-fixture", slug: "llm-research", name: "LLM研究", description: "浏览器交互校验空间" };
let control, page, completedLive = false, failList = false, failCreate = false, failSpaces = false, listRequests = 0;
const createRequests = [];
const errors = [];
try {
  let logs = "";
  browser.stderr.on("data", (data) => { logs += data.toString(); });
  await until(() => /DevTools listening on (ws:\/\/[^\s]+)/.test(logs), "Browser did not start");
  const endpoint = new URL(logs.match(/DevTools listening on (ws:\/\/[^\s]+)/)[1]);
  control = await connect(endpoint.href);
  const { targetId } = await control.send("Target.createTarget", { url: "about:blank" });
  page = await connect(`ws://${endpoint.host}/devtools/page/${targetId}`);
  await page.send("Page.enable"); await page.send("Runtime.enable");
  page.on("Runtime.exceptionThrown", (event) => errors.push(event.exceptionDetails.text));
  page.on("Fetch.requestPaused", (event) => {
    const url = new URL(event.request.url);
    let response = {}, status = 200, stream = false;
    if (event.request.method === "OPTIONS") status = 204;
    else if (url.pathname.endsWith("/events")) { stream = true; response = "event: done\ndata: {}\n\n"; }
    else if (url.pathname.endsWith("/runs") && event.request.method === "POST") {
      createRequests.push(JSON.parse(event.request.postData ?? "{}"));
      if (failCreate) { response = { detail: "测试创建失败" }; status = 500; }
      else response = { ...fixture, id: `created-ui-check-${createRequests.length}`, query: createRequests.at(-1).query };
    } else if (url.pathname.endsWith("/runs")) {
      listRequests++;
      if (failList) { response = { detail: "测试连接失败" }; status = 500; }
      else response = { items: [fixture], total: 1, page: 1, page_size: 10, pages: 1 };
    } else if (url.pathname.endsWith("/runs/failed-ui-check")) response = { ...fixture, id: "failed-ui-check", status: "failed", report: undefined, tool_calls: [{ ...fixture.tool_calls[0], success: false, error: "工具错误校验" }], events: [{ event_id: "failed-event", sequence: 1, event_type: "node_failed", agent: "planner", message: "规划失败校验", created_at: fixture.created_at, data: { error: "测试错误原因" } }] };
    else if (url.pathname.endsWith("/runs/live-ui-check")) response = { ...fixture, id: "live-ui-check", status: completedLive ? "completed" : "running", report: completedLive ? fixture.report : undefined };
    else if (url.pathname.includes("/runs/")) response = fixture;
    else if (url.pathname.includes("/spaces")) {
      if (failSpaces) { response = { detail: "测试空间读取失败" }; status = 500; }
      else response = [knowledgeSpace];
    }
    void page.send("Fetch.fulfillRequest", { requestId: event.requestId, responseCode: status, responseHeaders: [{ name: "Content-Type", value: stream ? "text/event-stream" : "application/json" }, { name: "Access-Control-Allow-Origin", value: "*" }, { name: "Access-Control-Allow-Methods", value: "GET,POST,PUT,PATCH,DELETE,OPTIONS" }, { name: "Access-Control-Allow-Headers", value: "Content-Type,Last-Event-ID" }], body: Buffer.from(status === 204 ? "" : stream ? response : JSON.stringify(response)).toString("base64") }).catch((error) => errors.push(error.message));
  });
  await page.send("Fetch.enable", { patterns: [{ urlPattern: "*/api/v1/*" }] });
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  const evaluate = async (expression) => {
    const result = await page.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const origin = process.argv[2] ?? "http://localhost:3210";
  const navigate = (route) => page.send("Page.navigate", { url: `${origin}${route}` });
  const activeTab = () => evaluate("document.querySelector('[role=tab][aria-selected=true]')?.textContent");
  if (process.argv.includes("--inspect-fonts-only")) {
    await navigate("/");
    await until(() => evaluate("!!document.querySelector('.composerHeader label')"), "Research composer did not load for font inspection");
    const styles = await evaluate("(()=>{const style=e=>({fontFamily:getComputedStyle(e).fontFamily,color:getComputedStyle(e).color,fontSize:getComputedStyle(e).fontSize,fontWeight:getComputedStyle(e).fontWeight});return {body:style(document.body),textarea:style(document.querySelector('#research-query')),label:style(document.querySelector('.composerHeader label')),ink:getComputedStyle(document.documentElement).getPropertyValue('--ink').trim()};})()");
    await page.send("DOM.enable");
    await page.send("CSS.enable");
    const { root } = await page.send("DOM.getDocument");
    const { nodeId } = await page.send("DOM.querySelector", { nodeId: root.nodeId, selector: ".composerHeader label" });
    const platformFonts = await page.send("CSS.getPlatformFontsForNode", { nodeId });
    console.log(JSON.stringify({ computedStyles: styles, labelPlatformFonts: platformFonts.fonts }, null, 2));
  } else {
  await navigate("/runs/run-ui-check");
  await until(async () => await activeTab() === "研究报告", "Completed runs should open their report");
  assert.equal(await evaluate("document.querySelector('.runDiagnostics').open"), false);
  await evaluate("[...document.querySelectorAll('.headerActions button')].find(e=>e.textContent==='运行诊断').click()");
  await until(() => evaluate("document.querySelector('.runDiagnostics').open"), "Diagnostics shortcut did not open");
  assert.equal(await evaluate("document.querySelector('.toolNavigation').href.startsWith('https://uri.amap.com/')"), true);
  assert.equal(await evaluate("document.querySelector('.traceActions a').href"), fixture.trace_segments[0].url);
  await evaluate("[...document.querySelectorAll('.headerActions button')].find(e=>e.textContent==='重新运行').click()");
  assert.equal(await evaluate("!!document.querySelector('[role=dialog]')"), true, "Rerun must keep its confirmation");
  await navigate("/runs/failed-ui-check");
  await until(async () => (await activeTab())?.startsWith("执行事件"), "Failed runs should open events");
  assert.equal(await evaluate("document.querySelector('.runDiagnostics').open"), true);
  assert.equal(await evaluate("document.querySelector('.eventTimeline').textContent.includes('规划失败校验')"), true);
  await navigate("/runs/live-ui-check");
  await until(async () => await activeTab() === "Agent流程", "Active runs should open their flow");
  await evaluate("[...document.querySelectorAll('[role=tab]')].find(e=>e.textContent.startsWith('执行事件')).click()");
  completedLive = true;
  await until(() => evaluate("document.querySelector('.statusBadge').textContent==='已完成'"), "Live fixture did not refresh to complete");
  assert.match(await activeTab(), /^执行事件/, "Snapshot refresh must respect the user's selected tab");
  failList = true;
  await navigate("/runs");
  await until(() => evaluate("!![...document.querySelectorAll('.runsPanel button')].find(e=>e.textContent==='重试')"), "Failed list should offer retry");
  assert.equal(await evaluate("document.querySelector('.runsPanel').textContent.includes('没有匹配的运行')"), false, "Network failure is not an empty result");
  failList = false;
  await evaluate("[...document.querySelectorAll('.runsPanel button')].find(e=>e.textContent==='重试').click()");
  await until(() => evaluate("!!document.querySelector('.runRow')"), "Run list retry did not recover");
  const beforeInput = listRequests;
  await evaluate("(()=>{const input=document.querySelector('.searchField input'); const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set; setter.call(input,'浏览器');input.dispatchEvent(new Event('input',{bubbles:true}));})()");
  await wait(400);
  assert.equal(listRequests, beforeInput, "Typing a draft filter should not request data");
  await evaluate("document.querySelector('.filterBar').requestSubmit()");
  await until(() => listRequests === beforeInput + 1, "Applying a filter should issue one request");
  await wait(300);
  assert.equal(listRequests, beforeInput + 1, "Filter submission requested twice");

  const setValue = async (selector, value) => evaluate(`(()=>{const input=document.querySelector(${JSON.stringify(selector)});const prototype=input instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:input instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(prototype,'value').set.call(input,${JSON.stringify(value)});input.dispatchEvent(new Event(input instanceof HTMLSelectElement?'change':'input',{bubbles:true}));})()`);
  const choosePreset = async (label) => evaluate(`[...document.querySelectorAll('.policySegment')].find(e=>e.textContent.trim()===${JSON.stringify(label)}).click()`);
  const configValues = () => evaluate("[...document.querySelectorAll('.runConfigGrid input,.runConfigGrid select')].map(e=>e.value)");
  const waitForComposer = () => until(() => evaluate("!!document.querySelector('#research-query')&&!document.querySelector('.runConfigGrid select').disabled"), "Research composer did not load");
  const inputSelectors = { turn: ".runConfigGrid label:nth-child(5) input", run: ".runConfigGrid label:nth-child(6) input" };

  await navigate("/");
  await waitForComposer();
  assert.equal(await evaluate("document.querySelector('#research-query').maxLength"), 4000);
  assert.equal(await evaluate("document.querySelector('.queryCounter').textContent"), "0 / 4,000");
  assert.equal(await evaluate("document.querySelectorAll('.policySegment').length"), 4);
  assert.equal(await evaluate("document.querySelector('.createForm .advancedToggle')"), null, "Configuration must not hide behind an advanced toggle");
  assert.equal(await evaluate("[...document.querySelectorAll('.runConfigGrid input,.runConfigGrid select,.configOptions input')].every(e=>e.getBoundingClientRect().height>0&&getComputedStyle(e).visibility!=='hidden'&&!e.closest('details:not([open])'))"), true, "Execution parameters must remain visible");
  await evaluate("document.querySelector('#research-query').focus()");
  await page.send("Input.insertText", { text: "数".repeat(4001) });
  assert.equal(await evaluate("document.querySelector('#research-query').value.length"), 4000, "Native input must enforce the 4000-character limit");
  assert.equal(await evaluate("document.querySelector('.queryCounter').textContent"), "4,000 / 4,000");
  await setValue("#research-query", "研");
  await evaluate("document.querySelector('#research-query').blur()");
  assert.equal(await evaluate("document.querySelector('#research-query').getAttribute('aria-invalid')"), "true");
  assert.equal(await evaluate("document.querySelector('.createSubmitBar button').disabled"), true);
  await setValue("#research-query", "分析混合检索的召回策略与质量评估");
  assert.equal(await evaluate("document.querySelector('#research-query').getAttribute('aria-invalid')"), "false");
  await setValue(".runConfigGrid select", knowledgeSpace.slug);
  await choosePreset("精简研究");
  const quickValues = await configValues();
  assert.equal(quickValues[1], "2");
  assert.equal(quickValues[2], "0");
  await choosePreset("自定义");
  assert.deepEqual(await configValues(), quickValues, "Entering custom mode must inherit the selected preset values");
  await choosePreset("标准研究");
  await setValue(inputSelectors.turn, "6");
  assert.equal(await evaluate("document.querySelector('.policySegment.is-selected').textContent.trim()"), "自定义", "Editing a parameter must select custom mode");
  assert.equal((await configValues())[0], knowledgeSpace.slug, "Changing strategy must preserve the selected knowledge space");
  await setValue(inputSelectors.run, "2");
  const beforeInvalidSubmit = createRequests.length;
  await evaluate("document.querySelector('.createForm').requestSubmit()");
  assert.equal(createRequests.length, beforeInvalidSubmit, "An invalid shared tool budget must not create a run");
  assert.equal(await evaluate("document.querySelector('#create-run-error').textContent"), "运行工具调用总额不得低于单轮Agent调用上限。");
  assert.equal(await evaluate("document.activeElement===document.querySelector('.runConfigGrid label:nth-child(6) input')"), true, "Budget validation must focus the affected input");
  await setValue(inputSelectors.run, "14");
  assert.equal(await evaluate("document.querySelector('#create-run-error')"), null, "Correcting a budget must clear the stale error");
  await evaluate("document.querySelector('.createForm').requestSubmit()");
  await until(() => evaluate("location.pathname==='/runs/created-ui-check-1'"), "Valid auto-approved run did not navigate to its result");
  assert.equal(createRequests.length, 1);
  assert.equal(createRequests[0].auto_approve, true);
  assert.equal(createRequests[0].policy.knowledge_mode, "selected");
  assert.equal(createRequests[0].policy.knowledge_space, knowledgeSpace.slug);
  assert.equal(createRequests[0].policy.max_tool_calls_per_turn, 6);
  assert.equal(createRequests[0].policy.max_tool_calls_per_run, 14);

  await navigate("/");
  await waitForComposer();
  await setValue("#research-query", "研究多Agent审查与计划演进");
  await setValue(".runConfigGrid select", knowledgeSpace.slug);
  await choosePreset("强化审查");
  await evaluate("document.querySelector('.createSubmitBar .switchLabel input').click()");
  failCreate = true;
  await evaluate("document.querySelector('.createForm').requestSubmit()");
  await until(() => evaluate("document.querySelector('#create-run-error')?.textContent==='测试创建失败'"), "Create failure did not expose the backend error");
  assert.equal(await evaluate("document.querySelector('.createSubmitBar button').disabled"), false, "Create failure must allow retry");
  assert.equal(await evaluate("document.querySelector('#research-query').disabled"), false, "Create failure must restore editable fields");
  assert.equal((await configValues())[0], knowledgeSpace.slug);
  assert.equal(createRequests[1].auto_approve, false);
  assert.equal(createRequests[1].policy.initial_task_count, 2);
  assert.equal(createRequests[1].policy.required_supplement_rounds, 1);
  assert.equal(createRequests[1].policy.supplement_task_count, 1);
  failCreate = false;
  await evaluate("document.querySelector('.createForm').requestSubmit()");
  await until(() => evaluate("location.pathname==='/runs/created-ui-check-3'"), "Create retry did not recover");
  assert.deepEqual(createRequests[2], createRequests[1], "Retry must retain the original query, approval and knowledge-space parameters");
  failSpaces = true;
  await navigate("/");
  await waitForComposer();
  assert.equal(await evaluate("document.querySelector('.configSection .formHint').textContent"), "知识空间读取失败，可暂不使用知识库。");
  assert.equal((await configValues())[0], "", "Unavailable knowledge spaces must fall back to the explicit off option");
  assert.deepEqual(errors, []);
  console.log("Run UI browser checks passed: status defaults, retained tab selection, diagnostics, navigation, rerun confirmation, list retry, explicit filtering, composer limits, visible configuration, preset inheritance, knowledge-space retention, approval payloads, budget validation and create retry.");
  }
} finally {
  page?.close();
  if (control) { await control.send("Browser.close").catch(() => {}); control.close(); }
  browser.kill();
}
