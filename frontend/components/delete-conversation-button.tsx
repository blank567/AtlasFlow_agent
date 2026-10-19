"use client";

import { useRef, useState } from "react";
import { Trash } from "@phosphor-icons/react";
import { RequestError } from "../lib/api";
import { Conversation, ConversationDeleted, deleteConversation, getConversation } from "../lib/conversations";
import { ConfirmDialog } from "./confirm-dialog";

export function DeleteConversationButton({ conversation, disabled, onDeleted, onError, onBusyChange }: {
  conversation: Conversation;
  disabled?: boolean;
  onDeleted: (result: ConversationDeleted) => void;
  onError: (message: string) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [target, setTarget] = useState<{ id: string; title: string; version: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  async function remove() {
    if (!target || inFlight.current) return;
    // Freeze the confirmation snapshot; a newer conversation/Run requires
    // another explicit confirmation, not whatever props arrived over SSE.
    const confirmed = target;
    inFlight.current = true; setBusy(true); setTarget(null); onBusyChange?.(true);
    let result: ConversationDeleted;
    try {
      result = await deleteConversation(confirmed.id, confirmed.version);
    } catch (reason) {
      let gone = reason instanceof RequestError && reason.status === 404;
      if (!gone && (!(reason instanceof RequestError) || reason.status >= 500)) {
        // A lost response is an unknown outcome, not permission to repeat the
        // destructive call. Read once to distinguish gone from retryable intent.
        try { await getConversation(confirmed.id); }
        catch (readError) { gone = readError instanceof RequestError && readError.status === 404; }
      }
      if (gone) {
        // Safe retry after a lost DELETE response or deletion in another tab.
        result = { conversation_id: confirmed.id, deleted_run_count: null };
      } else {
        onError(reason instanceof Error ? reason.message : "删除未完成，请刷新后重试；关联执行可能已停止。");
        return;
      }
    } finally {
      inFlight.current = false; setBusy(false); onBusyChange?.(false);
    }
    onDeleted(result);
  }

  return <>
    <button type="button" className="textButton dangerText conversationDeleteButton" disabled={disabled || busy}
      aria-label={`${conversation.deleting ? "重试删除会话" : "删除会话"}：${conversation.title}`}
      onClick={() => setTarget({ id: conversation.id, title: conversation.title, version: conversation.version })}>
      <Trash size={16} aria-hidden="true" />{busy ? "删除中…" : conversation.deleting ? "重试删除" : "删除会话"}
    </button>
    {target && <ConfirmDialog title="删除会话及关联Run？" tone="danger" confirmLabel="永久删除" cancelLabel="保留会话"
      description={`将永久删除“${target.title}”的本地消息、报告和全部归属Run。正在执行的处理会先停止，此操作不可撤销。独立运行、知识库及LangSmith远端Trace不受影响；已发送的远端请求仍可能计费。`}
      onCancel={() => setTarget(null)} onConfirm={() => void remove()} />}
  </>;
}
