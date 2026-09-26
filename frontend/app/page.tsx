import { CreateRunForm } from "../components/create-run-form";
import { RecentRuns } from "../components/recent-runs";
import { PageHeader, Panel } from "../components/ui";

const AGENTS = [
  ["01", "规划", "Supervisor / Planner"],
  ["02", "审批", "Human-in-the-loop"],
  ["03", "研究", "Scheduler / Researcher"],
  ["04", "审查", "Critic / Research Gate"],
  ["05", "成稿", "Synthesizer / Quality Gate"],
  ["06", "完成", "Finalizer"],
];

export default function HomePage() {
  return (
    <main className="pageContent workbenchPage">
      <PageHeader eyebrow="研究工作台" title="让研究过程有迹可循" description="从计划、任务执行到审查和报告，记录每一次 Agent 决策。" />
      <div className="homeGrid">
        <Panel title="发起研究" meta={<span className="panelHint">创建新的 Run</span>} className="createPanel"><CreateRunForm /></Panel>
        <Panel title="执行顺序" meta={<span className="liveLabel"><span className="pulseDot" /> 实时事件驱动</span>} className="agentOverview">
          <div className="agentRail">
            {AGENTS.map(([index, name, detail], position) => (
              <div className="agentStep" key={name}>
                <span className="agentIndex">{index}</span>
                <div><strong>{name}</strong><small>{detail}</small></div>
                {position < AGENTS.length - 1 && <span className="railLine" />}
              </div>
            ))}
          </div>
          <div className="observabilityNote"><span>↗</span><div><strong>可追溯的执行记录</strong><p>每次阶段切换都有事件；LangSmith Trace 提供调用详情，但不影响主流程。</p></div></div>
        </Panel>
      </div>
      <Panel title="最近运行" meta={<span className="panelHint">来自本地运行数据库</span>} className="recentPanel"><RecentRuns /></Panel>
    </main>
  );
}
