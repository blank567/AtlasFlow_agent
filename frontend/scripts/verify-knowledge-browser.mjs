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
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}${params.url ? ` (${params.url})` : ""}`)); }, method === "Page.navigate" ? 45_000 : 20_000);
      pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }));
    }); },
    close() { socket.close(); },
  };
}

const now = "2026-10-13T12:00:00Z";
const space = { id: "space", slug: "llm-notes", name: "LLM 知识库", description: "整空间版本验证", visibility: "private", embedding_profile: "default-embedding-v1", retrieval_profile: "default-hybrid-v1", active_generation_id: "generation-old", active_version_id: "v2", active_build_id: null, created_at: now, updated_at: now };
const documents = ["Attention Is All You Need", "RAG 检索指南"].map((title, index) => ({ id: `doc-${index}`, space_id: space.id, source_id: `source-${index}`, title, status: "active", current_version_id: `old-${index}`, metadata: {}, created_at: now, updated_at: now }));
const versions = [1, 2].map((number) => ({ id: `v${number}`, space_id: space.id, version_number: number, generation_id: number === 2 ? "generation-old" : "generation-first", documents: Object.fromEntries(documents.map((doc, index) => [doc.id, { version_id: number === 2 ? doc.current_version_id : `first-${index}`, status: "active" }])), note: number === 2 ? "重建前版本" : "初始版本", created_at: now }));
const builds = [], jobs = [], failures = [], writes = [];
const searchRequests = [];
const currentBuild = () => builds.find((build) => build.id === space.active_build_id);
const currentJobs = () => jobs.filter((job) => job.space_build_id === space.active_build_id);
function responseFor(request) {
  const url = new URL(request.url), route = url.pathname;
  if (request.method === "OPTIONS") return {};
  if (["POST", "PUT", "DELETE"].includes(request.method)) writes.push(route);
  if (route.endsWith("/knowledge/search/debug")) {
    const payload = JSON.parse(request.postData || "{}");
    searchRequests.push(payload);
    assert.equal(payload.space, space.slug);
    const normalizedQuery = payload.query.replace(/\s+/g, " ").trim();
    const expansionRequested = payload.tuning.query_expansion_enabled;
    const expansionMatched = /注意力|attention/i.test(normalizedQuery);
    // This fixture represents an injectable expander with four aliases;
    // the built-in glossary need not produce four variants for every term.
    const aliases = ["attention", "attention mechanism", "self-attention", "注意力机制"];
    const queryVariants = [normalizedQuery, ...(expansionRequested && expansionMatched ? aliases.slice(0, payload.tuning.query_expansion_limit).map((alias) => `${normalizedQuery} ${alias}`) : [])];
    return {
      query: normalizedQuery,
      decision: { status: "sufficient", reasons: ["两份来源提供有效证据"], coverage: 1, evidence_count: 2, best_relevance: 0.882, score_basis: "rerank" },
      hits: documents.map((doc, index) => ({
        chunk: { id: `chunk-${index}`, title: doc.title, content: `命中原文${index + 1}：注意力机制基于查询、键和值计算相关性。本段为界面回归测试资料。`, heading_path: ["模型架构", "注意力机制"], page_number: 3 + index, metadata: { canonical_uri: "https://arxiv.org/abs/1706.03762" } },
        context_excerpt: index === 0 ? "邻近上下文：多头注意力在不同表示空间中并行计算。" : null,
        lexical_score: 4.281 - index, vector_score: 0.814 - index * 0.1, fusion_score: 0.032 - index * 0.005, rerank_score: index === 0 ? 0.882 : null,
      })),
      trace: { profile_id: space.retrieval_profile, generation_id: space.active_generation_id, lexical_candidates: 24, vector_candidates: 31, fused_candidates: 40, rerank_candidates: 20, rerank_requested: payload.tuning.rerank_enabled, rerank_applied: payload.tuning.rerank_enabled, query_expansion_requested: expansionRequested, query_expansion_applied: queryVariants.length > 1, query_expansion_method: "bilingual-terms-v1", query_variants: queryVariants, matched_terms: expansionRequested && expansionMatched ? ["attention"] : [], score_scope: queryVariants.length > 1 ? "best_query_variant" : "original_query", applied_parameters: payload.tuning, degraded: [], duration_ms: 361 },
    };
  }
  if (route.endsWith("/knowledge/spaces")) return [space];
  if (route.endsWith("/documents")) return documents;
  if (route.endsWith("/knowledge/jobs")) return jobs;
  if (route.endsWith("/versions") && request.method === "GET") return versions.filter((version) => !version.deleted_at);
  if (route.endsWith("/versions") && request.method === "POST") {
    assert.equal(space.active_build_id, null, "Snapshot creation must wait for the space build");
    assert.equal(space.active_version_id, null, "The UI must not duplicate an existing current snapshot");
    assert.ok(!jobs.some((job) => ["queued", "running", "failed_retryable"].includes(job.status)));
    const versionNumber = Math.max(0, ...versions.map((version) => version.version_number)) + 1;
    const version = { id: `v${versionNumber}`, space_id: space.id, version_number: versionNumber, generation_id: space.active_generation_id, documents: Object.fromEntries(documents.map((doc) => [doc.id, { version_id: doc.current_version_id, status: doc.status }])), note: JSON.parse(request.postData || "{}").note, created_at: now };
    versions.push(version); space.active_version_id = version.id;
    return version;
  }
  if (request.method === "DELETE" && /\/versions\/[^/]+$/.test(route)) {
    const version = versions.find((item) => route.endsWith(`/versions/${item.id}`));
    assert.ok(version && !version.deleted_at, "Deletion must target a live fixture snapshot");
    assert.notEqual(version.id, space.active_version_id, "The current space version cannot be deleted");
    assert.equal(space.active_build_id, null, "Version deletion must wait for the space build");
    assert.ok(!jobs.some((job) => ["queued", "running", "failed_retryable"].includes(job.status)), "Version deletion must wait for active jobs");
    version.deleted_at = now;
    return version;
  }
  if (route.endsWith("/builds")) return builds;
  if (route.endsWith("/reindex")) {
    assert.equal(space.active_build_id, null, "Only one full-space build can be active");
    const id = `build-${builds.length + 1}`;
    const build = { id, space_id: space.id, base_version_id: space.active_version_id, generation_id: `generation-${id}`, documents: Object.fromEntries(documents.map((doc, i) => [doc.id, { version_id: `${id}-new-${i}`, status: "active" }])), job_ids: documents.map((_, i) => `${id}-job-${i}`), status: "building", parser_version: "structured-parser-v3", chunker_version: "parent-child-v2", note: JSON.parse(request.postData || "{}").note || "", created_at: now, updated_at: now };
    builds.push(build); space.active_build_id = build.id;
    jobs.push(...documents.map((doc, i) => ({ id: build.job_ids[i], space_id: space.id, document_id: doc.id, version_id: build.documents[doc.id].version_id, space_build_id: build.id, generation_id: build.generation_id, status: "queued", stage: "queued", attempt: 0, progress: 0, created_at: now, updated_at: now })));
    return build;
  }
  if (route.endsWith("/retry")) {
    const job = jobs.find((item) => route.includes(`/${item.id}/`));
    assert.ok(job, "Retry endpoint must target a fixture job");
    job.status = "queued"; job.error_message = null; currentBuild().status = "building";
    return job;
  }
  if (route.endsWith("/cancel")) {
    const build = currentBuild(); assert.ok(build);
    assert.ok(!currentJobs().some((job) => job.status === "running"));
    currentJobs().forEach((job) => { job.status = "cancelled"; });
    build.status = "cancelled"; space.active_build_id = null;
    return build;
  }
  if (route.endsWith("/publish")) {
    const build = currentBuild(); assert.equal(build.status, "ready");
    assert.ok(currentJobs().every((job) => job.status === "completed"));
    const version = { id: "v3", space_id: space.id, version_number: 3, generation_id: build.generation_id, documents: build.documents, note: build.note, created_at: now };
    versions.push(version); build.status = "published"; build.published_version_id = version.id;
    space.active_version_id = version.id; space.active_generation_id = version.generation_id; space.active_build_id = null;
    documents.forEach((doc) => { doc.current_version_id = version.documents[doc.id].version_id; });
    return version;
  }
  if (route.endsWith("/activate")) {
    assert.equal(space.active_build_id, null);
    const version = versions.find((item) => route.includes(`/versions/${item.id}/`)); assert.ok(version);
    space.active_version_id = version.id; space.active_generation_id = version.generation_id;
    documents.forEach((doc) => { doc.current_version_id = version.documents[doc.id].version_id; doc.status = version.documents[doc.id].status; });
    return space;
  }
  if (route.endsWith("/note") && request.method === "PUT") {
    const version = versions.find((item) => route.includes(`/versions/${item.id}/`)); assert.ok(version);
    version.note = JSON.parse(request.postData || "{}").note;
    return version;
  }
  throw new Error(`Unexpected fixture API: ${request.method} ${route}`);
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
        { name: "Access-Control-Allow-Methods", value: "GET, POST, PUT, DELETE, OPTIONS" }, { name: "Access-Control-Allow-Headers", value: "content-type" },
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
  const historyButton = "[...document.querySelectorAll('button')].find(e=>e.textContent.startsWith('历史版本与说明'))";
  const versionRow = (number) => `[...document.querySelectorAll('.knowledgeVersionDrawer .spaceVersionRow')].find(e=>e.querySelector('.spaceVersionNumber strong')?.textContent==='v${number}')`;
  const deleteVersionButton = (number) => `[...${versionRow(number)}.querySelectorAll('button')].find(e=>e.textContent==='删除版本')`;
  const isRendered = (expression) => evaluate(`(()=>{const e=${expression};return !!e && e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden' && !e.closest('details:not([open])')})()`);
  const fill = (selector, value) => evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});const p=e instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  const baselineVersions = documents.map((doc) => doc.current_version_id);
  await page.send("Page.navigate", { url: `${process.argv[2] ?? "http://localhost:3210"}/knowledge` });
  await until(() => evaluate(`!!${button("重建整个空间")}`), "Knowledge page failed to load");
  assert.ok(await isRendered(button("重建整个空间")), "Full-space rebuild must remain directly visible");
  assert.equal(await evaluate(`!!${button("保存当前空间版本")}`), false, "The easily confused snapshot action must not be a main button");
  assert.ok(await isRendered("document.querySelector('#space-version-note')"), "Space version note must be directly visible");
  assert.equal(await evaluate("document.querySelectorAll('.spaceVersionLedger details').length"), 0, "Version actions must not require nested disclosures");
  await evaluate(`${historyButton}.click()`);
  await until(() => evaluate("!!document.querySelector('.knowledgeVersionDrawer')"), "Version history drawer missing");
  assert.equal(await evaluate("document.querySelectorAll('.knowledgeVersionDrawer .spaceVersionRow').length"), 2);
  assert.ok(await isRendered(`${versionRow(1)}.querySelector('input')`), "Historical note input must be directly editable");
  assert.ok(await isRendered(`${versionRow(1)}.querySelector('button')`), "Historical note save action must be directly visible");
  assert.equal(await evaluate(`${deleteVersionButton(2)}.disabled`), true, "The current snapshot deletion button must be disabled");
  assert.equal(await evaluate(`${button("创建当前索引快照")}.disabled`), true, "A current snapshot must not be saved twice");
  await evaluate("document.querySelector('[aria-label=\"关闭详情\"]').click()");
  await until(() => evaluate("!document.querySelector('.knowledgeVersionDrawer')"), "Version history drawer did not close");

  // Opening or dismissing either confirmation must not perform a mutation.
  await evaluate(`${button("重建整个空间")}.click()`);
  await until(() => evaluate("!!document.querySelector('[role=dialog]')"), "Build confirmation missing");
  assert.equal(writes.length, 0, "Build must wait for confirmation");
  await evaluate(`${button("返回")}.click()`);
  await until(() => evaluate("!document.querySelector('.confirmDialog')"), "Build confirmation did not dismiss");
  assert.equal(writes.length, 0);
  await evaluate(`${button("重建整个空间")}.click()`);
  await until(() => evaluate(`!!${button("开始整空间重建")}`), "Second build confirmation missing");
  await evaluate(`${button("开始整空间重建")}.click()`);
  await until(() => evaluate("!!document.querySelector('.spaceBuildPanel')"), "Build panel missing");
  assert.equal(space.active_version_id, "v2");
  assert.equal(await evaluate("document.querySelector('.knowledgeDrop button').disabled"), true);
  assert.equal(await evaluate("document.querySelector('#knowledge-upload').disabled"), true, "Locked space must prevent file selection");
  assert.ok(await evaluate("[...document.querySelectorAll('.knowledgeRows button')].every(e=>e.disabled)"), "Locked space must prevent document archive");
  assert.equal(await evaluate(`!!${button("保存当前空间版本")}`), false);
  assert.equal(await evaluate(`${button("发布整个空间版本")}.disabled`), true);
  await evaluate(`${historyButton}.click()`);
  await until(() => evaluate("!!document.querySelector('.knowledgeVersionDrawer')"), "Locked history drawer missing");
  assert.equal(await evaluate(`${versionRow(1)}.querySelector('.secondaryButton').disabled`), true, "Build must lock full-space version activation");
  assert.equal(await evaluate(`${deleteVersionButton(1)}.disabled`), true, "Build must lock historical snapshot deletion");
  assert.equal(await evaluate(`${button("创建当前索引快照")}.disabled`), true, "Build must lock snapshot creation");
  await evaluate("document.querySelector('[aria-label=\"关闭详情\"]').click()");
  const writesBeforeCancel = writes.length;
  await evaluate(`${button("取消本次构建")}.click()`);
  await until(() => evaluate(`!!${button("确认取消构建")}`), "Cancel build confirmation missing");
  assert.equal(writes.length, writesBeforeCancel, "Build cancellation must wait for confirmation");
  await evaluate(`${button("返回")}.click()`);
  await until(() => evaluate("!document.querySelector('.confirmDialog')"), "Cancel confirmation did not dismiss");
  assert.equal(currentBuild().status, "building");
  await evaluate(`${button("取消本次构建")}.click()`);
  await until(() => evaluate(`!!${button("确认取消构建")}`), "Repeated cancel confirmation missing");
  await evaluate(`${button("确认取消构建")}.click()`);
  await until(() => evaluate("!document.querySelector('.spaceBuildPanel')"), "Cancelled build panel did not clear");
  assert.equal(builds[0].status, "cancelled");
  assert.equal(space.active_version_id, "v2");
  assert.deepEqual(documents.map((doc) => doc.current_version_id), baselineVersions, "Cancellation must preserve all current document versions");

  await fill("#space-version-note", "全空间发布回归");
  await evaluate(`${button("重建整个空间")}.click()`);
  await until(() => evaluate(`!!${button("开始整空间重建")}`), "Restart build confirmation missing");
  await evaluate(`${button("开始整空间重建")}.click()`);
  await until(() => evaluate("!!document.querySelector('.spaceBuildPanel')"), "Restarted build panel missing");
  const build = currentBuild(), buildJobs = currentJobs();
  build.status = "failed"; buildJobs[0].status = "failed_terminal"; buildJobs[0].error_message = "测试：向量服务暂时不可用";
  buildJobs[1].status = "completed"; buildJobs[1].stage = "ready"; buildJobs[1].progress = 1;
  await evaluate(`${button("刷新状态")}.click()`);
  await until(() => evaluate(`!!${button("重试文档")}`), "Failed job retry missing");
  await evaluate(`${button("重试文档")}.click()`);
  await until(() => buildJobs[0].status === "queued", "Retry did not call the job endpoint");
  await until(() => evaluate("!document.querySelector('.spaceBuildFailures')"), "Retry did not clear the failed document presentation");
  assert.deepEqual(documents.map((doc) => doc.current_version_id), baselineVersions, "Partially built space must continue serving every old document version");
  build.status = "ready"; buildJobs.forEach((job) => { job.status = "completed"; job.stage = "ready"; job.progress = 1; });
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
  const writesBeforePublish = writes.length;
  await evaluate(`${button("返回")}.click()`);
  await until(() => evaluate("!document.querySelector('.confirmDialog')"), "Publish confirmation did not dismiss");
  assert.equal(writes.length, writesBeforePublish);
  await evaluate(`${button("发布整个空间版本")}.click()`);
  await until(() => evaluate(`!!${button("确认发布整个空间")}`), "Second publish confirmation missing");
  await evaluate(`${button("确认发布整个空间")}.click()`);
  await until(() => evaluate("!document.querySelector('.spaceBuildPanel')"), "Published build panel did not clear");
  assert.equal(space.active_version_id, "v3");
  assert.equal(space.active_generation_id, build.generation_id);
  assert.deepEqual(documents.map((doc) => doc.current_version_id), documents.map((doc) => build.documents[doc.id].version_id), "Publish must switch every document together");
  assert.equal(writes.filter((route) => route.endsWith("/versions")).length, 0, "Publishing a rebuild must not require a second snapshot request");
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1080, deviceScaleFactor: 1, mobile: false });

  // Historical activation is a drawer action; cancellation leaves the new space intact.
  await evaluate(`${historyButton}.click()`);
  await until(() => evaluate("!!document.querySelector('.knowledgeVersionDrawer')"), "Published history drawer missing");
  assert.equal(await evaluate("document.querySelectorAll('.knowledgeVersionDrawer .spaceVersionRow').length"), 3);
  const switchOld = `${versionRow(2)}.querySelector('.secondaryButton')`;
  await evaluate(`${switchOld}.click()`);
  await until(() => evaluate(`!!${button("确认切换整个空间")}`), "History activation confirmation missing");
  const writesBeforeSwitch = writes.length;
  assert.equal(space.active_version_id, "v3");
  assert.equal(await evaluate("!!document.querySelector('.knowledgeVersionDrawer')"), false, "Version drawer must close before confirmation");
  await evaluate(`${button("返回")}.click()`);
  await until(() => evaluate("!document.querySelector('.confirmDialog')"), "History confirmation did not dismiss");
  assert.equal(writes.length, writesBeforeSwitch);
  await evaluate(`${historyButton}.click()`);
  await until(() => evaluate("!!document.querySelector('.knowledgeVersionDrawer')"), "History drawer did not reopen");
  await evaluate(`${switchOld}.click()`);
  await until(() => evaluate(`!!${button("确认切换整个空间")}`), "Repeated activation confirmation missing");
  await evaluate(`${button("确认切换整个空间")}.click()`);
  await until(() => evaluate("document.querySelector('.spaceVersionIndicator')?.textContent==='当前v2'"), "Old space version did not activate");
  assert.equal(space.active_generation_id, "generation-old");
  assert.deepEqual(documents.map((doc) => doc.current_version_id), baselineVersions);

  // Active ingestion jobs also protect historical deletion, independently
  // of whether a whole-space build is present.
  const backgroundJob = { id: "background-job", space_id: space.id, document_id: documents[0].id, version_id: documents[0].current_version_id, space_build_id: null, generation_id: space.active_generation_id, status: "queued", stage: "queued", attempt: 0, progress: 0, created_at: now, updated_at: now };
  jobs.push(backgroundJob);
  await evaluate(`${button("刷新状态")}.click()`);
  await evaluate(`${historyButton}.click()`);
  await until(() => evaluate("!!document.querySelector('.knowledgeVersionDrawer')"), "Busy-job history drawer missing");
  await until(() => evaluate(`${deleteVersionButton(1)}.disabled`), "Active ingestion jobs must lock historical deletion");
  assert.equal(space.active_build_id, null);
  backgroundJob.status = "completed"; backgroundJob.stage = "ready"; backgroundJob.progress = 1;
  await evaluate(`${button("刷新状态")}.click()`);
  await until(() => evaluate(`!${deleteVersionButton(1)}.disabled`), "Completed ingestion jobs did not release the deletion lock");
  await evaluate("document.querySelector('[aria-label=\"关闭详情\"]').click()");

  // The advanced snapshot action is reserved for an unpublished pointer
  // state and always requires confirmation; it does not rebuild the index.
  space.active_version_id = null;
  documents[0].current_version_id = "unversioned-published-doc-0";
  const snapshotGeneration = space.active_generation_id;
  const snapshotManifest = documents.map((doc) => doc.current_version_id);
  await evaluate(`${button("刷新状态")}.click()`);
  await until(() => evaluate("document.querySelector('.spaceVersionIndicator')?.textContent==='有未保存变更'"), "Unrecorded published changes did not refresh");
  await fill("#space-version-note", "文档索引更新后的当前快照");
  await evaluate(`${historyButton}.click()`);
  await until(() => evaluate(`!!${button("创建当前索引快照")}&&!${button("创建当前索引快照")}.disabled`), "Unrecorded ready documents must allow the advanced snapshot action");
  const writesBeforeSnapshot = writes.length;
  await evaluate(`${button("创建当前索引快照")}.click()`);
  await until(() => evaluate(`!!${button("确认创建索引快照")}`), "Snapshot creation confirmation missing");
  assert.equal(await evaluate("!!document.querySelector('.knowledgeVersionDrawer')"), false, "Snapshot drawer must close before confirmation");
  assert.equal(writes.length, writesBeforeSnapshot, "Snapshot creation must wait for confirmation");
  assert.ok(await evaluate("document.querySelector('.confirmDialog').innerText.includes('不应用新的解析代码、切块规则或模型配置')"), "Snapshot confirmation must distinguish recording from rebuilding");
  await evaluate(`${button("返回")}.click()`);
  await until(() => evaluate("!document.querySelector('.confirmDialog')"), "Snapshot confirmation did not dismiss");
  assert.equal(writes.length, writesBeforeSnapshot);
  assert.equal(space.active_version_id, null);
  await evaluate(`${historyButton}.click()`);
  await until(() => evaluate("!!document.querySelector('.knowledgeVersionDrawer')"), "Snapshot history drawer did not reopen");
  await evaluate(`${button("创建当前索引快照")}.click()`);
  await until(() => evaluate(`!!${button("确认创建索引快照")}`), "Repeated snapshot confirmation missing");
  await evaluate(`${button("确认创建索引快照")}.click()`);
  await until(() => evaluate("document.querySelector('.spaceVersionIndicator')?.textContent==='当前v4'"), "Confirmed snapshot was not created");
  assert.equal(writes.length, writesBeforeSnapshot + 1);
  assert.equal(space.active_version_id, "v4");
  assert.equal(space.active_generation_id, snapshotGeneration, "Recording a snapshot must not switch the search generation");
  assert.deepEqual(documents.map((doc) => doc.current_version_id), snapshotManifest, "Recording a snapshot must not rebuild or replace document indexes");
  assert.equal(versions.at(-1).note, "文档索引更新后的当前快照");

  // Historical deletion is a confirmed soft removal; no document, chunk
  // or current-generation pointer may change.
  await evaluate(`${historyButton}.click()`);
  await until(() => evaluate("document.querySelectorAll('.knowledgeVersionDrawer .spaceVersionRow').length===4"), "Created snapshot did not appear in history");
  assert.equal(await evaluate(`${deleteVersionButton(4)}.disabled`), true, "Current snapshot deletion must remain disabled");
  assert.equal(await evaluate(`${button("创建当前索引快照")}.disabled`), true, "The saved snapshot must disable duplicate recording");
  const writesBeforeDelete = writes.length;
  await evaluate(`${deleteVersionButton(1)}.click()`);
  await until(() => evaluate(`!!${button("确认删除版本")}`), "Historical delete confirmation missing");
  assert.equal(await evaluate("!!document.querySelector('.knowledgeVersionDrawer')"), false, "History drawer must close before delete confirmation");
  assert.ok(await evaluate("!!document.querySelector('.confirmDialog-danger')"), "Historical deletion must use the danger confirmation");
  assert.ok(await evaluate("document.querySelector('.confirmDialog').innerText.includes('原始文档、切块与向量仍保留')"), "Deletion must explain that indexed source data is retained");
  assert.equal(writes.length, writesBeforeDelete, "Deletion must wait for confirmation");
  await evaluate(`${button("返回")}.click()`);
  await until(() => evaluate("!document.querySelector('.confirmDialog')"), "Delete confirmation did not dismiss");
  assert.equal(writes.length, writesBeforeDelete);
  assert.equal(versions[0].deleted_at, undefined);
  await evaluate(`${historyButton}.click()`);
  await until(() => evaluate("!!document.querySelector('.knowledgeVersionDrawer')"), "History drawer did not reopen after cancelled deletion");
  await evaluate(`${deleteVersionButton(1)}.click()`);
  await until(() => evaluate(`!!${button("确认删除版本")}`), "Repeated deletion confirmation missing");
  await evaluate(`${button("确认删除版本")}.click()`);
  await until(() => versions[0].deleted_at === now, "Confirmed deletion did not create the snapshot tombstone");
  await until(() => evaluate("document.querySelector('.knowledgeVersionHistoryButton')?.textContent.includes('（3）')"), "Deleted snapshot did not leave the public history count");
  assert.equal(writes.length, writesBeforeDelete + 1);
  assert.equal(space.active_version_id, "v4");
  assert.equal(space.active_generation_id, snapshotGeneration);
  assert.deepEqual(documents.map((doc) => doc.current_version_id), snapshotManifest, "Historical deletion must retain every active document pointer");
  assert.equal(versions.length, 4, "Soft deletion must retain the internal snapshot and its version number");
  await evaluate(`${historyButton}.click()`);
  await until(() => evaluate("document.querySelectorAll('.knowledgeVersionDrawer .spaceVersionRow').length===3"), "Filtered history drawer missing");
  assert.equal(await evaluate(`!!${versionRow(1)}`), false, "Deleted snapshots must not remain activatable in public history");
  assert.equal(await evaluate(`${deleteVersionButton(4)}.disabled`), true);
  await evaluate("document.querySelector('[aria-label=\"关闭详情\"]').click()");

  // Search is fully intercepted; controls, evidence and scores remain directly visible.
  await page.send("Page.navigate", { url: `${process.argv[2] ?? "http://localhost:3210"}/knowledge/lab` });
  await until(() => evaluate("!!document.querySelector('.ragParameterGrid')"), "Retrieval lab failed to load");
  for (const selector of ["#rag-result-limit", "#rag-candidate-limit", "#rag-rerank-limit", "#rag-rrf-k", "#rag-lexical-share", ".ragSwitch input", "#rag-query-expansion", "#rag-query-expansion-limit"]) assert.ok(await isRendered(`document.querySelector(${JSON.stringify(selector)})`), `${selector} must be directly visible`);
  assert.equal(await evaluate("document.querySelectorAll('.ragLab details').length"), 0, "Retrieval controls must not require disclosures");
  assert.equal(await evaluate("document.querySelector('#rag-query-expansion').checked"), true, "Bilingual expansion should default to enabled");
  assert.equal(await evaluate("document.querySelector('#rag-query-expansion-limit').value"), "2", "The default limit is two additional queries");
  await fill("#rag-query", "注意力机制的计算过程");
  await until(() => evaluate(`${button("运行检索")} && !${button("运行检索")}.disabled`), "Query input did not enable search");
  await evaluate(`${button("运行检索")}.click()`);
  await until(() => evaluate("document.querySelectorAll('.ragHit').length===2"), "Fixture search results missing");
  assert.equal(searchRequests.length, 1, "Search should use one intercepted fixture request");
  assert.equal(searchRequests[0].query, "注意力机制的计算过程");
  assert.equal(searchRequests[0].tuning.query_expansion_enabled, true);
  assert.equal(searchRequests[0].tuning.query_expansion_limit, 2);
  assert.ok(await isRendered("document.querySelector('.ragQueryExpansion')"), "Actual expansion variants must be directly visible");
  assert.deepEqual(await evaluate("[...document.querySelectorAll('.ragQueryExpansion li > p')].map(e=>e.textContent)"), ["注意力机制的计算过程", "注意力机制的计算过程 attention", "注意力机制的计算过程 attention mechanism"]);
  assert.equal(await evaluate("document.querySelector('.ragQueryExpansion li > span').textContent"), "原始问题", "The original query must stay first");
  assert.ok(await evaluate("document.querySelector('.ragQueryExpansion').innerText.includes('模型重排仍使用原始问题')"), "Recall aliases must not imply rewriting the rerank question");
  assert.ok(await evaluate("document.querySelector('.ragResultCaption').innerText.includes('最高原始分')"), "Expanded scores must explain their query-variant scope");
  assert.ok(await isRendered("document.querySelector('.ragResultParameters')"), "Applied parameters and generation must be visible");
  assert.ok(await evaluate("document.querySelector('.ragResultParameters').innerText.includes('generation-old')"), "Search must show the currently activated generation");
  assert.equal(await evaluate("document.querySelectorAll('.ragScoreRows > div').length"), 8, "Every hit must display all four score labels");
  assert.ok(await evaluate("[...document.querySelectorAll('.ragScoreRows')].every(e=>e.getClientRects().length && !e.closest('details'))"), "Scores must not require hidden disclosures");
  assert.ok(await evaluate("document.querySelectorAll('.ragScoreRows')[1].innerText.includes('未记录')"), "Missing rerank score must remain explicit");
  assert.ok(await evaluate("document.querySelector('.ragHitHeading small').innerText.includes('第 3 页')"), "Evidence must preserve page location");
  await evaluate(`${button("查看完整原文")}.click()`);
  await until(() => evaluate("!!document.querySelector('.ragHitFullText')"), "Explicit full text action did not expand evidence");
  assert.ok(await evaluate("document.querySelector('.ragHitFullText').innerText.includes('邻近上下文')"), "Expanded evidence must include adjacent context");
  await evaluate(`${button("收起原文")}.click()`);
  await until(() => evaluate("!document.querySelector('.ragHitFullText')"), "Explicit full text action did not collapse evidence");
  await fill("#rag-rrf-k", "80");
  await until(() => evaluate("!!document.querySelector('.ragStaleNotice')"), "Parameter editing must mark the existing result stale");
  assert.equal(searchRequests.length, 1, "Parameter editing must not start an implicit search");

  // Bilingual expansion is a per-query tuning control, never an implicit request.
  const beforeExpansionLimit = searchRequests.length;
  await fill("#rag-query-expansion-limit", "4");
  await until(() => evaluate("document.querySelector('#rag-query-expansion-limit').value==='4'"), "Expansion limit did not update");
  await wait(120);
  assert.equal(searchRequests.length, beforeExpansionLimit, "Changing the expansion limit must not issue a search");
  assert.ok(await evaluate("!!document.querySelector('.ragStaleNotice')"), "Expansion limit changes must identify stale results");
  await evaluate(`${button("运行检索")}.click()`);
  await until(() => searchRequests.length === beforeExpansionLimit + 1, "Expansion limit was not submitted");
  await until(() => evaluate("document.querySelectorAll('.ragQueryExpansion li').length===5&&!document.querySelector('.ragStaleNotice')"), "Four variants should accompany the unchanged original query");
  assert.equal(searchRequests.at(-1).tuning.query_expansion_limit, 4);
  assert.ok(await evaluate("document.querySelector('.ragQueryExpansion header').innerText.includes('4条扩展查询')"));

  const beforeInvalidLimit = searchRequests.length;
  await fill("#rag-query-expansion-limit", "5");
  await until(() => evaluate(`!!document.querySelector('.ragValidation')&&${button("运行检索")}.disabled`), "Expansion limit above four must be invalid");
  assert.equal(searchRequests.length, beforeInvalidLimit);
  await fill("#rag-query-expansion-limit", "1");
  await until(() => evaluate(`!${button("运行检索")}.disabled`), "A single additional query must be accepted");
  await evaluate(`${button("运行检索")}.click()`);
  await until(() => searchRequests.length === beforeInvalidLimit + 1, "Minimum expansion limit was not submitted");
  await until(() => evaluate("document.querySelectorAll('.ragQueryExpansion li').length===2&&!document.querySelector('.ragStaleNotice')"), "Minimum limit must show the original plus one expansion");
  assert.equal(searchRequests.at(-1).tuning.query_expansion_limit, 1);

  const beforeExpansionToggle = searchRequests.length;
  await evaluate("document.querySelector('#rag-query-expansion').click()");
  await until(() => evaluate("document.querySelector('#rag-query-expansion-limit').disabled&&!!document.querySelector('.ragStaleNotice')"), "Disabling expansion must disable its limit and mark existing results stale");
  await wait(120);
  assert.equal(searchRequests.length, beforeExpansionToggle, "Toggling expansion must not issue a search");
  await evaluate(`${button("运行检索")}.click()`);
  await until(() => searchRequests.length === beforeExpansionToggle + 1, "Disabled expansion setting was not submitted");
  await until(() => evaluate("document.querySelector('.ragQueryExpansion header').innerText.includes('本次关闭')&&!document.querySelector('.ragStaleNotice')"), "Disabled expansion must be distinguished from a terminology miss");
  assert.equal(searchRequests.at(-1).tuning.query_expansion_enabled, false);
  assert.equal(await evaluate("document.querySelectorAll('.ragQueryExpansion li').length"), 1, "Disabled expansion must retain only the original query");
  assert.equal(await evaluate("document.querySelector('.ragResultCaption').innerText.includes('最高原始分')"), false, "Original-query scores must not claim a variant scope");

  const beforeNoMatch = searchRequests.length;
  await evaluate("document.querySelector('#rag-query-expansion').click()");
  await fill("#rag-query", "火星  岩石\n的成分如何判定");
  await wait(120);
  assert.equal(searchRequests.length, beforeNoMatch, "Editing the query and expansion switch must not auto-search");
  await evaluate(`${button("运行检索")}.click()`);
  await until(() => searchRequests.length === beforeNoMatch + 1, "Unmatched query was not submitted");
  await until(() => evaluate("document.querySelector('.ragQueryExpansion header').innerText.includes('未追加查询，保留原始问题')&&!document.querySelector('.ragStaleNotice')"), "Enabled expansion without additional queries must retain the original question explicitly");
  assert.equal(await evaluate("document.querySelectorAll('.ragQueryExpansion li').length"), 1);
  assert.equal(await evaluate("document.querySelector('.ragQueryExpansion li > p').textContent"), "火星 岩石 的成分如何判定", "Whitespace normalization alone must not create a second query variant");
  assert.equal(searchRequests.at(-1).tuning.query_expansion_enabled, true);

  // Server-normalized variants are authoritative: a raw input containing
  // newlines or repeated spaces must not be counted as an extra query.
  const beforeWhitespaceExpansion = searchRequests.length;
  await fill("#rag-query", "注意力机制  的\n计算过程");
  await fill("#rag-query-expansion-limit", "2");
  await wait(120);
  assert.equal(searchRequests.length, beforeWhitespaceExpansion, "Normalizing display must not start an implicit search");
  await evaluate(`${button("运行检索")}.click()`);
  await until(() => searchRequests.length === beforeWhitespaceExpansion + 1, "Mixed-whitespace query was not submitted");
  await until(() => evaluate("document.querySelector('.ragQueryExpansion header').innerText.includes('2条扩展查询')&&!document.querySelector('.ragStaleNotice')"), "Expansion count must use the executed variants, not the raw input");
  assert.equal(searchRequests.at(-1).query, "注意力机制  的\n计算过程", "The original submitted question must not be silently rewritten by the UI");
  assert.deepEqual(await evaluate("[...document.querySelectorAll('.ragQueryExpansion li > p')].map(e=>e.textContent)"), ["注意力机制 的 计算过程", "注意力机制 的 计算过程 attention", "注意力机制 的 计算过程 attention mechanism"]);
  assert.equal(await evaluate("document.querySelectorAll('.ragQueryExpansion li').length"), 3, "A normalized original must appear once, followed only by actual expansions");
  assert.equal(await evaluate("document.querySelector('.ragQueryExpansion li > span').textContent"), "原始问题");
  await fill("#rag-query", "注意力机制的计算过程");
  await fill("#rag-query-expansion-limit", "4");

  // The slider measures keyword share; visible ratios,
  // assistive text and the request weights must agree at every tested value.
  assert.equal(await evaluate("document.querySelector('.ragWeightEndpoints')"), null, "Recall balance should retain its original compact display");
  assert.equal(await evaluate("document.querySelector('.ragWeightValues')"), null, "Recall percentages should not become a separate description list");
  assert.equal(await evaluate("document.querySelector('#rag-lexical-share').getAttribute('aria-label')"), "关键词召回占比");
  assert.equal(await evaluate("document.querySelector('#rag-lexical-share').getAttribute('aria-describedby')"), "rag-weight-hint");
  for (const lexicalShare of [20, 80, 0, 100, 50]) {
    const semanticShare = 100 - lexicalShare;
    const beforeChange = searchRequests.length;
    await fill("#rag-lexical-share", String(lexicalShare));
    const expectedText = `关键词${lexicalShare}%，语义${semanticShare}%`;
    await until(() => evaluate(`document.querySelector('#rag-lexical-share').getAttribute('aria-valuetext')===${JSON.stringify(expectedText)}`), `Slider assistive text did not update for ${lexicalShare}/${semanticShare}`);
    assert.equal(await evaluate("document.querySelector('#rag-lexical-share').value"), String(lexicalShare));
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.ragWeightControl > div span')].map(e=>e.textContent.replace(/\\s+/g,''))"), [`关键词${lexicalShare}%`, `语义${semanticShare}%`]);
    await wait(120);
    assert.equal(searchRequests.length, beforeChange, "Changing a recall weight must not start an implicit search");
    assert.ok(await evaluate("!!document.querySelector('.ragStaleNotice')"), "Weight changes must identify the existing result as stale");
    await evaluate(`${button("运行检索")}.click()`);
    await until(() => searchRequests.length === beforeChange + 1, "Applying recall weights must issue exactly one request");
    await until(() => evaluate("!document.querySelector('.ragStaleNotice')"), "Updated recall weights did not refresh the result snapshot");
    const applied = searchRequests.at(-1);
    assert.equal(applied.tuning.lexical_weight, lexicalShare / 50, `Keyword weight reversed at ${lexicalShare}/${semanticShare}`);
    assert.equal(applied.tuning.vector_weight, semanticShare / 50, `Semantic weight reversed at ${lexicalShare}/${semanticShare}`);
    assert.equal(applied.tuning.rrf_k, 80, "Weight editing must preserve the remaining retrieval parameters");
    assert.equal(applied.space, space.slug);
  }

  // Native keyboard interaction verifies the real left/right direction,
  // independently of assigning the slider value through the DOM setter.
  const beforeArrowKeys = searchRequests.length;
  await evaluate("document.querySelector('#rag-lexical-share').focus()");
  for (const type of ["keyDown", "keyUp"]) await page.send("Input.dispatchKeyEvent", { type, key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39, nativeVirtualKeyCode: 39 });
  await until(() => evaluate("document.querySelector('#rag-lexical-share').value==='55'"), "Moving right must increase the keyword share");
  assert.deepEqual(await evaluate("[...document.querySelectorAll('.ragWeightControl > div span')].map(e=>e.textContent.replace(/\\s+/g,''))"), ["关键词55%", "语义45%"]);
  for (const type of ["keyDown", "keyUp"]) await page.send("Input.dispatchKeyEvent", { type, key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37, nativeVirtualKeyCode: 37 });
  await until(() => evaluate("document.querySelector('#rag-lexical-share').value==='50'"), "Moving left must increase the semantic share");
  assert.equal(searchRequests.length, beforeArrowKeys, "Arrow-key edits must not start an implicit search");

  const beforeRestore = searchRequests.length;
  await evaluate(`${button("恢复默认")}.click()`);
  await until(() => evaluate("document.querySelector('#rag-lexical-share').value==='50'&&document.querySelector('#rag-rrf-k').value==='60'&&document.querySelector('#rag-query-expansion').checked&&document.querySelector('#rag-query-expansion-limit').value==='2'"), "Restoring defaults must reset recall balance, RRF constant and bilingual expansion");
  assert.deepEqual(await evaluate("[...document.querySelectorAll('.ragWeightControl > div span')].map(e=>e.textContent.replace(/\\s+/g,''))"), ["关键词50%", "语义50%"]);
  assert.equal(await evaluate("document.querySelector('#rag-lexical-share').getAttribute('aria-valuetext')"), "关键词50%，语义50%");
  await wait(120);
  assert.equal(searchRequests.length, beforeRestore, "Restoring settings must not silently issue a search");
  assert.ok(await evaluate("!!document.querySelector('.ragStaleNotice')"), "Restoring defaults must preserve the previous result while marking it stale");
  await evaluate(`${button("运行检索")}.click()`);
  await until(() => searchRequests.length === beforeRestore + 1, "Restored defaults were not submitted");
  await until(() => evaluate("!document.querySelector('.ragStaleNotice')"), "Restored result snapshot did not settle");
  assert.deepEqual(searchRequests.at(-1).tuning, { lexical_weight: 1, vector_weight: 1, rrf_k: 60, candidate_limit: 40, rerank_limit: 20, rerank_enabled: true, query_expansion_enabled: true, query_expansion_limit: 2 });
  assert.equal(await evaluate("document.querySelectorAll('.ragQueryExpansion li').length"), 3, "Restored expansion should show the original plus two variants");
  writeFileSync(path.join(output, "retrieval-desktop.png"), Buffer.from((await page.send("Page.captureScreenshot")).data, "base64"));
  await page.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await wait(200);
  assert.ok(await evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Retrieval lab overflows on mobile");
  writeFileSync(path.join(output, "retrieval-mobile.png"), Buffer.from((await page.send("Page.captureScreenshot")).data, "base64"));
  assert.deepEqual(failures, []);
  console.log(`Knowledge browser checks passed: main rebuild without duplicate snapshots, staged build and job locking, confirmed cancellation/retry/release, history activation, advanced confirmed snapshots, confirmed soft deletion without index changes, RAG parameters/scores/evidence, bilingual expansion defaults/limits/toggle/no-match/visible original-first variants/no implicit search, recall UI/ARIA/request mapping at 20/80, 80/20, 0/100, 100/0 and 50/50, native arrow-key direction, explicit search/default restoration, mobile layout. Artifacts: ${output}`);
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
