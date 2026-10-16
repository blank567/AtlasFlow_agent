import { AgentEvaluationWorkspace } from "../../../components/evaluations/agent-workspace";
import { EvaluationNavigation } from "../../../components/evaluations/evaluation-common";
import { PageHeader } from "../../../components/ui";

export default function AgentEvaluationPage() {
  return <main className="pageContent"><PageHeader title="Agent实验" description="以独立场景验收任务结果、执行轨迹与工具行为。" /><EvaluationNavigation active="agent" /><AgentEvaluationWorkspace /></main>;
}
