"use client";

import { useEffect, useState } from "react";
import { listCapabilities, resolveApproval } from "../lib/api";
import { CapabilityDescription, ResearchPlan, RunRecord } from "../lib/types";
import { ConfirmDialog } from "./confirm-dialog";

export function ApprovalPanel({ run, onResolved }: { run: RunRecord; onResolved: (run: RunRecord) => void }) {
  const [plan, setPlan] = useState<ResearchPlan | null>(run.plan ? structuredClone(run.plan) : null);
  const [showJson, setShowJson] = useState(false);
  const [json, setJson] = useState(run.plan ? JSON.stringify(run.plan, null, 2) : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<"approve" | "edit" | "cancel" | null>(null);
  const [capabilities, setCapabilities] = useState<CapabilityDescription[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setCatalogError(null);
    listCapabilities().then((items) => { if (active) setCapabilities(items); })
      .catch(() => { if (active) setCatalogError("能力目录暂时不可用，提交时仍会由后端校验。"); });
    return () => { active = false; };
  }, [run.id]);

  useEffect(() => {
    setPlan(run.plan ? structuredClone(run.plan) : null);
    setJson(run.plan ? JSON.stringify(run.plan, null, 2) : "");
  }, [run.plan]);

  function updateTask(index: number, key: "title" | "objective" | "success_criteria" | "dependencies" | "required_capabilities" | "capability_alternatives" | "requires_fresh_data", value: string) {
    setPlan((current) => {
      if (!current) return current;
      const tasks = current.tasks.map((task, taskIndex) => taskIndex === index ? {
        ...task,
        [key]: key === "capability_alternatives" ? value.split("\n").filter((line) => line.trim()).map((line) => line.split("|").map((item) => item.trim()).filter(Boolean)) : key === "requires_fresh_data" ? value === "true" : key === "success_criteria" || key === "dependencies" || key === "required_capabilities" ? value.split("\n").map((item) => item.trim()).filter(Boolean) : value,
      } : task);
      const next = { ...current, tasks };
      setJson(JSON.stringify(next, null, 2));
      return next;
    });
  }

  async function submit(action: "approve" | "edit" | "cancel") {
    setPendingAction(null);
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
        {!!capabilities.length && <details><summary>查看能力目录与配置状态</summary><ul>{capabilities.filter((item) => run.policy?.knowledge_mode !== "off" || item.id !== "knowledge_search").map((item) => <li key={item.id}><code>{item.id}</code> · {item.description} · {item.available ? "可用" : "不可用"}{item.supports_fresh_data ? " · 支持获取新数据" : ""}{!item.available && `（${item.unavailable_reasons.join("；")}）`}</li>)}</ul></details>}
        {catalogError && <p className="formHint">{catalogError}</p>}
        <div className="structuredTasks">
          {plan.tasks.map((task, index) => (
            <article className="editableTask" key={task.task_id}>
              <div className="editableTaskHead"><code>{task.task_id}</code><span>优先级 {task.priority}</span></div>
              <label className="field"><span>标题</span><input value={task.title} onChange={(event) => updateTask(index, "title", event.target.value)} /></label>
              <label className="field"><span>目标</span><textarea rows={2} value={task.objective} onChange={(event) => updateTask(index, "objective", event.target.value)} /></label>
              <div className="taskEditGrid">
                <label className="field"><span>成功标准（每行一项）</span><textarea rows={3} value={task.success_criteria.join("\n")} onChange={(event) => updateTask(index, "success_criteria", event.target.value)} /></label>
                <label className="field"><span>依赖 Task ID（每行一项）</span><textarea rows={3} value={task.dependencies.join("\n")} onChange={(event) => updateTask(index, "dependencies", event.target.value)} placeholder="无依赖" /></label>
                <label className="field"><span>所需能力 ID（每行一项）</span><textarea rows={2} value={(task.required_capabilities ?? []).join("\n")} onChange={(event) => updateTask(index, "required_capabilities", event.target.value)} placeholder="无需工具时留空；填写已注册的能力 ID" /></label>
                <label className="field"><span>可选能力组（每行一组，组内任选一种）</span><textarea rows={2} value={(task.capability_alternatives ?? []).map((group) => group.join(" | ")).join("\n")} onChange={(event) => updateTask(index, "capability_alternatives", event.target.value)} placeholder="map_route | map_itinerary" /></label>
                <label className="field"><span>数据时效要求</span><select value={String(task.requires_fresh_data ?? false)} onChange={(event) => updateTask(index, "requires_fresh_data", event.target.value)}><option value="false">无需新获取数据</option><option value="true">必须新获取数据</option></select></label>
              </div>
            </article>
          ))}
        </div>
        <button className="advancedToggle jsonToggle" onClick={() => setShowJson((value) => !value)}><span>高级 JSON 编辑</span><span>{showJson ? "收起 −" : "展开 +"}</span></button>
        {showJson && <textarea className="jsonEditor" spellCheck={false} value={json} onChange={(event) => setJson(event.target.value)} />}
      </>}
      {error && <p className="formError" role="alert">{error}</p>}
      <div className="approvalActions"><button className="primaryButton" disabled={busy || !plan} onClick={() => setPendingAction("approve")}>批准原计划</button><button className="secondaryButton" disabled={busy || !plan} onClick={() => setPendingAction("edit")}>保存修改并继续</button><button className="textButton dangerText" disabled={busy} onClick={() => setPendingAction("cancel")}>取消运行</button></div>
      {pendingAction && <ConfirmDialog title={pendingAction === "cancel" ? "取消这次研究？" : pendingAction === "edit" ? "确认保存并继续？" : "确认批准计划？"} description={pendingAction === "cancel" ? "运行将结束，已产生的事件仍会保留。" : pendingAction === "edit" ? "系统会提交当前编辑的任务计划并继续执行；后端会再次校验任务依赖。" : "批准后研究任务将开始执行，并可能产生模型调用费用。"} confirmLabel={pendingAction === "cancel" ? "确认取消" : pendingAction === "edit" ? "保存并继续" : "批准并执行"} cancelLabel="返回检查" tone={pendingAction === "cancel" ? "danger" : "default"} onCancel={() => setPendingAction(null)} onConfirm={() => void submit(pendingAction)} />}
    </section>
  );
}
