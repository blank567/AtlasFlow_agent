import {
  comparisonReason, evaluationLimitation, metricDescription, metricName, metricValue, stateName,
} from "./evaluations-v2";
import type { EvaluationExperiment, EvaluationMetric, EvaluationResult, VariantComparison } from "./evaluations-v2";
import { isRateMetric } from "./evaluation-metrics";

export type EvaluationChartRow = {
  caseId: string; trial: number; label: string;
  baseline: number | null; candidate: number | null; delta: number | null;
  status: string; outcome: string | null; reasons: string[];
};
export type EvaluationChartModel = {
  metricId: string; title: string; description: string;
  unit: string; direction: string; directionLabel: string; scoringUnit: string;
  baselineLabel: string; candidateLabel: string;
  baselineMean: number | null; candidateMean: number | null; meanDelta: number | null;
  planned: number; paired: number; excluded: number;
  counts: { improved: number; tied: number; regressed: number; unclassified: number };
  meanDomain: [number, number]; deltaDomain: [number, number];
  rows: EvaluationChartRow[]; interpretation: string[]; limitations: string[];
};

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const numeric = (value: unknown): number | null => typeof value === "boolean" ? Number(value) : finite(value) ? value : null;

export function formatChartValue(value: number | null, unit?: string): string {
  unit = unit?.toLowerCase();
  if (value === null || !finite(value)) return "未评测";
  // Keep a real small value distinct from an exact zero in labels and axes.
  if (value !== 0 && unit === "ms" && Math.abs(value) < 10) return `${Number(value.toPrecision(3))}毫秒`;
  if (value !== 0 && unit === "usd" && Math.abs(value) < 0.00005) return `$${Number(value.toPrecision(3))}`;
  if (value !== 0 && unit !== "ms" && unit !== "usd" && Math.abs(value) < 0.0005) return String(Number(value.toPrecision(3)));
  return metricValue(value, unit);
}

export function formatChartDelta(value: number | null, unit?: string): string {
  if (value === null || !finite(value)) return "不可比较";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return sign + formatChartValue(Math.abs(value), unit);
}

/** Stable even for very large, finite values; never place a missing value at zero. */
export function chartPosition(value: number | null, domain: [number, number]): number | null {
  if (value === null || !finite(value) || !domain.every(finite)) return null;
  const [min, max] = domain;
  if (max <= min) return null;
  const scale = Math.max(Math.abs(min), Math.abs(max), 1);
  const position = (value / scale - min / scale) / (max / scale - min / scale);
  return Math.max(0, Math.min(1, position));
}

function ceiling(value: number): number {
  if (!finite(value) || value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  if (!finite(magnitude) || magnitude <= 0) return value;
  const factor = value / magnitude;
  const result = (factor <= 1 ? 1 : factor <= 2 ? 2 : factor <= 5 ? 5 : 10) * magnitude;
  return finite(result) ? result : value;
}

function domains(means: Array<number | null>, deltas: Array<number | null>, unit: string) {
  const values = means.filter(finite);
  const bounded = (unit === "ratio" || unit === "boolean") && values.every(value => value >= 0 && value <= 1);
  const meanDomain: [number, number] = bounded ? [0, 1] : [
    Math.min(0, ...values), ceiling(Math.max(0, ...values)),
  ];
  const validDeltas = deltas.filter(finite);
  const bound = validDeltas.length && validDeltas.some(value => value !== 0)
    ? ceiling(Math.max(...validDeltas.map(Math.abs))) : unit === "ms" ? 1000 : 1;
  return { meanDomain, deltaDomain: [-bound, bound] as [number, number] };
}

export function buildEvaluationChart({ comparison, candidateId, metricId }: {
  comparison: VariantComparison; candidateId: string; metricId: string;
}): EvaluationChartModel | null {
  const candidate = comparison.comparisons.find(item => item.candidate_variant_id === candidateId);
  const metric = candidate?.metrics[metricId];
  if (!candidate || !metric) return null;
  const unit = metric.unit?.toLowerCase() ?? "ratio", direction = metric.direction ?? "neutral";
  const valid = metric.paired > 0 && finite(metric.baseline_mean)
    && finite(metric.candidate_mean) && finite(metric.mean_delta);
  const baselineMean = valid ? metric.baseline_mean : null;
  const candidateMean = valid ? metric.candidate_mean : null;
  const meanDelta = valid ? metric.mean_delta : null;
  const directionLabel = direction === "higher" ? "越高越好" : direction === "lower" ? "越低越好" : "仅诊断，不判优劣";
  const rows = candidate.pairs.map(pair => {
    const item = pair.metrics[metricId];
    const paired = item?.status === "paired" && finite(item.delta)
      && numeric(item.baseline) !== null && numeric(item.candidate) !== null;
    return {
      caseId: pair.case_id, trial: pair.trial, label: `${pair.case_id} / 第${pair.trial}次`,
      baseline: numeric(item?.baseline), candidate: numeric(item?.candidate),
      delta: paired ? item.delta : null,
      status: paired ? "paired" : "excluded", outcome: paired ? item.outcome : null,
      reasons: (item?.reasons?.length ? item.reasons : paired ? [] : ["metric_missing"]).map(comparisonReason),
    };
  });
  const counts = { improved: metric.improved, tied: metric.tied, regressed: metric.regressed, unclassified: metric.unclassified };
  const interpretation: string[] = [];
  if (!valid) {
    interpretation.push(`当前${metric.paired}/${metric.planned}个单元可比较，尚无有效的两组均值对照。未评测不等于0，不绘制虚假的零值柱。`);
  } else {
    interpretation.push(`在相同的${metric.paired}个配对单元中，${comparison.baseline_name}均值为${formatChartValue(baselineMean, unit)}，${candidate.candidate_name}为${formatChartValue(candidateMean, unit)}；绝对差值${formatChartDelta(meanDelta, unit)}（候选−基线）。`);
    if (direction === "neutral") {
      interpretation.push("此项只记录消耗或行为变化，不按数值大小判定效果改善。工具更多或调用更少均不能单独证明质量更好。");
    } else if (Math.abs(meanDelta!) <= 1e-12) {
      interpretation.push("两组在该指标的配对均值上持平，不能用这一项区分整体效果；仍应检查逐题改善与退化是否相互抵消。");
    } else {
      const improved = direction === "lower" ? meanDelta! < 0 : meanDelta! > 0;
      interpretation.push(`该指标${directionLabel}，本次配对均值表现为${improved ? "改善" : "退化"}。这是该指标的变化，不等于全部任务质量已经${improved ? "提升" : "下降"}。`);
    }
    interpretation.push(`逐题记录：改善${counts.improved}、持平${counts.tied}、退化${counts.regressed}${counts.unclassified ? `、仅诊断${counts.unclassified}` : ""}。均值可能掩盖个别题目的差异。`);
  }
  if (metric.not_comparable > 0) {
    const reasons = Object.entries(metric.excluded_reasons ?? {}).map(([reason, count]) => `${comparisonReason(reason)}${count}项`);
    interpretation.push(`共有${metric.not_comparable}个单元未纳入对照${reasons.length ? `：${reasons.join("；")}` : ""}。同一单元可能有多个排除原因，可评覆盖不是成功率。`);
  }
  return {
    metricId, title: metricName(metricId), description: metricDescription(metricId), unit, direction,
    directionLabel, scoringUnit: metric.scoring_unit,
    baselineLabel: comparison.baseline_name, candidateLabel: candidate.candidate_name,
    baselineMean, candidateMean, meanDelta, planned: metric.planned,
    paired: metric.paired, excluded: metric.not_comparable, counts, rows,
    ...domains([baselineMean, candidateMean], [meanDelta, ...rows.map(row => row.delta)], unit),
    interpretation, limitations: comparison.limitations.map(evaluationLimitation),
  };
}

export type MultiGroupChartValue = {
  variantId: string; value: number | null; rawValue: number | null;
  available: boolean; status: string; reasons: string[];
};
export type MultiGroupChartModel = {
  metricId: string; title: string; description: string;
  unit: string; direction: string; directionLabel: string; scoringUnit: string;
  planned: number; commonEvaluated: number; excluded: number; domain: [number, number];
  groups: Array<{
    id: string; label: string; colorIndex: number; value: number | null;
    available: number; planned: number; common: number; excluded: number;
    excludedReasons: Record<string, number>;
  }>;
  rows: Array<{
    caseId: string; trial: number; label: string; status: "common" | "excluded";
    reasons: string[]; values: MultiGroupChartValue[];
  }>;
  range: {
    minimum: number; maximum: number; spread: number | null;
    minGroupIds: string[]; maxGroupIds: string[]; preferredGroupIds: string[];
  } | null;
  interpretation: string[]; limitations: string[];
};

const METRIC_CONTRACT_FIELDS = ["unit", "direction", "scoring_unit", "grader_id", "grader_version"] as const;

function percentNumber(value: number): string {
  const scaled = value * 100, magnitude = Math.abs(scaled);
  if (magnitude > 0 && magnitude < 0.000001) return scaled.toPrecision(2);
  return scaled.toFixed(magnitude > 0 && magnitude < 0.1 ? Math.min(8, 1 - Math.floor(Math.log10(magnitude))) : 1);
}

/** Percentages for rates; MRR remains a 0–1 rank score, never a percentage. */
export function formatPrimaryValue(metricId: string, value: number | null, unit: string): string {
  if (value === null || !Number.isFinite(value)) return "未评测";
  return isRateMetric(metricId, unit) ? `${percentNumber(value)}%` : formatChartValue(value, unit);
}

export function formatPrimaryGap(metricId: string, spread: number | null, unit: string): string {
  if (spread === null || !Number.isFinite(spread)) return "不可比较";
  return isRateMetric(metricId, unit) ? `${percentNumber(spread)}个百分点` : formatChartValue(spread, unit);
}
const resultPosition = (caseId: string, trial: number, variantId: string) => JSON.stringify([caseId, trial, variantId]);
const metricContract = (metric: EvaluationMetric) => JSON.stringify(METRIC_CONTRACT_FIELDS.map(key => metric[key]));

/** Options come from observed result fields, not absent or unrelated summary metrics. */
export function multiGroupMetricIds(experiment: EvaluationExperiment): string[] {
  const present = [...new Set(experiment.results.flatMap(row => Object.keys(row.metrics)))];
  const preferred = experiment.kind === "rag"
    ? ["anchor_coverage_at_5", "anchor_mrr_at_5", "context_anchor_coverage_at_5", "source_coverage_at_5",
      "answer_correctness", "reference_fact_coverage", "faithfulness", "citation_support", "retrieval_duration_ms"]
    : ["task_success", "task_outcome_score", "trajectory_compliance", "tool_policy_compliance",
      "citation_support_score", "duration_ms", "tool_executions", "model_calls", "known_cost"];
  const priority = (key: string) => { const index = preferred.indexOf(key); return index < 0 ? preferred.length : index; };
  return present.sort((left, right) => priority(left) - priority(right) || left.localeCompare(right));
}

function observation(records: EvaluationResult[], metricId: string): {
  rawValue: number | null; available: boolean; status: string; reasons: string[]; contract: string | null;
} {
  if (records.length !== 1) return {
    rawValue: null, available: false, status: "missing",
    reasons: [records.length ? "同一组存在重复的题目与重复编号，不能选择性合并" : "此单元尚无记录"], contract: null,
  };
  const result = records[0], metric = result.metrics[metricId], reasons: string[] = [];
  const rawValue = numeric(metric?.value);
  if (result.status !== "completed") reasons.push(stateName(result.status));
  if (result.degraded?.length) reasons.push("存在降级，未计入正常效果比较");
  if (!metric) reasons.push("此指标尚无评分记录");
  else {
    if (metric.status !== "evaluated") reasons.push(`指标${stateName(metric.status)}`);
    if (rawValue === null) reasons.push("没有有限评分值，未评测不等于0");
    if (METRIC_CONTRACT_FIELDS.some(key => typeof metric[key] !== "string" || !metric[key].trim())) reasons.push("评分契约不完整，单位或评分器身份无法核对");
  }
  return {
    rawValue, available: !reasons.length,
    status: result.status !== "completed" ? result.status : result.degraded?.length ? "degraded" : metric?.status ?? "missing",
    reasons, contract: metric && !reasons.length ? metricContract(metric) : null,
  };
}

/** Compensated scaled mean avoids overflow and preserves real subnormal values. */
function commonMean(values: number[]): number | null {
  if (!values.length) return null;
  const scale = Math.max(...values.map(Math.abs));
  if (scale === 0) return 0;
  let sum = 0, correction = 0;
  for (const value of values) {
    const adjusted = value / scale - correction, next = sum + adjusted;
    correction = next - sum - adjusted;
    sum = next;
  }
  return Math.max(-1, Math.min(1, sum / values.length)) * scale;
}

/**
 * All groups use the exact same case/trial intersection. This is a visualization
 * of saved metric results, never a new semantic grader or a merge of pair APIs.
 */
export function buildMultiGroupChart({ experiment, metricId }: {
  experiment: EvaluationExperiment; metricId: string;
}): MultiGroupChartModel | null {
  const variants = experiment.plan.variants;
  if (!variants.length || new Set(variants.map(row => row.variant_id)).size !== variants.length
    || !multiGroupMetricIds(experiment).includes(metricId)) return null;
  const fallbackTemplate = experiment.results.find(row => row.metrics[metricId])!.metrics[metricId];
  const caseIds = [...new Set(experiment.plan.case_ids.length ? experiment.plan.case_ids : experiment.results.map(row => row.case_id))];
  const repetitions = experiment.plan.repetitions;
  if (!Number.isInteger(repetitions) || repetitions < 1 || repetitions > 5) return null;
  const indexed = new Map<string, EvaluationResult[]>();
  for (const result of experiment.results) {
    const key = resultPosition(result.case_id, result.trial, result.variant_id);
    indexed.set(key, [...(indexed.get(key) ?? []), result]);
  }
  const working = caseIds.flatMap(caseId => Array.from({ length: repetitions }, (_, index) => {
    const trial = index + 1;
    const values = variants.map(variant => ({ variantId: variant.variant_id,
      ...observation(indexed.get(resultPosition(caseId, trial, variant.variant_id)) ?? [], metricId) }));
    const reasons = values.flatMap((value, groupIndex) => value.reasons.map(reason => `${variants[groupIndex].name}：${reason}`));
    let common = values.every(value => value.available);
    if (common && new Set(values.map(value => value.contract)).size !== 1) {
      common = false;
      reasons.push("同一单元各组的单位、方向、评分单元或评分器版本不一致");
    }
    return { caseId, trial, values, reasons, common };
  }));
  const commonContracts = new Set(working.filter(row => row.common).map(row => row.values[0].contract));
  if (commonContracts.size > 1) {
    for (const row of working) if (row.common) {
      row.common = false;
      row.reasons.push("不同题目或重复使用了不同评分契约，不能合并均值");
    }
  }
  const planned = working.length, common = working.filter(row => row.common);
  const template = common.length
    ? indexed.get(resultPosition(common[0].caseId, common[0].trial, variants[0].variant_id))![0].metrics[metricId]
    : fallbackTemplate;
  const unit = template.unit?.toLowerCase() ?? "ratio";
  const direction = template.direction === "higher" || template.direction === "lower" ? template.direction : "neutral";
  const directionLabel = direction === "higher" ? "越高越好" : direction === "lower" ? "越低越好" : "仅诊断，不判优劣";
  const rows: MultiGroupChartModel["rows"] = working.map(row => ({
    caseId: row.caseId, trial: row.trial, label: `${row.caseId} / 第${row.trial}次`,
    status: row.common ? "common" : "excluded", reasons: row.reasons,
    values: row.values.map(value => ({ variantId: value.variantId,
      value: row.common ? value.rawValue : null, rawValue: value.rawValue,
      available: value.available, status: value.status, reasons: value.reasons })),
  }));
  const excludedReasons: Record<string, number> = {};
  for (const row of working) if (!row.common) for (const reason of new Set(row.reasons)) excludedReasons[reason] = (excludedReasons[reason] ?? 0) + 1;
  const groups = variants.map((variant, colorIndex) => ({
    id: variant.variant_id, label: variant.name, colorIndex,
    value: commonMean(common.map(row => row.values[colorIndex].rawValue!)),
    available: working.filter(row => row.values[colorIndex].available).length,
    planned, common: common.length, excluded: planned - common.length, excludedReasons: { ...excludedReasons },
  }));
  const means = groups.map(group => group.value).filter(finite);
  let range: MultiGroupChartModel["range"] = null;
  if (means.length) {
    const minimum = Math.min(...means), maximum = Math.max(...means), difference = maximum - minimum;
    const minGroupIds = groups.filter(group => group.value !== null && Math.abs(group.value - minimum) <= 1e-12).map(group => group.id);
    const maxGroupIds = groups.filter(group => group.value !== null && Math.abs(group.value - maximum) <= 1e-12).map(group => group.id);
    range = { minimum, maximum, spread: finite(difference) ? difference : null, minGroupIds, maxGroupIds,
      preferredGroupIds: direction === "higher" ? maxGroupIds : direction === "lower" ? minGroupIds : [] };
  }
  const interpretation: string[] = [
    `全部${groups.length}组使用相同的${common.length}/${planned}个题目与重复编号单元计算均值；不拼接不同组各自可评的样本。`,
  ];
  if (!common.length) {
    interpretation.push("当前没有全组共同可评样本，因此各组均值保持未评测，不绘制虚假的零值柱，也不能判断哪组效果更好。");
  } else {
    interpretation.push(groups.map(group => `${group.label}：${formatPrimaryValue(metricId, group.value, unit)}`).join("；") + "。这些均值全部来自同一交集。");
    if (groups.length === 1) interpretation.push("当前只有一个实验组，不能作组间优劣判断。");
    else if (range) {
      interpretation.push(`共同样本均值范围为${formatPrimaryValue(metricId, range.minimum, unit)}至${formatPrimaryValue(metricId, range.maximum, unit)}${range.spread === null ? "；最大最小差超出有限数值显示范围" : `，最大最小原值差为${formatPrimaryGap(metricId, range.spread, unit)}`}，不是相对增幅。`);
      if (direction === "neutral") interpretation.push("该指标只记录消耗或行为变化，不按数值大小判定效果改善；工具更多或调用更少均不能单独证明任务质量更好。");
      else if (range.spread !== null && range.spread <= 1e-12) interpretation.push("各组在该指标的共同样本均值上持平；仍应检查逐题差异是否相互抵消。");
      else interpretation.push(`该指标${directionLabel}，共同样本均值${direction === "higher" ? "较高" : "较低"}的组为${groups.filter(group => range!.preferredGroupIds.includes(group.id)).map(group => group.label).join("、")}。这只是单项指标观察，不等于整体任务质量提升，也不能证明配置变化带来因果收益。`);
    }
  }
  if (planned > common.length) {
    const reasons = Object.entries(excludedReasons).map(([reason, count]) => `${reason}（${count}项）`);
    interpretation.push(`有${planned - common.length}个单元未纳入全组比较${reasons.length ? `：${reasons.join("；")}` : ""}。图中只绘制共同可评值；各组自身可评数量不替代共同分母，同一单元可能有多个排除原因。`);
  }
  const plotted = [...means, ...rows.flatMap(row => row.values.map(value => value.value))];
  return {
    metricId, title: metricName(metricId), description: metricDescription(metricId), unit, direction,
    directionLabel, scoringUnit: template.scoring_unit, planned, commonEvaluated: common.length,
    excluded: planned - common.length, groups, rows, range,
    domain: domains(plotted, [], unit).meanDomain, interpretation,
    limitations: [
      "只比较全部组同题、同次重复的共同可评交集；失败、降级、缺标注与无有限评分值均不填0。",
      "共同覆盖不是端到端成功率；剔除失败后的条件均值可能高估整个实验的效果。",
      "评分单位、方向与评分器身份必须一致；不同指标不相加，也不生成综合总分。",
      "最大最小差仅描述本次共同样本，不代表统计显著性、因果结论或新配置的未来表现。",
    ],
  };
}
