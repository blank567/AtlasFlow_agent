"use client";

import { useEffect, useState } from "react";
import { listCapabilities, resolveApproval } from "../lib/api";
import { CapabilityDescription, ResearchPlan, RunRecord } from "../lib/types";
import { ConfirmDialog } from "./confirm-dialog";
import { DetailDrawer } from "./detail-drawer";

export function ApprovalPanel({ run, onResolved }: { run: RunRecord; onResolved: (run: RunRecord) => void }) {
  const [plan, setPlan] = useState<ResearchPlan | null>(run.plan ? structuredClone(run.plan) : null);
  const [showJson, setShowJson] = useState(false);
  const [json, setJson] = useState(run.plan ? JSON.stringify(run.plan, null, 2) : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<"approve" | "edit" | "cancel" | null>(null);
  const [capabilities, setCapabilities] = useState<CapabilityDescription[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [showCatalog, setShowCatalog] = useState(false);
  const visibleCapabilities = capabilities.filter((item) => run.policy?.knowledge_mode !== "off" || item.id !== "knowledge_search");

  useEffect(() => {
    let active = true;
    setShowCatalog(false);
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
      <div className="approvalHeading"><div><span className="attentionDot" /><strong>研究计划待审批</strong><p>核验任务目标、依赖与成功标准；提交时再次校验任务依赖图。</p></div><span className="softBadge amber">人工审批</span></div>
      {!plan ? <p className="formError">当前快照中没有可审批的计划。</p> : <>
        {!!capabilities.length && <div className="approvalCatalog"><span>能力配置：{visibleCapabilities.filter((item) => item.available).length}/{visibleCapabilities.length}项可用</span><button type="button" className="textButton" onClick={() => setShowCatalog(true)}>查看能力目录</button></div>}
        {catalogError && <p className="formHint">{catalogError}</p>}
        <div className="structuredTasks">
          {plan.tasks.map((task, index) => (
            <article className="editableTask" key={task.task_id}>
              <div className="editableTaskHead"><code>{task.task_id}</code><span>优先级 {task.priority}</span></div>
              <label className="field"><span>标题</span><input value={task.title} onChange={(event) => updateTask(index, "title", event.target.value)} /></label>
              <label className="field"><span>目标</span><textarea rows={2} value={task.objective} onChange={(event) => updateTask(index, "objective", event.target.value)} /></label>
              <div className="taskEditGrid">
                <label className="field"><span>成功标准（每行一项）</span><textarea rows={3} value={task.success_criteria.join("\n")} onChange={(event) => updateTask(index, "success_criteria", event.target.value)} /></label>
                <label className="field"><span>依赖任务ID（每行一项）</span><textarea rows={3} value={task.dependencies.join("\n")} onChange={(event) => updateTask(index, "dependencies", event.target.value)} placeholder="无依赖" /></label>
                <label className="field"><span>所需能力ID（每行一项）</span><textarea rows={2} value={(task.required_capabilities ?? []).join("\n")} onChange={(event) => updateTask(index, "required_capabilities", event.target.value)} placeholder="填写已注册的能力ID，无需工具时留空" /></label>
                <label className="field"><span>可选能力组（每行一组，组内任选一种）</span><textarea rows={2} value={(task.capability_alternatives ?? []).map((group) => group.join(" | ")).join("\n")} onChange={(event) => updateTask(index, "capability_alternatives", event.target.value)} placeholder="map_route | map_itinerary" /></label>
                <label className="field"><span>数据时效要求</span><select value={String(task.requires_fresh_data ?? false)} onChange={(event) => updateTask(index, "requires_fresh_data", event.target.value)}><option value="false">无需新获取数据</option><option value="true">必须新获取数据</option></select></label>
              </div>
            </article>
          ))}
        </div>
        <button className="advancedToggle jsonToggle" onClick={() => setShowJson((value) => !value)}><span>JSON结构编辑</span><span>{showJson ? "收起" : "展开"}</span></button>
        {showJson && <textarea className="jsonEditor" spellCheck={false} value={json} onChange={(event) => setJson(event.target.value)} />}
      </>}
      {error && <p className="formError" role="alert">{error}</p>}
      <div className="approvalActions"><button className="primaryButton" disabled={busy || !plan} onClick={() => setPendingAction("approve")}>批准原计划</button><button className="secondaryButton" disabled={busy || !plan} onClick={() => setPendingAction("edit")}>保存修改并继续</button><button className="textButton dangerText" disabled={busy} onClick={() => setPendingAction("cancel")}>取消运行</button></div>
      {showCatalog && <DetailDrawer title="能力目录" subtitle="当前运行可使用的能力与配置状态" onClose={() => setShowCatalog(false)}><dl className="capabilityCatalog">{visibleCapabilities.map((item) => <div key={item.id}><dt><code>{item.id}</code><span className={`statusBadge ${item.available ? "status-completed" : "status-failed"}`}>{item.available ? "可用" : "不可用"}</span></dt><dd><p>{item.description}</p>{item.supports_fresh_data && <small>支持获取新数据</small>}{!item.available && <p className="formError">{item.unavailable_reasons.join("；")}</p>}</dd></div>)}</dl></DetailDrawer>}
      {pendingAction && <ConfirmDialog title={pendingAction === "cancel" ? "确认取消运行？" : pendingAction === "edit" ? "确认提交修订计划？" : "确认批准计划？"} description={pendingAction === "cancel" ? "运行将终止，已产生的事件仍保留。" : pendingAction === "edit" ? "当前计划将在任务依赖校验通过后继续执行。" : "批准后进入研究执行阶段，模型调用可能产生费用。"} confirmLabel={pendingAction === "cancel" ? "确认取消" : pendingAction === "edit" ? "保存并继续" : "批准并执行"} cancelLabel="返回核验" tone={pendingAction === "cancel" ? "danger" : "default"} onCancel={() => setPendingAction(null)} onConfirm={() => void submit(pendingAction)} />}
    </section>
  );
}
