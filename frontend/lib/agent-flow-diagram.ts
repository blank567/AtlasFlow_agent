import type { FlowStageLink } from "./agent-flow-model";

// Coordinates share the SVG's 1000 × 480 viewBox. Nodes are 180 × 110.
export const STAGE_PLACEMENT: Record<string, { left: number; top: number }> = {
  plan: { left: 7, top: 75 },
  approval: { left: 41, top: 75 },
  research: { left: 75, top: 75 },
  review: { left: 75, top: 280 },
  report: { left: 41, top: 280 },
  finish: { left: 7, top: 280 },
};

const MAIN_PATHS: Record<string, string> = {
  "plan:approval": "M 250 130 H 410",
  "approval:research": "M 590 130 H 750",
  "research:review": "M 840 185 V 280",
  "review:report": "M 750 335 H 590",
  "report:finish": "M 410 335 H 250",
};

const FEEDBACK_PATHS: Record<string, string> = {
  "review:plan:replan": "M 840 390 V 435 H 30 V 130 H 70",
  "report:plan:replan": "M 500 390 V 455 H 48 V 160 H 70",
  "research:plan:replan": "M 840 75 V 28 H 160 V 75",
  "review:research:supplement": "M 930 335 H 966 V 130 H 930",
  "report:report:revise": "M 540 390 V 416 H 460 V 390",
};

export function mainDiagramPath(link: FlowStageLink): string | undefined {
  return MAIN_PATHS[`${link.source}:${link.target}`];
}

export function feedbackDiagramPath(link: FlowStageLink): string | undefined {
  return FEEDBACK_PATHS[`${link.source}:${link.target}:${link.kind}`];
}
