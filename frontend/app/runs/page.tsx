"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { deleteRun, listRuns, rerun } from "../../lib/api";
import { RunListResponse, RunRecord, TERMINAL_STATUSES } from "../../lib/types";
import { EmptyState, LoadingBlock, PageHeader, Panel, RunRow } from "../../components/ui";

const EMPTY_LIST: RunListResponse = { items: [], total: 0, page: 1, page_size: 10, pages: 1 };

export default function RunsPage() {
  const router = useRouter();
  const [data, setData] = useState<RunListResponse | null>(null);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [page, setPage] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<RunRecord | null>(null);
  const [confirmation, setConfirmation] = useState("");

  const load = useCallback(async (nextPage = page) => {
    setError(null);
    const params = new URLSearchParams({ page: String(nextPage), page_size: "10" });
    if (query.trim()) params.set("query", query.trim());
    if (status) params.set("status", status);
    if (dateFrom) params.set("date_from", new Date(`${dateFrom}T00:00:00`).toISOString());
    if (dateTo) params.set("date_to", new Date(`${dateTo}T23:59:59.999`).toISOString());
    try {
      setData(await listRuns(params));
    } catch (reason) {
      setData(EMPTY_LIST);
      setError(reason instanceof Error ? reason.message : "运行记录读取失败");
    }
  }, [dateFrom, dateTo, page, query, status]);

  useEffect(() => { void load(); }, [load]);

  function submitFilters(event: FormEvent) {
    event.preventDefault();
    setPage(1);
    void load(1);
  }

  async function handleRerun(runId: string) {
    setBusyId(runId);
    try {
      const created = await rerun(runId);
      router.push(`/runs/${created.id}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重新运行失败");
      setBusyId(null);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget || confirmation !== deleteTarget.id) return;
    setBusyId(deleteTarget.id);
    try {
      await deleteRun(deleteTarget.id);
      setDeleteTarget(null);
      setConfirmation("");
      const targetPage = data?.items.length === 1 && page > 1 ? page - 1 : page;
      if (targetPage !== page) setPage(targetPage);
      else await load(targetPage);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除失败");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <main className="pageContent">
      <PageHeader eyebrow="RUN HISTORY" title="运行记录" description="检索、复用和审计本地持久化的每一次研究执行。" />
      <Panel className="filterPanel">
        <form className="filterBar" onSubmit={submitFilters}>
          <label className="searchField"><span aria-hidden>⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索研究问题或 Run ID" /></label>
          <select value={status} onChange={(event) => setStatus(event.target.value)} aria-label="运行状态">
            <option value="">全部状态</option><option value="running">运行中</option><option value="waiting_approval">等待审批</option><option value="completed">已完成</option><option value="completed_with_warnings">完成 · 有警告</option><option value="failed">失败</option><option value="cancelled">已取消</option>
          </select>
          <label className="dateField"><span>从</span><input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} /></label>
          <label className="dateField"><span>至</span><input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} /></label>
          <button className="secondaryButton">应用筛选</button>
        </form>
      </Panel>
      <Panel title="全部 Runs" meta={<span className="panelHint">{data ? `${data.total} 条记录` : "读取中"}</span>} className="runsPanel">
        {error && <div className="inlineAlert errorAlert"><strong>无法完成请求</strong><span>{error}</span></div>}
        {!data ? <LoadingBlock /> : data.items.length === 0 ? <EmptyState title="没有匹配的运行" description="调整筛选条件，或回到工作台创建新任务。" /> : (
          <div className="runList">
            {data.items.map((run) => (
              <RunRow key={run.id} run={run} actions={<>
                <button className="iconButton" title="使用相同参数重新运行" disabled={busyId === run.id} onClick={() => void handleRerun(run.id)}>↻</button>
                <button className="iconButton dangerText" title={TERMINAL_STATUSES.has(run.status) ? "删除本地记录" : "请先取消运行，再删除记录"} disabled={busyId === run.id || !TERMINAL_STATUSES.has(run.status)} onClick={() => { setDeleteTarget(run); setConfirmation(""); }}>⌫</button>
              </>} />
            ))}
          </div>
        )}
        {data && data.pages > 1 && <div className="pagination"><button disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>← 上一页</button><span>第 {data.page} / {data.pages} 页</span><button disabled={page >= data.pages} onClick={() => setPage((value) => value + 1)}>下一页 →</button></div>}
      </Panel>

      {deleteTarget && (
        <div className="modalBackdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) setDeleteTarget(null); }}>
          <section className="modal" role="dialog" aria-modal="true" aria-labelledby="delete-title">
            <span className="dangerIcon">!</span><h2 id="delete-title">删除本地运行记录</h2>
            <p>此操作会删除 SQLite 中的 Run、事件和统计投影，无法恢复；不会删除 LangSmith 中的 Trace。</p>
            <label className="field"><span>输入完整 Run ID 以确认</span><code>{deleteTarget.id}</code><input autoFocus value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder={deleteTarget.id} /></label>
            <div className="modalActions"><button className="secondaryButton" onClick={() => setDeleteTarget(null)}>保留记录</button><button className="dangerButton" disabled={confirmation !== deleteTarget.id || busyId === deleteTarget.id} onClick={() => void confirmDelete()}>{busyId === deleteTarget.id ? "删除中…" : "确认删除"}</button></div>
          </section>
        </div>
      )}
    </main>
  );
}
