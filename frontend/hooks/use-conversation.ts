"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { API_URL, RequestError } from "../lib/api";
import { ConversationDetail, getConversation, normalizeConversation } from "../lib/conversations";

export function useConversation(id: string) {
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState("connecting");
  const [deletedId, setDeletedId] = useState<string | null>(null);
  const deletedScope = useRef<string | null>(null);
  const scope = useRef(id);
  const version = useRef(0);
  scope.current = id;
  const markDeleted = useCallback(() => {
    if (scope.current !== id) return;
    deletedScope.current = id; setDeletedId(id); setConnection("closed");
  }, [id]);
  const accept = useCallback((value: ConversationDetail) => {
    if (scope.current !== id || value.conversation.id !== id || deletedScope.current === id) return;
    setDetail((current) => !current || value.conversation.version >= current.conversation.version ? value : current);
    version.current = Math.max(version.current, value.conversation.version);
    setError(null);
  }, [id]);
  const refresh = useCallback(async () => {
    try { const value = await getConversation(id); accept(value); return value; }
    catch (reason) { if (scope.current === id) {
      if (reason instanceof RequestError && reason.status === 404) markDeleted();
      else setError(reason instanceof Error ? reason.message : "会话读取失败");
    } return null; }
  }, [accept, id, markDeleted]);
  useEffect(() => {
    let disposed = false;
    let source: EventSource | null = null;
    setDetail(null); setError(null); setDeletedId(null); deletedScope.current = null; version.current = 0;
    void refresh().then((value) => {
      if (disposed || !value) return;
      source = new EventSource(`${API_URL}/conversations/${encodeURIComponent(id)}/events?last_event_id=${version.current}`);
      source.onopen = () => { if (!disposed) setConnection("live"); };
      source.addEventListener("conversation_snapshot", (event) => {
        if (disposed) return;
        try { const snapshot = JSON.parse((event as MessageEvent<string>).data) as ConversationDetail;
          if (snapshot.conversation && Array.isArray(snapshot.messages) && Array.isArray(snapshot.turns)) accept(normalizeConversation(snapshot));
        } catch { /* Telemetry must not discard the saved conversation. */ }
      });
      source.addEventListener("conversation_deleted", (event) => {
        if (disposed) return;
        try { const value = JSON.parse((event as MessageEvent<string>).data);
          if (value.conversation_id === id) { source?.close(); markDeleted(); }
        } catch { /* Ignore malformed telemetry. */ }
      });
      source.onerror = () => { if (!disposed && deletedScope.current !== id) {
        setConnection("reconnecting");
        if (source?.readyState === EventSource.CLOSED) void refresh();
      } };
    });
    return () => { disposed = true; source?.close(); };
  }, [accept, id, markDeleted, refresh]);
  return { detail, error, connection, refresh, accept, deleted: deletedId === id };
}
