"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { createRun, listKnowledgeSpaces } from "../lib/api";
import { POLICY_PRESETS, PolicyPreset } from "../lib/presets";
import { KnowledgeSpace, RunPolicy } from "../lib/types";

export function CreateRunForm() {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [preset, setPreset] = useState<PolicyPreset>("standard");
  const [autoApprove, setAutoApprove] = useState(true);
  const [knowledgeSpace, setKnowledgeSpace] = useState("");
  const [knowledgeSpaces, setKnowledgeSpaces] = useState<KnowledgeSpace[]>([]);
  const [spaceError, setSpaceError] = useState<string | null>(null);
  const queryInput = useRef<HTMLTextAreaElement>(null);
  const runBudgetInput = useRef<HTMLInputElement>(null);
  const [queryTouched, setQueryTouched] = useState(false);
  const [loadingSpaces, setLoadingSpaces] = useState(true);
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
  const queryError = queryTouched && query.trim().length < 3 ? "研究目标至少需要3个字符。" : query.length > 4000 ? "研究目标不得超过4000个字符。" : null;

  useEffect(() => {
    let active = true;
    listKnowledgeSpaces()
      .then((spaces) => { if (active) setKnowledgeSpaces(spaces); })
      .catch(() => { if (active) setSpaceError("知识空间读取失败，可暂不使用知识库。"); })
      .finally(() => { if (active) setLoadingSpaces(false); });
    return () => { active = false; };
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setQueryTouched(true);
    if (query.trim().length < 3 || query.length > 4000) {
      queryInput.current?.focus();
      return;
    }
    setBusy(true);
    if ((policy.max_tool_calls_per_run ?? 20) < (policy.max_tool_calls_per_turn ?? 5)) {
      setError("运行工具调用总额不得低于单轮Agent调用上限。");
      setBusy(false);
      runBudgetInput.current?.focus();
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
    setError(null);
    setCustomPolicy((current) => ({ ...(preset === "custom" ? current : policy), [key]: value === "" ? null : Number(value) }));
    setPreset("custom");
  }

  function updateBoolean(key: keyof RunPolicy, value: boolean) {
    setError(null);
    setCustomPolicy((current) => ({ ...(preset === "custom" ? current : policy), [key]: value }));
    setPreset("custom");
  }

  function selectPreset(value: PolicyPreset) {
    setError(null);
    if (value === "custom" && preset !== "custom") setCustomPolicy({ ...policy });
    setPreset(value);
  }

  return (
    <form onSubmit={submit} className="createForm researchComposer" aria-busy={busy}>
      <div className="composerHeader">
        <label htmlFor="research-query">研究目标</label>
        <span className={`queryCounter ${query.length >= 3600 ? "nearLimit" : ""}`} aria-label={`已输入${query.length}个字符，上限4000个字符`}>{query.length.toLocaleString()} / 4,000</span>
      </div>
      <div className={`composerField ${queryError ? "is-invalid" : ""}`}>
        <textarea id="research-query" ref={queryInput} value={query} onChange={(event) => setQuery(event.target.value)} onBlur={() => setQueryTouched(true)} rows={4} required minLength={3} maxLength={4000} disabled={busy} aria-invalid={!!queryError} aria-describedby={queryError ? "research-query-error" : "research-query-hint"} placeholder={"输入研究主题与核心问题。\n可补充分析范围、约束条件与预期交付内容。"} />
        <div className="composerMeta"><small id="research-query-hint">3-4000字符，支持多行研究约束。</small>{queryError && <span id="research-query-error" className="formError" role="alert">{queryError}</span>}</div>
      </div>

      <fieldset className="policySelector" disabled={busy}>
        <legend>编排策略</legend>
        <div className="policySegments">
        {Object.entries(POLICY_PRESETS).map(([key, item]) => (
          <label className={`policySegment ${preset === key ? "is-selected" : ""}`} key={key}>
            <input type="radio" name="preset" checked={preset === key} onChange={() => selectPreset(key as PolicyPreset)} />
            <span>{item.label}</span>
          </label>
        ))}
        <label className={`policySegment ${preset === "custom" ? "is-selected" : ""}`}>
          <input type="radio" name="preset" checked={preset === "custom"} onChange={() => selectPreset("custom")} />
          <span>自定义</span>
        </label>
        </div>
      </fieldset>
      <p className="policyDescription">{preset === "custom" ? "当前参数独立配置；切换预设可恢复对应策略。" : POLICY_PRESETS[preset].description}</p>

      <section className="configSection" aria-labelledby="run-config-title">
        <div className="configHeading"><h2 id="run-config-title">执行配置</h2><span>编排参数修改后切换为自定义</span></div>
        <div className="runConfigGrid">
          <label className="field"><span>知识检索空间</span><select value={knowledgeSpace} disabled={busy || loadingSpaces} onChange={(event) => setKnowledgeSpace(event.target.value)}><option value="">{loadingSpaces ? "读取知识空间…" : "不使用知识库"}</option>{knowledgeSpaces.map((space) => <option key={space.id} value={space.slug}>{space.name}·{space.slug}</option>)}</select><small>仅检索所选空间。</small></label>
          <label className="field"><span>初始任务数</span><input type="number" min={2} max={5} disabled={busy} value={policy.initial_task_count ?? ""} onChange={(event) => updateNumber("initial_task_count", event.target.value)} placeholder="自动规划" /><small>2-5项，留空由Planner确定。</small></label>
          <label className="field"><span>强制补充轮次</span><select value={policy.required_supplement_rounds ?? 0} disabled={busy} onChange={(event) => updateNumber("required_supplement_rounds", event.target.value)}><option value={0}>按审查结果补充</option><option value={1}>至少补充一轮</option></select><small>由Critic执行补充决策。</small></label>
          <label className="field"><span>每轮补充任务数</span><input type="number" min={1} max={2} disabled={busy} value={policy.supplement_task_count ?? ""} onChange={(event) => updateNumber("supplement_task_count", event.target.value)} placeholder="按审查结果确定" /><small>1-2项，留空由Critic确定。</small></label>
          <label className="field"><span>单轮Agent工具上限</span><input type="number" min={1} max={10} required disabled={busy} value={policy.max_tool_calls_per_turn ?? 5} onChange={(event) => updateNumber("max_tool_calls_per_turn", event.target.value)} /><small>1-10次工具调用。</small></label>
          <label className="field"><span>运行工具调用总额</span><input ref={runBudgetInput} type="number" min={1} max={50} required disabled={busy} value={policy.max_tool_calls_per_run ?? 20} onChange={(event) => updateNumber("max_tool_calls_per_run", event.target.value)} /><small>1-50次，所有Agent共享。</small></label>
        </div>
        <div className="configOptions">
          <label className="configOption"><input type="checkbox" disabled={busy} checked={policy.planner_allow_research ?? false} onChange={(event) => updateBoolean("planner_allow_research", event.target.checked)} /><span><strong>Planner背景调研</strong><small>每次规划至多一次背景查询。</small></span></label>
          <label className="configOption"><input type="checkbox" disabled={busy} checked={policy.replan_requires_critical_issue ?? true} onChange={(event) => updateBoolean("replan_requires_critical_issue", event.target.checked)} /><span><strong>关键问题重规划约束</strong><small>整轮重规划须具备关键问题依据。</small></span></label>
        </div>
        {spaceError && <p className="formHint" role="status">{spaceError}</p>}
      </section>

      {error && <p className="formError" id="create-run-error" role="alert">{error}</p>}
      <div className="createFooter createSubmitBar">
        <label className="switchLabel"><input type="checkbox" disabled={busy} checked={autoApprove} onChange={(event) => setAutoApprove(event.target.checked)} /><span className="switch" /><span><strong>自动审批</strong><small>{autoApprove ? "计划生成后直接执行" : "计划生成后等待人工审批"}</small></span></label>
        <button className="primaryButton" disabled={busy || query.trim().length < 3 || query.length > 4000} aria-describedby={error ? "create-run-error" : undefined}>{busy ? <><span className="spinner" />创建运行…</> : "开始研究"}</button>
      </div>
    </form>
  );
}
