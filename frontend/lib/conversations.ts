import { normalizeRun, request } from "./api";
import type { RunPolicy, RunRecord } from "./types";

export type Requirement = { value: string; source_message_ids: string[]; confirmed: boolean };
export type Conversation = {
  id: string; title: string; version: number; content_version?: number; policy: RunPolicy; auto_approve: boolean;
  requirements: Record<string, Requirement>; current_report_run_id: string | null;
  active_turn_id: string | null; created_at: string; updated_at: string;
  deleting?: boolean;
};
export type ConversationMessage = {
  id: string; conversation_id: string; turn_id: string; sequence: number;
  role: "user" | "assistant"; kind: "text" | "clarification" | "proposal" | "report" | "error" | "notice";
  content: string; run_id: string | null; created_at: string;
};
export type ConversationTurn = {
  id: string; conversation_id: string; user_message_id: string; request_id: string;
  status: string; action: "clarify" | "answer" | "research" | "revise" | null;
  decision: { action: string; resolved_request: string; rationale: string; base_run_id: string | null } | null;
  requirements_snapshot: Record<string, Requirement>; run_id: string | null; error: string | null;
  trace_segments: Array<{ segment_id: string; url: string | null; status: string; provider_calls?: Array<{ usage?: { total_tokens?: number; cost?: number } | null }> }>;
};
export type ConversationDetail = {
  conversation: Conversation; messages: ConversationMessage[]; turns: ConversationTurn[];
  reports: Array<{ run_id: string; turn_id: string; title: string; status: string; source_run_id: string | null; created_at: string }>;
  active_run: RunRecord | null;
};
export type MessagePayload = { content: string; request_id: string; expected_version: number; base_run_id: string | null };

export function normalizeConversation(value: ConversationDetail): ConversationDetail {
  return { ...value, active_run: value.active_run ? normalizeRun(value.active_run) : null };
}
export function listConversations(page = 1) {
  return request<{ items: Conversation[]; total: number; page: number; page_size: number }>(`/conversations?page=${page}`);
}
export function createConversation(payload: { title?: string; policy: RunPolicy; auto_approve: boolean }) {
  return request<Conversation>("/conversations", { method: "POST", body: JSON.stringify(payload) });
}
// The API returns an integer; null is local-only when a lost response is
// reconciled by GET/404 and the committed count is no longer observable.
export type ConversationDeleted = { conversation_id: string; deleted_run_count: number | null };
export function deleteConversation(id: string, expectedVersion: number) {
  return request<ConversationDeleted>(`/conversations/${encodeURIComponent(id)}`, {
    method: "DELETE", body: JSON.stringify({ confirmation_conversation_id: id, expected_version: expectedVersion }),
  });
}
export async function getConversation(id: string) {
  return normalizeConversation(await request<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`));
}
export async function sendConversationMessage(id: string, payload: MessagePayload) {
  return normalizeConversation(await request<ConversationDetail>(`/conversations/${encodeURIComponent(id)}/messages`, { method: "POST", body: JSON.stringify(payload) }));
}
export async function actOnConversationTurn(id: string, turnId: string, action: "confirm" | "cancel", version: number) {
  return normalizeConversation(await request<ConversationDetail>(`/conversations/${encodeURIComponent(id)}/turns/${encodeURIComponent(turnId)}/${action}`, {
    method: "POST", body: JSON.stringify({ expected_version: version, confirmation_turn_id: turnId }),
  }));
}

export const TURN_LABELS: Record<string, string> = {
  routing: "理解需求中", awaiting_clarification: "待补充需求", awaiting_confirmation: "待确认执行",
  running: "研究执行中", waiting_approval: "计划待审批", interrupted: "执行已中断",
  completed: "本轮已完成", completed_with_warnings: "本轮带警告完成", failed: "本轮未完成", cancelled: "本轮已停止",
};
