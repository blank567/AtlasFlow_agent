import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import * as React from "react";
import * as jsx from "react/jsx-runtime";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function loadTs(path, imports = {}) {
  const source = readFileSync(fileURLToPath(new URL(path, import.meta.url)), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText;
  const exports = {};
  new Function("exports", "require", compiled)(exports, (name) => {
    assert.ok(name in imports, `Unexpected import: ${name}`);
    return imports[name];
  });
  return exports;
}

const documentUtils = loadTs("../lib/report-document.ts");
const { safeReportUrl, reportFilename, buildReportHtml, downloadReport } = documentUtils;
const { ReportView } = loadTs("../components/report-view.tsx", {
  react: React, "react/jsx-runtime": jsx, "react-markdown": ReactMarkdown, "remark-gfm": remarkGfm,
  "../lib/report-document": documentUtils,
  "../lib/format": { formatDate: () => "09/28 10:00:00" },
  "./ui": { EmptyState: ({ title }) => React.createElement("p", null, title) },
});
const fixture = {
  id: "test-report-20260928", query: "北京一日游：从北大出发，最后返回学校", status: "completed",
  created_at: "2026-09-28T02:00:00Z", warnings: [],
  report: "## 行程概览\n\n从北京大学出发，按 **颐和园、圆明园、天安门广场** 的顺序游览。\n\n> 这是界面测试样例，不是实时出行建议。\n\n| 时段 | 地点 | 安排 |\n| --- | --- | --- |\n| 上午 | 颐和园 | 园林漫步 |\n| 中午 | 圆明园 | 遗址参观 |\n| 傍晚 | 天安门广场 | 返回学校 |\n\n## 导航与来源\n\n[打开导航](https://uri.amap.com/navigation?from=test&to=test)\n\n## 导航与来源\n\n重复标题保留独立定位。\n\n```python\n# 不是标题\nprint('北京')\n```\n\n<script>alert('unsafe')</script>\n\n[危险链接](javascript:alert%281%29)\n\n![图](https://example.com/image.png)\n",
};

const rendered = renderToStaticMarkup(React.createElement(ReportView, { run: fixture }));
assert.match(rendered, /class="reportTableScroll"/);
assert.match(rendered, /id="report-section-1"/);
assert.match(rendered, /id="report-section-3"/);
assert.doesNotMatch(rendered, /id="report-section-4"/);
assert.match(rendered, /<blockquote>/);
assert.match(rendered, /noopener noreferrer/);
assert.doesNotMatch(rendered, /<script|<img|href="javascript:/i);
assert.match(rendered, /下载 Markdown/);
assert.match(rendered, /下载 HTML/);
assert.match(rendered, /打印 \/ 存为 PDF/);
const paper = rendered.match(/<article class="reportPaper"[\s\S]*?<\/article>/)?.[0];
assert.ok(paper);
const exported = buildReportHtml('<script>"test"</script>', paper);
assert.match(exported, /&lt;script&gt;&quot;test&quot;&lt;\/script&gt;/);
assert.match(exported, /Content-Security-Policy/);
assert.match(exported, /@media print/);
assert.match(exported, /class="reportPaper"/);
assert.doesNotMatch(exported, /<script|<button|reportToolbar|<link /i);
assert.match(exported, /https:\/\/uri.amap.com\/navigation\?from=test&amp;to=test/);
const draft = renderToStaticMarkup(React.createElement(ReportView, { run: { ...fixture, status: "failed" } }));
assert.match(draft, /尚未完成最终验收/);
const empty = renderToStaticMarkup(React.createElement(ReportView, { run: { ...fixture, report: "  " } }));
assert.match(empty, /报告尚未生成/);
assert.doesNotMatch(empty, /下载 HTML/);
for (const bad of ["javascript:alert(1)", "data:text/html,bad", "file:///C:/secret", "/settings", "//evil.example"]) assert.equal(safeReportUrl(bad), undefined);
for (const good of ["https://example.com", "http://example.com", "mailto:test@example.com", "#report-section-1"]) assert.equal(safeReportUrl(good), good);
assert.equal(reportFilename("", ""), "AtlasFlow-研究报告-export");
assert.doesNotMatch(reportFilename('北京:<>/\\|?*"\u0000', "id/"), /[<>:"/\\|?*\u0000]/);
assert.ok(reportFilename("长".repeat(200), "123456789").length < 90);

// Download the exact Markdown, retain Chinese characters and release object URLs.
let savedBlob, savedName, clicked = false, removed = false, revoked = false, appended = false, release;
const original = { document: globalThis.document, create: URL.createObjectURL, revoke: URL.revokeObjectURL, timeout: globalThis.setTimeout };
try {
  globalThis.document = { createElement: () => ({ set download(value) { savedName = value; }, click() { clicked = true; }, remove() { removed = true; } }), body: { appendChild() { appended = true; } } };
  URL.createObjectURL = (blob) => { savedBlob = blob; return "blob:test"; };
  URL.revokeObjectURL = () => { revoked = true; };
  globalThis.setTimeout = (callback) => { release = callback; };
  downloadReport(fixture.report, "北京.md", "text/markdown;charset=utf-8");
  assert.equal(savedName, "北京.md");
  assert.ok(clicked && removed && appended);
  assert.equal(await savedBlob.text(), fixture.report);
  assert.equal(revoked, false);
  release();
  assert.equal(revoked, true);
} finally {
  if (original.document === undefined) delete globalThis.document; else globalThis.document = original.document;
  URL.createObjectURL = original.create; URL.revokeObjectURL = original.revoke; globalThis.setTimeout = original.timeout;
}
console.log("Report rendering, Markdown preservation, HTML export, URL safety, draft state and download cleanup checks passed.");
