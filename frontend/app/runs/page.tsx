"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { deleteRun, listRuns, rerun } from "../../lib/api";
import { RunListResponse, RunRecord, TERMINAL_STATUSES } from "../../lib/types";
import { EmptyState, LoadingBlock, PageHeader, Panel, RunRow } from "../../components/ui";
import { ConfirmDialog } from "../../components/confirm-dialog";

type RunFilters = { query: string; status: string; dateFrom: string; dateTo: string };

export default function RunsPage() {
  const router = useRouter();
  const [data, setData] = useState<RunListResponse | null>(null);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [appliedFilters, setAppliedFilters] = useState<RunFilters>({ query: "", status: "", dateFrom: "", dateTo: "" });
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<RunRecord | null>(null);
  const [rerunTarget, setRerunTarget] = useState<RunRecord | null>(null);
  const [confirmation, setConfirmation] = useState("");

  const load = useCallback(async (nextPage = page) => {
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({ page: String(nextPage), page_size: "10" });
    if (appliedFilters.query.trim()) params.set("query", appliedFilters.query.trim());
    if (appliedFilters.status) params.set("status", appliedFilters.status);
    if (appliedFilters.dateFrom) params.set("date_from", new Date(`${appliedFilters.dateFrom}T00:00:00`).toISOString());
    if (appliedFilters.dateTo) params.set("date_to", new Date(`${appliedFilters.dateTo}T23:59:59.999`).toISOString());
    try {
      setData(await listRuns(params));
    } catch (reason) {
      setData(null);
      setError(reason instanceof Error ? reason.message : "运行记录读取失败");
    } finally {
      setLoading(false);
    }
  }, [appliedFilters, page]);

  useEffect(() => { void load(); }, [load]);

  function submitFilters(event: FormEvent) {
    event.preventDefault();
    if (dateFrom && dateTo && dateFrom > dateTo) {
      setError("开始日期不能晚于结束日期。");
      return;
    }
    setPage(1);
    setAppliedFilters({ query, status, dateFrom, dateTo });
  }

  async function handleRerun(runId: string) {
    setRerunTarget(null);
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
    <main className="pageContent runsPage">
      <PageHeader title="运行记录" description="检索研究结果、执行状态及原始编排参数。" actions={<Link className="primaryButton" href="/">发起研究</Link>} />
      <Panel className="filterPanel">
        <form className="filterBar" onSubmit={submitFilters}>
          <label className="searchField"><span className="srOnly">检索研究目标或运行ID</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="检索研究目标或运行ID" /></label>
          <select value={status} onChange={(event) => setStatus(event.target.value)} aria-label="运行状态">
            <option value="">全部状态</option><option value="pending">等待中</option><option value="running">运行中</option><option value="waiting_approval">等待审批</option><option value="completed">已完成</option><option value="completed_with_warnings">完成 · 有警告</option><option value="failed">失败</option><option value="cancelled">已取消</option>
          </select>
          <label className="dateField"><span>从</span><input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} /></label>
          <label className="dateField"><span>至</span><input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} /></label>
          <button className="secondaryButton" disabled={loading}>应用筛选</button>
        </form>
      </Panel>
      <Panel title="全部运行" meta={<span className="panelHint">{loading ? "正在读取" : data ? `${data.total} 条记录` : "连接未就绪"}</span>} className="runsPanel">
        {error && <div className="inlineAlert errorAlert"><strong>无法完成请求</strong><span>{error}</span>{!data && <button type="button" className="secondaryButton" disabled={loading} onClick={() => void load()}>重试</button>}</div>}
        {loading ? <LoadingBlock label="读取运行记录" /> : !data ? null : data.items.length === 0 ? <EmptyState title="没有匹配的运行" description="调整筛选条件，或回到工作台创建新任务。" /> : (
          <div className="runList">
            {data.items.map((run) => (
              <RunRow key={run.id} run={run} actions={<>
                <button className="rowActionButton" type="button" disabled={busyId === run.id} onClick={() => setRerunTarget(run)}>重新运行</button>
                <button className="rowActionButton dangerText" type="button" title={TERMINAL_STATUSES.has(run.status) ? "删除本地记录" : "请先取消运行，再删除记录"} disabled={busyId === run.id || !TERMINAL_STATUSES.has(run.status)} onClick={() => { setDeleteTarget(run); setConfirmation(""); }}>删除</button>
              </>} />
            ))}
          </div>
        )}
        {data && data.pages > 1 && <div className="pagination"><button disabled={loading || page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</button><span>第 {data.page} / {data.pages} 页</span><button disabled={loading || page >= data.pages} onClick={() => setPage((value) => value + 1)}>下一页</button></div>}
      </Panel>

      {deleteTarget && (
        <div className="modalBackdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) setDeleteTarget(null); }}>
          <section className="modal" role="dialog" aria-modal="true" aria-labelledby="delete-title">
            <span className="dangerIcon">!</span><h2 id="delete-title">删除本地运行记录</h2>
            <p>本地运行、事件与统计记录将永久删除，LangSmith追踪记录仍保留。</p>
            <label className="field"><span>输入完整运行ID以确认</span><code>{deleteTarget.id}</code><input autoFocus value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder={deleteTarget.id} /></label>
            <div className="modalActions"><button className="secondaryButton" onClick={() => setDeleteTarget(null)}>保留记录</button><button className="dangerButton" disabled={confirmation !== deleteTarget.id || busyId === deleteTarget.id} onClick={() => void confirmDelete()}>{busyId === deleteTarget.id ? "删除中…" : "确认删除"}</button></div>
          </section>
        </div>
      )}
      {rerunTarget && <ConfirmDialog title="确认重新运行？" description={`将使用“${rerunTarget.query || "未命名研究任务"}”的原始参数创建新运行，模型调用可能产生费用。`} confirmLabel="创建新运行" cancelLabel="保留当前页面" onCancel={() => setRerunTarget(null)} onConfirm={() => void handleRerun(rerunTarget.id)} />}
    </main>
  );
}
