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
  const incoming = action.type === "hydrate" ? action.events : [...state.items, action.event];
  const unique = new Map<string, RunEvent>();
  incoming.forEach((event) => unique.set(event.event_id || String(event.sequence), event));
  const items = [...unique.values()]
    .sort((left, right) => left.sequence - right.sequence)
    .slice(-MAX_EVENTS);
  return { items, lastSequence: items.at(-1)?.sequence ?? 0 };
}
