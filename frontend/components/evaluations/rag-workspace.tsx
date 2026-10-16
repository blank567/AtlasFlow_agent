"use client";

import { useEffect, useMemo, useState } from "react";
import { useEvaluationCatalog, useEvaluationSession } from "../../hooks/use-evaluation-v2";
import { listKnowledgeSpaces, listKnowledgeSpaceVersions } from "../../lib/api";
import { EvaluationArtifact, EvaluationBudget, EvaluationCase, EvaluationPlan, EvaluationProtocol, RagVariant, asText, items, record } from "../../lib/evaluations-v2";
import { KnowledgeSpace, KnowledgeSpaceVersion } from "../../lib/types";
import { DEFAULT_RETRIEVAL_SETTINGS, RetrievalControls, retrievalSettingsError } from "../retrieval-controls";
import { BudgetFields, CaseSelection, ExecutionOptions, ExperimentHistory, ExperimentResultHeader, GoldDetails, LaunchExperiment, MarkdownArtifact, ModelSelection, ResultCells, ResultDetails, WithinExperimentComparison, budgetError } from "./evaluation-common";

const VARIANTS = [
  { variant_id: "lexical", name: "关键词", retrieval_tuning: { lexical_weight: 1, vector_weight: 0, query_expansion_enabled: false, rerank_enabled: false } },
  { variant_id: "semantic", name: "语义", retrieval_tuning: { lexical_weight: 0, vector_weight: 1, query_expansion_enabled: false, rerank_enabled: false } },
  { variant_id: "baseline", name: "混合召回", retrieval_tuning: { query_expansion_enabled: false, rerank_enabled: false } },
  { variant_id: "reranked", name: "混合与重排", retrieval_tuning: { query_expansion_enabled: false, rerank_enabled: true } },
  { variant_id: "expanded", name: "混合与查询扩展", retrieval_tuning: { query_expansion_enabled: true, rerank_enabled: false } },
  { variant_id: "candidate", name: "混合、扩展与重排", retrieval_tuning: { query_expansion_enabled: true, rerank_enabled: true } },
];
const PRIMARY_CUTOFFS = [1, 5];

const VARIANT_DETAILS: Record<string, string> = { lexical: "只保留关键词召回，观察词面匹配。", semantic: "只保留向量召回，观察语义匹配。", baseline: "关键词与语义融合；不扩展、不重排。", reranked: "相对混合基线，只增加模型重排。", expanded: "相对混合基线，只增加中英查询扩展。", candidate: "同时启用扩展与重排，观察组合效果。" };

export function RagEvaluationWorkspace() {
  const catalog = useEvaluationCatalog("rag");
  const [datasetId, setDatasetId] = useState(""), [caseIds, setCaseIds] = useState<string[]>([]);
  const [protocol, setProtocol] = useState<EvaluationProtocol>("rag-retrieval-v2");
  const [spaces, setSpaces] = useState<KnowledgeSpace[]>([]), [space, setSpace] = useState(""), [versionId, setVersionId] = useState("");
  const [versions, setVersions] = useState<KnowledgeSpaceVersion[]>([]), [spaceError, setSpaceError] = useState("");
  const [variantIds, setVariantIds] = useState(["baseline", "candidate"]), [settings, setSettings] = useState(DEFAULT_RETRIEVAL_SETTINGS);
  const [budget, setBudget] = useState<EvaluationBudget>({ max_http_attempts: 60, max_elapsed_seconds: 1200, case_timeout_seconds: 90, concurrency: 2 });
  const [judge, setJudge] = useState(false), cutoffs = PRIMARY_CUTOFFS;
  const [model, setModel] = useState(""), [contextChars, setContextChars] = useState(12000), [answerTokens, setAnswerTokens] = useState(4096);
  const answerPromptVersion = catalog.settings?.answer_defaults?.prompt_version ?? "rag-answer-v2.1";
  const dataset = catalog.datasets.find(item => item.dataset_id === datasetId);
  useEffect(() => { if (catalog.datasets.length) setDatasetId(previous => previous || catalog.datasets[0].dataset_id); }, [catalog.datasets]);
  useEffect(() => {
    if (!catalog.settings) return;
    setBudget(catalog.settings.budget);
    if (catalog.settings.answer_defaults) {
      setAnswerTokens(catalog.settings.answer_defaults.max_tokens);
      setContextChars(catalog.settings.answer_defaults.max_context_chars);
    }
  }, [catalog.settings]);
  useEffect(() => { let disposed = false; listKnowledgeSpaces().then(value => { if (!disposed) setSpaces(value); }).catch(reason => { if (!disposed) setSpaceError(reason.message); }); return () => { disposed = true; }; }, []);
  useEffect(() => { setCaseIds(dataset?.cases.slice(0, 5).map(item => item.case_id) ?? []); setSpace(dataset?.space ?? spaces[0]?.slug ?? ""); }, [datasetId, catalog.datasets, spaces]);
  useEffect(() => { let disposed = false; setVersionId(""); setVersions([]); if (space) listKnowledgeSpaceVersions(space).then(value => { if (!disposed) setVersions(value); }).catch(reason => { if (!disposed) setSpaceError(reason.message); }); return () => { disposed = true; }; }, [space]);
  useEffect(() => { if (protocol === "rag-retrieval-v2") setJudge(false); }, [protocol]);
  const variants = useMemo<RagVariant[]>(() => VARIANTS.filter(variant => variantIds.includes(variant.variant_id)).map(variant => {
    const { result_limit, ...tuning } = settings;
    return { ...variant, retrieval_tuning: { ...tuning, ...variant.retrieval_tuning }, retrieval_limit: result_limit, ...(protocol === "rag-answer-v2" ? { generator: { ...(model.trim() ? { model: model.trim() } : {}), prompt_version: answerPromptVersion, max_context_chars: contextChars, max_tokens: answerTokens } } : {}) };
  }), [variantIds, settings, protocol, model, contextChars, answerTokens, answerPromptVersion]);
  const plan: EvaluationPlan = useMemo(() => ({ kind: "rag", protocol, dataset_id: datasetId, mode: "live", case_ids: caseIds, variants, budget, space, space_version_id: versionId || null, input_refs: {}, repetitions: 1, allow_degraded: false, confirm_live: true, judge_enabled: protocol === "rag-answer-v2" && judge, confirm_judge: protocol === "rag-answer-v2" && judge, langsmith_sync: false, cutoffs }), [protocol, datasetId, caseIds, variants, budget, space, versionId, judge, cutoffs]);
  const session = useEvaluationSession("rag", plan);
  const validation = variants.map(variant => retrievalSettingsError({ ...settings, ...variant.retrieval_tuning, result_limit: variant.retrieval_limit })).find(Boolean) || budgetError(budget) || (!space ? "请选择知识空间。" : "") || (!cutoffs.length ? "至少选择一个排名截断值。" : "") || (Math.max(...cutoffs) > settings.result_limit ? "采集的返回片段数不能少于所评估的最大K值。" : "") || (protocol === "rag-answer-v2" && (!Number.isInteger(contextChars) || contextChars < 1000 || contextChars > 40000 || !Number.isInteger(answerTokens) || answerTokens < 128 || answerTokens > 4096) ? "回答上下文限1000—40000字符，输出上限限128—4096Token。" : "");
  const controls = { ...settings, rerank_enabled: variants.some(variant => variant.retrieval_tuning.rerank_enabled), query_expansion_enabled: variants.some(variant => variant.retrieval_tuning.query_expansion_enabled) };
  const selectedDataset = catalog.datasets.find(item => item.dataset_id === session.selected?.plan.dataset_id && item.dataset_hash === session.selected?.dataset_hash);
  const selectedGold = selectedDataset?.cases.find(item => item.case_id === session.selectedCase?.case_id);
  return <div className="evaluationV2 ragEvaluation" data-evaluation-kind="rag">
    {(catalog.error || spaceError || session.error) && <div className="inlineAlert errorAlert" role="alert">{catalog.error || spaceError || session.error}</div>}
    <div className="experimentIntro"><p>检索定位与答案质量分开验证。文献命中仅代表来源覆盖，不能代替答案段落命中。</p><span>协议版本v2</span></div>
    <div className="experimentSetup"><section className="experimentSection experimentConfiguration"><h2>RAG实验配置</h2><fieldset className="experimentProtocol"><legend>实验协议</legend><label><input type="radio" name="rag-protocol" checked={protocol === "rag-retrieval-v2"} onChange={() => setProtocol("rag-retrieval-v2")} /><span><strong>证据检索</strong><small>召回、排序与上下文定位；不生成答案。</small></span></label><label><input type="radio" name="rag-protocol" checked={protocol === "rag-answer-v2"} onChange={() => setProtocol("rag-answer-v2")} /><span><strong>检索与回答</strong><small>固定生成器，分别检查回答与引用质量。</small></span></label></fieldset>
      <div className="evalFields"><label className="field"><span>评测题集</span><select aria-label="评测题集" value={datasetId} onChange={event => setDatasetId(event.target.value)}>{catalog.datasets.map(item => <option key={item.dataset_id} value={item.dataset_id}>{item.name}（{item.cases.length}题）</option>)}</select></label><label className="field"><span>知识空间</span><select aria-label="知识空间" value={space} onChange={event => setSpace(event.target.value)}>{spaces.map(item => <option key={item.id} value={item.slug}>{item.name}</option>)}</select></label><label className="field"><span>空间版本</span><select aria-label="空间版本" value={versionId} onChange={event => setVersionId(event.target.value)}><option value="">固定当前已发布清单</option>{versions.map(item => <option key={item.id} value={item.id}>v{item.version_number} {item.note}</option>)}</select></label></div>
      <fieldset className="experimentVariantChoices"><legend>检索对照组</legend>{VARIANTS.map(variant => <label key={variant.variant_id}><input type="checkbox" checked={variantIds.includes(variant.variant_id)} onChange={() => setVariantIds(previous => previous.includes(variant.variant_id) ? previous.filter(id => id !== variant.variant_id) : [...previous, variant.variant_id])} /><span>{variant.name}<small>{VARIANT_DETAILS[variant.variant_id]}</small></span></label>)}</fieldset>
      <p className="experimentCutoffs">主指标统一按前5条证据评估，另看第1条是否命中。返回片段数至少为5；不再平铺多档K值。</p>
      {protocol === "rag-answer-v2" && <fieldset className="experimentGenerator"><legend>固定回答生成器</legend><p>对照组共享生成模型、提示词和上下文预算；默认4096Token是输出上限，不要求写满。</p><div className="evalFields"><ModelSelection label="回答模型" value={model} onChange={setModel} settings={catalog.settings} /><label className="field"><span>上下文字符上限</span><input type="number" min={1000} max={40000} value={contextChars} onChange={event => setContextChars(Number(event.target.value))} /></label><label className="field"><span>回答Token上限</span><input aria-label="回答Token上限" type="number" min={128} max={4096} value={answerTokens} onChange={event => setAnswerTokens(Number(event.target.value))} /></label></div></fieldset>}
      <ExecutionOptions liveOnly judge={judge} setJudge={setJudge} settings={catalog.settings} semantic={protocol === "rag-answer-v2"} />
      <BudgetFields value={budget} setValue={setBudget} /></section>
      <div className="experimentControlRail"><RetrievalControls prefix="rag-eval-v2" settings={controls} setSettings={setSettings} fixedFeatures note="通道、扩展和重排由各对照组决定。其余参数固定；向右增加关键词权重。" /><section className="experimentParameterGuide"><h3>参数与对照口径</h3><dl><div><dt>返回片段与K</dt><dd>返回量是采集深度，K是评分截断。评@10必须实际采集至少10条。</dd></div><div><dt>融合候选与重排候选</dt><dd>先召回融合，再从候选中重排；返回量不应超过实际候选上限。</dd></div><div><dt>权重与RRF</dt><dd>权重决定两路名次贡献；RRF常数调节排名差距影响，不是相似度阈值。</dd></div><div><dt>扩展与重排</dt><dd>扩展补充中英术语，重排重新排序。单项组用于归因，组合组观察整体变化。</dd></div></dl></section></div>
    </div>
    <CaseSelection dataset={dataset} caseIds={caseIds} setCaseIds={setCaseIds} plan={plan} />
    <LaunchExperiment session={session} plan={plan} validation={validation} /><ExperimentHistory session={session} />
    {session.selected && <section className="experimentSection experimentResults"><ExperimentResultHeader session={session} /><WithinExperimentComparison experiment={session.selected} /><ResultCells session={session} dataset={selectedDataset} columns={session.selected.protocol === "rag-answer-v2" ? ["answer_correctness", "faithfulness", "citation_support"] : ["anchor_coverage_at_5", "anchor_hit_at_1", "anchor_mrr_at_5"]} /><ResultDetails session={session}>{session.artifact && <RagArtifact artifact={session.artifact} gold={selectedGold} />}</ResultDetails></section>}
  </div>;
}

function RagArtifact({ artifact, gold }: { artifact: EvaluationArtifact; gold?: EvaluationCase }) {
  const content = record(artifact.content), search = record(content.search_result ?? content.retrieval ?? content.search ?? content);
  const hits = items(search.hits), trace = record(search.trace);
  const response = content.response ?? content.answer;
  return <div className="ragArtifact"><div className="experimentEvidenceSplit"><GoldDetails value={gold} /><section className="experimentRankings"><h4>实际返回的child排名</h4>{hits.length ? hits.map((hit, index) => { const chunk = record(hit.chunk), metadata = record(chunk.metadata), source = asText(metadata.source_id ?? artifact.source_id_map?.[asText(metadata.document_id)] ?? chunk.document_id); return <article className="experimentHit" key={asText(chunk.id) || index}><header><span className="experimentRank">{index + 1}</span><div><strong>{asText(chunk.title) || source || "未命名来源"}</strong><small>{source ? `${source} / ` : ""}{chunk.page_number ? `第${asText(chunk.page_number)}页` : "页码未记录"}{Array.isArray(chunk.heading_path) ? ` / ${chunk.heading_path.join(" › ")}` : ""}</small></div></header><p className="experimentChild">{asText(chunk.content)}</p><dl className="experimentHitScores">{["lexical_score", "vector_score", "fusion_score", "rerank_score"].map(key => <div key={key}><dt>{({ lexical_score: "关键词", vector_score: "语义", fusion_score: "融合", rerank_score: "重排" } as Record<string, string>)[key]}</dt><dd>{typeof hit[key] === "number" ? Number(hit[key]).toFixed(4) : "未记录"}</dd></div>)}</dl>{Boolean(hit.context_excerpt) && <div className="experimentParent"><strong>parent补充上下文</strong><p>{asText(hit.context_excerpt)}</p></div>}</article>; }) : <p className="emptyInline">没有返回child。结合拒检判断、失败或降级原因解释，不以空结果自动判成功。</p>}</section></div>
    {Object.keys(trace).length > 0 && <section className="experimentActualPipeline"><h4>实际生效管线</h4><dl>{["generation_id", "embedding_compatibility", "lexical_candidates", "vector_candidates", "fused_candidates", "rerank_applied", "query_expansion_applied", "duration_ms"].filter(key => key in trace).map(key => <div key={key}><dt>{key}</dt><dd>{asText(trace[key])}</dd></div>)}</dl>{Array.isArray(trace.degraded) && trace.degraded.length > 0 && <p className="formError">降级：{trace.degraded.join("；")}</p>}</section>}
    {artifact.protocol === "rag-answer-v2" && <section className="experimentAnswer"><h4>本次生成的回答</h4><MarkdownArtifact content={response} />{Boolean(content.citations) && <p className="evalHint">保存的引用：{asText(content.citations)}</p>}</section>}
  </div>;
}
