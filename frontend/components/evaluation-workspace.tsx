"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { API_URL, listKnowledgeSpaces, listKnowledgeSpaceVersions, request } from "../lib/api";
import { EVAL_ARMS, EvalDataset, EvalExperiment, EvalInput, EvalPlan, EvalPreflight, EvalResult, cancelEvaluation, compareEvaluations, getExperiment, listEvalDatasets, listEvalInputs, listExperiments, preflightEvaluation, startEvaluation } from "../lib/evaluation";
import { KnowledgeSpace, KnowledgeSpaceVersion } from "../lib/types";
import { ConfirmDialog } from "./confirm-dialog";
import { DEFAULT_RETRIEVAL_SETTINGS, RetrievalControls, retrievalSettingsError } from "./retrieval-controls";

const STATUS: Record<string, string> = { planned: "等待执行", running: "评测中", completed: "已完成", partial: "部分完成", failed: "失败", cancelled: "已取消", not_evaluated: "未评测", budget_stopped: "预算停止" };
const SYNC_STATUS: Record<string, string> = { not_requested: "未同步", not_configured: "尚未配置", synced_metadata_only: "摘要已同步", failed: "同步失败" };
function metric(value: unknown) { return typeof value === "number" ? value.toFixed(3) : typeof value === "boolean" ? value ? "通过" : "未通过" : "未评测"; }
const METRICS: Record<string, string> = { source_recall_at_5: "来源Recall@5", source_recall_5: "来源Recall@5", recall_at_5: "来源Recall@5", mrr: "MRR", anchor_hit_rate: "段落命中率", answer_correctness: "答案正确性", citation_support: "引用支持", invariant_pass_rate: "契约通过率" };

export function EvaluationWorkspace() {
  const [datasets, setDatasets] = useState<EvalDataset[]>([]), [datasetId, setDatasetId] = useState("");
  const [spaces, setSpaces] = useState<KnowledgeSpace[]>([]), [space, setSpace] = useState("");
  const [versions, setVersions] = useState<KnowledgeSpaceVersion[]>([]), [versionId, setVersionId] = useState("");
  const [caseIds, setCaseIds] = useState<string[]>([]), [armIds, setArmIds] = useState(["baseline", "candidate"]);
  const [settings, setSettings] = useState(DEFAULT_RETRIEVAL_SETTINGS);
  const [mode, setMode] = useState<"replay" | "live">("replay"), [sync, setSync] = useState(false), [judge, setJudge] = useState(false);
  const [judgeAvailable, setJudgeAvailable] = useState(false);
  const [attempts, setAttempts] = useState(60), [seconds, setSeconds] = useState(1200);
  const [inputs, setInputs] = useState<EvalInput[]>([]), [inputRefs, setInputRefs] = useState<Record<string, string>>({});
  const [experiments, setExperiments] = useState<EvalExperiment[]>([]), [selected, setSelected] = useState<EvalExperiment | null>(null);
  const [selectedCase, setSelectedCase] = useState<EvalResult | null>(null);
  const [preflight, setPreflight] = useState<{ value: EvalPreflight; hash: string } | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [confirm, setConfirm] = useState<"run" | "cancel" | null>(null);
  const [baseline, setBaseline] = useState(""), [candidate, setCandidate] = useState("");
  const [compareScope, setCompareScope] = useState<"retrieval" | "index">("retrieval"), [comparing, setComparing] = useState(false);
  const [comparison, setComparison] = useState<Record<string, unknown> | null>(null);
  const pending = useRef(false), selectionRequest = useRef(0), comparisonRequest = useRef(0);
  const compareSending = useRef(false);
  const confirmedHash = useRef(""), confirmedCancelId = useRef("");
  const dataset = datasets.find(item => item.dataset_id === datasetId);
  const plan: EvalPlan = useMemo(() => ({ dataset_id: datasetId, mode, case_ids: caseIds, space, space_version_id: versionId || null,
    arms: EVAL_ARMS.filter(arm => armIds.includes(arm.arm_id)).map(arm => { const { result_limit, ...tuning } = settings; return { ...arm, result_limit, tuning: { ...tuning, ...arm.tuning } }; }),
    input_refs: inputRefs, confirm_live: mode === "live", judge_enabled: judge, confirm_judge: judge, langsmith_sync: sync,
    budget: { max_http_attempts: attempts, max_elapsed_seconds: seconds, case_timeout_seconds: 90, concurrency: 2 } }), [datasetId, mode, caseIds, space, versionId, armIds, settings, inputRefs, judge, sync, attempts, seconds]);
  const planHash = JSON.stringify(plan), currentPlan = useRef(planHash); currentPlan.current = planHash;
  const checked = preflight?.hash === planHash ? preflight.value : null;
  const validation = plan.arms.map(arm => retrievalSettingsError({ ...settings, ...arm.tuning, result_limit: arm.result_limit })).find(Boolean)
    || (!Number.isInteger(attempts) || attempts < 1 || attempts > 5000 ? "HTTP尝试上限应为1—5000之间的整数。" : "")
    || (!Number.isInteger(seconds) || seconds < 1 || seconds > 7200 ? "总时限应为1—7200秒之间的整数。" : "");
  const controls = { ...settings, rerank_enabled: plan.arms.some(arm => arm.tuning.rerank_enabled === true), query_expansion_enabled: plan.arms.some(arm => arm.tuning.query_expansion_enabled === true) };

  useEffect(() => { let disposed = false;
    Promise.all([listEvalDatasets(), listKnowledgeSpaces(), listEvalInputs(), listExperiments(), request<{ budget: EvalPlan["budget"]; judge_available: boolean }>("/evaluations/settings")]).then(([nextDatasets, nextSpaces, nextInputs, nextExperiments, config]) => {
      if (disposed) return; setDatasets(nextDatasets); setSpaces(nextSpaces); setInputs(nextInputs); setExperiments(nextExperiments);
      setDatasetId(nextDatasets[0]?.dataset_id ?? ""); setSpace(nextDatasets[0]?.space ?? nextSpaces[0]?.slug ?? "");
      setAttempts(config.budget.max_http_attempts); setSeconds(config.budget.max_elapsed_seconds);
      setJudgeAvailable(config.judge_available);
    }).catch(reason => { if (!disposed) setError(String(reason.message ?? reason)); });
    return () => { disposed = true; };
  }, []);
  useEffect(() => { setCaseIds(dataset?.cases.slice(0, 10).map(item => item.case_id) ?? []); setInputRefs({}); setSpace(spaces.find(item => item.slug === dataset?.space)?.slug ?? spaces[0]?.slug ?? ""); }, [datasetId, datasets, spaces]); // Dataset changes intentionally reset inputs.
  useEffect(() => { if (confirm === "run" && confirmedHash.current !== planHash) setConfirm(null); }, [planHash, confirm]);
  useEffect(() => { if (confirm === "cancel" && (selected?.experiment_id !== confirmedCancelId.current || !["running", "planned"].includes(selected?.status ?? ""))) setConfirm(null); }, [selected?.experiment_id, selected?.status, confirm]);
  useEffect(() => { comparisonRequest.current += 1; compareSending.current = false; setComparing(false); setComparison(null); }, [baseline, candidate, compareScope]);
  useEffect(() => { let disposed = false; setVersionId(""); setVersions([]);
    if (space) listKnowledgeSpaceVersions(space).then(value => { if (!disposed) setVersions(value); }).catch(reason => { if (!disposed) setError(String(reason.message ?? reason)); });
    return () => { disposed = true; };
  }, [space]);
  useEffect(() => { if (!selected || !["running", "planned"].includes(selected.status)) return;
    let disposed = false; const id = selected.experiment_id;
    const timer = window.setInterval(() => { getExperiment(id).then(value => { if (!disposed) { setSelected(value); setExperiments(previous => previous.map(item => item.experiment_id === id ? value : item)); } }).catch(reason => { if (!disposed) setError(String(reason.message ?? reason)); }); }, 2000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [selected?.experiment_id, selected?.status]);

  async function check() {
    if (pending.current || validation) return; pending.current = true; setBusy(true); setError(""); const hash = planHash;
    try { const value = await preflightEvaluation(plan); if (currentPlan.current === hash) { setPreflight({ value, hash }); setInputs(value.available_inputs); } }
    catch (reason) { if (currentPlan.current === hash) setError(reason instanceof Error ? reason.message : "预检未完成"); }
    finally { pending.current = false; setBusy(false); }
  }
  async function run() {
    setConfirm(null); if (pending.current || !checked?.ready || validation || confirmedHash.current !== planHash) return; pending.current = true; setBusy(true); setError("");
    try { const submitted = mode === "replay" ? { ...plan, input_refs: { ...plan.input_refs, ...checked.resolved_input_refs } } : plan; const value = await startEvaluation(submitted); setSelected(value); setSelectedCase(null); setExperiments(previous => [value, ...previous]); setInputs(await listEvalInputs()); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "实验未启动"); }
    finally { pending.current = false; setBusy(false); }
  }
  async function pick(id: string) {
    const request = ++selectionRequest.current; setSelectedCase(null); setError("");
    try { const value = await getExperiment(id); if (selectionRequest.current === request) setSelected(value); }
    catch (reason) { if (selectionRequest.current === request) setError(reason instanceof Error ? reason.message : "实验读取失败"); }
  }
  async function cancel() {
    setConfirm(null); if (!selected || pending.current || selected.experiment_id !== confirmedCancelId.current || !["running", "planned"].includes(selected.status)) return; pending.current = true; setBusy(true);
    try { const value = await cancelEvaluation(selected.experiment_id); setSelected(value); setExperiments(previous => previous.map(item => item.experiment_id === value.experiment_id ? value : item)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "取消未完成"); }
    finally { pending.current = false; setBusy(false); }
  }
  async function compare() { if (compareSending.current) return; compareSending.current = true; const id = ++comparisonRequest.current; setComparing(true); setError(""); setComparison(null); try { const result = await compareEvaluations(baseline, candidate, compareScope); if (comparisonRequest.current === id) setComparison(result); } catch (reason) { if (comparisonRequest.current === id) setError(reason instanceof Error ? reason.message : "实验不可比较"); } finally { if (comparisonRequest.current === id) { compareSending.current = false; setComparing(false); } } }
  const toggleCase = (id: string) => setCaseIds(previous => previous.includes(id) ? previous.filter(value => value !== id) : [...previous, id]);

  return <div className="evaluationWorkspace">
    {error && <div className="inlineAlert errorAlert" role="alert">{error}</div>}
    <div className="evalSetup">
      <section className="evalPlan"><h2>实验配置</h2><p className="evalHint">固定题集与知识版本，逐项比较检索表现。</p>
        <div className="evalFields"><label className="field"><span>评测题集</span><select value={datasetId} onChange={event => setDatasetId(event.target.value)}>{datasets.map(item => <option key={item.dataset_id} value={item.dataset_id}>{item.name}（{item.cases.length}题）</option>)}</select></label>
          <label className="field"><span>知识空间</span><select value={space} onChange={event => setSpace(event.target.value)}>{spaces.map(item => <option key={item.id} value={item.slug}>{item.name}</option>)}</select></label>
          <label className="field"><span>空间版本</span><select value={versionId} onChange={event => setVersionId(event.target.value)}><option value="">捕获当前已发布清单</option>{versions.map(item => <option key={item.id} value={item.id}>v{item.version_number} {item.note}</option>)}</select></label></div>
        <fieldset className="evalChoices"><legend>运行方式</legend><label><input type="radio" name="eval-mode" checked={mode === "replay"} onChange={() => setMode("replay")} />离线回放<span>读取已保存结果，不重新研究</span></label><label><input type="radio" name="eval-mode" checked={mode === "live"} onChange={() => setMode("live")} />真实评测<span>重新检索或执行Agent，可能产生费用</span></label></fieldset>
        <fieldset className="evalArms"><legend>对比配置</legend>{EVAL_ARMS.map(arm => <label key={arm.arm_id}><input type="checkbox" checked={armIds.includes(arm.arm_id)} onChange={() => setArmIds(previous => previous.includes(arm.arm_id) ? previous.filter(id => id !== arm.arm_id) : [...previous, arm.arm_id])} />{arm.name}</label>)}</fieldset>
        <div className="evalFields"><label className="field"><span>HTTP尝试上限</span><input type="number" min={1} max={5000} value={attempts} onChange={event => setAttempts(Number(event.target.value))} /></label><label className="field"><span>总时限（秒）</span><input type="number" min={1} max={7200} value={seconds} onChange={event => setSeconds(Number(event.target.value))} /></label></div>
        <div className="evalOptions"><label><input type="checkbox" disabled={!judgeAvailable} checked={judge} onChange={event => setJudge(event.target.checked)} />独立Judge评审<span>{judgeAvailable ? "额外模型调用；默认关闭" : "尚未配置独立评审模型"}</span></label><label><input type="checkbox" checked={sync} onChange={event => setSync(event.target.checked)} />同步LangSmith摘要<span>仅指标与元数据</span></label></div>
      </section>
      <RetrievalControls prefix="eval" settings={controls} setSettings={setSettings} fixedFeatures note="召回通道、扩展与重排由对比配置决定；其余参数共同固定。" />
    </div>
    <section className="evalCases"><div className="evalSectionHead"><div><h2>题目与冻结输入</h2><p>已选{caseIds.length}题，{armIds.length}组配置。离线回放需要对应的已保存结果。</p></div><button type="button" className="textButton" onClick={() => setCaseIds(caseIds.length === dataset?.cases.length ? [] : dataset?.cases.map(item => item.case_id) ?? [])}>{caseIds.length === dataset?.cases.length ? "清空选择" : "选择全部"}</button></div>
      <div className="evalTableScroll"><table className="evalTable"><thead><tr><th>选择</th><th>研究问题</th><th>标签状态</th>{mode === "replay" && plan.arms.map(arm => <th key={arm.arm_id}>{arm.name}输入</th>)}</tr></thead><tbody>{dataset?.cases.map(item => <tr key={item.case_id}><td><input type="checkbox" aria-label={`选择${item.case_id}`} checked={caseIds.includes(item.case_id)} onChange={() => toggleCase(item.case_id)} /></td><td><strong>{item.query}</strong><small>{item.case_id} / {item.tags.join("、")}</small></td><td><span className={`softBadge ${item.gold_status === "draft" ? "amber" : ""}`}>{item.gold_status === "verified" ? "来源已核验" : "待核验"}</span></td>{mode === "replay" && plan.arms.map(arm => { const slot = `${item.case_id}:${arm.arm_id}`; return <td key={arm.arm_id}><select aria-label={`${item.case_id}的${arm.name}冻结输入`} disabled={!caseIds.includes(item.case_id)} value={inputRefs[slot] ?? ""} onChange={event => setInputRefs(previous => ({ ...previous, [slot]: event.target.value }))}><option value="">自动匹配已保存输入</option>{inputs.filter(input => input.case_id === item.case_id && input.arm_id === arm.arm_id).map(input => <option key={input.input_id} value={input.input_id}>{input.created_at.slice(0, 16)} {input.provenance}</option>)}</select></td>; })}</tr>)}</tbody></table></div>
      {validation && <p className="formError" role="alert">{validation}</p>}
      <div className="evalLaunch"><p>预检不调用模型。预算耗尽会保留部分结果，不自动追加付费请求。</p><div><button className="secondaryButton" disabled={busy || !datasetId || !space || !caseIds.length || !armIds.length || Boolean(validation)} onClick={() => void check()}>{busy ? "处理中" : "检查实验条件"}</button><button className="primaryButton" disabled={busy || !checked?.ready || Boolean(validation)} onClick={() => { confirmedHash.current = planHash; setConfirm("run"); }}>开始{mode === "replay" ? "回放" : "真实评测"}</button></div></div>
      {checked && <div className={`evalPreflight ${checked.ready ? "ready" : "blocked"}`} role="status"><strong>{checked.ready ? "实验条件已核对" : "实验条件尚未满足"}</strong><p>{checked.estimated_provider_requests == null ? "Agent调用量由执行过程决定。" : `预计${checked.estimated_provider_requests}次逻辑Provider请求，HTTP重试单独计量。`}</p>{[...checked.errors, ...checked.warnings].map((message, i) => <p key={i}>{message}</p>)}</div>}
    </section>
    <section className="evalHistory"><div className="evalSectionHead"><div><h2>实验记录</h2><p>真实执行与离线回放分别记录，失败样例保留在结果中。</p></div></div>
      {!experiments.length ? <p className="emptyInline">尚无实验。选择题目与配置，完成预检后开始。</p> : <div className="evalExperimentList">{experiments.map(item => <button type="button" key={item.experiment_id} aria-pressed={selected?.experiment_id === item.experiment_id} onClick={() => void pick(item.experiment_id)}><span><strong>{item.plan.mode === "live" ? "真实评测" : "离线回放"}</strong><small>{item.created_at.slice(0, 19).replace("T", " ")}</small></span><span>{item.plan.case_ids.length}题 / {item.plan.arms.length}组</span><span className={`softBadge ${item.status === "partial" || item.status === "failed" ? "amber" : ""}`}>{STATUS[item.status] ?? item.status}</span></button>)}</div>}
      {selected && <div className="evalResults"><div className="evalSectionHead"><div><h3>本次实验结果</h3><p>{selected.experiment_id}；{STATUS[selected.status] ?? selected.status}</p></div><div className="headerActions">{["running", "planned"].includes(selected.status) && <button className="secondaryButton" disabled={busy} onClick={() => { confirmedCancelId.current = selected.experiment_id; setConfirm("cancel"); }}>停止评测</button>}<a className="secondaryButton" href={`${API_URL}/evaluations/${encodeURIComponent(selected.experiment_id)}/export?format=json`} download>下载JSON</a><a className="secondaryButton" href={`${API_URL}/evaluations/${encodeURIComponent(selected.experiment_id)}/export?format=csv`} download>下载CSV</a></div></div>
        <div className="evalExecutionStats"><span>HTTP尝试<strong>{selected.budget_ledger.http_attempts ?? 0} / {selected.plan.budget.max_http_attempts}</strong></span><span>已记录样例<strong>{selected.results.length}</strong></span><span>耗时<strong>{selected.budget_ledger.elapsed_seconds ?? 0}秒</strong></span><span>LangSmith<strong>{SYNC_STATUS[selected.sync_status] ?? selected.sync_status}</strong></span></div>
        {selected.stop_reason && <p className="inlineAlert neutralAlert">停止原因：{selected.stop_reason}</p>}
        <div className="evalTableScroll"><table className="evalTable"><thead><tr><th>题目</th><th>配置</th><th>状态</th><th>Recall@5</th><th>MRR</th><th>段落命中</th><th>查看</th></tr></thead><tbody>{selected.results.map(item => <tr key={`${item.case_id}:${item.arm_id}`}><td>{item.case_id}</td><td>{selected.plan.arms.find(arm => arm.arm_id === item.arm_id)?.name ?? item.arm_id}</td><td>{STATUS[item.status] ?? item.status}{item.replayed && <small>回放结果</small>}</td><td>{metric(item.metrics.source_recall_at_5 ?? item.metrics.source_recall_5 ?? item.metrics.recall_at_5)}</td><td>{metric(item.metrics.mrr)}</td><td>{metric(item.metrics.anchor_hit_rate)}</td><td><button className="textButton" onClick={() => setSelectedCase(item)}>结果详情</button></td></tr>)}</tbody></table></div>
        {selectedCase && <section className="evalCaseDetail"><h3>{selectedCase.case_id} / {selectedCase.arm_id}</h3>{selectedCase.error && <p className="formError">{selectedCase.error}</p>}{selectedCase.invariant_failures.map(value => <p key={value}>{value}</p>)}<dl>{Object.entries(selectedCase.metrics).map(([key, value]) => <div key={key}><dt>{METRICS[key] ?? key}</dt><dd>{metric(value)}</dd></div>)}</dl>{Object.entries(selectedCase.availability).map(([key, value]) => <p key={key}>{METRICS[key] ?? key}：{value}</p>)}{selectedCase.degraded.length > 0 && <p>降级：{selectedCase.degraded.join("、")}</p>}</section>}
      </div>}
    </section>
    {experiments.length > 1 && <section className="evalCompare"><h2>实验对比</h2><p>题集、语料与评分口径一致时，才能解释配置变化带来的差异。</p><label className="field evalCompareScope"><span>对比范围</span><select value={compareScope} onChange={event => setCompareScope(event.target.value as "retrieval" | "index")}><option value="retrieval">检索调参：同一索引快照</option><option value="index">索引迭代：相同原文与来源标签</option></select></label><div className="evalFields"><label className="field"><span>基线实验</span><select value={baseline} onChange={event => { setBaseline(event.target.value); setComparison(null); }}><option value="">选择基线</option>{experiments.map(item => <option key={item.experiment_id} value={item.experiment_id}>{item.experiment_id.slice(0, 8)} {STATUS[item.status]}</option>)}</select></label><label className="field"><span>候选实验</span><select value={candidate} onChange={event => { setCandidate(event.target.value); setComparison(null); }}><option value="">选择候选</option>{experiments.map(item => <option key={item.experiment_id} value={item.experiment_id}>{item.experiment_id.slice(0, 8)} {STATUS[item.status]}</option>)}</select></label><button className="secondaryButton" disabled={comparing || !baseline || !candidate || baseline === candidate} onClick={() => void compare()}>{comparing ? "正在比较" : "比较结果"}</button></div>{comparison && <ComparisonResult value={comparison} />}</section>}
    {confirm && <ConfirmDialog title={confirm === "cancel" ? "确认停止评测？" : mode === "live" || judge ? "确认发起真实调用？" : "确认开始回放？"} description={confirm === "cancel" ? "停止后续请求并保留已完成结果。" : `本次选择${caseIds.length}题与${armIds.length}组配置。${mode === "live" || judge ? `最多${attempts}次业务HTTP尝试、${seconds}秒，模型和工具可能产生费用。` : "读取冻结结果，不重新执行研究流程。"}`} confirmLabel={confirm === "cancel" ? "停止评测" : "开始实验"} onCancel={() => setConfirm(null)} onConfirm={() => void (confirm === "cancel" ? cancel() : run())} />}
  </div>;
}

function ComparisonResult({ value }: { value: Record<string, unknown> }) {
  const object = (item: unknown): Record<string, unknown> => item && typeof item === "object" ? item as Record<string, unknown> : {};
  const baselineArms = object(object(value.baseline).arms), candidateArms = object(object(value.candidate).arms);
  const rows = Object.keys(baselineArms).filter(arm => candidateArms[arm]).flatMap(arm => {
    const first = object(object(baselineArms[arm]).metrics), second = object(object(candidateArms[arm]).metrics);
    return Object.keys(first).filter(key => key in second).map(key => {
      const baseline = object(first[key]).mean, candidate = object(second[key]).mean;
      return { label: `${arm} / ${METRICS[key] ?? key}`, baseline, candidate,
        delta: typeof baseline === "number" && typeof candidate === "number" ? candidate - baseline : null };
    });
  });
  return <>{value.snapshots_differ === true && <p className="inlineAlert neutralAlert">两个实验使用不同索引快照；请结合构建规范解释差异。</p>}<div className="evalTableScroll"><table className="evalTable"><thead><tr><th>配置 / 指标</th><th>基线</th><th>候选</th><th>差异</th></tr></thead><tbody>{rows.map(row => <tr key={row.label}><td>{row.label}</td><td>{metric(row.baseline)}</td><td>{metric(row.candidate)}</td><td>{metric(row.delta)}</td></tr>)}</tbody></table></div><p className="evalHint">小样本探索性对比；缺少评分依据的项目不计算差异。</p></>;
}
