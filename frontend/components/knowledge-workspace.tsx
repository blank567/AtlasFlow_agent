"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  archiveKnowledgeDocument,
  createKnowledgeSpace,
  listKnowledgeDocuments,
  listKnowledgeJobs,
  listKnowledgeSpaces,
  uploadKnowledgeDocument,
} from "../lib/api";
import { KnowledgeDocument, KnowledgeJob, KnowledgeSpace } from "../lib/types";
import { ConfirmDialog } from "./confirm-dialog";
import { RetrievalLab } from "./retrieval-lab";

const STAGE_LABELS: Record<string, string> = {
  queued: "等待处理", extracting: "读取文件", parsing: "解析结构", chunking: "生成分块",
  embedding: "生成向量", indexing: "写入索引", validating: "校验版本", ready: "已就绪",
};

export function KnowledgeWorkspace({ view = "overview", initialSpace = "" }: { view?: "overview" | "jobs" | "lab"; initialSpace?: string }) {
  const [spaces, setSpaces] = useState<KnowledgeSpace[]>([]);
  const [selected, setSelected] = useState(initialSpace);
  const [selectionReady, setSelectionReady] = useState(false);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [jobs, setJobs] = useState<KnowledgeJob[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [archiveTarget, setArchiveTarget] = useState<KnowledgeDocument | null>(null);

  const refresh = useCallback(async () => {
    try {
      const nextSpaces = await listKnowledgeSpaces();
      setSpaces(nextSpaces);
      const slug = nextSpaces.some((space) => space.slug === selected) ? selected : nextSpaces[0]?.slug || "";
      if (slug && slug !== selected) setSelected(slug);
      if (slug) {
        const [nextDocuments, nextJobs] = await Promise.all([
          listKnowledgeDocuments(slug), listKnowledgeJobs(slug),
        ]);
        setDocuments(nextDocuments);
        setJobs(nextJobs);
      } else {
        setDocuments([]);
        setJobs([]);
      }
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "知识库状态读取失败");
    } finally {
      setLoading(false);
    }
  }, [selected]);

  useEffect(() => {
    if (!initialSpace) {
      const remembered = window.sessionStorage.getItem("atlasflow.knowledge.space");
      if (remembered) setSelected(remembered);
    }
    setSelectionReady(true);
  }, [initialSpace]);
  useEffect(() => {
    if (selected) window.sessionStorage.setItem("atlasflow.knowledge.space", selected);
  }, [selected]);
  useEffect(() => { if (selectionReady) void refresh(); }, [refresh, selectionReady]);
  useEffect(() => {
    if (!jobs.some((job) => !["completed", "failed_terminal", "cancelled"].includes(job.status))) return;
    const timer = window.setInterval(() => void refresh(), 1200);
    return () => window.clearInterval(timer);
  }, [jobs, refresh]);

  const current = spaces.find((space) => space.slug === selected);
  const readyCount = documents.filter((item) => item.current_version_id && item.status === "active").length;

  if (loading) return <div className="knowledgeLoading">正在读取知识索引…</div>;

  return <>
    {error && <div className="inlineAlert errorAlert"><strong>读取失败</strong><span>{error}</span></div>}
    <div className="knowledgeToolbar">
      <label><span>{view === "lab" ? "检索空间" : "当前空间"}</span><select value={selected} onChange={(event) => setSelected(event.target.value)} disabled={spaces.length === 0}>{spaces.length === 0 && <option value="">暂无知识空间</option>}{spaces.map((space) => <option value={space.slug} key={space.id}>{space.name} · {space.slug}</option>)}</select></label>
      <div><strong>{readyCount}</strong><span>已发布文档</span></div>
      <div><strong>{jobs.filter((job) => job.status === "running" || job.status === "queued").length}</strong><span>处理中</span></div>
      <button className="secondaryButton" type="button" onClick={() => void refresh()}>刷新状态</button>
    </div>
    {view === "overview" && <Overview current={current} documents={documents} jobs={jobs} onUploaded={refresh} onCreate={refresh} onArchive={setArchiveTarget} />}
    {view === "jobs" && <JobLedger jobs={jobs} documents={documents} />}
    {view === "lab" && <RetrievalLab space={selected} />}
    {archiveTarget && <ConfirmDialog title="归档这份文档？" description="归档后它会立即停止参与检索，但版本和审计记录仍然保留，可以通过后端重新激活。" confirmLabel="归档文档" onCancel={() => setArchiveTarget(null)} onConfirm={() => { void archiveKnowledgeDocument(archiveTarget.id).then(() => { setArchiveTarget(null); return refresh(); }).catch((reason) => setError(reason instanceof Error ? reason.message : "归档失败")); }} />}
  </>;
}

function Overview({ current, documents, jobs, onUploaded, onCreate, onArchive }: { current?: KnowledgeSpace; documents: KnowledgeDocument[]; jobs: KnowledgeJob[]; onUploaded: () => Promise<void>; onCreate: () => Promise<void>; onArchive: (value: KnowledgeDocument) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [formError, setFormError] = useState("");
  async function upload() {
    if (!current || !file) return;
    setSubmitting(true); setFormError("");
    try { await uploadKnowledgeDocument(current.slug, file); setFile(null); await onUploaded(); }
    catch (reason) { setFormError(reason instanceof Error ? reason.message : "上传失败"); }
    finally { setSubmitting(false); }
  }
  return <div className="knowledgeGrid">
    <aside className="knowledgeIndex">
      <div className="knowledgeSectionHead"><div><h2>空间索引</h2><p>{current?.description || "这个空间还没有说明。"}</p></div><div className="knowledgeSectionActions"><Link href="/knowledge/lab" className="textButton">测试检索</Link><button type="button" className="textButton" onClick={() => setShowCreate((value) => !value)}>新建空间</button></div></div>
      {showCreate && <CreateSpaceForm onCreated={async () => { setShowCreate(false); await onCreate(); }} />}
      <dl className="knowledgeDefinition"><div><dt>可见性</dt><dd>{current?.visibility || "—"}</dd></div><div><dt>Embedding</dt><dd>{current?.embedding_profile || "—"}</dd></div><div><dt>检索配置</dt><dd>{current?.retrieval_profile || "—"}</dd></div><div><dt>索引世代</dt><dd>{current?.active_generation_id?.slice(0, 8) || "尚未建立"}</dd></div></dl>
      <div className="knowledgeDrop"><input aria-label="选择知识文档" type="file" accept=".txt,.md,.html,.htm,.pdf" onChange={(event) => setFile(event.target.files?.[0] || null)} /><span>{file ? file.name : "选择 TXT、Markdown、HTML 或 PDF"}</span><small>单个文件不超过 25 MB；原始文件只保存在 E 盘。</small><button type="button" className="primaryButton" disabled={!file || submitting || !current} onClick={() => void upload()}>{submitting ? "正在提交" : "导入文档"}</button>{formError && <p className="formError">{formError}</p>}</div>
    </aside>
    <section className="knowledgeLedger"><div className="knowledgeSectionHead"><div><h2>文档台账</h2><p>只显示用户明确导入的内容；系统没有默认演示语料。</p></div><span>{documents.length} 份</span></div>{documents.length === 0 ? <div className="knowledgeEmpty"><strong>空间仍然是空的</strong><p>选择一份文档开始建立第一个可追溯版本。</p></div> : <div className="knowledgeRows">{documents.map((document) => { const job = jobs.find((item) => item.document_id === document.id); return <article key={document.id}><div><strong>{document.title}</strong><span>{document.source_id}</span></div><div className="knowledgeDocState"><i className={`knowledgeState ${document.status === "archived" ? "archived" : document.current_version_id ? "ready" : "pending"}`} />{document.status === "archived" ? "已归档" : document.current_version_id ? "可检索" : STAGE_LABELS[job?.stage || "queued"]}</div><button type="button" className="textButton" disabled={document.status === "archived"} onClick={() => onArchive(document)}>归档</button></article>; })}</div>}</section>
    <aside className="knowledgePulse"><div className="knowledgeSectionHead"><div><h2>处理状态</h2><p>最近的摄取任务</p></div></div>{jobs.slice(0, 6).map((job) => <div className="jobPulse" key={job.id}><span><i className={`knowledgeState ${job.status === "completed" ? "ready" : job.status.startsWith("failed") ? "failed" : "pending"}`} />{STAGE_LABELS[job.stage] || job.stage}</span><strong>{Math.round(job.progress * 100)}%</strong><progress value={job.progress} max={1} /></div>)}{jobs.length === 0 && <p className="quietCopy">尚无处理任务。</p>}</aside>
  </div>;
}

function CreateSpaceForm({ onCreated }: { onCreated: () => Promise<void> }) {
  const [name, setName] = useState(""); const [slug, setSlug] = useState(""); const [error, setError] = useState("");
  async function submit(event: FormEvent) { event.preventDefault(); try { await createKnowledgeSpace({ name, slug, description: "用户创建的独立知识空间。" }); await onCreated(); } catch (reason) { setError(reason instanceof Error ? reason.message : "创建失败"); } }
  return <form className="spaceCreate" onSubmit={(event) => void submit(event)}><input value={name} placeholder="空间名称" onChange={(event) => setName(event.target.value)} required /><input value={slug} placeholder="英文标识，如 llm-notes" pattern="[a-z0-9][a-z0-9-]{1,62}" onChange={(event) => setSlug(event.target.value)} required /><button className="primaryButton" type="submit">创建</button>{error && <p className="formError">{error}</p>}</form>;
}

function JobLedger({ jobs, documents }: { jobs: KnowledgeJob[]; documents: KnowledgeDocument[] }) {
  return <section className="jobLedger"><header><span>任务</span><span>文档</span><span>阶段</span><span>进度</span><span>尝试</span></header>{jobs.map((job) => <article key={job.id}><code>{job.id.slice(0, 8)}</code><strong>{documents.find((item) => item.id === job.document_id)?.title || job.document_id.slice(0, 8)}</strong><span><i className={`knowledgeState ${job.status === "completed" ? "ready" : job.status.startsWith("failed") ? "failed" : "pending"}`} />{STAGE_LABELS[job.stage] || job.stage}</span><progress value={job.progress} max={1} /><span>{job.attempt}</span>{job.error_message && <p>{job.error_message}</p>}</article>)}{jobs.length === 0 && <div className="knowledgeEmpty"><strong>没有摄取任务</strong><p>上传文档后，解析和索引阶段会显示在这里。</p></div>}</section>;
}
