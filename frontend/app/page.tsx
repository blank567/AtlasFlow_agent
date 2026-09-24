import { CreateRunForm } from "../components/create-run-form";
import { RecentRuns } from "../components/recent-runs";
import { PageHeader, Panel } from "../components/ui";

const AGENTS = [
  ["01", "Supervisor", "控制状态流转、预算和终态"],
  ["02", "Planner", "把研究目标拆成可执行 DAG"],
  ["03", "Researcher", "并行完成任务与证据整理"],
  ["04", "Critic", "补充局部缺口或给出 Replan 理由"],
  ["05", "Quality", "对最终报告执行质量门验收"],
];

export default function HomePage() {
  return (
    <main className="pageContent">
      <PageHeader eyebrow="MULTI-AGENT WORKSPACE" title="研究工作台" description="提交一个问题，实时观察 Agent 如何规划、并行执行、审查并交付可追溯报告。" />
      <div className="homeGrid">
        <Panel title="创建研究任务" meta={<span className="softBadge">New Run</span>} className="createPanel"><CreateRunForm /></Panel>
        <Panel title="协作拓扑" meta={<span className="liveLabel"><span className="pulseDot" /> Event-driven</span>} className="agentOverview">
          <div className="agentRail">
            {AGENTS.map(([index, name, detail], position) => (
              <div className="agentStep" key={name}>
                <span className="agentIndex">{index}</span>
                <div><strong>{name}</strong><small>{detail}</small></div>
                {position < AGENTS.length - 1 && <span className="railLine" />}
              </div>
            ))}
          </div>
          <div className="observabilityNote"><span>↗</span><div><strong>LangSmith-ready</strong><p>每个 AtlasFlow Run 可关联多个 Trace Segment；观测失败不会中断主流程。</p></div></div>
        </Panel>
      </div>
      <Panel title="最近运行" meta={<span className="panelHint">快照来自本地运行数据库</span>} className="recentPanel"><RecentRuns /></Panel>
    </main>
  );
}
