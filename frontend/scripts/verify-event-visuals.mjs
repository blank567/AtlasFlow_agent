import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const visualSource = readFileSync(fileURLToPath(new URL("../lib/event-visuals.ts", import.meta.url)), "utf8");
const compiled = ts.transpileModule(visualSource, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const exports = {};
new Function("exports", compiled)(exports);
const { EVENT_VISUALS, eventVisual } = exports;

const schema = readFileSync(fileURLToPath(new URL("../../backend/src/atlasflow/schemas.py", import.meta.url)), "utf8");
const eventEnum = schema.split("class RunEventType(StrEnum):")[1]?.split("\n\n")[0];
assert.ok(eventEnum, "backend event enum must remain discoverable");
const backendTypes = [...eventEnum.matchAll(/^\s+[A-Z_]+\s*=\s*"([^"]+)"/gm)].map((match) => match[1]);
assert.deepEqual(Object.keys(EVENT_VISUALS).sort(), backendTypes.sort(), "every backend event type needs an explicit visual identity");
assert.equal(new Set(Object.values(EVENT_VISUALS).map((visual) => visual.color)).size, backendTypes.length, "each event type needs a distinct color");
for (const type of backendTypes) {
  const visual = eventVisual(type);
  assert.match(visual.color, /^#[0-9a-f]{6}$/i);
  assert.match(visual.tint, /^#[0-9a-f]{6}$/i);
  assert.ok(visual.label, `${type} must have a readable label`);
}
assert.deepEqual(eventVisual("future_event"), eventVisual("future_event"), "unknown event color must stay stable");
console.log(`Event visual mapping covers ${backendTypes.length} unique event types.`);
