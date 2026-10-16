// Offline component rendering: no backend, model requests, or generated files.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import React from "react";
import * as jsxRuntime from "react/jsx-runtime";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const source = readFileSync(fileURLToPath(new URL("../components/retrieval-controls.tsx", import.meta.url)), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const exports = {};
new Function("exports", "require", compiled)(exports, name => {
  assert.equal(name, "react/jsx-runtime", `Unexpected component import: ${name}`);
  return jsxRuntime;
});
const { RetrievalControls, DEFAULT_RETRIEVAL_SETTINGS } = exports;
const render = (fixedFeatures, changes = {}) => renderToStaticMarkup(React.createElement(RetrievalControls, {
  settings: { ...DEFAULT_RETRIEVAL_SETTINGS, ...changes }, setSettings: () => {}, prefix: "test", fixedFeatures,
}));
for (const rerank_enabled of [false, true]) for (const query_expansion_enabled of [false, true]) {
  const settings = { rerank_enabled, query_expansion_enabled };
  const experiment = render(true, settings);
  assert.doesNotMatch(experiment, /ragSwitch|type="checkbox"|启用重排|中英查询扩展|由选中的对比配置决定/);
  assert.match(experiment, /id="test-query-expansion-limit"/);
  assert.match(experiment, /id="test-rerank-limit"/);
  const expansion = experiment.match(/<input[^>]*id="test-query-expansion-limit"[^>]*>/)?.[0];
  assert.equal(expansion.includes('disabled=""'), !query_expansion_enabled);
  const singleSearch = render(false, settings), toggles = singleSearch.match(/<input[^>]*type="checkbox"[^>]*>/g) ?? [];
  assert.equal(toggles.length, 2, "Single-query controls must keep both feature toggles");
  assert.ok(toggles.every(input => !input.includes("disabled")));
  assert.match(singleSearch, /启用重排/); assert.match(singleSearch, /中英查询扩展/);
  assert.equal(toggles[0].includes('checked=""'), rerank_enabled);
  assert.equal(toggles[1].includes('checked=""'), query_expansion_enabled);
}
console.log("Experiment feature switches removed; single-query toggles, actual settings and numeric controls preserved.");
