import type { CSSProperties } from "react";
import { formatDate } from "../lib/format";
import { eventVisual } from "../lib/event-visuals";
import { RunEvent } from "../lib/types";
import { EmptyState } from "./ui";

export function EventTimeline({ events }: { events: RunEvent[] }) {
  if (!events.length) return <EmptyState title="尚无执行事件" description="实时记录Agent状态、路由决策与工具调用。" />;
  return (
    <ol className="eventTimeline">
      {[...events].reverse().map((event) => {
        const visual = eventVisual(event.event_type);
        return <li key={event.event_id} style={{ "--event-color": visual.color, "--event-tint": visual.tint } as CSSProperties}>
          <div className="eventIcon"><span /></div>
          <div className="eventBody"><div><strong className="eventTypeLabel" title={event.event_type}>{visual.label}</strong><time>{formatDate(event.created_at)}</time></div><p>{event.message}</p>{typeof event.data.error === "string" && <p className="formError eventError"><span>错误详情</span>{event.data.error}</p>}<small>#{event.sequence} · {event.agent ?? event.node ?? "system"}{event.task_id ? ` · ${event.task_id}` : ""}</small></div>
        </li>;
      })}
    </ol>
  );
}
