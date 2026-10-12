"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  activateKnowledgeSpaceVersion,
  archiveKnowledgeDocument,
  cancelKnowledgeSpaceBuild,
  createKnowledgeSpace,
  createKnowledgeSpaceVersion,
  listKnowledgeDocuments,
  listKnowledgeJobs,
  listKnowledgeSpaceVersions,
  listKnowledgeSpaceBuilds,
  listKnowledgeSpaces,
  publishKnowledgeSpaceBuild,
  reindexKnowledgeSpace,
  retryKnowledgeJob,
  updateKnowledgeSpaceVersionNote,
  uploadKnowledgeDocument,
} from "../lib/api";
import { KnowledgeDocument, KnowledgeJob, KnowledgeSpace, KnowledgeSpaceBuild, KnowledgeSpaceVersion } from "../lib/types";
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
  const [builds, setBuilds] = useState<KnowledgeSpaceBuild[]>([]);
  const [dataSpace, setDataSpace] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [archiveTarget, setArchiveTarget] = useState<KnowledgeDocument | null>(null);
  const [archiving, setArchiving] = useState(false);
  const selectedRef = useRef(selected);
  const requestRef = useRef(0);
  const pendingRef = useRef<{ slug: string; id: number } | null>(null);
  selectedRef.current = selected;

  const refresh = useCallback(async () => {
    if (selectedRef.current !== selected || pendingRef.current?.slug === selected) return;
    const requestId = ++requestRef.current;
    pendingRef.current = { slug: selected, id: requestId };
    const isCurrent = () => selectedRef.current === selected && requestRef.current === requestId;
    try {
      const nextSpaces = await listKnowledgeSpaces();
      if (!isCurrent()) return;
      setSpaces(nextSpaces);
      const slug = nextSpaces.some((space) => space.slug === selected) ? selected : nextSpaces[0]?.slug || "";
      if (slug !== selected) {
        selectedRef.current = slug;
        setSelected(slug);
        return;
      }
      if (slug) {
        const [nextDocuments, nextJobs, nextBuilds] = await Promise.all([
          listKnowledgeDocuments(slug), listKnowledgeJobs(slug), listKnowledgeSpaceBuilds(slug),
        ]);
        if (!isCurrent()) return;
        setDocuments(nextDocuments);
        setJobs(nextJobs);
        setBuilds(nextBuilds);
      } else {
        setDocuments([]);
        setJobs([]);
        setBuilds([]);
      }
      setDataSpace(slug);
      setError("");
    } catch (reason) {
      if (isCurrent()) setError(reason instanceof Error ? reason.message : "知识库状态读取失败");
    } finally {
      if (pendingRef.current?.id === requestId) pendingRef.current = null;
      if (isCurrent()) setLoading(false);
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
  const current = spaces.find((space) => space.slug === selected);
  const activeBuild = builds.find((build) => build.id === current?.active_build_id)
    ?? builds.find((build) => ["building", "ready", "failed"].includes(build.status));
  const buildLocked = Boolean(current?.active_build_id || activeBuild);
  const stateReady = dataSpace === selected && !error;
  useEffect(() => {
    if (!buildLocked && !jobs.some((job) => !["completed", "failed_terminal", "cancelled"].includes(job.status))) return;
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [jobs, buildLocked, refresh]);

  const readyCount = documents.filter((item) => item.current_version_id && item.status === "active").length;

  function selectSpace(slug: string) {
    selectedRef.current = slug;
    setSelected(slug);
    setDataSpace(""); setDocuments([]); setJobs([]); setBuilds([]);
    setArchiveTarget(null); setError("");
  }

  async function archive(document: KnowledgeDocument) {
    if (!stateReady || buildLocked || archiving || document.space_id !== current?.id) return;
    setArchiveTarget(null); setArchiving(true);
    try { await archiveKnowledgeDocument(document.id); await refresh(); }
    catch (reason) { if (selectedRef.current === selected) setError(reason instanceof Error ? reason.message : "归档失败"); }
    finally { setArchiving(false); }
  }

  if (loading) return <div className="knowledgeLoading">正在读取知识索引…</div>;

  return <>
    {error && <div className="inlineAlert errorAlert"><strong>读取失败</strong><span>{error}</span></div>}
    <div className="knowledgeToolbar">
      <label><span>{view === "lab" ? "检索空间" : "当前空间"}</span><select value={selected} onChange={(event) => selectSpace(event.target.value)} disabled={spaces.length === 0 || archiving}>{spaces.length === 0 && <option value="">暂无知识空间</option>}{spaces.map((space) => <option value={space.slug} key={space.id}>{space.name} · {space.slug}</option>)}</select></label>
      <div><strong>{readyCount}</strong><span>已发布文档</span></div>
      <div><strong>{jobs.filter((job) => job.status === "running" || job.status === "queued").length}</strong><span>处理中</span></div>
      <button className="secondaryButton" type="button" onClick={() => void refresh()}>刷新状态</button>
    </div>
    {view === "overview" && <SpaceVersionLedger key={`versions:${current?.id || "none"}`} space={current} build={activeBuild} jobs={jobs} documents={documents} locked={buildLocked} stateReady={stateReady} busy={archiving || jobs.some((job) => ["queued", "running", "failed_retryable"].includes(job.status))} onChanged={refresh} />}
    {view === "overview" && <Overview key={`overview:${current?.id || "none"}`} current={current} documents={documents} jobs={jobs} locked={buildLocked || !stateReady || archiving} onUploaded={refresh} onCreate={refresh} onArchive={setArchiveTarget} />}
    {view === "jobs" && <JobLedger jobs={jobs} documents={documents} />}
    {view === "lab" && <RetrievalLab space={selected} />}
    {archiveTarget && !buildLocked && <ConfirmDialog title="归档这份文档？" description="归档后它会立即停止参与检索，但版本和审计记录仍然保留，可以通过后端重新激活。" confirmLabel="归档文档" onCancel={() => setArchiveTarget(null)} onConfirm={() => void archive(archiveTarget)} />}
  </>;
}

function Overview({ current, documents, jobs, locked, onUploaded, onCreate, onArchive }: { current?: KnowledgeSpace; documents: KnowledgeDocument[]; jobs: KnowledgeJob[]; locked: boolean; onUploaded: () => Promise<void>; onCreate: () => Promise<void>; onArchive: (value: KnowledgeDocument) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [formError, setFormError] = useState("");
  async function upload() {
    if (!current || !file || locked || submitting) return;
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
      <div className="knowledgeDrop"><input aria-label="选择知识文档" type="file" accept=".txt,.md,.html,.htm,.pdf" disabled={locked || submitting} onChange={(event) => setFile(event.target.files?.[0] || null)} /><span>{file ? file.name : "选择 TXT、Markdown、HTML 或 PDF"}</span><small>单个文件不超过 25 MB；原始文件只保存在 E 盘。</small><button type="button" className="primaryButton" disabled={!file || submitting || !current || locked} onClick={() => void upload()}>{submitting ? "正在提交" : "导入文档"}</button>{formError && <p className="formError">{formError}</p>}</div>
    </aside>
    <section className="knowledgeLedger"><div className="knowledgeSectionHead"><div><h2>文档台账</h2><p>当前空间的文档状态；版本切换请使用上方空间版本台账。</p></div><span>{documents.length} 份</span></div>{documents.length === 0 ? <div className="knowledgeEmpty"><strong>空间仍然是空的</strong><p>选择一份文档开始建立第一个可追溯版本。</p></div> : <div className="knowledgeRows">{documents.map((document) => { const job = jobs.find((item) => item.document_id === document.id); return <article key={document.id}><div><strong>{document.title}</strong><span>{document.source_id}</span></div><div className="knowledgeDocState"><i className={`knowledgeState ${document.status === "archived" ? "archived" : document.current_version_id ? "ready" : "pending"}`} />{document.status === "archived" ? "已归档" : document.current_version_id ? "可检索" : STAGE_LABELS[job?.stage || "queued"]}</div><button type="button" className="textButton" disabled={document.status === "archived" || locked} onClick={() => onArchive(document)}>归档</button></article>; })}</div>}</section>
    <aside className="knowledgePulse"><div className="knowledgeSectionHead"><div><h2>处理状态</h2><p>最近的摄取任务</p></div></div>{jobs.slice(0, 6).map((job) => <div className="jobPulse" key={job.id}><span><i className={`knowledgeState ${job.status === "completed" ? "ready" : job.status.startsWith("failed") ? "failed" : "pending"}`} />{STAGE_LABELS[job.stage] || job.stage}</span><strong>{Math.round(job.progress * 100)}%</strong><progress value={job.progress} max={1} /></div>)}{jobs.length === 0 && <p className="quietCopy">尚无处理任务。</p>}</aside>
  </div>;
}

function SpaceVersionLedger({ space, build, jobs, documents, busy, locked, stateReady, onChanged }: {
  space?: KnowledgeSpace;
  build?: KnowledgeSpaceBuild;
  jobs: KnowledgeJob[];
  documents: KnowledgeDocument[];
  busy: boolean;
  locked: boolean;
  stateReady: boolean;
  onChanged: () => Promise<void>;
}) {
  const [versions, setVersions] = useState<KnowledgeSpaceVersion[]>([]);
  const [note, setNote] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [working, setWorking] = useState(false);
  const [confirmTarget, setConfirmTarget] = useState<KnowledgeSpaceVersion | null>(null);
  const [buildConfirmation, setBuildConfirmation] = useState<"start" | "publish" | "cancel" | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const slug = space?.slug;
  const alive = useRef(true);
  const actionPending = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const load = useCallback(async () => {
    if (!slug) return;
    const next = await listKnowledgeSpaceVersions(slug);
    if (!alive.current) return;
    setVersions(next);
    setDrafts(Object.fromEntries(next.map((version) => [version.id, version.note])));
    setError("");
  }, [slug]);

  async function retry() {
    try { await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "空间版本读取失败"); }
  }

  useEffect(() => {
    let active = true;
    if (!slug) return;
    void listKnowledgeSpaceVersions(slug).then((next) => {
      if (!active) return;
      setVersions(next);
      setDrafts(Object.fromEntries(next.map((version) => [version.id, version.note])));
      setError(""); setMessage("");
    }).catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "空间版本读取失败"); });
    return () => { active = false; };
  }, [slug, space?.active_version_id]);

  // A keyed ledger is discarded when the space changes. Its outstanding requests
  // may finish, but must not mutate the new space's controls or confirmation state.
  async function perform(operation: () => Promise<string>) {
    if (!stateReady || actionPending.current || !alive.current) return;
    actionPending.current = true;
    setWorking(true); setError(""); setMessage("");
    setBuildConfirmation(null); setConfirmTarget(null);
    try {
      const message = await operation();
      await Promise.all([load(), onChanged()]);
      if (alive.current) setMessage(message);
    } catch (reason) {
      if (alive.current) setError(reason instanceof Error ? reason.message : "空间操作失败");
    } finally {
      actionPending.current = false;
      if (alive.current) setWorking(false);
    }
  }

  async function create() {
    if (!slug || locked || busy) return;
    await perform(async () => {
      const created = await createKnowledgeSpaceVersion(slug, note);
      if (alive.current) setNote("");
      return `空间版本 v${created.version_number} 已保存`;
    });
  }

  async function saveNote(version: KnowledgeSpaceVersion) {
    if (!slug) return;
    await perform(async () => {
      await updateKnowledgeSpaceVersionNote(slug, version.id, drafts[version.id] || "");
      return `空间版本 v${version.version_number} 的说明已保存`;
    });
  }

  async function activate(version: KnowledgeSpaceVersion) {
    if (!slug || locked || busy) return;
    await perform(async () => {
      await activateKnowledgeSpaceVersion(slug, version.id);
      return `整个空间已切换至 v${version.version_number}`;
    });
  }

  const buildJobs = build ? jobs.filter((job) => job.space_build_id === build.id || build.job_ids.includes(job.id)) : [];
  const completedCount = buildJobs.filter((job) => job.status === "completed").length;
  const failedJobs = buildJobs.filter((job) => job.status.startsWith("failed"));
  const running = buildJobs.some((job) => job.status === "running");
  const total = build?.job_ids.length ?? 0;
  const progress = total ? buildJobs.reduce((sum, job) => sum + (job.status === "completed" ? 1 : Math.max(0, Math.min(1, job.progress))), 0) / total : 0;
  const canPublish = build?.status === "ready" && total > 0 && completedCount === total;
  const activeDocumentCount = documents.filter((document) => document.status === "active").length;
  const baseVersion = versions.find((version) => version.id === build?.base_version_id);
  const disabled = working || !stateReady;

  async function startBuild() {
    if (!slug || locked || busy || activeDocumentCount === 0) return;
    await perform(async () => {
      await reindexKnowledgeSpace(slug, note);
      if (alive.current) setNote("");
      return "整个空间已开始重建；完成并发布前，检索继续使用旧版。";
    });
  }

  async function publishBuild() {
    if (!slug || !build || !canPublish) return;
    await perform(async () => {
      const version = await publishKnowledgeSpaceBuild(slug, build.id);
      return `整个空间已发布为 v${version.version_number}`;
    });
  }

  async function cancelBuild() {
    if (!slug || !build || running) return;
    await perform(async () => {
      await cancelKnowledgeSpaceBuild(slug, build.id);
      return "本次空间构建已取消；正在使用的版本保持不变。";
    });
  }

  async function retryJob(job: KnowledgeJob) {
    if (!build || !build.job_ids.includes(job.id) || !job.status.startsWith("failed")) return;
    await perform(async () => {
      await retryKnowledgeJob(job.id);
      return "失败文档已重新排队，全部完成后可发布整个空间。";
    });
  }

  if (!space) return null;
  return <section className="spaceVersionLedger" aria-label="知识空间版本">
    <div className="spaceVersionIntro"><div><h2>空间版本</h2><p>每个版本保存整个空间的文档清单、发布状态和索引世代。</p></div><span className="spaceVersionIndicator">{space.active_version_id ? `当前 v${versions.find((version) => version.id === space.active_version_id)?.version_number ?? "…"}` : versions.length ? "当前有未保存变更" : "尚无空间版本"}</span></div>
    <div className="spaceVersionCreate"><label htmlFor="space-version-note">新版本说明</label><input id="space-version-note" maxLength={500} value={note} placeholder="例如：统一解析与父子切块完成" disabled={disabled || locked} onChange={(event) => setNote(event.target.value)} /><div className="spaceVersionActions"><button type="button" className="primaryButton" disabled={disabled || busy || locked || activeDocumentCount === 0} onClick={() => setBuildConfirmation("start")}>重建整个空间</button><button type="button" className="secondaryButton" disabled={disabled || busy || locked} onClick={() => void create()}>保存当前空间版本</button></div></div>
    {!locked && <p className="spaceVersionHint">重建会按当前解析与切块规则处理全部有效文档；保存当前版本仅记录现有清单。</p>}
    {locked && <p className="spaceVersionHint">空间构建期间暂停导入、归档、保存快照与历史版本切换。检索继续使用旧版。</p>}
    {busy && !locked && <p className="spaceVersionHint">摄取任务完成后才能重建、保存或切换空间版本。</p>}
    {build && <div className="spaceBuildPanel" aria-label="整个空间构建状态">
      <div className="spaceBuildHeading"><div><strong>{build.status === "ready" ? "全部文档已就绪，等待发布" : build.status === "failed" ? "部分文档处理失败" : "正在重建整个空间"}</strong><p>正在服务{baseVersion ? ` v${baseVersion.version_number}` : "构建前的版本"}，发布时一次切换全部文档。</p></div><span>{completedCount} / {total} 份完成</span></div>
      <progress aria-label="空间重建进度" value={progress} max={1} />
      {build.note && <p className="spaceVersionHint">版本说明：{build.note}</p>}
      {failedJobs.length > 0 && <div className="spaceBuildFailures">{failedJobs.map((job) => <div key={job.id}><div><strong>{documents.find((document) => document.id === job.document_id)?.title || job.document_id}</strong><p>{job.error_message || "处理失败，请重试该文档。"}</p></div><button type="button" className="secondaryButton" disabled={disabled} onClick={() => void retryJob(job)}>重试文档</button></div>)}</div>}
      <div className="spaceVersionActions"><button type="button" className="primaryButton" disabled={disabled || !canPublish} onClick={() => setBuildConfirmation("publish")}>发布整个空间版本</button><button type="button" className="textButton" disabled={disabled || running} onClick={() => setBuildConfirmation("cancel")}>取消本次构建</button></div>
      {running && <p className="spaceVersionHint">当前文档处理结束后可取消构建。</p>}
    </div>}
    {error && <div className="spaceVersionError" role="alert"><p>{error === "Failed to fetch" ? "无法连接后端，请确认服务已启动并完成数据库迁移。" : error}</p><button type="button" className="textButton" onClick={() => void retry()}>重试读取</button></div>}
    {message && <p className="knowledgeVersionMessage" role="status">{message}</p>}
    {!error && versions.length === 0 ? <p className="spaceVersionEmpty">尚未保存空间版本。所有文档处理完成后，可将当前空间保存为第一个版本。</p> : <div className="spaceVersionRows">{versions.slice().reverse().map((version) => { const current = version.id === space.active_version_id; const draft = drafts[version.id] ?? ""; const activeCount = Object.values(version.documents).filter((item) => item.status === "active").length; return <div className="spaceVersionRow" key={version.id}><div className="spaceVersionNumber"><strong>v{version.version_number}</strong><span className={current ? "knowledgeVersionCurrent" : "knowledgeVersionStatus"}>{current ? "当前版本" : "历史版本"}</span></div><div className="spaceVersionBody"><span>{activeCount} 份可检索文档 · {new Date(version.created_at).toLocaleString("zh-CN")}</span><label><span>版本说明</span><input maxLength={500} value={draft} disabled={disabled} onChange={(event) => setDrafts((values) => ({ ...values, [version.id]: event.target.value }))} /></label></div><div className="spaceVersionActions"><button type="button" className="textButton" disabled={disabled || draft === version.note} onClick={() => void saveNote(version)}>保存说明</button>{!current && <button type="button" className="secondaryButton" disabled={disabled || busy || locked} onClick={() => setConfirmTarget(version)}>切换整个空间</button>}</div></div>; })}</div>}
    {confirmTarget && !locked && <ConfirmDialog title={`将整个空间切换到 v${confirmTarget.version_number}？`} description="空间内所有文档将按该版本的清单恢复。之后新增的文档会停止参与检索，但不会删除；索引和原文不会重算。" confirmLabel="确认切换整个空间" onCancel={() => setConfirmTarget(null)} onConfirm={() => void activate(confirmTarget)} />}
    {buildConfirmation === "start" && !locked && <ConfirmDialog title="重建整个空间？" description={`将重新解析、切块并生成当前 ${activeDocumentCount} 份有效文档的向量，可能产生模型调用费用。构建期间检索继续使用旧版，全部完成后由你发布。`} confirmLabel="开始整空间重建" onCancel={() => setBuildConfirmation(null)} onConfirm={() => void startBuild()} />}
    {buildConfirmation === "publish" && build && canPublish && <ConfirmDialog title="发布整个空间版本？" description={`本次 ${total} 份文档已全部完成。确认后整个空间将同时切换到新索引，历史版本仍可回退。`} confirmLabel="确认发布整个空间" onCancel={() => setBuildConfirmation(null)} onConfirm={() => void publishBuild()} />}
    {buildConfirmation === "cancel" && build && !running && <ConfirmDialog title="取消本次空间构建？" description="尚未执行的任务会停止，本次构建不会发布。当前检索版本保持不变。" confirmLabel="确认取消构建" tone="danger" onCancel={() => setBuildConfirmation(null)} onConfirm={() => void cancelBuild()} />}
  </section>;
}

function CreateSpaceForm({ onCreated }: { onCreated: () => Promise<void> }) {
  const [name, setName] = useState(""); const [slug, setSlug] = useState(""); const [error, setError] = useState("");
  async function submit(event: FormEvent) { event.preventDefault(); try { await createKnowledgeSpace({ name, slug, description: "用户创建的独立知识空间。" }); await onCreated(); } catch (reason) { setError(reason instanceof Error ? reason.message : "创建失败"); } }
  return <form className="spaceCreate" onSubmit={(event) => void submit(event)}><input value={name} placeholder="空间名称" onChange={(event) => setName(event.target.value)} required /><input value={slug} placeholder="英文标识，如 llm-notes" pattern="[a-z0-9][a-z0-9-]{1,62}" onChange={(event) => setSlug(event.target.value)} required /><button className="primaryButton" type="submit">创建</button>{error && <p className="formError">{error}</p>}</form>;
}

function JobLedger({ jobs, documents }: { jobs: KnowledgeJob[]; documents: KnowledgeDocument[] }) {
  return <section className="jobLedger"><header><span>任务</span><span>文档</span><span>阶段</span><span>进度</span><span>尝试</span></header>{jobs.map((job) => <article key={job.id}><code>{job.id.slice(0, 8)}</code><strong>{documents.find((item) => item.id === job.document_id)?.title || job.document_id.slice(0, 8)}</strong><span><i className={`knowledgeState ${job.status === "completed" ? "ready" : job.status.startsWith("failed") ? "failed" : "pending"}`} />{STAGE_LABELS[job.stage] || job.stage}</span><progress value={job.progress} max={1} /><span>{job.attempt}</span>{job.error_message && <p>{job.error_message}</p>}</article>)}{jobs.length === 0 && <div className="knowledgeEmpty"><strong>没有摄取任务</strong><p>上传文档后，解析和索引阶段会显示在这里。</p></div>}</section>;
}
