"use client";

import { Dispatch, SetStateAction, useEffect, useMemo, useState } from "react";
import { useEvaluationCatalog, useEvaluationSession } from "../../hooks/use-evaluation-v2";
import { listKnowledgeSpaces, listKnowledgeSpaceVersions } from "../../lib/api";
import { AgentVariant, EvaluationArtifact, EvaluationBudget, EvaluationCase, EvaluationPlan, EvaluationProtocol, JsonRecord, asText, items, record } from "../../lib/evaluations-v2";
import { KnowledgeSpace, KnowledgeSpaceVersion } from "../../lib/types";
import { BudgetFields, CaseSelection, CheckList, ExecutionOptions, ExperimentHistory, ExperimentResultHeader, GoldDetails, LaunchExperiment, MarkdownArtifact, ModelSelection, ResultCells, ResultDetails, WithinExperimentComparison, budgetError } from "./evaluation-common";

const DEFAULT_POLICY = { planner_allow_research: false, replan_requires_critical_issue: true, max_tool_calls_per_turn: 10, max_tool_calls_per_run: 40 };

export function AgentEvaluationWorkspace() {
  const catalog = useEvaluationCatalog("agent");
  const [datasetId, setDatasetId] = useState(""), [caseIds, setCaseIds] = useState<string[]>([]), [protocol, setProtocol] = useState<EvaluationProtocol>("agent-task-v2");
  const [mode, setMode] = useState<"replay" | "live">("replay"), [model, setModel] = useState(""), [policy, setPolicy] = useState<JsonRecord>(DEFAULT_POLICY);
  const [baselineTools, setBaselineTools] = useState<string[]>([]), [candidateTools, setCandidateTools] = useState<string[]>([]);
  const [budget, setBudget] = useState<EvaluationBudget>({ max_http_attempts: 60, max_elapsed_seconds: 1200, case_timeout_seconds: 600, concurrency: 1 });
  const [repetitions, setRepetitions] = useState(1), [inputRefs, setInputRefs] = useState<Record<string, string>>({});
  const [judge, setJudge] = useState(false);
  const [knowledgeMode, setKnowledgeMode] = useState<"off" | "selected">("off"), [spaces, setSpaces] = useState<KnowledgeSpace[]>([]), [space, setSpace] = useState("");
  const [versions, setVersions] = useState<KnowledgeSpaceVersion[]>([]), [versionId, setVersionId] = useState(""), [spaceError, setSpaceError] = useState("");
  const tools = catalog.settings?.tools ?? [];
  const dataset = catalog.datasets.find(item => item.dataset_id === datasetId);
  const displayDataset = useMemo(() => dataset ? { ...dataset, cases: dataset.cases.filter(item => !item.protocol || item.protocol === protocol) } : undefined, [dataset, protocol]);
  useEffect(() => { if (catalog.datasets.length) setDatasetId(previous => previous || catalog.datasets[0].dataset_id); }, [catalog.datasets]);
  useEffect(() => {
    if (!catalog.settings) return;
    setBudget(catalog.settings.budget);
    setBaselineTools(catalog.settings.default_tool_allowlist ?? []);
    setCandidateTools(catalog.settings.recommended_candidate_tool_allowlist ?? []);
  }, [catalog.settings]);
  useEffect(() => {
    const recommended = displayDataset?.cases.filter(item => ["A05", "A06", "A08"].includes(item.case_id)) ?? [];
    const selected = (recommended.length ? recommended : displayDataset?.cases ?? []).slice(0, 2);
    setCaseIds(selected.map(item => item.case_id)); setInputRefs({});
    if (selected.some(item => item.requires_knowledge)) setKnowledgeMode("selected");
  }, [displayDataset]);
  useEffect(() => { if (protocol === "agent-contract-v2") setJudge(false); }, [protocol]);
  useEffect(() => { let disposed = false; if (knowledgeMode === "selected") listKnowledgeSpaces().then(value => { if (!disposed) { setSpaces(value); setSpace(previous => previous || dataset?.space || value[0]?.slug || ""); } }).catch(reason => { if (!disposed) setSpaceError(reason.message); }); return () => { disposed = true; }; }, [knowledgeMode, dataset?.space]);
  useEffect(() => { let disposed = false; setVersionId(""); setVersions([]); if (knowledgeMode === "selected" && space) listKnowledgeSpaceVersions(space).then(value => { if (!disposed) setVersions(value); }).catch(reason => { if (!disposed) setSpaceError(reason.message); }); return () => { disposed = true; }; }, [knowledgeMode, space]);
  const sharedPolicy = useMemo(() => ({ ...policy, knowledge_mode: knowledgeMode, knowledge_space: knowledgeMode === "selected" ? space : null }), [policy, knowledgeMode, space]);
  const variants = useMemo<AgentVariant[]>(() => [
    { variant_id: "baseline", name: "基线工具组", policy: sharedPolicy, ...(model.trim() ? { model: model.trim() } : {}), tool_allowlist: baselineTools, workflow_version: "current", prompt_version: "current" },
    { variant_id: "candidate", name: "候选工具组", policy: sharedPolicy, ...(model.trim() ? { model: model.trim() } : {}), tool_allowlist: candidateTools, workflow_version: "current", prompt_version: "current" },
  ], [sharedPolicy, model, baselineTools, candidateTools]);
  const plan: EvaluationPlan = useMemo(() => ({ kind: "agent", protocol, dataset_id: datasetId, mode, case_ids: caseIds, variants, budget, repetitions, comparison_focus: "tools", input_refs: inputRefs, allow_degraded: false, confirm_live: mode === "live", judge_enabled: protocol === "agent-task-v2" && judge, confirm_judge: protocol === "agent-task-v2" && judge, langsmith_sync: false, ...(knowledgeMode === "selected" ? { space, space_version_id: versionId || null } : {}) }), [protocol, datasetId, mode, caseIds, variants, budget, repetitions, inputRefs, judge, knowledgeMode, space, versionId]);
  const session = useEvaluationSession("agent", plan);
  const validation = budgetError(budget)
    || (!tools.length ? "服务端尚未返回工具目录，不能猜测可用工具。" : "")
    || (!Number.isInteger(repetitions) || repetitions < 1 || repetitions > 5 ? "重复次数应为1—5之间的整数。" : "")
    || (knowledgeMode === "off" && displayDataset?.cases.some(item => caseIds.includes(item.case_id) && item.requires_knowledge) ? "所选场景需要知识绑定，请选择知识空间；工具禁用请调整白名单。" : "")
    || (knowledgeMode === "selected" && !space ? "请选择绑定的知识空间。" : "")
    || (JSON.stringify([...baselineTools].sort()) === JSON.stringify([...candidateTools].sort()) ? "两个工具白名单相同，无法进行工具差异对比。请调整候选工具组。" : "")
    || (!Number.isInteger(Number(policy.max_tool_calls_per_turn)) || Number(policy.max_tool_calls_per_turn) < 1 || Number(policy.max_tool_calls_per_turn) > 10 || !Number.isInteger(Number(policy.max_tool_calls_per_run)) || Number(policy.max_tool_calls_per_run) < Number(policy.max_tool_calls_per_turn) || Number(policy.max_tool_calls_per_run) > 50 ? "每轮工具额度为1—10，每Run最多50且不小于每轮额度。" : "")
    || (model && /\s|:\/\//.test(model) ? "模型只允许已配置Provider内的标识，不接受URL。" : "");
  const selectedDataset = catalog.datasets.find(item => item.dataset_id === session.selected?.plan.dataset_id && item.dataset_hash === session.selected?.dataset_hash);
  const selectedGold = selectedDataset?.cases.find(item => item.case_id === session.selectedCase?.case_id);
  const toggle = (name: string, setValue: Dispatch<SetStateAction<string[]>>) => setValue(previous => previous.includes(name) ? previous.filter(item => item !== name) : [...previous, name]);
  return <div className="evaluationV2 agentEvaluation" data-evaluation-kind="agent">
    {(catalog.error || session.error || spaceError) && <div className="inlineAlert errorAlert" role="alert">{catalog.error || session.error || spaceError}</div>}
    <div className="experimentIntro"><p>本轮仅比较工具白名单。两组使用同一模型、RunPolicy、工作流、题集和预算，避免同时改变多个变量。</p><span>协议版本v2</span></div>
    <section className="experimentSection experimentConfiguration"><div className="evalSectionHead"><div><h2>Agent工具对照实验</h2><p>任务成功、轨迹合规与工具消耗分别解释；流程完成不等于任务目标满足。</p></div><span className="softBadge">单变量：工具白名单</span></div>
      <fieldset className="experimentProtocol"><legend>实验协议</legend><label><input type="radio" name="agent-protocol" checked={protocol === "agent-task-v2"} onChange={() => setProtocol("agent-task-v2")} /><span><strong>任务场景</strong><small>验证最终目标、必要行为与工具差异。</small></span></label><label><input type="radio" name="agent-protocol" checked={protocol === "agent-contract-v2"} onChange={() => setProtocol("agent-contract-v2")} /><span><strong>工程契约</strong><small>审批、依赖与恢复断言独立报告，不与任务分数合并。</small></span></label></fieldset>
      <div className="evalFields"><label className="field"><span>场景题集</span><select aria-label="场景题集" value={datasetId} onChange={event => setDatasetId(event.target.value)}>{catalog.datasets.map(item => <option key={item.dataset_id} value={item.dataset_id}>{item.name}</option>)}</select></label><label className="field"><span>每个场景重复次数</span><input aria-label="每个场景重复次数" type="number" min={1} max={5} value={repetitions} onChange={event => setRepetitions(Number(event.target.value))} /></label></div>
      <div className="agentSharedSettings"><h3>两组共用的执行配置</h3><p>修改此处会同时应用于基线和候选组；不会形成模型或策略差异。</p><div className="evalFields"><ModelSelection label="共同执行模型" value={model} onChange={setModel} settings={catalog.settings} /><label className="field"><span>每轮工具额度</span><input aria-label="共同每轮工具额度" type="number" min={1} max={10} value={Number(policy.max_tool_calls_per_turn)} onChange={event => setPolicy(previous => ({ ...previous, max_tool_calls_per_turn: Number(event.target.value) }))} /></label><label className="field"><span>每Run工具额度</span><input aria-label="共同每Run工具额度" type="number" min={1} max={50} value={Number(policy.max_tool_calls_per_run)} onChange={event => setPolicy(previous => ({ ...previous, max_tool_calls_per_run: Number(event.target.value) }))} /></label></div><div className="experimentOptions"><label><input type="checkbox" checked={Boolean(policy.planner_allow_research)} onChange={event => setPolicy(previous => ({ ...previous, planner_allow_research: event.target.checked }))} /><span>允许Planner轻量调研</span></label><label><input type="checkbox" checked={Boolean(policy.replan_requires_critical_issue)} onChange={event => setPolicy(previous => ({ ...previous, replan_requires_critical_issue: event.target.checked }))} /><span>仅关键缺口触发replan</span></label></div><p>工作流与提示词：当前版本；知识绑定与场景强制约束也由两组共同使用。</p></div>
      <section className="agentToolCatalog" aria-label="工具白名单对照"><div className="evalSectionHead"><div><h3>工具白名单</h3><p>目录来自服务端实际注册工具。配置就绪不代表已通过网络连通性验证。</p></div><span className="softBadge">基线{baselineTools.length}项 / 候选{candidateTools.length}项</span></div>{!tools.length ? <p className="emptyInline">正在读取工具目录；不会用预设工具名代替服务端配置。</p> : <div className="evalTableScroll"><table className="evalTable agentToolTable"><thead><tr><th>工具与用途</th><th>配置状态</th><th>基线工具组<button className="textButton" onClick={() => setBaselineTools(catalog.settings?.default_tool_allowlist ?? [])}>恢复全部可用</button></th><th>候选工具组<button className="textButton" onClick={() => setCandidateTools(catalog.settings?.recommended_candidate_tool_allowlist ?? [])}>恢复精简推荐</button></th></tr></thead><tbody>{tools.map(tool => <tr key={tool.name}><td><strong>{tool.name}</strong><small>{tool.description}</small>{tool.capabilities?.length ? <small>能力：{tool.capabilities.join("、")}</small> : null}</td><td>{tool.available ? "配置就绪" : "不可用"}{!tool.available && <small>{tool.unavailable_reason || "缺少服务端配置"}</small>}</td><td><input aria-label={`基线工具组启用${tool.name}`} type="checkbox" checked={baselineTools.includes(tool.name)} disabled={!tool.available} onChange={() => toggle(tool.name, setBaselineTools)} /></td><td><input aria-label={`候选工具组启用${tool.name}`} type="checkbox" checked={candidateTools.includes(tool.name)} disabled={!tool.available} onChange={() => toggle(tool.name, setCandidateTools)} /></td></tr>)}</tbody></table></div>}<p className="evalHint">候选组缺少场景所需能力是本次消融观察的一部分，不能将缺能力当作工具接口故障。工具多也不自动代表质量更高。</p></section>
      <fieldset className="agentKnowledge"><legend>共同知识依赖</legend><label className="field"><span>知识绑定</span><select aria-label="知识绑定" value={knowledgeMode} onChange={event => setKnowledgeMode(event.target.value as "off" | "selected")}><option value="off">关闭知识库</option><option value="selected">绑定知识空间与版本</option></select></label>{knowledgeMode === "selected" && <div className="evalFields"><label className="field"><span>知识空间</span><select aria-label="知识空间" value={space} onChange={event => setSpace(event.target.value)}>{spaces.map(item => <option key={item.id} value={item.slug}>{item.name}</option>)}</select></label><label className="field"><span>空间版本</span><select aria-label="空间版本" value={versionId} onChange={event => setVersionId(event.target.value)}><option value="">固定当前发布版本</option>{versions.map(item => <option key={item.id} value={item.id}>v{item.version_number} {item.note}</option>)}</select></label></div>}<p>现有知识研究场景需要知识绑定；两组共享同一快照，不引入RAG参数。</p></fieldset>
      <div className="agentExecutionGrid"><div><ExecutionOptions mode={mode} setMode={setMode} judge={judge} setJudge={setJudge} settings={catalog.settings} semantic={protocol === "agent-task-v2"} /></div><BudgetFields value={budget} setValue={setBudget} /></div>
    </section>
    <p className="experimentScenarioGuide">工具实验建议先选A05、A06或A08，次选A03；A01/A07仅限知识库，适合受限工具合规负例。A09/A10属于审批与真实恢复契约。题集未覆盖地图、天气或计算任务，不据此宣称这些工具的收益。语义rubric待审核时，任务成功保持未评测。</p>
    <CaseSelection label="场景与冻结轨迹" dataset={displayDataset} caseIds={caseIds} setCaseIds={setCaseIds} plan={plan} inputs={session.checked?.available_inputs ?? catalog.inputs} inputRefs={inputRefs} setInputRefs={setInputRefs} />
    <LaunchExperiment session={session} plan={plan} validation={validation} /><ExperimentHistory session={session} />
    {session.selected && <section className="experimentSection experimentResults"><ExperimentResultHeader session={session} /><div className="agentDimensionGuide"><span><strong>任务结果</strong>目标与必要事实</span><span><strong>执行轨迹</strong>审批、依赖与路由</span><span><strong>工具行为</strong>能力、参数与失败恢复</span><span><strong>效率稳定性</strong>耗时、消耗与重复结果</span></div><WithinExperimentComparison experiment={session.selected} /><ResultCells session={session} dataset={selectedDataset} columns={session.selected.protocol === "agent-contract-v2" ? ["trajectory_compliance", "tool_policy_compliance", "tool_failures"] : ["task_outcome_score", "tool_policy_compliance", "tool_executions"]} /><ResultDetails session={session}>{session.artifact && <AgentArtifact artifact={session.artifact} gold={selectedGold} checks={session.selectedCase?.checks ?? []} />}</ResultDetails></section>}
  </div>;
}

function AgentArtifact({ artifact, gold, checks }: { artifact: EvaluationArtifact; gold?: EvaluationCase; checks: JsonRecord[] }) {
  const content = record(artifact.content), run = record(content.run ?? content.run_record ?? content);
  const events = artifact.events?.length ? artifact.events : items(run.events), routes = items(run.route_history), tools = items(run.tool_calls);
  const trajectoryChecks = checks.filter(item => !asText(item.dimension ?? item.category ?? item.kind).includes("tool"));
  const toolChecks = checks.filter(item => asText(item.dimension ?? item.category ?? item.kind).includes("tool"));
  return <div className="agentArtifact"><div className="experimentEvidenceSplit"><GoldDetails value={gold} title="场景验收条件" /><div><CheckList checks={trajectoryChecks} title="轨迹与任务断言" /><CheckList checks={toolChecks} title="工具行为断言" /></div></div><section className="experimentAgentReport"><h4>冻结的最终报告</h4><p className="evalHint">执行状态：{asText(run.status) || "未记录"}。任务是否满足以独立验收依据为准。</p><MarkdownArtifact content={run.report} /></section><section className="experimentRoutes"><h4>实际执行轨迹</h4>{routes.length ? <ol>{routes.map((route, index) => <li key={index}><strong>{asText(route.source ?? route.from_node ?? route.from)} → {asText(route.target ?? route.to_node ?? route.to)}</strong><span>{asText(route.reason ?? route.decision_reason)}</span></li>)}</ol> : <p className="emptyInline">没有保存路由记录；不据此推测审批或恢复顺序正确。</p>}{events.length > 0 && <div className="experimentEventLog">{events.map((event, index) => <article key={asText(event.event_id) || index}><span>{asText(event.sequence ?? index + 1)}</span><div><strong>{asText(event.event_type ?? event.type)}</strong><p>{asText(event.message)}</p></div></article>)}</div>}</section><section className="experimentTools"><h4>工具调用事实</h4>{tools.length ? <div className="evalTableScroll"><table className="evalTable"><thead><tr><th>工具</th><th>执行者</th><th>状态</th><th>保存的依据</th></tr></thead><tbody>{tools.map((tool, index) => <tr key={index}><td>{asText(tool.tool_name ?? tool.name)}</td><td>{asText(tool.agent)} / {asText(tool.task_id)}</td><td>{asText(tool.status ?? (record(tool.result).success === true ? "成功" : "未确认"))}</td><td>{asText(record(tool.result).summary ?? tool.error ?? record(tool.result).error) || "未记录"}</td></tr>)}</tbody></table></div> : <p className="emptyInline">没有工具事实记录；零调用不自动表示更高质量。</p>}</section></div>;
}
