import { request } from "./api";

export type EvalCase = { case_id: string; kind: "rag" | "agent"; query: string; tags: string[]; gold_status: "verified" | "draft" };
export type EvalDataset = { dataset_id: string; version: string; name: string; space: string; cases: EvalCase[] };
export type EvalArm = { arm_id: string; name: string; tuning: Record<string, number | boolean>; result_limit: number; agent_policy?: Record<string, unknown> };
export type EvalPlan = {
  dataset_id: string; mode: "replay" | "live"; case_ids: string[]; arms: EvalArm[]; space?: string; space_version_id?: string | null;
  input_refs: Record<string, string>; confirm_live: boolean; judge_enabled: boolean; confirm_judge: boolean; langsmith_sync: boolean;
  budget: { max_http_attempts: number; max_elapsed_seconds: number; case_timeout_seconds: number; concurrency: number };
};
export type EvalInput = { input_id: string; case_id: string; arm_id: string; kind: "rag" | "agent"; provenance: string; created_at: string };
export type EvalResult = { case_id: string; arm_id: string; status: string; input_id?: string | null; metrics: Record<string, number | boolean | null>; availability: Record<string, string>; error?: string | null; invariant_failures: string[]; replayed: boolean; elapsed_ms: number; degraded: string[] };
export type EvalExperiment = { experiment_id: string; status: string; plan: EvalPlan; dataset_hash: string; comparison_key: string; snapshot: Record<string, unknown>; created_at: string; results: EvalResult[]; summary: Record<string, unknown>; budget_ledger: { http_attempts?: number; max_http_attempts?: number; elapsed_seconds?: number; max_elapsed_seconds?: number }; stop_reason?: string | null; sync_status: string };
export type EvalPreflight = { ready: boolean; errors: string[]; warnings: string[]; selected_cases: number; case_ids: string[]; estimated_provider_requests: number | null; snapshot: Record<string, unknown>; comparison_key: string; available_inputs: EvalInput[]; resolved_input_refs?: Record<string, string> };

const arm = (arm_id: string, name: string, tuning: EvalArm["tuning"]): EvalArm => ({ arm_id, name, tuning, result_limit: 5 });
export const EVAL_ARMS = [
  arm("lexical", "关键词", { lexical_weight: 1, vector_weight: 0, query_expansion_enabled: false, rerank_enabled: false }),
  arm("semantic", "语义", { lexical_weight: 0, vector_weight: 1, query_expansion_enabled: false, rerank_enabled: false }),
  arm("baseline", "混合召回", { query_expansion_enabled: false, rerank_enabled: false }),
  arm("reranked", "混合+重排", { query_expansion_enabled: false, rerank_enabled: true }),
  arm("expanded", "混合+扩展", { query_expansion_enabled: true, rerank_enabled: false }),
  arm("candidate", "混合+中英扩展+重排", { query_expansion_enabled: true, rerank_enabled: true }),
];
export const listEvalDatasets = () => request<EvalDataset[]>("/evaluations/datasets");
export const listEvalInputs = () => request<EvalInput[]>("/evaluations/inputs");
export const listExperiments = () => request<EvalExperiment[]>("/evaluations");
export const getExperiment = (id: string) => request<EvalExperiment>(`/evaluations/${encodeURIComponent(id)}`);
export const preflightEvaluation = (plan: EvalPlan) => request<EvalPreflight>("/evaluations/preflight", { method: "POST", body: JSON.stringify(plan) });
export const startEvaluation = (plan: EvalPlan) => request<EvalExperiment>("/evaluations", { method: "POST", body: JSON.stringify(plan) });
export const cancelEvaluation = (id: string) => request<EvalExperiment>(`/evaluations/${encodeURIComponent(id)}/cancel`, { method: "POST" });
export const compareEvaluations = (baseline: string, candidate: string, scope: "retrieval" | "index" = "retrieval") => request<Record<string, unknown>>("/evaluations/compare", { method: "POST", body: JSON.stringify({ baseline_id: baseline, candidate_id: candidate, scope }) });
