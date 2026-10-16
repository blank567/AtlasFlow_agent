"use client";

import { useRef, useState } from "react";
import { resumeRun } from "../lib/api";
import { RunRecord } from "../lib/types";
import { ConfirmDialog } from "./confirm-dialog";

export function RecoveryPanel({ run, onResumed }: { run: RunRecord; onResumed: (run: RunRecord) => void }) {
  const recovery = run.recovery;
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sending = useRef(false);
  const command = useRef<{ id: string; payload: string } | null>(null);
  if (run.status !== "interrupted" || !recovery) return null;
  const unknown = recovery.unknown_calls ?? [];
  async function resume() {
    if (sending.current || !recovery?.available || !recovery?.checkpoint_id) return;
    sending.current = true; setBusy(true); setConfirm(false); setError("");
    const payload = JSON.stringify({ run: run.id, checkpoint: recovery.checkpoint_id, calls: unknown.map(call => call.call_id) });
    if (command.current?.payload !== payload) command.current = { id: crypto.randomUUID(), payload };
    try {
      const next = await resumeRun(run.id, { request_id: command.current.id, expected_checkpoint_id: recovery.checkpoint_id,
        acknowledged_unknown_call_ids: unknown.map(call => call.call_id) });
      command.current = null; onResumed(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "恢复请求未完成，请刷新运行状态后重试。");
    } finally { sending.current = false; setBusy(false); }
  }
  return <section className="recoveryPanel" aria-label="断点恢复">
    <div className="recoveryHeading"><div><h2>执行已中断</h2><p>{recovery.reason ?? "已保存执行进度，可从最近的断点继续。"}</p></div>
      <button type="button" className="primaryButton" disabled={busy || !recovery.available || !recovery.checkpoint_id} onClick={() => setConfirm(true)}>{busy ? "正在恢复" : "继续执行"}</button></div>
    {unknown.length > 0 && <div className="recoveryUnknown"><strong>{unknown.length}次调用尚未确认结果</strong><p>这些请求可能已产生费用，继续执行可能再次请求。已消耗的工具额度会保留。</p>
      <ul>{unknown.map(call => <li key={call.call_id}><span>{call.kind}：{call.name}</span><span>{call.tool_budget_counted ? "已计入工具额度" : "费用未确认"}</span></li>)}</ul></div>}
    {recovery.blocked_by && <p className="formError">{recovery.blocked_by}</p>}
    <p className="recoveryFoot">本次运行已恢复{recovery.recovery_count ?? 0}次</p>
    {error && <p className="formError" role="alert">{error}</p>}
    {confirm && <ConfirmDialog title="确认继续执行？" description={unknown.length ? `将继续原运行，并允许重新请求${unknown.length}次结果不明的只读调用；可能产生重复费用。` : "将继续原运行的未完成阶段，后续模型与工具请求可能产生费用。"} confirmLabel="确认继续" onConfirm={() => void resume()} onCancel={() => setConfirm(false)} />}
  </section>;
}
