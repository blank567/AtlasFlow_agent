// Optional UI check against a running frontend; API fixtures stay in this process.
// Browser profiles and screenshots stay on E:. No live knowledge API is called.
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
const root = path.join(workspace, "build", "knowledge-browser");
mkdirSync(root, { recursive: true });
const output = mkdtempSync(path.join(root, "check-"));
const binary = ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].find(existsSync);
assert.ok(binary, "Chrome or Edge is required");
const browser = spawn(binary, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-breakpad", "--disable-crash-reporter", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${path.join(output, "profile")}`, "about:blank"], {
  windowsHide: true, env: { ...process.env, TEMP: "E:\\codex\\tmp", TMP: "E:\\codex\\tmp" }, stdio: ["ignore", "ignore", "pipe"],
});
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
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 20_000);
      pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }));
    }); },
    close() { socket.close(); },
  };
}

const now = "2026-10-12T12:00:00Z";
const space = { id: "space", slug: "llm-notes", name: "LLM 知识库", description: "整空间版本验证", visibility: "private", embedding_profile: "default-embedding-v1", retrieval_profile: "default-hybrid-v1", active_generation_id: "generation-old", active_version_id: "v2", active_build_id: null, created_at: now, updated_at: now };
const documents = ["Attention Is All You Need", "RAG 检索指南"].map((title, index) => ({ id: `doc-${index}`, space_id: space.id, source_id: `source-${index}`, title, status: "active", current_version_id: `old-${index}`, metadata: {}, created_at: now, updated_at: now }));
const versions = [{ id: "v2", space_id: space.id, version_number: 2, generation_id: "generation-old", documents: Object.fromEntries(documents.map((doc) => [doc.id, { version_id: doc.current_version_id, status: "active" }])), note: "重建前版本", created_at: now }];
const builds = [], jobs = [], failures = [], writes = [];
function responseFor(request) {
  const url = new URL(request.url), route = url.pathname;
  if (request.method === "OPTIONS") return {};
  if (request.method === "POST") writes.push(route);
  if (route.endsWith("/knowledge/spaces")) return [space];
  if (route.endsWith("/documents")) return documents;
  if (route.endsWith("/knowledge/jobs")) return jobs;
  if (route.endsWith("/versions")) return versions;
  if (route.endsWith("/builds")) return builds;
  if (route.endsWith("/reindex")) {
    const build = { id: "build", space_id: space.id, base_version_id: "v2", generation_id: "generation-new", documents: Object.fromEntries(documents.map((doc, i) => [doc.id, { version_id: `new-${i}`, status: "active" }])), job_ids: ["job-0", "job-1"], status: "building", parser_version: "structured-parser-v3", chunker_version: "parent-child-v2", note: JSON.parse(request.postData || "{}").note || "", created_at: now, updated_at: now };
    builds.push(build); space.active_build_id = build.id;
    jobs.push(...documents.map((doc, i) => ({ id: `job-${i}`, space_id: space.id, document_id: doc.id, version_id: `new-${i}`, space_build_id: build.id, generation_id: build.generation_id, status: "queued", stage: "queued", attempt: 0, progress: 0, created_at: now, updated_at: now })));
    return build;
  }
  if (route.endsWith("/retry")) {
    const job = jobs.find((item) => route.includes(`/${item.id}/`));
    job.status = "queued"; job.error_message = null; builds[0].status = "building";
    return job;
  }
  if (route.endsWith("/publish")) {
    const build = builds[0]; assert.equal(build.status, "ready");
    const version = { id: "v3", space_id: space.id, version_number: 3, generation_id: build.generation_id, documents: build.documents, note: build.note, created_at: now };
    versions.push(version); build.status = "published"; build.published_version_id = version.id;
    space.active_version_id = version.id; space.active_generation_id = version.generation_id; space.active_build_id = null;
    documents.forEach((doc) => { doc.current_version_id = version.documents[doc.id].version_id; });
    return version;
  }
  return {};
}

let control, page;
try {
  let logs = "";
  browser.stderr.on("data", (data) => { logs += data.toString(); });
  await until(() => /DevTools listening on (ws:\/\/[^\s]+)/.test(logs), "Browser failed to start");
  const endpoint = logs.match(/DevTools listening on (ws:\/\/[^\s]+)/)[1];
  control = await connect(endpoint);
  const { targetId } = await control.send("Target.createTarget", { url: "about:blank" });
  page = await connect(`ws://${new URL(endpoint).host}/devtools/page/${targetId}`);
  await page.send("Page.enable"); await page.send("Runtime.enable");
  page.on("Runtime.exceptionThrown", (event) => failures.push(event.exceptionDetails.text));
  page.on("Fetch.requestPaused", (event) => {
    try {
      const body = responseFor(event.request);
      void page.send("Fetch.fulfillRequest", { requestId: event.requestId, responseCode: event.request.method === "OPTIONS" ? 204 : 200, responseHeaders: [
        { name: "Content-Type", value: "application/json" }, { name: "Access-Control-Allow-Origin", value: "*" },
        { name: "Access-Control-Allow-Methods", value: "GET, POST, PUT, OPTIONS" }, { name: "Access-Control-Allow-Headers", value: "content-type" },
      ], body: Buffer.from(JSON.stringify(body)).toString("base64") }).catch((error) => failures.push(error.message));
    } catch (error) { failures.push(error.message); }
  });
  await page.send("Fetch.enable", { patterns: [{ urlPattern: "*/api/v1/*" }] });
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1080, deviceScaleFactor: 1, mobile: false });
  const evaluate = async (expression) => {
    const result = await page.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const button = (text) => `[...document.querySelectorAll('button')].find(e=>e.textContent===${JSON.stringify(text)})`;
  await page.send("Page.navigate", { url: `${process.argv[2] ?? "http://localhost:3210"}/knowledge` });
  await until(() => evaluate(`!!${button("重建整个空间")}`), "Knowledge page failed to load");
  await evaluate(`${button("重建整个空间")}.click()`);
  await until(() => evaluate("!!document.querySelector('[role=dialog]')"), "Build confirmation missing");
  assert.equal(writes.length, 0, "Build must wait for confirmation");
  await evaluate(`${button("开始整空间重建")}.click()`);
  await until(() => evaluate("!!document.querySelector('.spaceBuildPanel')"), "Build panel missing");
  assert.equal(space.active_version_id, "v2");
  assert.equal(await evaluate("document.querySelector('.knowledgeDrop button').disabled"), true);
  assert.equal(await evaluate(`${button("保存当前空间版本")}.disabled`), true);
  assert.equal(await evaluate(`${button("发布整个空间版本")}.disabled`), true);
  builds[0].status = "failed"; jobs[0].status = "failed_terminal"; jobs[0].error_message = "测试：向量服务暂时不可用";
  jobs[1].status = "completed"; jobs[1].stage = "ready"; jobs[1].progress = 1;
  await evaluate(`${button("刷新状态")}.click()`);
  await until(() => evaluate(`!!${button("重试文档")}`), "Failed job retry missing");
  await evaluate(`${button("重试文档")}.click()`);
  await until(() => jobs[0].status === "queued", "Retry did not call the job endpoint");
  builds[0].status = "ready"; jobs.forEach((job) => { job.status = "completed"; job.stage = "ready"; job.progress = 1; });
  await evaluate(`${button("刷新状态")}.click()`);
  await until(() => evaluate(`${button("发布整个空间版本")} && !${button("发布整个空间版本")}.disabled`), "Ready build cannot publish");
  assert.equal(await evaluate("document.querySelectorAll('.spaceVersionLedger').length"), 1, "Refresh duplicated the space ledger");
  writeFileSync(path.join(output, "desktop.png"), Buffer.from((await page.send("Page.captureScreenshot")).data, "base64"));
  await page.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await wait(200);
  assert.ok(await evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Knowledge page overflows on mobile");
  writeFileSync(path.join(output, "mobile.png"), Buffer.from((await page.send("Page.captureScreenshot")).data, "base64"));
  await evaluate(`${button("发布整个空间版本")}.click()`);
  await until(() => evaluate(`!!${button("确认发布整个空间")}`), "Publish confirmation missing");
  assert.equal(space.active_version_id, "v2", "Publish must wait for confirmation");
  await evaluate(`${button("确认发布整个空间")}.click()`);
  await until(() => evaluate("!document.querySelector('.spaceBuildPanel')"), "Published build panel did not clear");
  assert.equal(space.active_version_id, "v3");
  assert.deepEqual(failures, []);
  console.log(`Knowledge browser checks passed: staged build, locking, retry, confirmed release, mobile layout. Artifacts: ${output}`);
} catch (error) {
  if (page) {
    const state = await page.send("Runtime.evaluate", { expression: "JSON.stringify({text:document.querySelector('.spaceVersionLedger')?.innerText,buttons:[...document.querySelectorAll('.spaceVersionLedger button')].map(e=>({text:e.textContent,disabled:e.disabled}))})", returnByValue: true }).catch(() => null);
    console.error(state?.result?.value, { writes, jobs, builds, failures });
    const screenshot = await page.send("Page.captureScreenshot").catch(() => null);
    if (screenshot) writeFileSync(path.join(output, "failure.png"), Buffer.from(screenshot.data, "base64"));
  }
  throw error;
} finally {
  page?.close();
  if (control) { await control.send("Browser.close").catch(() => {}); control.close(); }
  browser.kill();
}
