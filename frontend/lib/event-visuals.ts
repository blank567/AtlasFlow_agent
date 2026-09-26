export type EventVisual = { label: string; color: string; tint: string };

// One stable visual identity per backend RunEventType. Colors encode type, not
// success/failure alone; labels remain present so the timeline never relies on
// color as the only signal.
export const EVENT_VISUALS: Record<string, EventVisual> = {
  run_started: { label: "运行启动", color: "#176f77", tint: "#e4f2f1" },
  node_started: { label: "节点开始", color: "#4464a0", tint: "#eaf0fa" },
  node_succeeded: { label: "节点完成", color: "#29816c", tint: "#e6f4ed" },
  node_failed: { label: "节点失败", color: "#b74756", tint: "#fbe9ec" },
  plan_created: { label: "计划生成", color: "#7059a3", tint: "#f0ebf8" },
  approval_required: { label: "等待审批", color: "#a46d25", tint: "#fbf1df" },
  approval_resolved: { label: "审批完成", color: "#82723b", tint: "#f4f1df" },
  task_scheduled: { label: "任务调度", color: "#8066af", tint: "#f0ecf8" },
  task_completed: { label: "任务完成", color: "#277d54", tint: "#e8f3e9" },
  review_decided: { label: "审查决策", color: "#aa526e", tint: "#f8eaf0" },
  route_selected: { label: "路由选择", color: "#547d99", tint: "#eaf1f5" },
  quality_evaluated: { label: "质量评估", color: "#9c623d", tint: "#f8ede7" },
  trace_segment_updated: { label: "追踪更新", color: "#606c91", tint: "#edf0f8" },
  run_completed: { label: "运行完成", color: "#167451", tint: "#e2f3e9" },
  run_degraded: { label: "有警告完成", color: "#a6771d", tint: "#fbf3df" },
  run_failed: { label: "运行失败", color: "#a52d43", tint: "#fbe6e9" },
  run_cancelled: { label: "运行取消", color: "#85657b", tint: "#f2eaf0" },
};

export function eventVisual(type: string): EventVisual {
  const known = EVENT_VISUALS[type];
  if (known) return known;
  let hash = 0;
  for (const char of type) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  const hue = hash % 360;
  return { label: type || "其他事件", color: `hsl(${hue} 38% 34%)`, tint: `hsl(${hue} 42% 94%)` };
}
