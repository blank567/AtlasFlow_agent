"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { createRun, listKnowledgeSpaces } from "../lib/api";
import { POLICY_PRESETS, PolicyPreset } from "../lib/presets";
import { KnowledgeSpace, RunPolicy } from "../lib/types";

export function CreateRunForm() {
  const router = useRouter();
  const [query, setQuery] = useState("分析 Planner–Researcher–Critic 多 Agent 架构的优势、风险与适用场景");
  const [preset, setPreset] = useState<PolicyPreset>("standard");
  const [autoApprove, setAutoApprove] = useState(true);
  const [knowledgeSpace, setKnowledgeSpace] = useState("");
  const [knowledgeSpaces, setKnowledgeSpaces] = useState<KnowledgeSpace[]>([]);
  const [spaceError, setSpaceError] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [customPolicy, setCustomPolicy] = useState<RunPolicy>({
    initial_task_count: 2,
    required_supplement_rounds: 1,
    supplement_task_count: 1,
    replan_requires_critical_issue: true,
    max_tool_calls_per_turn: 5,
    max_tool_calls_per_run: 20,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const policy = useMemo(
    () => preset === "custom" ? customPolicy : POLICY_PRESETS[preset].policy,
    [customPolicy, preset],
  );

  useEffect(() => {
    let active = true;
    listKnowledgeSpaces()
      .then((spaces) => { if (active) setKnowledgeSpaces(spaces); })
      .catch(() => { if (active) setSpaceError("知识空间列表暂不可用；仍可选择不使用知识库。"); });
    return () => { active = false; };
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    if ((policy.max_tool_calls_per_run ?? 20) < (policy.max_tool_calls_per_turn ?? 5)) {
      setError("整个 Run 的工具次数不能小于每次 Agent 决策的次数。");
      setBusy(false);
      return;
    }
    try {
      const runPolicy: RunPolicy = {
        ...policy,
        knowledge_mode: knowledgeSpace ? "selected" : "off",
        knowledge_space: knowledgeSpace || null,
      };
      const run = await createRun({ query: query.trim(), auto_approve: autoApprove, policy: runPolicy });
      router.push(`/runs/${run.id}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "任务创建失败");
      setBusy(false);
    }
  }

  function updateNumber(key: keyof RunPolicy, value: string) {
    setCustomPolicy((current) => ({ ...(preset === "custom" ? current : policy), [key]: value === "" ? null : Number(value) }));
    setPreset("custom");
  }

  return (
    <form onSubmit={submit} className="createForm">
      <label className="field fieldWide"><span>研究问题</span><textarea value={query} onChange={(event) => setQuery(event.target.value)} rows={4} placeholder="描述需要多个 Agent 协作完成的研究目标…" /></label>
      <fieldset className="presetGroup">
        <legend>运行策略</legend>
        <div className="presetGrid">
          {Object.entries(POLICY_PRESETS).map(([key, item]) => (
            <label className={preset === key ? "presetCard selected" : "presetCard"} key={key}>
              <input type="radio" name="preset" checked={preset === key} onChange={() => setPreset(key as PolicyPreset)} />
              <strong>{item.label}</strong><small>{item.description}</small>
            </label>
          ))}
          <label className={preset === "custom" ? "presetCard selected" : "presetCard"}>
            <input type="radio" name="preset" checked={preset === "custom"} onChange={() => { setPreset("custom"); setAdvanced(true); }} />
            <strong>自定义</strong><small>精确控制初始计划、补充轮次与 Replan 条件。</small>
          </label>
        </div>
      </fieldset>

      <label className="field fieldWide">
        <span>本次研究的知识空间</span>
        <select value={knowledgeSpace} onChange={(event) => setKnowledgeSpace(event.target.value)}>
          <option value="">不使用知识库</option>
          {knowledgeSpaces.map((space) => <option key={space.id} value={space.slug}>{space.name} · {space.slug}</option>)}
        </select>
        <small>选择后仅在该空间检索；Agent 不会自行改为 default 或其他空间。运行开始后不可更改。</small>
        {spaceError && <small className="formHint">{spaceError}</small>}
      </label>

      <button className="advancedToggle" type="button" onClick={() => setAdvanced((value) => !value)} aria-expanded={advanced}>
        <span>高级 RunPolicy</span><span>{advanced ? "收起 −" : "展开 +"}</span>
      </button>
      {advanced && (
        <div className="advancedFields">
          <label className="checkField"><input type="checkbox" checked={policy.planner_allow_research ?? false} onChange={(event) => { setCustomPolicy((current) => ({ ...(preset === "custom" ? current : policy), planner_allow_research: event.target.checked })); setPreset("custom"); }} /><span><strong>允许 Planner 轻量调研</strong><small>默认直接规划。开启后每次规划最多一次背景查询，不调用地图。</small></span></label>
          <label className="field"><span>初始任务数（2–5）</span><input type="number" min={2} max={5} value={policy.initial_task_count ?? ""} onChange={(event) => updateNumber("initial_task_count", event.target.value)} placeholder="Planner 自主" /></label>
          <label className="field"><span>要求补充轮次（0–1）</span><input type="number" min={0} max={1} value={policy.required_supplement_rounds ?? 0} onChange={(event) => updateNumber("required_supplement_rounds", event.target.value)} /></label>
          <label className="field"><span>每轮补充任务数（1–2）</span><input type="number" min={1} max={2} value={policy.supplement_task_count ?? ""} onChange={(event) => updateNumber("supplement_task_count", event.target.value)} placeholder="Critic 决定" /></label>
          <label className="checkField"><input type="checkbox" checked={policy.replan_requires_critical_issue ?? true} onChange={(event) => { setCustomPolicy((current) => ({ ...(preset === "custom" ? current : policy), replan_requires_critical_issue: event.target.checked })); setPreset("custom"); }} /><span><strong>Replan 需要关键问题</strong><small>防止仅因任务数量变化触发整轮重规划。</small></span></label>
          <label className="field"><span>每次 Agent 决策的工具次数（1–10）</span><input type="number" min={1} max={10} required value={policy.max_tool_calls_per_turn ?? 5} onChange={(event) => updateNumber("max_tool_calls_per_turn", event.target.value)} /></label>
          <label className="field"><span>整个 Run 的工具次数（1–50）</span><input type="number" min={1} max={50} required value={policy.max_tool_calls_per_run ?? 20} onChange={(event) => updateNumber("max_tool_calls_per_run", event.target.value)} /><small>所有 Agent 共享，运行开始后固定。</small></label>
        </div>
      )}

      <div className="createFooter">
        <label className="switchLabel"><input type="checkbox" checked={autoApprove} onChange={(event) => setAutoApprove(event.target.checked)} /><span className="switch" /><span><strong>自动审批</strong><small>关闭后可在 Planner 阶段编辑任务 DAG</small></span></label>
        <button className="primaryButton" disabled={busy || query.trim().length < 3}>{busy ? <><span className="spinner" />正在创建</> : "开始研究"}</button>
      </div>
      {error && <p className="formError" role="alert">{error}</p>}
    </form>
  );
}
