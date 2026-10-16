"use client";

import { Dispatch, SetStateAction } from "react";
import { KnowledgeSearchTuning } from "../lib/types";

export type RetrievalSettings = KnowledgeSearchTuning & { result_limit: number };
export const DEFAULT_RETRIEVAL_SETTINGS: RetrievalSettings = { result_limit: 5, candidate_limit: 40, rerank_limit: 20, lexical_weight: 1, vector_weight: 1, rrf_k: 60, rerank_enabled: true, query_expansion_enabled: true, query_expansion_limit: 2 };

export function retrievalSettingsError(settings: RetrievalSettings): string {
  const bounds = [[settings.result_limit, 1, 20, "返回片段"], [settings.candidate_limit, 1, 200, "融合候选"], [settings.rerank_limit, 1, 100, "重排候选"], [settings.rrf_k, 1, 200, "RRF常数"], [settings.query_expansion_limit, 1, 4, "扩展查询上限"]] as const;
  for (const [value, min, max, name] of bounds) if (!Number.isInteger(value) || value < min || value > max) return `${name}应为${min}—${max}之间的整数。`;
  if (settings.lexical_weight + settings.vector_weight <= 0) return "关键词与语义权重不能同时为0。";
  if (settings.rerank_enabled && settings.rerank_limit > settings.candidate_limit) return "重排候选不能多于融合候选。";
  if (settings.result_limit > (settings.rerank_enabled ? settings.rerank_limit : settings.candidate_limit)) return `返回片段不能多于${settings.rerank_enabled ? "重排候选" : "融合候选"}。`;
  return "";
}

export function RetrievalControls({ settings, setSettings, prefix = "rag", note = "参数仅在本次查询中生效。", fixedFeatures = false }: { settings: RetrievalSettings; setSettings: Dispatch<SetStateAction<RetrievalSettings>>; prefix?: string; note?: string; fixedFeatures?: boolean }) {
  const sum = settings.lexical_weight + settings.vector_weight;
  const share = sum > 0 ? Math.round(settings.lexical_weight / sum * 100) : 50;
  const fields = [
    ["result_limit", "返回片段", "输出证据数量上限。", 20],
    ["candidate_limit", "融合候选", "单路Top-K及融合候选上限。", 200],
    ["rerank_limit", "重排候选", "送入重排模型的片段上限。", 100],
    ["rrf_k", "RRF常数", "调节召回名次对融合评分的影响。", 200],
  ] as const;
  return <aside className="ragControls" aria-label="检索参数">
    <div className="ragControlsHeader"><div><h3>检索参数</h3><p>证据召回与排序</p></div><button type="button" onClick={() => setSettings(DEFAULT_RETRIEVAL_SETTINGS)}>恢复默认</button></div>
    <div className="ragParameterGrid">{fields.map(([key, label, description, max]) => <div className="ragControl" key={key}>
      <label htmlFor={`${prefix}-${key.replaceAll("_", "-")}`}>{label}</label><p>{description}</p>
      <input id={`${prefix}-${key.replaceAll("_", "-")}`} type="number" min={1} max={max} value={settings[key]} disabled={key === "rerank_limit" && !settings.rerank_enabled} onChange={event => setSettings(previous => ({ ...previous, [key]: Number(event.target.value) }))} />
    </div>)}
      <div className="ragWeightControl"><label htmlFor={`${prefix}-lexical-share`}>召回策略</label><p id={`${prefix}-weight-hint`}>关键词与语义召回的融合权重。</p>
        <input id={`${prefix}-lexical-share`} type="range" min={0} max={100} step={5} value={share} aria-label="关键词召回占比" aria-describedby={`${prefix}-weight-hint`} aria-valuetext={`关键词${share}%，语义${100 - share}%`} title="向右增加关键词权重，向左增加语义权重" onChange={event => { const value = Number(event.target.value); setSettings(previous => ({ ...previous, lexical_weight: value / 50, vector_weight: (100 - value) / 50 })); }} />
        <div><span>关键词{share}%</span><span>语义{100 - share}%</span></div></div>
      {!fixedFeatures && <label className="ragSwitch"><input type="checkbox" checked={settings.rerank_enabled} onChange={event => setSettings(previous => ({ ...previous, rerank_enabled: event.target.checked }))} /><span><strong>启用重排</strong><small>重新评估候选证据的相关性。</small></span></label>}
      <div className="ragExpansionControl">{!fixedFeatures && <label className="ragSwitch" htmlFor={`${prefix}-query-expansion`}><input id={`${prefix}-query-expansion`} type="checkbox" checked={settings.query_expansion_enabled} onChange={event => setSettings(previous => ({ ...previous, query_expansion_enabled: event.target.checked }))} /><span><strong>中英查询扩展</strong><small>补充术语和缩写，保留原始问题。</small></span></label>}
        <div className="ragExpansionLimit"><label htmlFor={`${prefix}-query-expansion-limit`}>扩展查询上限</label><input id={`${prefix}-query-expansion-limit`} type="number" min={1} max={4} value={settings.query_expansion_limit} disabled={!settings.query_expansion_enabled} onChange={event => setSettings(previous => ({ ...previous, query_expansion_limit: Number(event.target.value) }))} /></div><p>额外查询数，不含原始问题。</p></div>
    </div><p className="ragControlNote">{note}</p>
  </aside>;
}
