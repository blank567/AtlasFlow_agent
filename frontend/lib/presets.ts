import { RunPolicy } from "./types";

export type PolicyPreset = "standard" | "quick" | "strict" | "custom";

export const POLICY_PRESETS: Record<Exclude<PolicyPreset, "custom">, { label: string; description: string; policy: RunPolicy }> = {
  standard: {
    label: "标准研究",
    description: "Planner自主拆分任务，依据证据质量触发补充。",
    policy: { required_supplement_rounds: 0, replan_requires_critical_issue: true, max_tool_calls_per_turn: 10, max_tool_calls_per_run: 40 },
  },
  quick: {
    label: "精简研究",
    description: "固定两个初始任务，按审查结果确定补充需求。",
    policy: { initial_task_count: 2, required_supplement_rounds: 0, replan_requires_critical_issue: true, max_tool_calls_per_turn: 10, max_tool_calls_per_run: 40 },
  },
  strict: {
    label: "强化审查",
    description: "初始两个任务，Critic至少追加一轮补充任务。",
    policy: {
      initial_task_count: 2,
      required_supplement_rounds: 1,
      supplement_task_count: 1,
      replan_requires_critical_issue: true,
      max_tool_calls_per_turn: 10,
      max_tool_calls_per_run: 40,
    },
  },
};
