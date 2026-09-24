import { formatDate } from "../lib/format";
import { RunEvent } from "../lib/types";
import { EmptyState } from "./ui";

const EVENT_LABELS: Record<string, string> = {
  run_started: "Run 启动",
  node_started: "节点开始",
  node_succeeded: "节点完成",
  node_failed: "节点失败",
  plan_created: "计划生成",
  approval_required: "等待审批",
  approval_resolved: "审批完成",
  task_scheduled: "任务调度",
  task_completed: "任务完成",
  review_decided: "Critic 决策",
  route_selected: "路由选择",
  quality_evaluated: "质量评估",
  trace_segment_updated: "Trace 更新",
  run_completed: "Run 完成",
  run_degraded: "降级完成",
  run_failed: "Run 失败",
  run_cancelled: "Run 取消",
};

export function EventTimeline({ events }: { events: RunEvent[] }) {
  if (!events.length) return <EmptyState title="等待第一个事件" description="SSE 建立连接后，Agent 状态变化会按真实顺序出现在这里。" />;
  return (
    <ol className="eventTimeline">
      {[...events].reverse().map((event) => (
        <li key={event.event_id}>
          <div className={`eventIcon event-${event.event_type}`}><span /></div>
          <div className="eventBody"><div><strong>{EVENT_LABELS[event.event_type] ?? event.event_type}</strong><time>{formatDate(event.created_at)}</time></div><p>{event.message}</p><small>#{event.sequence} · {event.agent ?? event.node ?? "system"}{event.task_id ? ` · ${event.task_id}` : ""}</small></div>
        </li>
      ))}
    </ol>
  );
}
