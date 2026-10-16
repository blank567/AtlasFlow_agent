"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { ArrowSquareOut, MagnifyingGlass } from "@phosphor-icons/react";
import { listKnowledgeSpaceVersions, searchKnowledge } from "../lib/api";
import { KnowledgeSearchResult, KnowledgeSearchTuning, KnowledgeSpaceVersion } from "../lib/types";
import { RetrievalControls } from "./retrieval-controls";
import { IndexIdentity } from "./index-identity";

type LabSettings = KnowledgeSearchTuning & { result_limit: number };
type SearchSnapshot = {
  space: string;
  space_version_id?: string;
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
  query_expansion_enabled: true,
  query_expansion_limit: 2,
};

function settingsError(settings: LabSettings): string {
  const bounded = [
    [settings.result_limit, 1, 20, "返回片段"],
    [settings.candidate_limit, 1, 200, "融合候选"],
    [settings.rerank_limit, 1, 100, "重排候选"],
    [settings.rrf_k, 1, 200, "RRF常数"],
    [settings.query_expansion_limit, 1, 4, "扩展查询上限"],
  ] as const;
  for (const [value, minimum, maximum, label] of bounded) {
    if (!Number.isInteger(value) || value < minimum || value > maximum) {
      return `${label}应为 ${minimum}-${maximum} 之间的整数。`;
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
  return Number.isFinite(value) ? value.toFixed(3) : "未记录";
}

export function RetrievalLab({ space }: { space: string }) {
  const [query, setQuery] = useState("");
  const [settings, setSettings] = useState<LabSettings>(DEFAULT_SETTINGS);
  const [snapshot, setSnapshot] = useState<SearchSnapshot | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [versions, setVersions] = useState<KnowledgeSpaceVersion[]>([]);
  const [versionId, setVersionId] = useState("");
  const searchId = useRef(0);

  useEffect(() => {
    searchId.current += 1;
    setSnapshot(null);
    setError("");
    setLoading(false);
    setVersionId(""); setVersions([]);
    let disposed = false;
    if (space) listKnowledgeSpaceVersions(space).then(value => { if (!disposed) setVersions(value); }).catch(() => { /* Search remains usable for current published content. */ });
    return () => { disposed = true; };
  }, [space]);

  const validation = settingsError(settings);
  const stale = snapshot !== null && (
    snapshot.query !== query.trim() || snapshot.space !== space || snapshot.space_version_id !== versionId ||
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
        space_version_id: versionId || null,
        result_limit: submittedSettings.result_limit,
        tuning: {
          lexical_weight: submittedSettings.lexical_weight,
          vector_weight: submittedSettings.vector_weight,
          rrf_k: submittedSettings.rrf_k,
          candidate_limit: submittedSettings.candidate_limit,
          rerank_limit: submittedSettings.rerank_limit,
          rerank_enabled: submittedSettings.rerank_enabled,
          query_expansion_enabled: submittedSettings.query_expansion_enabled,
          query_expansion_limit: submittedSettings.query_expansion_limit,
        },
      });
      if (requestId === searchId.current) {
        setSnapshot({ space, space_version_id: versionId, query: submittedQuery, settings: submittedSettings, result });
      }
    } catch (reason) {
      if (requestId === searchId.current) {
        setError(reason instanceof Error ? reason.message : "检索失败；请检查知识空间和后端状态。")
      }
    } finally {
      if (requestId === searchId.current) setLoading(false);
    }
  }

  return <section className="ragLab" aria-label="检索实验台">
    <form className="ragQueryForm" onSubmit={(event) => void submit(event)}>
      <div className="ragQueryHeader">
        <div><h2>证据检索</h2><p>检索已发布资料，核验相关性与来源定位。</p></div>
      </div>
      <label className="field"><span>检索版本</span><select value={versionId} onChange={event => { searchId.current += 1; setLoading(false); setVersionId(event.target.value); }}><option value="">当前已发布清单</option>{versions.map(version => <option key={version.id} value={version.id}>v{version.version_number} {version.note}</option>)}</select></label>
      <label htmlFor="rag-query">检索问题</label>
      <textarea id="rag-query" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="例如：PagedAttention的KVCache管理机制是什么？" rows={2} maxLength={4000} required aria-describedby="rag-query-meta" />
      <div className="ragQueryActions">
        <span id="rag-query-meta">已发布文档 · {query.length.toLocaleString()}/4,000字符</span>
        <button className="primaryButton" type="submit" disabled={!space || !query.trim() || Boolean(validation) || loading}><MagnifyingGlass size={16} aria-hidden="true" />{loading ? "正在检索…" : "运行检索"}</button>
      </div>
    </form>

    <div className="ragWorkbench">
      <div><IndexIdentity space={space} versionId={versionId || undefined} /><RetrievalControls settings={settings} setSettings={setSettings} />{validation && <p className="ragValidation" role="alert">{validation}</p>}</div>

      <div className="ragResults" aria-live="polite">
        {error && <div className="inlineAlert errorAlert" role="alert"><strong>检索失败</strong><span>{error}</span></div>}
        {!snapshot ? <div className="ragResultEmpty"><div className="ragEmptyMark"><MagnifyingGlass size={28} weight="light" aria-hidden="true" /></div><h3>等待检索</h3><p>运行查询后，在此核验召回过程、评分与来源定位。</p></div> : <SearchResults snapshot={snapshot} stale={stale} />}
      </div>
    </div>
  </section>;
}

function SearchResults({ snapshot, stale }: { snapshot: SearchSnapshot; stale: boolean }) {
  const [expandedHits, setExpandedHits] = useState<Record<string, boolean>>({});
  const { result, settings } = snapshot;
  const trace = result.trace;
  // The trace owns the executed query forms, including server normalization.
  // Never prepend the raw textarea value as another retrieval variant.
  const queryVariants = trace.query_variants?.length ? trace.query_variants : [result.query || snapshot.query];
  const expansionRecorded = typeof trace.query_expansion_requested === "boolean";
  const expansionLabel = !trace.query_expansion_requested ? "本次关闭" : trace.query_expansion_applied ? `已生成${queryVariants.length - 1}条扩展查询` : "未追加查询，保留原始问题";
  const statusLabel = result.decision.status === "sufficient" ? "证据充分" : result.decision.status === "partial" ? "证据有限" : "证据不足";
  const stages = [
    { name: "双路召回", value: `${trace.lexical_candidates} / ${trace.vector_candidates}`, note: "关键词/语义Top-K" },
    { name: "融合候选", value: String(trace.fused_candidates), note: `上限 ${settings.candidate_limit}` },
    { name: "模型重排", value: trace.rerank_requested ? String(trace.rerank_candidates) : "跳过", note: trace.rerank_applied ? "已完成" : trace.rerank_requested ? "未应用" : "本次关闭" },
    { name: "返回证据", value: String(result.hits.length), note: `上限 ${settings.result_limit}` },
  ];

  return <>
    <div className="ragResultsHeader"><div><h3>检索结果</h3><p>{snapshot.query}</p></div><span>{trace.duration_ms}ms</span></div>
    <p className="evalHint">索引身份：{trace.identity_status === "verified" ? "已核验" : trace.identity_status === "declared" ? "待核验" : "历史身份未知"}；{trace.embedding_model ?? "模型未记录"}；{trace.embedding_dimensions ?? "维度未记录"}{trace.manifest_hash ? `；清单${trace.manifest_hash.slice(0, 12)}` : ""}</p>
    {stale && <div className="ragStaleNotice">查询条件已变更。当前显示上次结果，请重新运行检索。</div>}
    <div className="ragPipeline" aria-label="检索阶段统计">{stages.map((stage) => <div key={stage.name}><span>{stage.name}</span><strong>{stage.value}</strong><small>{stage.note}</small></div>)}</div>
    {expansionRecorded && <section className="ragQueryExpansion" aria-label="查询扩展">
      <header><h4>查询扩展</h4><span>{expansionLabel}</span></header>
      <ul>{queryVariants.map((variant, index) => <li key={variant}><span>{index === 0 ? "原始问题" : "扩展查询"}</span><p>{variant}</p></li>)}</ul>
      <p className="ragExpansionNote">扩展仅用于召回，模型重排仍使用原始问题。</p>
    </section>}
    <div className={`ragDecision decision-${result.decision.status}`}><div><strong>{statusLabel}</strong><p>{result.decision.reasons.join("；") || "已获得多条可用上下文。"}</p></div><div><span>最高相关度</span><strong>{score(result.decision.best_relevance)}</strong><small>{result.decision.score_basis === "rerank" ? "重排分" : "归一化融合分"}</small></div></div>
    {trace.degraded.length > 0 && <div className="ragDegraded">检索能力降级：{trace.degraded.join("、")}</div>}
    <div className="ragResultCaption"><h4>原文证据</h4><span>{trace.score_scope === "best_query_variant" ? "关键词与语义分取查询变体中的最高原始分；融合按名次计算，非概率。" : "评分使用不同量纲，分别比较；融合分不代表概率。"}</span></div>
    {result.hits.length === 0 ? <div className="ragNoHits"><strong>未检索到有效证据</strong><p>确认空间已有发布文档，或调整查询条件。</p></div> : <div className="ragHitList">{result.hits.map((hit, index) => {
      const source = hit.chunk.metadata.canonical_uri;
      const sourceUrl = typeof source === "string" && /^https?:\/\//.test(source) ? source : null;
      const location = [...hit.chunk.heading_path, hit.chunk.page_number ? `第 ${hit.chunk.page_number} 页` : ""].filter(Boolean).join(" / ");
      const scoreRows = [
        { label: "关键词", value: hit.lexical_score, kind: "lexical" },
        { label: "语义", value: hit.vector_score, kind: "vector" },
        { label: "融合", value: hit.fusion_score, kind: "fusion" },
        { label: "重排", value: hit.rerank_score, kind: "rerank" },
      ];
      const expanded = Boolean(expandedHits[hit.chunk.id]);
      return <article className="ragHit" key={hit.chunk.id}>
        <div className="ragHitHeading"><span className="ragHitRank">{String(index + 1).padStart(2, "0")}</span><div><strong>{hit.chunk.title}</strong><small>{location || "原文片段"}</small></div>{sourceUrl && <a href={sourceUrl} target="_blank" rel="noopener noreferrer">查看来源<ArrowSquareOut size={12} aria-hidden="true" /></a>}</div>
        <p className="ragHitPreview">{hit.chunk.content}</p>
        <div className="ragHitActions"><button type="button" className="textButton" aria-expanded={expanded} aria-controls={`rag-full-text-${hit.chunk.id}`} onClick={() => setExpandedHits((previous) => ({ ...previous, [hit.chunk.id]: !previous[hit.chunk.id] }))}>{expanded ? "收起原文" : "查看完整原文"}</button><span title={hit.chunk.id}>片段 {hit.chunk.id.slice(0, 8)}</span></div>
        {expanded && <div className="ragHitFullText" id={`rag-full-text-${hit.chunk.id}`}><strong>命中原文</strong><p>{hit.chunk.content}</p>{hit.context_excerpt && <><strong>邻近上下文（辅助理解，引用以命中原文为准）</strong><p>{hit.context_excerpt}</p></>}</div>}
        <div className="ragScoreSummary"><div className="ragScoreRows">{scoreRows.map((row) => <div key={row.kind}><span>{row.label}</span><em>{row.value == null ? "未记录" : score(row.value)}</em></div>)}</div></div>
      </article>;
    })}</div>}
    <footer className="ragResultParameters"><h4>本次参数与索引</h4><dl>{[
      ["返回证据上限", settings.result_limit], ["关键词权重", trace.applied_parameters.lexical_weight], ["语义权重", trace.applied_parameters.vector_weight], ["融合候选上限", trace.applied_parameters.candidate_limit], ["重排候选上限", trace.applied_parameters.rerank_limit], ["RRF常数", trace.applied_parameters.rrf_k], ["重排开关", trace.rerank_requested ? "启用" : "关闭"], ["中英查询扩展", expansionRecorded ? trace.query_expansion_requested ? "启用" : "关闭" : "未记录"], ["扩展查询上限", trace.applied_parameters.query_expansion_limit], ["检索配置", trace.profile_id], ["索引世代", trace.generation_id || "未记录"],
    ].map(([label, value]) => <div key={String(label)}><dt>{label}</dt><dd title={value == null ? undefined : String(value)}>{value == null ? "未记录" : String(value)}</dd></div>)}</dl></footer>
  </>;
}
