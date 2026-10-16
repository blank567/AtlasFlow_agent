import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import ts from "typescript";

function load(relative, imports = {}) {
  const source = readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const exports = {};
  new Function("exports", "require", compiled)(exports, name => {
    assert.ok(name in imports, `Unexpected import: ${name}`);
    return imports[name];
  });
  return exports;
}
const shared = load("../lib/evaluations-v2.ts", { "./api": { API_URL: "http://127.0.0.1:8000", request: () => { throw new Error("Unexpected network request"); } } });
const primary = load("../lib/evaluation-metrics.ts");
const { buildEvaluationChart, buildMultiGroupChart, multiGroupMetricIds, chartPosition, formatChartValue, formatChartDelta, formatPrimaryValue, formatPrimaryGap } = load("../lib/evaluation-chart.ts", { "./evaluations-v2": shared, "./evaluation-metrics": primary });

function fixture(changes = {}) {
  const metric = { baseline_mean: 0.5, candidate_mean: 0.75, mean_delta: 0.25,
    unit: "ratio", direction: "higher", scoring_unit: "case", paired: 2, planned: 3,
    not_comparable: 1, excluded_reasons: { candidate_degraded: 1 }, improved: 1, tied: 1,
    regressed: 0, unclassified: 0, ...changes };
  return { experiment_id: "fixture", kind: "rag", protocol: "rag-retrieval-v2", status: "completed",
    baseline_variant_id: "baseline", baseline_name: "基线", planned_pairs: 3,
    comparisons: [{ candidate_variant_id: "candidate", candidate_name: "候选", planned_pairs: 3,
      metrics: { anchor_coverage_at_5: metric }, pairs: [
        { case_id: "R01", trial: 1, metrics: { anchor_coverage_at_5: { baseline: 0, candidate: 0.5, delta: 0.5, status: "paired", outcome: "improved", reasons: [] } } },
        { case_id: "R02", trial: 1, metrics: { anchor_coverage_at_5: { baseline: 1, candidate: 1, delta: 0, status: "paired", outcome: "tied", reasons: [] } } },
        { case_id: "R03", trial: 1, metrics: { anchor_coverage_at_5: { baseline: 1, candidate: 1, delta: null, status: "excluded", outcome: null, reasons: ["candidate_degraded"] } } },
      ] }], limitations: ["Small-sample diagnostics, not statistical significance or a cross-domain overall score."] };
}
const build = comparison => buildEvaluationChart({ comparison, candidateId: "candidate", metricId: "anchor_coverage_at_5" });
const chart = build(fixture());
assert.deepEqual(chart.meanDomain, [0, 1]);
assert.deepEqual(chart.deltaDomain, [-0.5, 0.5]);
assert.equal(chart.rows[0].baseline, 0, "real zero remains valid");
assert.equal(chart.rows[1].delta, 0, "tied zero remains valid");
assert.equal(chart.rows[2].delta, null, "excluded values never become zero deltas");
assert.equal(chart.paired, 2); assert.equal(chart.planned, 3);
assert.match(chart.interpretation.join(""), /绝对差值\+0.250/);
assert.match(chart.interpretation.join(""), /候选降级1项/);
assert.match(chart.limitations.join(""), /不代表统计显著性/);
assert.equal(chartPosition(null, [0, 1]), null);
assert.equal(chartPosition(0, [0, 1]), 0);
assert.equal(chartPosition(0, [-1, 1]), 0.5);
assert.equal(chartPosition(1e308, [-1e308, 1e308]), 1);
assert.equal(chartPosition(Number.NaN, [0, 1]), null);
assert.equal(chartPosition(1, [0, 0]), null);
assert.equal(chartPosition(-3, [0, 1]), 0);
assert.equal(formatChartValue(0, "ratio"), "0.000");
assert.equal(formatPrimaryValue("anchor_coverage_at_5", 0.5, "ratio"), "50.0%");
assert.equal(formatPrimaryValue("anchor_hit_at_1", 0, "boolean"), "0.0%");
assert.equal(formatPrimaryValue("anchor_mrr_at_5", 0.25, "ratio"), "0.250");
assert.equal(formatPrimaryValue("anchor_coverage_at_5", null, "ratio"), "未评测");
assert.equal(formatPrimaryGap("anchor_coverage_at_5", 0.5, "ratio"), "50.0个百分点");
assert.equal(formatPrimaryGap("anchor_mrr_at_5", 0.25, "ratio"), "0.250");
assert.equal(formatPrimaryGap("retrieval_duration_ms", 2000, "ms"), "2.00秒");
assert.equal(formatPrimaryGap("known_cost", null, "USD"), "不可比较");
assert.notEqual(formatPrimaryValue("faithfulness", 1e-10, "ratio"), "0.0%", "Small nonzero rates must not become zero");
assert.deepEqual(Object.values(primary.PRIMARY_METRICS).map(keys => keys.length), [5, 8, 8, 7]);
for (const keys of Object.values(primary.PRIMARY_METRICS)) {
  assert.equal(keys.length, new Set(keys).size);
  assert.ok(!keys.some(key => /^source_coverage|ndcg|citation_validity|answer_present|evidence_count/.test(key)), "Saturated or unlabelled diagnostics must not fill primary slots");
}
const primaryFixture = { protocol: "rag-retrieval-v2", results: [{ metrics: { anchor_coverage_at_5: {}, anchor_mrr_at_5: {}, source_coverage_at_5: {}, ndcg_at_5: {} } }] };
assert.deepEqual(primary.primaryMetricIds(primaryFixture), ["anchor_coverage_at_5", "anchor_mrr_at_5"], "Historical missing metrics stay absent rather than inventing scores or diagnostic fallbacks");
const reversedScores = structuredClone(primaryFixture); reversedScores.results[0].metrics.anchor_coverage_at_5.value = 0;
assert.deepEqual(primary.primaryMetricIds(reversedScores), primary.primaryMetricIds(primaryFixture), "Primary selection cannot depend on measured gaps");
assert.equal(formatChartValue(null), "未评测");
assert.equal(formatChartValue(1000, "ms"), "1.00秒");
assert.equal(formatChartValue(0.01, "ms"), "0.01毫秒");
assert.equal(formatChartValue(0.00001, "usd"), "$0.00001");
assert.equal(formatChartValue(0.00001, "USD"), "$0.00001");
assert.equal(formatChartValue(0.2, "USD"), "$0.2000");
assert.equal(shared.metricValue(0.2, "USD"), "$0.2000");
assert.match(shared.metricDescription("trajectory_compliance"), /全部必需.*不是单场景的断言平均分/);
assert.match(shared.metricDescription("tool_policy_compliance"), /全部必需.*不是单场景的断言平均分/);
assert.equal(formatChartValue(0.00001, "ratio"), "0.00001");
assert.equal(formatChartDelta(-1000, "ms"), "−1.00秒");
assert.equal(formatChartDelta(0.2, "usd"), "+$0.2000");
assert.equal(formatChartDelta(null), "不可比较");
assert.equal(formatChartDelta(Infinity), "不可比较");
assert.match(build(fixture({ direction: "lower", mean_delta: -0.25 })).interpretation.join(""), /越低越好.*改善/);
assert.match(build(fixture({ direction: "lower", mean_delta: 0.25 })).interpretation.join(""), /越低越好.*退化/);
assert.match(build(fixture({ direction: "neutral" })).interpretation.join(""), /不按数值大小判定/);
assert.match(build(fixture({ mean_delta: 0 })).interpretation.join(""), /持平.*抵消/);
assert.match(build(fixture({ mean_delta: 1e-12, improved: 0, tied: 2 })).interpretation.join(""), /持平.*抵消/);
assert.equal(build(fixture({ unit: "USD" })).unit, "usd");
const missing = build(fixture({ baseline_mean: null, candidate_mean: null, mean_delta: null, paired: 0 }));
assert.equal(missing.baselineMean, null); assert.equal(missing.candidateMean, null);
assert.match(missing.interpretation.join(""), /未评测不等于0/);
assert.equal(build(fixture({ paired: 0, baseline_mean: 0, candidate_mean: 0 })).baselineMean, null);
assert.equal(build(fixture({ baseline_mean: Infinity })).baselineMean, null);
const large = build(fixture({ baseline_mean: 1e308, candidate_mean: 1e308, mean_delta: 0, unit: "count" }));
assert.ok(large.meanDomain.every(Number.isFinite));
assert.equal(buildEvaluationChart({ comparison: fixture(), candidateId: "missing", metricId: "anchor_coverage_at_5" }), null);
assert.equal(buildEvaluationChart({ comparison: fixture(), candidateId: "candidate", metricId: "missing" }), null);
const multi = fixture(); multi.comparisons.push({ ...multi.comparisons[0], candidate_variant_id: "other", candidate_name: "另一个候选", metrics: { anchor_coverage_at_5: { ...multi.comparisons[0].metrics.anchor_coverage_at_5, baseline_mean: 0.2, candidate_mean: 0.3, mean_delta: 0.1 } } });
assert.equal(buildEvaluationChart({ comparison: multi, candidateId: "other", metricId: "anchor_coverage_at_5" }).baselineMean, 0.2, "never borrow a different candidate's paired baseline");
const bools = fixture(); bools.comparisons[0].pairs[0].metrics.anchor_coverage_at_5.baseline = false;
assert.equal(build(bools).rows[0].baseline, 0);
console.log("Evaluation chart domains, paired subsets, zero/null, units, direction and evidence-based explanations passed.");

function metric(value, changes = {}) {
  return { value, status: "evaluated", unit: "ratio", direction: "higher", scoring_unit: "case",
    grader_id: "fixture-rules", grader_version: "2", evidence_refs: [], ...changes };
}
function groupFixture() {
  const variants = ["A", "B", "C"].map((variant_id, index) => ({ variant_id, name: `第${index + 1}组` }));
  const results = variants.flatMap((variant, index) => [
    { case_id: "Q1", variant_id: variant.variant_id, trial: 1, status: "completed", degraded: [], metrics: {
      anchor_coverage_at_5: metric(index / 2), retrieval_duration_ms: metric([1000, 500, 1500][index], { unit: "ms", direction: "lower" }),
      known_cost: metric([.00001, .00002, 0][index], { unit: "USD", direction: "lower" }),
      tool_executions: metric(index, { unit: "count", direction: "neutral" }),
      ndcg_at_5: metric(null, { status: "missing_gold" }),
    } },
    { case_id: "Q2", variant_id: variant.variant_id, trial: 1, status: "completed", degraded: index === 1 ? ["semantic_lane_unavailable"] : [], metrics: {
      anchor_coverage_at_5: metric([1, 1, 0][index]), retrieval_duration_ms: metric([100, 9999, 5000][index], { unit: "ms", direction: "lower" }),
      known_cost: metric(1, { unit: "USD", direction: "lower" }),
      tool_executions: metric(100, { unit: "count", direction: "neutral" }),
      ndcg_at_5: metric(null, { status: "missing_gold" }),
    } },
  ]);
  return { experiment_id: "multi-fixture", kind: "rag", protocol: "rag-retrieval-v2", status: "completed",
    plan: { variants, case_ids: ["Q1", "Q2"], repetitions: 1 }, results,
    summary: { variants: { A: { metrics: { absent_summary_metric: { mean: 999 } } } } } };
}
const allGroups = (experiment, metricId = "anchor_coverage_at_5") => buildMultiGroupChart({ experiment, metricId });
const all = allGroups(groupFixture());
assert.equal(all.planned, 2); assert.equal(all.commonEvaluated, 1); assert.equal(all.excluded, 1);
assert.deepEqual(all.groups.map(group => group.id), ["A", "B", "C"]);
assert.deepEqual(all.groups.map(group => group.colorIndex), [0, 1, 2]);
assert.deepEqual(all.groups.map(group => group.value), [0, .5, 1], "all means use Q1, not unrelated per-group subsets or pair means");
assert.deepEqual(all.groups.map(group => group.available), [2, 1, 2]);
assert.ok(all.groups.every(group => group.planned === 2 && group.common === 1 && group.excluded === 1));
assert.ok(all.groups.every(group => Object.keys(group.excludedReasons).some(reason => /降级/.test(reason))));
assert.deepEqual(all.domain, [0, 1]);
assert.deepEqual(all.rows[0].values.map(value => value.value), [0, .5, 1]);
assert.equal(all.rows[1].status, "excluded");
assert.deepEqual(all.rows[1].values.map(value => value.value), [null, null, null], "non-common observations are never plotted as if compared");
assert.deepEqual(all.rows[1].values.map(value => value.rawValue), [1, 1, 0], "excluded observations remain diagnostic only");
assert.match(all.interpretation.join(""), /不拼接不同组各自可评/);
assert.match(all.interpretation.join(""), /不等于整体任务质量提升/);
assert.match(all.limitations.join(""), /不代表统计显著性/);
assert.deepEqual(all.range.preferredGroupIds, ["C"]);
assert.equal(all.range.spread, 1);
assert.equal(multiGroupMetricIds(groupFixture())[0], "anchor_coverage_at_5");
assert.ok(!multiGroupMetricIds(groupFixture()).includes("absent_summary_metric"), "metric options must originate from actual result fields");

const timing = allGroups(groupFixture(), "retrieval_duration_ms");
assert.deepEqual(timing.groups.map(group => group.value), [1000, 500, 1500]);
assert.deepEqual(timing.domain, [0, 2000], "excluded 9999ms and 5000ms observations must not expand the common chart axis");
assert.equal(timing.directionLabel, "越低越好"); assert.deepEqual(timing.range.preferredGroupIds, ["B"]);
assert.match(timing.interpretation.join(""), /均值较低的组为第2组/);
const currency = allGroups(groupFixture(), "known_cost");
assert.equal(currency.unit, "usd"); assert.equal(formatChartValue(currency.groups[0].value, currency.unit), "$0.00001");
assert.equal(currency.groups[2].value, 0, "known zero cost is not missing");
const neutral = allGroups(groupFixture(), "tool_executions");
assert.deepEqual(neutral.range.preferredGroupIds, []); assert.match(neutral.interpretation.join(""), /不按数值大小判定/);
const noGold = allGroups(groupFixture(), "ndcg_at_5");
assert.equal(noGold.commonEvaluated, 0); assert.ok(noGold.groups.every(group => group.value === null));
assert.ok(noGold.rows.every(row => row.values.every(value => value.value === null)));
assert.match(noGold.interpretation.join(""), /不能判断哪组效果更好/);
assert.equal(allGroups(groupFixture(), "absent_metric"), null);

const failed = groupFixture(); failed.results.find(row => row.case_id === "Q1" && row.variant_id === "C").status = "failed";
assert.equal(allGroups(failed).commonEvaluated, 0, "a saved numeric score in a failed cell is not common quality evidence");
const nullValue = groupFixture(); nullValue.results[0].metrics.anchor_coverage_at_5.value = null;
assert.equal(allGroups(nullValue).commonEvaluated, 0);
const infinite = groupFixture(); infinite.results[0].metrics.anchor_coverage_at_5.value = Infinity;
assert.equal(allGroups(infinite).commonEvaluated, 0);
const booleanValue = groupFixture(); booleanValue.results[0].metrics.anchor_coverage_at_5.value = false;
assert.equal(allGroups(booleanValue).groups[0].value, 0);
const differentUnit = groupFixture(); differentUnit.results[0].metrics.anchor_coverage_at_5.unit = "count";
assert.equal(allGroups(differentUnit).commonEvaluated, 0);
assert.match(allGroups(differentUnit).rows[0].reasons.join(""), /评分器版本不一致/);
const missingContract = groupFixture(); delete missingContract.results[0].metrics.anchor_coverage_at_5.grader_version;
assert.equal(allGroups(missingContract).commonEvaluated, 0);
const acrossCells = groupFixture(); acrossCells.results.forEach(row => {
  row.degraded = [];
  if (row.case_id === "Q2") row.metrics.anchor_coverage_at_5.grader_version = "different-contract";
});
assert.equal(allGroups(acrossCells).commonEvaluated, 0, "matching within a cell does not permit averaging different graders across cells");
const excludedFirst = groupFixture(); excludedFirst.results.forEach(row => {
  if (row.case_id === "Q1") { row.degraded = ["excluded"]; row.metrics.anchor_coverage_at_5.unit = "count"; }
  else row.degraded = [];
});
assert.equal(allGroups(excludedFirst).unit, "ratio", "chart units must come from common evidence, not an excluded first row");

const repeats = groupFixture(); repeats.plan.repetitions = 2;
for (const [index, variant] of repeats.plan.variants.entries()) repeats.results.push({
  ...repeats.results.find(row => row.case_id === "Q1" && row.variant_id === variant.variant_id),
  trial: 2, metrics: { anchor_coverage_at_5: metric([.2, .4, .6][index]) },
});
repeats.results.reverse();
assert.equal(allGroups(repeats).planned, 4); assert.equal(allGroups(repeats).commonEvaluated, 2);
for (const [index, expected] of [.1, .45, .8].entries()) assert.ok(Math.abs(allGroups(repeats).groups[index].value - expected) < 1e-12);
assert.deepEqual(allGroups(repeats).rows.map(row => [row.caseId, row.trial]), [["Q1", 1], ["Q1", 2], ["Q2", 1], ["Q2", 2]]);
const duplicate = groupFixture(); duplicate.results.push(duplicate.results[0]);
assert.equal(allGroups(duplicate).commonEvaluated, 0, "duplicate cells must not silently choose first or last");
const unknownDirection = groupFixture(); unknownDirection.results.forEach(row => { row.metrics.anchor_coverage_at_5.direction = "unknown-direction"; });
assert.equal(allGroups(unknownDirection).direction, "neutral"); assert.deepEqual(allGroups(unknownDirection).range.preferredGroupIds, []);
const agent = groupFixture(); agent.kind = "agent"; agent.results.forEach(row => { row.metrics.task_success = metric(row.variant_id === "A" ? false : true, { unit: "boolean" }); });
assert.equal(multiGroupMetricIds(agent)[0], "task_success");
assert.deepEqual(allGroups(agent, "task_success").groups.map(group => group.value), [0, 1, 1]);

const extreme = groupFixture(); extreme.results.forEach(row => {
  row.degraded = []; row.metrics.anchor_coverage_at_5 = metric(Number.MAX_VALUE, { unit: "count" });
});
assert.ok(allGroups(extreme).groups.every(group => Number.isFinite(group.value) && group.value === Number.MAX_VALUE));
assert.ok(allGroups(extreme).domain.every(Number.isFinite));
const subnormal = groupFixture(); subnormal.results.forEach(row => {
  row.degraded = []; row.metrics.anchor_coverage_at_5 = metric(Number.MIN_VALUE, { unit: "count" });
});
assert.ok(allGroups(subnormal).groups.every(group => group.value === Number.MIN_VALUE), "tiny real observations must not underflow to fake zero means");
console.log("All-group common cohorts, shared axes, coverage, contracts, repeat alignment, USD, direction and null/zero checks passed.");
