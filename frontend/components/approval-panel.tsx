"use client";

import { useEffect, useState } from "react";
import { resolveApproval } from "../lib/api";
import { ResearchPlan, RunRecord } from "../lib/types";

export function ApprovalPanel({ run, onResolved }: { run: RunRecord; onResolved: (run: RunRecord) => void }) {
  const [plan, setPlan] = useState<ResearchPlan | null>(run.plan ? structuredClone(run.plan) : null);
  const [showJson, setShowJson] = useState(false);
  const [json, setJson] = useState(run.plan ? JSON.stringify(run.plan, null, 2) : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPlan(run.plan ? structuredClone(run.plan) : null);
    setJson(run.plan ? JSON.stringify(run.plan, null, 2) : "");
  }, [run.plan]);

  function updateTask(index: number, key: "title" | "objective" | "success_criteria" | "dependencies", value: string) {
    setPlan((current) => {
      if (!current) return current;
      const tasks = current.tasks.map((task, taskIndex) => taskIndex === index ? {
        ...task,
        [key]: key === "success_criteria" || key === "dependencies" ? value.split("\n").map((item) => item.trim()).filter(Boolean) : value,
      } : task);
      const next = { ...current, tasks };
      setJson(JSON.stringify(next, null, 2));
      return next;
    });
  }

  async function submit(action: "approve" | "edit" | "cancel") {
    setBusy(true);
    setError(null);
    try {
      let edited: ResearchPlan | undefined;
      if (action === "edit") edited = showJson ? JSON.parse(json) as ResearchPlan : plan ?? undefined;
      onResolved(await resolveApproval(run.id, action, edited));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "审批提交失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="approvalPanel">
      <div className="approvalHeading"><div><span className="attentionDot" /> <strong>Planner 计划等待审批</strong><p>检查任务目标、依赖和成功标准；提交后后端会再次校验 DAG。</p></div><span className="softBadge amber">Human-in-the-loop</span></div>
      {!plan ? <p className="formError">当前快照中没有可审批的计划。</p> : <>
        <div className="structuredTasks">
          {plan.tasks.map((task, index) => (
            <article className="editableTask" key={task.task_id}>
              <div className="editableTaskHead"><code>{task.task_id}</code><span>优先级 {task.priority}</span></div>
              <label className="field"><span>标题</span><input value={task.title} onChange={(event) => updateTask(index, "title", event.target.value)} /></label>
              <label className="field"><span>目标</span><textarea rows={2} value={task.objective} onChange={(event) => updateTask(index, "objective", event.target.value)} /></label>
              <div className="taskEditGrid">
                <label className="field"><span>成功标准（每行一项）</span><textarea rows={3} value={task.success_criteria.join("\n")} onChange={(event) => updateTask(index, "success_criteria", event.target.value)} /></label>
                <label className="field"><span>依赖 Task ID（每行一项）</span><textarea rows={3} value={task.dependencies.join("\n")} onChange={(event) => updateTask(index, "dependencies", event.target.value)} placeholder="无依赖" /></label>
              </div>
            </article>
          ))}
        </div>
        <button className="advancedToggle jsonToggle" onClick={() => setShowJson((value) => !value)}><span>高级 JSON 编辑</span><span>{showJson ? "收起 −" : "展开 +"}</span></button>
        {showJson && <textarea className="jsonEditor" spellCheck={false} value={json} onChange={(event) => setJson(event.target.value)} />}
      </>}
      {error && <p className="formError" role="alert">{error}</p>}
      <div className="approvalActions"><button className="primaryButton" disabled={busy || !plan} onClick={() => void submit("approve")}>批准原计划</button><button className="secondaryButton" disabled={busy || !plan} onClick={() => void submit("edit")}>保存修改并继续</button><button className="textButton dangerText" disabled={busy} onClick={() => void submit("cancel")}>取消运行</button></div>
    </section>
  );
}
