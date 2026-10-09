"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { searchKnowledge } from "../lib/api";
import { KnowledgeSearchResult, KnowledgeSearchTuning } from "../lib/types";

type LabSettings = KnowledgeSearchTuning & { result_limit: number };
type SearchSnapshot = {
  space: string;
  query: string;
  settings: LabSettings;
  result: KnowledgeSearchResult;
};

const DEFAULT_SETTINGS: LabSettings = {
  result_limit: 5,
  candidate_limit: 40,
  rerank_limit: 20,
  lexical_weight: 1,
  vector_weight: 1,
  rrf_k: 60,
  rerank_enabled: true,
};

function settingsError(settings: LabSettings): string {
  const bounded = [
    [settings.result_limit, 1, 20, "返回片段"],
    [settings.candidate_limit, 1, 200, "融合候选"],
    [settings.rerank_limit, 1, 100, "重排候选"],
    [settings.rrf_k, 1, 200, "RRF 常数"],
  ] as const;
  for (const [value, minimum, maximum, label] of bounded) {
    if (!Number.isInteger(value) || value < minimum || value > maximum) {
      return `${label}应为 ${minimum}–${maximum} 之间的整数。`;
    }
  }
  if (settings.lexical_weight + settings.vector_weight <= 0) return "关键词与语义权重不能同时为 0。";
  if (settings.rerank_enabled && settings.rerank_limit > settings.candidate_limit) return "重排候选不能多于融合候选。";
  if (settings.result_limit > (settings.rerank_enabled ? settings.rerank_limit : settings.candidate_limit)) {
    return `返回片段不能多于${settings.rerank_enabled ? "重排候选" : "融合候选"}。`;
  }
  return "";
}

function score(value: number): string {
  return Number.isFinite(value) ? value.toFixed(3) : "—";
}

export function RetrievalLab({ space }: { space: string }) {
  const [query, setQuery] = useState("");
  const [settings, setSettings] = useState<LabSettings>(DEFAULT_SETTINGS);
  const [snapshot, setSnapshot] = useState<SearchSnapshot | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const searchId = useRef(0);

  useEffect(() => {
    searchId.current += 1;
    setSnapshot(null);
    setError("");
    setLoading(false);
  }, [space]);

  const validation = settingsError(settings);
  const stale = snapshot !== null && (
    snapshot.query !== query.trim() || snapshot.space !== space ||
    JSON.stringify(snapshot.settings) !== JSON.stringify(settings)
  );

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!space || !query.trim() || validation) return;
    const requestId = ++searchId.current;
    const submittedSettings = { ...settings };
    const submittedQuery = query.trim();
    setLoading(true);
    setError("");
    try {
      const result = await searchKnowledge({
        query: submittedQuery,
        space,
        result_limit: submittedSettings.result_limit,
        tuning: {
          lexical_weight: submittedSettings.lexical_weight,
          vector_weight: submittedSettings.vector_weight,
          rrf_k: submittedSettings.rrf_k,
          candidate_limit: submittedSettings.candidate_limit,
          rerank_limit: submittedSettings.rerank_limit,
          rerank_enabled: submittedSettings.rerank_enabled,
        },
      });
      if (requestId === searchId.current) {
        setSnapshot({ space, query: submittedQuery, settings: submittedSettings, result });
      }
    } catch (reason) {
      if (requestId === searchId.current) {
        setError(reason instanceof Error ? reason.message : "检索失败；请检查知识空间和后端状态。")
      }
    } finally {
      if (requestId === searchId.current) setLoading(false);
    }
  }

  const semanticShare = Math.round(settings.vector_weight / (settings.lexical_weight + settings.vector_weight) * 100);
  function changeSemanticShare(value: number) {
    setSettings((previous) => ({
      ...previous,
      lexical_weight: Number(((100 - value) / 50).toFixed(2)),
      vector_weight: Number((value / 50).toFixed(2)),
    }));
  }

  return <section className="ragLab" aria-label="检索实验台">
    <form className="ragQueryForm" onSubmit={(event) => void submit(event)}>
      <div className="ragQueryHeader">
        <div><h2>试一次真实检索</h2><p>调整参数后重新运行，观察哪一批证据进入最终结果。</p></div>
        <div className="ragSpaceBadge"><span>检索空间</span><strong>{space || "尚未选择"}</strong></div>
      </div>
      <label htmlFor="rag-query">检索问题</label>
      <textarea id="rag-query" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="例如：PagedAttention 怎样管理 KV Cache？" rows={2} maxLength={4000} required />
      <div className="ragQueryActions">
        <span>只检索上方空间中已发布的文档，不会写入新内容。</span>
        <button className="primaryButton" type="submit" disabled={!space || !query.trim() || Boolean(validation) || loading}>{loading ? "正在检索…" : "运行检索"}</button>
      </div>
    </form>

    <div className="ragWorkbench">
      <aside className="ragControls" aria-label="本次检索参数">
        <div className="ragControlsHeader"><div><h3>本次检索参数</h3><p>只对这一次实验生效</p></div><button type="button" onClick={() => setSettings(DEFAULT_SETTINGS)}>恢复默认</button></div>

        <div className="ragControl"><label htmlFor="rag-result-limit">返回片段</label><p>最终最多展示多少条证据。</p><input id="rag-result-limit" type="number" min={1} max={20} value={settings.result_limit} onChange={(event) => setSettings((previous) => ({ ...previous, result_limit: Number(event.target.value) }))} /></div>

        <div className="ragControl"><label htmlFor="rag-candidate-limit">融合候选</label><p>双路召回融合后，最多保留多少条进入下一步。</p><input id="rag-candidate-limit" type="number" min={1} max={200} value={settings.candidate_limit} onChange={(event) => setSettings((previous) => ({ ...previous, candidate_limit: Number(event.target.value) }))} /></div>

        <div className="ragWeightControl"><label htmlFor="rag-semantic-share">召回侧重</label><p>向左更看重关键词匹配，向右更看重语义相似。</p><input id="rag-semantic-share" type="range" min={0} max={100} step={5} value={semanticShare} onChange={(event) => changeSemanticShare(Number(event.target.value))} /><div><span>关键词 {100 - semanticShare}%</span><span>语义 {semanticShare}%</span></div></div>

        <label className="ragSwitch"><input type="checkbox" checked={settings.rerank_enabled} onChange={(event) => setSettings((previous) => ({ ...previous, rerank_enabled: event.target.checked }))} /><span><strong>启用重排</strong><small>让 Rerank 模型重新判断候选的相关性。</small></span></label>
        <div className="ragControl"><label htmlFor="rag-rerank-limit">重排候选</label><p>最多送入 Rerank 模型的片段数。</p><input id="rag-rerank-limit" type="number" min={1} max={100} value={settings.rerank_limit} disabled={!settings.rerank_enabled} onChange={(event) => setSettings((previous) => ({ ...previous, rerank_limit: Number(event.target.value) }))} /></div>

        <details className="ragAdvanced"><summary>高级融合参数</summary><div className="ragControl"><label htmlFor="rag-rrf-k">RRF 常数</label><p>值越大，名次差异对融合分的影响越小。</p><input id="rag-rrf-k" type="number" min={1} max={200} value={settings.rrf_k} onChange={(event) => setSettings((previous) => ({ ...previous, rrf_k: Number(event.target.value) }))} /></div></details>
        {validation && <p className="ragValidation" role="alert">{validation}</p>}
        <p className="ragControlNote">这里不改变知识空间的永久配置，也不影响正在执行的 Agent Run。</p>
      </aside>

      <div className="ragResults" aria-live="polite">
        {error && <div className="inlineAlert errorAlert" role="alert"><strong>检索失败</strong><span>{error}</span></div>}
        {!snapshot ? <div className="ragResultEmpty"><div className="ragEmptyMark">⌕</div><h3>结果会出现在这里</h3><p>先选好空间，输入问题，再运行检索。这里会显示每一步保留了多少片段，以及最终引用的原文。</p></div> : <SearchResults snapshot={snapshot} stale={stale} />}
      </div>
    </div>
  </section>;
}

function SearchResults({ snapshot, stale }: { snapshot: SearchSnapshot; stale: boolean }) {
  const { result, settings } = snapshot;
  const trace = result.trace;
  const statusLabel = result.decision.status === "sufficient" ? "证据充分" : result.decision.status === "partial" ? "证据有限" : "证据不足";
  const maxima = {
    lexical: Math.max(0.001, ...result.hits.map((hit) => hit.lexical_score)),
    vector: Math.max(0.001, ...result.hits.map((hit) => hit.vector_score)),
    fusion: Math.max(0.001, ...result.hits.map((hit) => hit.fusion_score)),
    rerank: Math.max(0.001, ...result.hits.map((hit) => hit.rerank_score ?? 0)),
  };
  const stages = [
    { name: "双路召回", value: `${trace.lexical_candidates} / ${trace.vector_candidates}`, note: "关键词 / 向量" },
    { name: "融合候选", value: String(trace.fused_candidates), note: `上限 ${settings.candidate_limit}` },
    { name: "模型重排", value: trace.rerank_requested ? String(trace.rerank_candidates) : "跳过", note: trace.rerank_applied ? "已完成" : trace.rerank_requested ? "未应用" : "本次关闭" },
    { name: "返回证据", value: String(result.hits.length), note: `上限 ${settings.result_limit}` },
  ];

  return <>
    <div className="ragResultsHeader"><div><h3>检索结果</h3><p>{snapshot.query}</p></div><span>{trace.duration_ms} ms</span></div>
    {stale && <div className="ragStaleNotice">问题或参数已修改；下方仍是上一次运行的结果。点击“运行检索”查看新结果。</div>}
    <div className="ragPipeline" aria-label="检索阶段统计">{stages.map((stage) => <div key={stage.name}><span>{stage.name}</span><strong>{stage.value}</strong><small>{stage.note}</small></div>)}</div>
    <div className={`ragDecision decision-${result.decision.status}`}><div><strong>{statusLabel}</strong><p>{result.decision.reasons.join("；") || "已获得多条可用上下文。"}</p></div><div><span>最高相关度</span><strong>{score(result.decision.best_relevance)}</strong><small>{result.decision.score_basis === "rerank" ? "重排分" : "归一化融合分"}</small></div></div>
    {trace.degraded.length > 0 && <div className="ragDegraded">部分检索能力降级：{trace.degraded.join("、")}。请结合下方分数判断结果。</div>}
    <div className="ragResultCaption"><h4>返回的原文片段</h4><span>分数条分别按各自最大值显示，不能跨指标直接比较。</span></div>
    {result.hits.length === 0 ? <div className="ragNoHits"><strong>没有找到可用片段</strong><p>检查空间中是否已有可检索文档，或放宽问题中的限定词后再试。</p></div> : <div className="ragHitList">{result.hits.map((hit, index) => {
      const source = hit.chunk.metadata.canonical_uri;
      const sourceUrl = typeof source === "string" && /^https?:\/\//.test(source) ? source : null;
      const location = [...hit.chunk.heading_path, hit.chunk.page_number ? `第 ${hit.chunk.page_number} 页` : ""].filter(Boolean).join(" / ");
      const scoreRows = [
        { label: "关键词", value: hit.lexical_score, max: maxima.lexical, kind: "lexical" },
        { label: "语义", value: hit.vector_score, max: maxima.vector, kind: "vector" },
        { label: "融合", value: hit.fusion_score, max: maxima.fusion, kind: "fusion" },
        ...(hit.rerank_score == null ? [] : [{ label: "重排", value: hit.rerank_score, max: maxima.rerank, kind: "rerank" }]),
      ];
      return <article className="ragHit" key={hit.chunk.id}>
        <div className="ragHitHeading"><span className="ragHitRank">{String(index + 1).padStart(2, "0")}</span><div><strong>{hit.chunk.title}</strong><small>{location || "原文片段"}</small></div>{sourceUrl && <a href={sourceUrl} target="_blank" rel="noopener noreferrer">查看来源</a>}</div>
        <p className="ragHitPreview">{hit.chunk.content}</p>
        <details className="ragHitExpand"><summary>展开完整片段</summary><p>{hit.chunk.content}</p></details>
        <div className="ragScoreRows">{scoreRows.map((row) => <div key={row.kind}><span>{row.label}</span><i><b className={row.kind} style={{ width: `${Math.max(0, Math.min(100, row.value / row.max * 100))}%` }} /></i><em>{score(row.value)}</em></div>)}</div>
      </article>;
    })}</div>}
    <p className="ragResultFooter">本次参数：关键词 {score(Number(trace.applied_parameters.lexical_weight))}，语义 {score(Number(trace.applied_parameters.vector_weight))}，RRF {trace.applied_parameters.rrf_k}。当前实验不会更新已发布索引。</p>
  </>;
}
