export type RunStatus =
  | "pending"
  | "running"
  | "waiting_approval"
  | "completed"
  | "completed_with_warnings"
  | "failed"
  | "cancelled"
  | string;

export type RunPolicy = {
  planner_allow_research?: boolean;
  knowledge_mode?: "default" | "selected" | "off";
  knowledge_space?: string | null;
  initial_task_count?: number | null;
  required_supplement_rounds?: number;
  supplement_task_count?: number | null;
  replan_requires_critical_issue?: boolean;
  max_tool_calls_per_turn?: number;
  max_tool_calls_per_run?: number;
};

export type CapabilityDescription = {
  id: string;
  description: string;
  supports_fresh_data: boolean;
  available: boolean;
  tools: string[];
  unavailable_reasons: string[];
};

export type ResearchTask = {
  task_id: string;
  title: string;
  objective: string;
  success_criteria: string[];
  priority: number;
  dependencies: string[];
  requires_fresh_data?: boolean;
  required_capabilities?: string[];
  capability_alternatives?: string[][];
  plan_version: number;
  provenance?: "initial" | "supplement" | "replan" | string;
};

export type ResearchPlan = {
  plan_version: number;
  rationale: string;
  tasks: ResearchTask[];
};

export type SupplementBatch = {
  round: number;
  plan_version: number;
  tasks: ResearchTask[];
  task_ids?: string[];
  rationale?: string;
};

export type PlanLineage = {
  plan_version: number;
  base_plan: ResearchPlan;
  supplements: SupplementBatch[];
  current_plan_version?: number;
};

export type ResearchResult = {
  task_id: string;
  plan_version: number;
  summary: string;
  confidence?: number;
  attempt?: number;
  duration_ms?: number;
};

export type AgentError = {
  agent: string;
  code: string;
  message: string;
  task_id?: string;
  plan_version?: number;
};

export type CritiqueDecision = {
  decision: "accept" | "supplement" | "replan" | string;
  rationale: string;
  plan_version: number;
  replan_reason?: string | null;
  missing_aspects?: string[];
  critical_issues?: Array<string | { code?: string; message?: string }>;
  created_at?: string;
};

export type QualityDecision = {
  decision: "accept" | "revise" | "replan" | string;
  score?: number;
  rationale: string;
  plan_version: number;
  draft_version: number;
  created_at?: string;
};

export type ProviderUsage = {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  cost?: number;
  currency?: string;
  request_id?: string;
  model?: string;
};

export type ProviderCallMetric = {
  call_id: string;
  provider?: string;
  operation?: string;
  endpoint?: string;
  status?: string;
  requested_model?: string;
  model?: string;
  request_id?: string;
  generation_id?: string;
  usage?: ProviderUsage;
  duration_ms?: number;
  attempt_count?: number;
  http_status?: number;
  error_type?: string;
  recorded_at?: string;
};

export type ExecutionMetrics = {
  total_tasks: number;
  successful_tasks: number;
  failed_tasks: number;
  peak_concurrency: number;
  plan_versions: number;
  supplement_rounds: number;
  replan_count: number;
  revision_count: number;
  model_calls: number;
  tool_calls?: number;
  duration_ms?: number;
  provider_usage?: ProviderUsage;
};

export type TraceSegment = {
  id: string;
  name?: string;
  url?: string;
  status?: string;
  started_at?: string;
  ended_at?: string;
  duration_ms?: number;
  kind?: string;
  trace_status?: string;
  provider_calls: ProviderCallMetric[];
};

export type RunEvent = {
  event_id: string;
  run_id?: string;
  sequence: number;
  event_type: string;
  message: string;
  agent?: string;
  node?: string;
  task_id?: string;
  plan_version?: number;
  attempt?: number;
  status?: string;
  duration_ms?: number;
  decision_reason?: string;
  created_at: string;
  data: Record<string, unknown>;
};

export type ToolCallRecord = {
  call_id: string;
  tool_name: string;
  agent: string;
  task_id?: string | null;
  plan_version?: number | null;
  capabilities?: string[];
  reused_from_call_id?: string | null;
  retryable?: boolean | null;
  failure_scope?: string | null;
  arguments: Record<string, unknown>;
  success: boolean;
  duration_ms: number;
  evidence_ids: string[];
  summary?: string | null;
  navigation_url?: string | null;
  navigation_urls?: string[];
  error?: string | null;
  created_at: string;
};

export type ToolEvidence = {
  id: string;
  source_id: string;
  title: string;
  content: string;
  uri?: string | null;
};

export type RunRecord = {
  id: string;
  query: string;
  status: RunStatus;
  auto_approve?: boolean;
  policy?: RunPolicy;
  source_run_id?: string;
  created_at?: string;
  updated_at?: string;
  started_at?: string;
  completed_at?: string;
  plan?: ResearchPlan;
  plans: ResearchPlan[];
  plan_lineage?: PlanLineage;
  research_results: ResearchResult[];
  tool_calls: ToolCallRecord[];
  evidence: ToolEvidence[];
  critique_history: CritiqueDecision[];
  quality_history: QualityDecision[];
  errors: AgentError[];
  metrics: ExecutionMetrics;
  warnings: string[];
  events: RunEvent[];
  trace_segments: TraceSegment[];
  report?: string;
  error?: string;
};

export type RunListResponse = {
  items: RunRecord[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
};

export type AnalyticsData = {
  range: string;
  summary: {
    total_runs?: number;
    success_rate?: number;
    avg_duration_ms?: number;
    total_model_calls?: number;
    total_tasks?: number;
    task_success_rate?: number;
    successful_tasks?: number;
    failed_tasks?: number;
    avg_quality_score?: number;
    total_supplement_rounds?: number;
    total_replans?: number;
    total_revisions?: number;
  };
  status_distribution: Array<{ status: string; count: number }>;
  duration_trend: Array<{ date: string; avg_duration_ms: number; runs?: number }>;
  decision_counts: Array<{ decision: string; count: number }>;
  plan_versions: Array<{ version: string | number; count: number }>;
};

export type SettingsStatus = {
  app_version?: string;
  database?: { path?: string; size_bytes?: number; persistent?: boolean };
  langsmith?: {
    state?: "ready" | "disabled" | "not_configured" | "unreachable" | "error" | string;
    configured?: boolean;
    enabled?: boolean;
    project?: string;
    endpoint?: string;
    trace_content?: boolean | "metadata" | "full" | string;
    connection_status?: "not_checked" | "reachable" | "unreachable" | string;
    last_checked_at?: string;
    message?: string;
  };
  provider?: { configured?: boolean; model?: string; name?: string };
};

export const EMPTY_METRICS: ExecutionMetrics = {
  total_tasks: 0,
  successful_tasks: 0,
  failed_tasks: 0,
  peak_concurrency: 0,
  plan_versions: 0,
  supplement_rounds: 0,
  replan_count: 0,
  revision_count: 0,
  model_calls: 0,
  tool_calls: 0,
};

export const TERMINAL_STATUSES = new Set([
  "completed",
  "completed_with_warnings",
  "failed",
  "cancelled",
]);

export type KnowledgeSpace = {
  id: string;
  slug: string;
  name: string;
  description: string;
  visibility: "private" | "internal" | "public";
  embedding_profile: string;
  retrieval_profile: string;
  active_generation_id?: string | null;
  created_at: string;
  updated_at: string;
};

export type KnowledgeDocument = {
  id: string;
  space_id: string;
  source_id: string;
  title: string;
  canonical_uri?: string | null;
  current_version_id?: string | null;
  status: "active" | "archived";
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type KnowledgeJob = {
  id: string;
  space_id: string;
  document_id: string;
  version_id: string;
  status: string;
  stage: string;
  attempt: number;
  progress: number;
  error_code?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
};

export type KnowledgeSearchResult = {
  query: string;
  decision: { status: "sufficient" | "partial" | "insufficient"; reasons: string[]; coverage: number; evidence_count: number; best_relevance: number; score_basis: "rerank" | "fusion" };
  hits: Array<{
    chunk: { id: string; title: string; content: string; heading_path: string[]; page_number?: number | null; metadata: Record<string, unknown> };
    lexical_score: number;
    vector_score: number;
    fusion_score: number;
    rerank_score?: number | null;
  }>;
  trace: { profile_id: string; generation_id?: string | null; lexical_candidates: number; vector_candidates: number; fused_candidates: number; rerank_candidates: number; rerank_requested: boolean; rerank_applied: boolean; applied_parameters: Record<string, number | boolean>; degraded: string[]; duration_ms: number };
};

export type KnowledgeSearchTuning = {
  lexical_weight: number;
  vector_weight: number;
  rrf_k: number;
  candidate_limit: number;
  rerank_limit: number;
  rerank_enabled: boolean;
};
