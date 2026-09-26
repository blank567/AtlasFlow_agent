import type { CSSProperties } from "react";
import { formatDate } from "../lib/format";
import { eventVisual } from "../lib/event-visuals";
import { RunEvent } from "../lib/types";
import { EmptyState } from "./ui";

export function EventTimeline({ events }: { events: RunEvent[] }) {
  if (!events.length) return <EmptyState title="等待第一个事件" description="SSE 建立连接后，Agent 状态变化会按真实顺序出现在这里。" />;
  return (
    <ol className="eventTimeline">
      {[...events].reverse().map((event) => {
        const visual = eventVisual(event.event_type);
        return <li key={event.event_id} style={{ "--event-color": visual.color, "--event-tint": visual.tint } as CSSProperties}>
          <div className="eventIcon"><span /></div>
          <div className="eventBody"><div><strong className="eventTypeLabel" title={event.event_type}>{visual.label}</strong><time>{formatDate(event.created_at)}</time></div><p>{event.message}</p><small>#{event.sequence} · {event.agent ?? event.node ?? "system"}{event.task_id ? ` · ${event.task_id}` : ""}</small></div>
        </li>;
      })}
    </ol>
  );
}
