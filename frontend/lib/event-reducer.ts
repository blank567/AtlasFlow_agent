import { RunEvent } from "./types";

export type EventState = { items: RunEvent[]; lastSequence: number };

export type EventAction =
  | { type: "hydrate"; events: RunEvent[] }
  | { type: "append"; event: RunEvent }
  | { type: "clear" };

const MAX_EVENTS = 1000;

export const initialEventState: EventState = { items: [], lastSequence: 0 };

export function eventReducer(state: EventState, action: EventAction): EventState {
  if (action.type === "clear") return initialEventState;
  // A snapshot can lag behind a just-arrived SSE event. Merge it instead of
  // replacing the live buffer, so a route never disappears during refresh.
  const incoming = action.type === "hydrate" ? [...state.items, ...action.events] : [...state.items, action.event];
  const unique = new Map<string, RunEvent>();
  incoming.forEach((event) => unique.set(event.event_id || String(event.sequence), event));
  const ordered = [...unique.values()].sort((left, right) => left.sequence - right.sequence);
  // Route events are the source of truth for the execution path. Keep every route,
  // while bounding the remaining high-volume telemetry shown in the timeline.
  const recent = ordered.slice(-MAX_EVENTS);
  const retained = new Map(recent.map((event) => [event.event_id || String(event.sequence), event]));
  for (const event of ordered) {
    if (event.event_type === "route_selected") retained.set(event.event_id || String(event.sequence), event);
  }
  const items = [...retained.values()].sort((left, right) => left.sequence - right.sequence);
  return { items, lastSequence: items.at(-1)?.sequence ?? 0 };
}
