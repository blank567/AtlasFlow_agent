"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { API_URL, getRun, normalizeEvent } from "../lib/api";
import { eventReducer, initialEventState } from "../lib/event-reducer";
import { RunRecord, TERMINAL_STATUSES } from "../lib/types";

const EVENT_TYPES = [
  "run_started",
  "node_started",
  "node_succeeded",
  "node_failed",
  "plan_created",
  "approval_required",
  "approval_resolved",
  "task_scheduled",
  "task_completed",
  "review_decided",
  "route_selected",
  "quality_evaluated",
  "run_completed",
  "run_degraded",
  "run_failed",
  "run_cancelled",
  "trace_segment_started",
  "trace_segment_completed",
  "trace_segment_updated",
] as const;

export type StreamState = "connecting" | "live" | "reconnecting" | "closed";

export function useRunStream(runId: string) {
  const [run, setRun] = useState<RunRecord | null>(null);
  const [events, dispatch] = useReducer(eventReducer, initialEventState);
  const [streamState, setStreamState] = useState<StreamState>("connecting");
  const [error, setError] = useState<string | null>(null);
  const sequenceRef = useRef(0);
  const refreshTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    sequenceRef.current = events.lastSequence;
  }, [events.lastSequence]);

  const refresh = useCallback(async () => {
    try {
      const snapshot = await getRun(runId);
      setRun(snapshot);
      if (snapshot.events.length) dispatch({ type: "hydrate", events: snapshot.events });
      setError(null);
      return snapshot;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取运行快照");
      return null;
    }
  }, [runId]);

  useEffect(() => {
    let disposed = false;
    let source: EventSource | null = null;
    let reconnectTimer: number | undefined;
    const postTerminalTimers: number[] = [];
    let attempts = 0;

    const scheduleRefresh = () => {
      if (refreshTimer.current) window.clearTimeout(refreshTimer.current);
      refreshTimer.current = window.setTimeout(() => void refresh(), 180);
    };

    const connect = () => {
      if (disposed) return;
      const query = sequenceRef.current ? `?last_event_id=${sequenceRef.current}` : "";
      source = new EventSource(`${API_URL}/runs/${encodeURIComponent(runId)}/events${query}`);
      setStreamState(attempts ? "reconnecting" : "connecting");

      source.onopen = () => {
        attempts = 0;
        setStreamState("live");
      };

      const receive = (message: MessageEvent<string>) => {
        try {
          const event = normalizeEvent(JSON.parse(message.data));
          if (!event) return;
          dispatch({ type: "append", event });
          if (event.event_type !== "heartbeat") scheduleRefresh();
        } catch {
          // A malformed telemetry event must never break the research run UI.
        }
      };

      source.onmessage = receive;
      EVENT_TYPES.forEach((type) => source?.addEventListener(type, receive as EventListener));
      source.addEventListener("heartbeat", () => setStreamState("live"));
      source.addEventListener("done", () => {
        source?.close();
        setStreamState("closed");
        void refresh();
        // Trace URL resolution is asynchronous and can finish just after workflow terminal.
        postTerminalTimers.push(window.setTimeout(() => void refresh(), 800));
        postTerminalTimers.push(window.setTimeout(() => void refresh(), 2500));
        postTerminalTimers.push(window.setTimeout(() => void refresh(), 6000));
      });
      source.onerror = () => {
        source?.close();
        if (disposed) return;
        attempts += 1;
        setStreamState("reconnecting");
        reconnectTimer = window.setTimeout(connect, Math.min(1000 * 2 ** attempts, 10_000));
      };
    };

    dispatch({ type: "clear" });
    void refresh().then((snapshot) => {
      if (!disposed && snapshot && !TERMINAL_STATUSES.has(snapshot.status)) connect();
      else if (!disposed) {
        setStreamState("closed");
        postTerminalTimers.push(window.setTimeout(() => void refresh(), 800));
        postTerminalTimers.push(window.setTimeout(() => void refresh(), 2500));
        postTerminalTimers.push(window.setTimeout(() => void refresh(), 6000));
      }
    });

    return () => {
      disposed = true;
      source?.close();
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      if (refreshTimer.current) window.clearTimeout(refreshTimer.current);
      postTerminalTimers.forEach((timer) => window.clearTimeout(timer));
    };
  }, [refresh, runId]);

  return { run, events: events.items, streamState, error, refresh, setRun };
}
