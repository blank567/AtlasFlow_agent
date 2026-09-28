// Run against an existing local frontend: node scripts/verify-report-browser.mjs http://localhost:3210
// Uses an isolated E: browser profile and intercepts API requests; never starts an LLM run.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
const require = createRequire(import.meta.url);
const { WebSocket } = require("next/dist/compiled/ws");
const workspace = path.resolve(fileURLToPath(new URL("../../", import.meta.url)));
assert.match(workspace, /^E:/i, "Browser artifacts must stay on E:");
const root = path.join(workspace, "build", "report-browser");
mkdirSync(root, { recursive: true });
const output = mkdtempSync(path.join(root, "check-"));
const binary = ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].find(existsSync);
assert.ok(binary, "Chrome or Edge is required for the optional visual smoke test");
const browser = spawn(binary, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-breakpad", "--disable-crash-reporter", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${path.join(output, "profile")}`, "about:blank"], {
  windowsHide: true, env: { ...process.env, TEMP: "E:\\codex\\tmp", TMP: "E:\\codex\\tmp" }, stdio: ["ignore", "ignore", "pipe"],
});
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(check, message) {
  for (let attempt = 0; attempt < 100; attempt++) { if (await check()) return; await wait(100); }
  throw new Error(message);
}
async function connect(endpoint) {
  const socket = new WebSocket(endpoint);
  await new Promise((resolve, reject) => { socket.once("open", resolve); socket.once("error", reject); });
  let sequence = 0;
  const pending = new Map();
  const events = new Map();
  socket.on("message", (data) => {
    const packet = JSON.parse(data);
    if (packet.id) {
      const callback = pending.get(packet.id);
      pending.delete(packet.id);
      if (callback) { clearTimeout(callback.timer); packet.error ? callback.reject(new Error(packet.error.message)) : callback.resolve(packet.result); }
    } else events.get(packet.method)?.(packet.params);
  });
  return {
    on(method, callback) { events.set(method, callback); },
    send(method, params = {}) { return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15_000);
      pending.set(id, { resolve, reject, timer });
      socket.send(JSON.stringify({ id, method, params }));
    }); },
    close() { socket.close(); },
  };
}
const report = `## 行程概览

从北京大学出发，串联 **颐和园、圆明园和天安门广场**，最后返回学校。

> 本文为排版测试数据，不是实时旅游建议。景区开放与预约要求请以官方公告为准。

### 推荐节奏

上午安排园林游览，下午参观遗址，傍晚前往城市广场。保留休息时间，不把行程排满。

| 时段 | 目的地 | 建议安排 | 注意事项 |
| --- | --- | --- | --- |
| 上午 | 颐和园 | 游览园林，沿湖步行 | 穿舒适的鞋 |
| 下午 | 圆明园遗址公园 | 遗址与展陈 | 留意闭园时间 |
| 傍晚 | 天安门广场 | 城市景观 | 提前核实预约 |

## 导航与出行

1. 确认每段导航的出入口。
2. 根据当日天气与体力调整停留时间。
3. 返回学校前预留交通缓冲。

[打开高德导航](https://uri.amap.com/navigation?from=test&to=test)

## 预算与局限

这里不编造实时票价。交通、门票与餐饮预算应在查询官方信息后补全。

### 来源说明

本页仅验证报告展示、目录与下载功能，未调用任何模型或地图服务。

## 来源说明

重复标题仍有独立的目录定位。代码块和长链接不会撑破正文。

\`\`\`python
# 这行不是目录标题
route = ["北京大学", "颐和园", "圆明园", "天安门广场", "北京大学"]
\`\`\`
` + "\n\n" + Array.from({ length: 32 }, (_, index) => `来源核对备注 ${index + 1}：仅用于验证长报告滚动时目录保持可见，界面不会把这段测试文本当作实时出行事实。`).join("\n\n");
const run = { id: "report-visual-check", query: "从北大出发的北京一日游", status: "completed", report, created_at: "2026-09-28T02:00:00Z", completed_at: "2026-09-28T02:03:00Z", warnings: [], metrics: { total_tasks: 3, successful_tasks: 3, model_calls: 8 }, events: [] };
let control, page;
const failures = [];
try {
  let logs = "";
  browser.stderr.on("data", (data) => { logs += data.toString(); });
  await until(() => /DevTools listening on (ws:\/\/[^\s]+)/.test(logs), "Browser did not start");
  control = await connect(logs.match(/DevTools listening on (ws:\/\/[^\s]+)/)[1]);
  const { targetId } = await control.send("Target.createTarget", { url: "about:blank" });
  const endpoint = new URL(logs.match(/DevTools listening on (ws:\/\/[^\s]+)/)[1]);
  page = await connect(`ws://${endpoint.host}/devtools/page/${targetId}`);
  await page.send("Page.enable");
  await page.send("Runtime.enable");
  page.on("Runtime.exceptionThrown", (event) => failures.push(event.exceptionDetails.text));
  page.on("Fetch.requestPaused", (event) => {
    const stream = event.request.url.includes("/events");
    const body = stream ? 'event: done\ndata: {}\n\n' : JSON.stringify(run);
    void page.send("Fetch.fulfillRequest", { requestId: event.requestId, responseCode: 200, responseHeaders: [
      { name: "Content-Type", value: stream ? "text/event-stream" : "application/json" },
      { name: "Access-Control-Allow-Origin", value: "*" },
    ], body: Buffer.from(body).toString("base64") }).catch((error) => failures.push(error.message));
  });
  await page.send("Fetch.enable", { patterns: [{ urlPattern: "*/api/v1/*" }] });
  await control.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: output });
  await page.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1080, deviceScaleFactor: 1, mobile: false });
  await page.send("Page.addScriptToEvaluateOnNewDocument", { source: "window.print = () => { window.__printRequested = true; };" });
  const evaluate = async (expression) => {
    const result = await page.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  await page.send("Page.navigate", { url: `${process.argv[2] ?? "http://localhost:3210"}/runs/report-visual-check` });
  await until(() => evaluate(`!![...document.querySelectorAll('[role="tab"]')].find(e=>e.textContent==='研究报告')`), "Run page did not load");
  await evaluate(`[...document.querySelectorAll('[role="tab"]')].find(e=>e.textContent==='研究报告').click()`);
  await until(() => evaluate("document.querySelectorAll('.reportOutline nav a').length === 6"), "Report TOC missing");
  assert.equal(await evaluate("document.querySelectorAll('.reportProse table').length"), 1);
  await evaluate("document.querySelector('.reportWorkspace').scrollIntoView()");
  await wait(250);
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.reportOutline')).position"), "sticky");
  await evaluate("window.scrollBy(0, 650)");
  await wait(200);
  const desktopOutlineTop = await evaluate("document.querySelector('.reportOutline').getBoundingClientRect().top");
  assert.ok(desktopOutlineTop >= 20 && desktopOutlineTop <= 35, `Desktop outline stopped sticking: ${desktopOutlineTop}`);
  await evaluate("document.querySelector('.reportWorkspace').scrollIntoView()");
  await wait(100);
  writeFileSync(path.join(output, "desktop.png"), Buffer.from((await page.send("Page.captureScreenshot")).data, "base64"));
  for (const [label, extension] of [["下载 Markdown", ".md"], ["下载 HTML", ".html"]]) {
    await evaluate(`[...document.querySelectorAll('.reportActions button')].find(e=>e.textContent===${JSON.stringify(label)}).click()`);
    await until(() => readdirSync(output).some((name) => name.endsWith(extension)), `Missing ${extension} download`);
  }
  const markdown = readFileSync(path.join(output, readdirSync(output).find((name) => name.endsWith(".md"))), "utf8");
  assert.equal(markdown.replace(/^\uFEFF/, ""), report);
  const html = readFileSync(path.join(output, readdirSync(output).find((name) => name.endsWith(".html"))), "utf8");
  assert.match(html, /reportPaper/); assert.doesNotMatch(html, /reportToolbar|<script/i);
  await evaluate(`[...document.querySelectorAll('.reportActions button')].find(e=>e.textContent==='打印 / 存为 PDF').click()`);
  assert.equal(await evaluate("window.__printRequested"), true);
  await page.send("Emulation.setEmulatedMedia", { media: "print" });
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.sidebar')).display"), "none");
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.reportToolbar')).display"), "none");
  assert.notEqual(await evaluate("getComputedStyle(document.querySelector('.reportPaper')).display"), "none");
  const pdf = await page.send("Page.printToPDF", { printBackground: true, preferCSSPageSize: true });
  const pdfBuffer = Buffer.from(pdf.data, "base64");
  assert.equal(pdfBuffer.subarray(0, 4).toString(), "%PDF");
  writeFileSync(path.join(output, "report.pdf"), pdfBuffer);
  await page.send("Emulation.setEmulatedMedia", { media: "screen" });
  await page.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await evaluate("document.querySelector('.reportWorkspace').scrollIntoView()");
  await wait(250);
  assert.equal(await evaluate("document.querySelector('.reportOutline details').open"), false);
  await evaluate("window.scrollBy(0, 420)");
  await wait(200);
  const mobileOutlineTop = await evaluate("document.querySelector('.reportOutline').getBoundingClientRect().top");
  assert.ok(mobileOutlineTop >= 58 && mobileOutlineTop <= 72, `Mobile outline stopped sticking: ${mobileOutlineTop}`);
  await evaluate("document.querySelector('.reportWorkspace').scrollIntoView()");
  assert.ok(await evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile page overflows horizontally");
  writeFileSync(path.join(output, "mobile.png"), Buffer.from((await page.send("Page.captureScreenshot")).data, "base64"));
  assert.deepEqual(failures, []);
  console.log(`Browser checks passed: sticky desktop/mobile TOC, real MD/HTML downloads, print isolation and PDF. Artifacts: ${output}`);
} finally {
  page?.close();
  if (control) { await control.send("Browser.close").catch(() => {}); control.close(); }
  browser.kill();
}
