import {
  AnalyticsData,
  EMPTY_METRICS,
  RunEvent,
  RunListResponse,
  RunPolicy,
  RunRecord,
  SettingsStatus,
} from "./types";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

type JsonObject = Record<string, unknown>;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = "";
    try {
      const value = (await response.json()) as { detail?: unknown };
      detail = typeof value.detail === "string" ? value.detail : JSON.stringify(value.detail ?? value);
    } catch {
      detail = await response.text();
    }
    throw new Error(detail || `请求失败（${response.status}）`);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function object(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

function array<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function number(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function normalizeEvent(value: unknown): RunEvent | null {
  const raw = object(value);
  const eventId = String(raw.event_id ?? raw.id ?? "");
  if (!eventId && typeof raw.sequence !== "number") return null;
  return {
    event_id: eventId || `sequence-${raw.sequence}`,
    run_id: typeof raw.run_id === "string" ? raw.run_id : undefined,
    sequence: number(raw.sequence),
    event_type: String(raw.event_type ?? raw.type ?? "event"),
    message: String(raw.message ?? "状态已更新"),
    agent: typeof raw.agent === "string" ? raw.agent : undefined,
    node: typeof raw.node === "string" ? raw.node : undefined,
    task_id: typeof raw.task_id === "string" ? raw.task_id : undefined,
    plan_version: typeof raw.plan_version === "number" ? raw.plan_version : undefined,
    attempt: typeof raw.attempt === "number" ? raw.attempt : undefined,
    status: typeof raw.status === "string" ? raw.status : undefined,
    duration_ms: typeof raw.duration_ms === "number" ? raw.duration_ms : undefined,
    decision_reason: typeof raw.decision_reason === "string" ? raw.decision_reason : undefined,
    created_at: String(raw.created_at ?? new Date().toISOString()),
    data: object(raw.data),
  };
}

export function normalizeRun(value: unknown): RunRecord {
  const raw = object(value);
  const metrics = object(raw.metrics);
  const traceSegments = array<unknown>(raw.trace_segments ?? raw.langsmith_traces).map((item, index) => {
    const segment = object(item);
    const calls = array<unknown>(segment.provider_calls).map((callValue, callIndex) => {
      const call = object(callValue);
      const usage = object(call.usage);
      return {
        ...call,
        call_id: String(call.call_id ?? `${segment.segment_id ?? index}-${callIndex}`),
        usage: Object.keys(usage).length ? usage : undefined,
      };
    });
    const startedAt = typeof segment.started_at === "string" ? segment.started_at : undefined;
    const endedAt = typeof segment.ended_at === "string" ? segment.ended_at : undefined;
    const measuredDuration = startedAt && endedAt ? Math.max(0, new Date(endedAt).getTime() - new Date(startedAt).getTime()) : undefined;
    return {
      id: String(segment.id ?? segment.segment_id ?? segment.trace_id ?? index),
      name: typeof segment.name === "string" ? segment.name : undefined,
      url: typeof segment.url === "string" ? segment.url : undefined,
      status: typeof segment.status === "string" ? segment.status : undefined,
      started_at: startedAt,
      ended_at: endedAt,
      duration_ms: typeof segment.duration_ms === "number" ? segment.duration_ms : measuredDuration,
      kind: typeof segment.kind === "string" ? segment.kind : undefined,
      trace_status: typeof segment.trace_status === "string" ? segment.trace_status : undefined,
      provider_calls: calls,
    };
  });
  return {
    id: String(raw.id ?? raw.run_id ?? "unknown"),
    query: String(raw.query ?? ""),
    status: String(raw.status ?? "pending"),
    auto_approve: typeof raw.auto_approve === "boolean" ? raw.auto_approve : undefined,
    policy: object(raw.policy) as RunPolicy,
    source_run_id: typeof raw.source_run_id === "string" ? raw.source_run_id : undefined,
    created_at: typeof raw.created_at === "string" ? raw.created_at : undefined,
    updated_at: typeof raw.updated_at === "string" ? raw.updated_at : undefined,
    started_at: typeof raw.started_at === "string" ? raw.started_at : undefined,
    completed_at: typeof raw.completed_at === "string" ? raw.completed_at : undefined,
    plan: raw.plan ? (raw.plan as RunRecord["plan"]) : undefined,
    plans: array(raw.plans),
    plan_lineage: raw.plan_lineage ? (raw.plan_lineage as RunRecord["plan_lineage"]) : undefined,
    research_results: array(raw.research_results),
    critique_history: array(raw.critique_history),
    quality_history: array(raw.quality_history),
    errors: array(raw.errors),
    metrics: {
      ...EMPTY_METRICS,
      ...metrics,
      total_tasks: number(metrics.total_tasks, number(raw.task_count)),
      successful_tasks: number(metrics.successful_tasks),
      failed_tasks: number(metrics.failed_tasks),
      peak_concurrency: number(metrics.peak_concurrency),
      plan_versions: number(metrics.plan_versions),
      supplement_rounds: number(metrics.supplement_rounds),
      replan_count: number(metrics.replan_count),
      revision_count: number(metrics.revision_count),
      model_calls: number(metrics.model_calls, number(raw.model_calls)),
      duration_ms: number(metrics.duration_ms, typeof raw.duration_ms === "number" ? raw.duration_ms : 0) || undefined,
    },
    warnings: array<string>(raw.warnings),
    events: array<unknown>(raw.events).map(normalizeEvent).filter((item): item is RunEvent => item !== null),
    trace_segments: traceSegments,
    report: typeof raw.report === "string" ? raw.report : undefined,
    error: typeof raw.error === "string" ? raw.error : undefined,
  };
}

export async function createRun(payload: { query: string; auto_approve: boolean; policy?: RunPolicy }) {
  return normalizeRun(await request<unknown>("/runs", { method: "POST", body: JSON.stringify(payload) }));
}

export async function getRun(runId: string) {
  return normalizeRun(await request<unknown>(`/runs/${encodeURIComponent(runId)}`));
}

export async function listRuns(params: URLSearchParams): Promise<RunListResponse> {
  const value = object(await request<unknown>(`/runs?${params.toString()}`));
  const items = array<unknown>(value.items ?? value.runs).map(normalizeRun);
  const pageSize = number(value.page_size, 10);
  const total = number(value.total, items.length);
  return {
    items,
    total,
    page: number(value.page, 1),
    page_size: pageSize,
    pages: number(value.pages, Math.max(1, Math.ceil(total / pageSize))),
  };
}

export async function resolveApproval(runId: string, action: "approve" | "edit" | "cancel", editedPlan?: unknown) {
  return normalizeRun(
    await request<unknown>(`/runs/${encodeURIComponent(runId)}/approval`, {
      method: "POST",
      body: JSON.stringify({ action, ...(editedPlan ? { edited_plan: editedPlan } : {}) }),
    }),
  );
}

export async function cancelRun(runId: string) {
  return normalizeRun(await request<unknown>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }));
}

export async function rerun(runId: string) {
  return normalizeRun(await request<unknown>(`/runs/${encodeURIComponent(runId)}/rerun`, { method: "POST" }));
}

export async function deleteRun(runId: string) {
  return request<void>(`/runs/${encodeURIComponent(runId)}`, {
    method: "DELETE",
    body: JSON.stringify({ confirmation_run_id: runId }),
  });
}

export async function getAnalytics(range: string): Promise<AnalyticsData> {
  const raw = object(await request<unknown>(`/analytics?range=${encodeURIComponent(range)}`));
  return {
    range: String(raw.range ?? range),
    summary: object(raw.summary) as AnalyticsData["summary"],
    status_distribution: array(raw.status_distribution),
    duration_trend: array(raw.duration_trend),
    decision_counts: array(raw.decision_counts),
    plan_versions: array(raw.plan_versions),
  };
}

export async function getSettingsStatus() {
  return request<SettingsStatus>("/settings/status");
}

export async function checkLangSmith() {
  return request<SettingsStatus | SettingsStatus["langsmith"]>("/settings/langsmith/check", { method: "POST" });
}
