import { CreateRunForm } from "../components/create-run-form";
import { RecentRuns } from "../components/recent-runs";
import { PageHeader, Panel } from "../components/ui";

export default function HomePage() {
  return (
    <main className="pageContent workbenchPage">
      <PageHeader title="工作台" description="配置研究目标与编排策略，观测执行过程及报告成果。" />
      <div className="workbenchLayout">
        <section className="workbenchEditor" aria-label="研究目标与编排配置"><CreateRunForm /></section>
        <Panel title="最近运行" className="recentPanel workbenchRecent"><RecentRuns /></Panel>
      </div>
      <p className="workflowNote">编排路径：规划 → 审批 → 研究 → 审查 → 成稿。证据缺口触发补充任务，关键问题触发重新规划。</p>
    </main>
  );
}
