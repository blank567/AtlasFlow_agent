import { RunPolicy } from "./types";

export type PolicyPreset = "standard" | "quick" | "strict" | "custom";

export const POLICY_PRESETS: Record<Exclude<PolicyPreset, "custom">, { label: string; description: string; policy: RunPolicy }> = {
  standard: {
    label: "标准研究",
    description: "由 Planner 自主决定任务数，按证据质量触发补充。",
    policy: { required_supplement_rounds: 0, replan_requires_critical_issue: true, max_tool_calls_per_turn: 5, max_tool_calls_per_run: 20 },
  },
  quick: {
    label: "快速演示",
    description: "固定两个初始任务，减少调用量并快速展示协作流程。",
    policy: { initial_task_count: 2, required_supplement_rounds: 0, replan_requires_critical_issue: true, max_tool_calls_per_turn: 5, max_tool_calls_per_run: 20 },
  },
  strict: {
    label: "严格审查",
    description: "初始两个任务，并要求 Critic 至少补充一轮一个任务。",
    policy: {
      initial_task_count: 2,
      required_supplement_rounds: 1,
      supplement_task_count: 1,
      replan_requires_critical_issue: true,
      max_tool_calls_per_turn: 5,
      max_tool_calls_per_run: 20,
    },
  },
};
