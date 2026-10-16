import { API_URL, request } from "./api";

export type EvaluationKind = "rag" | "agent";
export type EvaluationProtocol = "rag-retrieval-v2" | "rag-answer-v2" | "agent-task-v2" | "agent-contract-v2";
export type JsonRecord = Record<string, unknown>;
export type EvaluationCase = {
  case_id: string; query: string; kind?: EvaluationKind; tags?: string[]; gold_status?: string;
  gold?: JsonRecord; objective?: string; expected_facts?: unknown[]; expected_sources?: unknown[];
  trajectory_assertions?: unknown[]; required_invariants?: string[]; [key: string]: unknown;
};
export type EvaluationDataset = { dataset_id: string; kind: EvaluationKind; name: string; version: string; dataset_hash?: string; space?: string | null; cases: EvaluationCase[]; [key: string]: unknown };
export type EvaluationBudget = { max_http_attempts: number; max_elapsed_seconds: number; case_timeout_seconds: number; concurrency: number };
export type RagVariant = { variant_id: string; name: string; retrieval_tuning: Record<string, number | boolean>; retrieval_limit: number; generator?: { model?: string; prompt_version: string; max_context_chars: number; max_tokens: number } };
export type AgentVariant = { variant_id: string; name: string; policy: JsonRecord; model?: string; tool_allowlist?: string[]; workflow_version: string; prompt_version: string };
export type EvaluationVariant = RagVariant | AgentVariant;
export type EvaluationPlan = {
  kind: EvaluationKind; protocol: EvaluationProtocol; dataset_id: string; mode: "replay" | "live"; case_ids: string[];
  variants: EvaluationVariant[]; budget: EvaluationBudget; space?: string | null; space_version_id?: string | null;
  input_refs: Record<string, string>; repetitions: number; allow_degraded: boolean; confirm_live: boolean;
  judge_enabled: boolean; confirm_judge: boolean; langsmith_sync: boolean;
  cutoffs?: number[];
  comparison_focus?: "tools" | "general";
};
export type EvaluationTool = { name: string; description: string; available: boolean; unavailable_reason?: string | null; capabilities?: string[] };
export type EvaluationModel = { id: string; name: string; is_default: boolean };
export type EvaluationSettings = { kind: EvaluationKind; protocols: string[]; budget: EvaluationBudget; default_mode: string; judge_available: boolean; langsmith_available: boolean; models?: EvaluationModel[]; default_model?: string; judge_model?: string | null; judge_reuses_default_model?: boolean; judge_limits?: { max_tokens: number; max_reason_chars: number }; answer_defaults?: { prompt_version: string; max_context_chars: number; max_tokens: number }; model_catalog_note?: string; tools?: EvaluationTool[]; default_tool_allowlist?: string[]; recommended_candidate_tool_allowlist?: string[] };
export type EvaluationInput = { input_id: string; kind: EvaluationKind; protocol: string; case_id: string; variant_id: string; trial?: number; provenance: string; created_at: string };
export type EvaluationMetric = { value: number | boolean | null; status: string; unit: string; direction: string; scoring_unit: string; numerator?: number | null; denominator?: number | null; grader_id: string; grader_version: string; reason?: string | null; evidence_refs: string[] };
export type EvaluationResult = { case_id: string; variant_id: string; trial: number; status: string; input_id?: string | null; metrics: Record<string, EvaluationMetric>; checks: JsonRecord[]; stage_status: Record<string, string>; diagnostics?: JsonRecord; elapsed_ms: number; error?: string | null; degraded: string[]; provider_calls: JsonRecord[] };
export type MetricAggregate = { mean: number | null; evaluated: number; planned: number; status_counts?: Record<string, number>; unit?: string; direction?: string; scoring_unit?: string };
export type EvaluationSummary = { planned?: number; completed?: number; failed?: number; timed_out?: number; budget_stopped?: number; not_run?: number; grader_failed?: number; degraded?: number; variants?: Record<string, { planned: number; completed: number; failed: number; timed_out?: number; metrics: Record<string, MetricAggregate>; [key: string]: unknown }>; [key: string]: unknown };
export type EvaluationExperiment = { experiment_id: string; kind: EvaluationKind; protocol: EvaluationProtocol; plan: EvaluationPlan; dataset_hash: string; context: JsonRecord; status: string; created_at: string; ended_at?: string | null; results: EvaluationResult[]; summary: EvaluationSummary; budget_ledger: JsonRecord; stop_reason?: string | null; sync_status: string };
export type EvaluationPreflight = { ready: boolean; errors: string[]; warnings: string[]; dataset_hash: string; context: JsonRecord; expected_cells: number; budget: EvaluationBudget; resolved_input_refs: Record<string, string>; available_inputs?: EvaluationInput[] };
export type EvaluationArtifact = { input_id: string; kind: EvaluationKind; protocol: string; case_id: string; variant_id: string; trial: number; content: JsonRecord; events: JsonRecord[]; metadata?: JsonRecord; source_id_map?: Record<string, string>; source_hash_map?: Record<string, string>; [key: string]: unknown };
export type EvaluationComparison = { baseline_id: string; candidate_id: string; scope: string; pairs: Array<{ case_id: string; variant_id: string; trial: number; baseline_status: string; candidate_status: string; metrics: Record<string, { baseline: number | boolean | null; candidate: number | boolean | null; delta: number | null; status: string }> }>; baseline: EvaluationSummary; candidate: EvaluationSummary; limitations: string[] };
export type VariantMetricComparison = { baseline_mean: number | null; candidate_mean: number | null; mean_delta: number | null; unit: string; direction: string; scoring_unit: string; paired: number; planned: number; not_comparable: number; excluded_reasons: Record<string, number>; improved: number; tied: number; regressed: number; unclassified: number };
export type VariantComparison = { experiment_id: string; kind: EvaluationKind; protocol: string; status: string; baseline_variant_id: string; baseline_name: string; planned_pairs: number; comparisons: Array<{ candidate_variant_id: string; candidate_name: string; planned_pairs: number; metrics: Record<string, VariantMetricComparison>; pairs: Array<{ case_id: string; trial: number; baseline_status: string; candidate_status: string; metrics: Record<string, { baseline: number | boolean | null; candidate: number | boolean | null; delta: number | null; status: string; outcome: string | null; reasons: string[] }> }> }>; limitations: string[] };

const base = (kind: EvaluationKind) => `/evaluations/${kind}`;
export const evaluationApi = (kind: EvaluationKind) => ({
  datasets: () => request<EvaluationDataset[]>(`${base(kind)}/datasets`),
  settings: () => request<EvaluationSettings>(`${base(kind)}/settings`),
  inputs: () => request<EvaluationInput[]>(`${base(kind)}/inputs`),
  registerInput: (value: JsonRecord) => request<EvaluationInput>(`${base(kind)}/inputs`, { method: "POST", body: JSON.stringify(value) }),
  list: () => request<EvaluationExperiment[]>(`${base(kind)}/experiments`),
  get: (id: string) => request<EvaluationExperiment>(`${base(kind)}/experiments/${encodeURIComponent(id)}`),
  preflight: (plan: EvaluationPlan) => request<EvaluationPreflight>(`${base(kind)}/preflight`, { method: "POST", body: JSON.stringify(plan) }),
  start: (plan: EvaluationPlan) => request<EvaluationExperiment>(`${base(kind)}/experiments`, { method: "POST", body: JSON.stringify(plan) }),
  cancel: (id: string) => request<EvaluationExperiment>(`${base(kind)}/experiments/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  remove: (id: string) => request<{ experiment_id: string; kind: EvaluationKind; status: "deleted"; deleted_at: string; artifacts_retained: boolean }>(`${base(kind)}/experiments/${encodeURIComponent(id)}`, { method: "DELETE" }),
  artifact: (id: string, input: string) => request<EvaluationArtifact>(`${base(kind)}/experiments/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(input)}`),
  compare: (baseline: string, candidate: string, scope: string) => request<EvaluationComparison>(`${base(kind)}/compare`, { method: "POST", body: JSON.stringify({ baseline_id: baseline, candidate_id: candidate, scope }) }),
  comparison: (id: string, baseline?: string) => request<VariantComparison>(`${base(kind)}/experiments/${encodeURIComponent(id)}/comparison${baseline ? `?baseline_variant_id=${encodeURIComponent(baseline)}` : ""}`),
  exportUrl: (id: string, format: string) => `${API_URL}${base(kind)}/experiments/${encodeURIComponent(id)}/export?format=${format}`,
});

export const isActiveExperiment = (status?: string) => status === "planned" || status === "running";
export const cellKey = (result: { case_id: string; variant_id: string; trial?: number }) => `${result.case_id}:${result.variant_id}:${result.trial ?? 1}`;
export const record = (value: unknown): JsonRecord => value && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : {};
export const items = (value: unknown): JsonRecord[] => Array.isArray(value) ? value.map(record) : [];
export const asText = (value: unknown): string => typeof value === "string" ? value : value === null || value === undefined ? "" : JSON.stringify(value);
export const protocolName = (value: string) => ({ "rag-retrieval-v2": "证据检索", "rag-answer-v2": "检索与回答", "agent-task-v2": "任务场景", "agent-contract-v2": "工程契约", "legacy-v1": "历史协议v1" }[value] ?? value);
export const stateName = (value: string) => ({ planned: "等待执行", running: "执行中", completed: "执行完成", partial: "部分完成", failed: "执行失败", cancelled: "已取消", timed_out: "单题超时", timeout: "单题超时", not_run: "未运行", not_evaluated: "未评测", missing_gold: "缺少标注", not_applicable: "不适用", evaluated: "已评测", unknown: "未确定", degraded: "降级结果", grader_failed: "评审失败", budget_stopped: "预算停止", draft: "待审核", verified: "已核验", synced_metadata_only: "摘要已同步", not_requested: "未同步" }[value] ?? value);
export const metricName = (value: string) => ({
  source_coverage_at_1: "文献覆盖@1", source_coverage_at_3: "文献覆盖@3", source_coverage_at_5: "文献覆盖@5", source_coverage_at_10: "文献覆盖@10",
  anchor_coverage_at_1: "child定位覆盖@1", anchor_coverage_at_3: "child定位覆盖@3", anchor_coverage_at_5: "child定位覆盖@5", anchor_coverage_at_10: "child定位覆盖@10",
  context_anchor_coverage_at_1: "上下文覆盖@1", context_anchor_coverage_at_3: "上下文覆盖@3", context_anchor_coverage_at_5: "上下文覆盖@5", context_anchor_coverage_at_10: "上下文覆盖@10",
  anchor_hit_at_1: "首位命中率@1", anchor_hit_at_3: "证据命中@3", anchor_hit_at_5: "证据命中@5", anchor_hit_at_10: "证据命中@10",
  anchor_mrr_at_1: "证据MRR@1", anchor_mrr_at_3: "证据MRR@3", anchor_mrr_at_5: "证据MRR@5", anchor_mrr_at_10: "证据MRR@10",
  ndcg_at_1: "nDCG@1", ndcg_at_3: "nDCG@3", ndcg_at_10: "nDCG@10", false_sufficiency: "无答案误判充分", abstention: "无答案拒答", retrieval_duration_ms: "检索耗时", reference_fact_coverage: "参考事实覆盖", answer_present: "回答已生成",
  source_recall_at_1: "文献覆盖@1", source_recall_at_3: "文献覆盖@3", source_recall_at_5: "文献覆盖@5", source_recall_at_10: "文献覆盖@10",
  evidence_recall_at_1: "证据Recall@1", evidence_recall_at_3: "证据Recall@3", evidence_recall_at_5: "证据Recall@5", evidence_recall_at_10: "证据Recall@10",
  evidence_mrr_at_5: "证据MRR@5", evidence_ndcg_at_5: "证据nDCG@5", mrr_at_5: "MRR@5", ndcg_at_5: "nDCG@5",
  child_anchor_coverage: "child定位覆盖", context_anchor_coverage: "上下文定位覆盖", anchor_hit_rate: "child定位覆盖", context_anchor_hit_rate: "上下文定位覆盖",
  answer_correctness: "答案正确性", answer_completeness: "参考事实覆盖", faithfulness: "生成事实支持", citation_validity: "引用合法性", citation_support: "引用支持度", task_success: "任务目标满足", trajectory_pass_rate: "轨迹约束通过", contract_pass_rate: "工程契约通过", tool_behavior_pass_rate: "工具行为合规", tool_calls: "工具调用", elapsed_ms: "耗时", known_cost: "已知费用", source_mrr_at_5: "文献MRR@5", evidence_count: "返回证据数",
  task_outcome_score: "任务输出评分", citation_support_score: "来源支持评分", execution_completed: "流程完成", report_present: "报告已生成", trajectory_compliance: "执行轨迹合规", tool_policy_compliance: "工具行为合规", tool_executions: "工具执行次数", tool_failures: "工具失败次数", tool_budget_reserved: "已占工具额度", duration_ms: "执行耗时", model_calls: "模型调用", http_attempts: "HTTP尝试", known_tokens: "已知Token",
}[value] ?? value.replaceAll("_", " "));
export function metricDescription(key: string) {
  if (key.startsWith("source_coverage")) return "前K个child覆盖的已标注文献，仅作来源诊断，不代表段落正确。";
  if (key.startsWith("context_anchor_coverage")) return "补充parent上下文覆盖的参考锚点，与child定位分开计算。";
  if (key.startsWith("anchor_coverage")) return "前K个child直接包含的已核验参考锚点比例；不是语义正确率。";
  if (key === "anchor_hit_at_1") return "第1条child是否直接命中已核验锚点；均值是首位命中的题目比例，能区分答案是否被排到最前。";
  if (key.startsWith("anchor_hit")) return "前K个child是否至少命中一个参考锚点。";
  if (key.startsWith("anchor_mrr")) return "第一个锚点命中的倒数排名：第1名为1、第2名为0.5、第5名为0.2，未命中为0；不是百分比。";
  if (key === "task_outcome_score") return "按经核验rubric逐项评分后取均值，观察部分完成程度；任务整体是否成功另列，缺标注时不评分。";
  if (key === "tool_failures") return "仅实际执行工具的业务失败次数，不含权限拒绝；没有调用而失败为0，不能据此判断任务更好。";
  if (key === "known_tokens") return "Provider明确返回的已知Token消耗；未记录不填0，也不把更少消耗当作质量提升。";
  if (key.startsWith("ndcg")) return "依据明确的相关性标注评估排序；缺少qrels时不评分。";
  return ({ answer_correctness: "答案与参考要求是否一致；须独立语义评审。", reference_fact_coverage: "参考事实被答案完整覆盖的比例。", faithfulness: "生成事实是否得到提供上下文的支持。", citation_validity: "引用ID是否存在，不能代替引用支持度。", citation_support: "引用证据是否真正支持对应结论。", task_success: "任务目标是否通过明确rubric；流程结束不代表满足。", trajectory_compliance: "场景的全部必需轨迹约束是否通过；均值为共同可评场景的整体通过比例，不是单场景的断言平均分。", tool_policy_compliance: "场景的全部必需工具约束是否通过；均值为共同可评场景的整体通过比例，不是单场景的断言平均分。", retrieval_duration_ms: "真实检索耗时，低耗时不自动代表质量更好。", duration_ms: "完整场景执行耗时，需结合任务质量解释。", known_cost: "Provider已返回的已知费用，缺失记录不等于免费，应结合费用记录覆盖率。", tool_executions: "实际业务工具执行次数，不含被拒请求或缓存重用；次数多寡不直接代表质量。", evidence_count: "检索返回的证据条数，仅作数量诊断，不代表证据正确或充分。" } as Record<string, string>)[key] ?? "按本协议独立计算，缺少依据时保持未评测。";
}
export function comparisonReason(value: string): string {
  const labels: Record<string, string> = { degraded: "存在降级", baseline_degraded: "基线降级", candidate_degraded: "候选降级", missing_metric: "指标缺失", missing_value: "无评分值", missing_gold: "缺少标注", not_evaluated: "尚未评分", status_not_completed: "执行未完成", baseline_not_completed: "基线未完成", candidate_not_completed: "候选未完成", metric_identity_mismatch: "评分契约不一致", metric_contract_mismatch: "评分契约不一致", metric_delta_nonfinite: "差值无法计算", metric_not_evaluated: "指标未评测", metric_missing: "指标缺失", metric_value_unavailable: "指标没有有效值", cell_missing: "单元未记录", grader_failed: "评审失败" };
  if (labels[value]) return labels[value];
  const match = value.match(/^(baseline|candidate)_(.+)$/);
  if (match) { const side = match[1] === "baseline" ? "基线" : "候选", suffix = match[2]; if (labels[suffix]) return side + labels[suffix]; if (suffix.startsWith("cell_")) return side + stateName(suffix.slice(5)); if (suffix.startsWith("metric_")) return side + "指标" + stateName(suffix.slice(7)); }
  return value;
}
export function evaluationLimitation(value: string) {
  const labels: Record<string, string> = {
    "Only identical case/trial cells with completed, non-degraded, finite evaluated metrics are paired.": "只配对同题、同次重复且完成、未降级、评分有效的单元。",
    "Conditional paired means exclude failed/unavailable cells; coverage is not end-to-end success rate.": "配对均值排除失败和不可评单元；可评覆盖不等于端到端成功率。",
    "Delta is candidate minus baseline; lower-is-better and neutral metrics are interpreted separately.": "差值为候选减基线；低值优先指标与中性诊断分别解释。",
    "Small-sample diagnostics, not statistical significance or a cross-domain overall score.": "小样本诊断不代表统计显著性，也不是跨领域综合评分。",
    "Small-sample paired diagnostics; no statistical significance claim.": "小样本配对诊断，不作统计显著性结论。",
  };
  return labels[value] ?? (/[\u4e00-\u9fff]/.test(value) ? value : "附加评测限制已保存在导出结果中。");
}
export function metricValue(value: unknown, unit?: string) {
  unit = unit?.toLowerCase();
  if (value === null || value === undefined || (typeof value === "number" && !Number.isFinite(value))) return "未评测";
  if (typeof value === "boolean") return value ? "满足" : "未满足";
  if (typeof value !== "number") return String(value);
  if (unit === "ms") return `${(value / 1000).toFixed(2)}秒`;
  if (unit === "usd") return `$${value.toFixed(4)}`;
  if (unit === "count") return String(value);
  return value.toFixed(3);
}
