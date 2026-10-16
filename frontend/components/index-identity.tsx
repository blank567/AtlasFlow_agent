"use client";
import { useEffect, useState } from "react";
import { getKnowledgeSnapshot, KnowledgeSnapshot } from "../lib/api";

export function IndexIdentity({ space, versionId, refreshKey = "" }: { space: string; versionId?: string; refreshKey?: string }) {
  const [snapshot, setSnapshot] = useState<KnowledgeSnapshot | null>(null), [error, setError] = useState("");
  useEffect(() => { let disposed = false; setSnapshot(null); setError("");
    if (space) getKnowledgeSnapshot(space, versionId).then(value => { if (!disposed) setSnapshot(value); }).catch(reason => { if (!disposed) setError(reason instanceof Error ? reason.message : "索引身份未读取"); });
    return () => { disposed = true; };
  }, [space, versionId, refreshKey]);
  if (!space) return null;
  if (error) return <p className="formError" role="alert">{error}</p>;
  if (!snapshot) return <p className="evalHint">正在核对索引身份…</p>;
  if (!snapshot.generation_id) return <section className="indexIdentity" aria-label="索引身份"><div><strong>索引身份</strong><span className="softBadge">尚无索引</span></div><p>导入并完成文档处理后，将记录首个索引世代的模型与构建身份。</p></section>;
  const spec = snapshot.build_spec;
  return <section className="indexIdentity" aria-label="索引身份"><div><strong>索引身份</strong><span className={`softBadge ${snapshot.identity_status !== "verified" ? "amber" : ""}`}>{snapshot.identity_status === "verified" ? "已核验" : snapshot.identity_status === "declared" ? "等待向量核验" : "历史身份未知"}</span></div>
    {!spec ? <p>历史记录未保存Embedding模型身份，语义检索将降级。整空间重建后可生成带身份的新索引。</p> : <dl><div><dt>Embedding模型</dt><dd>{spec.embedding.model}</dd></div><div><dt>实际维度</dt><dd>{spec.embedding.observed_dimensions ?? "待核验"}</dd></div><div><dt>解析 / 切块</dt><dd>{spec.parser_version} / {spec.chunker_version}</dd></div><div><dt>child / parent</dt><dd>{spec.child_tokens} / {spec.parent_tokens}</dd></div></dl>}
    <p title={snapshot.manifest_hash}>清单指纹：{snapshot.manifest_hash.slice(0, 16)}　世代：{snapshot.generation_id?.slice(0, 12) ?? "尚无索引"}</p></section>;
}
