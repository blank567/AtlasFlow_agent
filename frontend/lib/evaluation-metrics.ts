import type { EvaluationExperiment, EvaluationProtocol } from "./evaluations-v2";

/** Fixed before looking at scores: never cherry-pick metrics with larger gaps. */
export const PRIMARY_METRICS: Record<EvaluationProtocol, readonly string[]> = {
  "rag-retrieval-v2": ["anchor_coverage_at_5", "anchor_hit_at_1", "anchor_mrr_at_5",
    "context_anchor_coverage_at_5", "retrieval_duration_ms"],
  "rag-answer-v2": ["anchor_coverage_at_5", "anchor_hit_at_1", "anchor_mrr_at_5",
    "reference_fact_coverage", "answer_correctness", "faithfulness", "citation_support", "retrieval_duration_ms"],
  "agent-task-v2": ["task_success", "task_outcome_score", "tool_policy_compliance",
    "tool_failures", "tool_executions", "duration_ms", "known_tokens", "known_cost"],
  "agent-contract-v2": ["trajectory_compliance", "tool_policy_compliance", "tool_failures",
    "tool_executions", "duration_ms", "known_tokens", "known_cost"],
};

/** Old experiments may not have collected every primary metric; do not invent it. */
export function primaryMetricIds(experiment: EvaluationExperiment): string[] {
  const present = new Set(experiment.results.flatMap(row => Object.keys(row.metrics)));
  return PRIMARY_METRICS[experiment.protocol].filter(key => present.has(key));
}

export function isRateMetric(metricId: string, unit: string): boolean {
  return ["ratio", "boolean"].includes(unit.toLowerCase()) && !metricId.includes("mrr") && !metricId.includes("ndcg");
}
