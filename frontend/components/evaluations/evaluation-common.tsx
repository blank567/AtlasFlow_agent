"use client";

import Link from "next/link";
import { CSSProperties, Dispatch, ReactNode, SetStateAction, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { EvaluationSession } from "../../hooks/use-evaluation-v2";
import { EvaluationBudget, EvaluationCase, EvaluationDataset, EvaluationExperiment, EvaluationInput, EvaluationPlan, EvaluationResult, EvaluationSettings, JsonRecord, asText, cellKey, isActiveExperiment, metricDescription, metricName, metricValue, protocolName, record, stateName } from "../../lib/evaluations-v2";
import { ConfirmDialog } from "../confirm-dialog";
import { EvaluationDetailDialog } from "./detail-dialog";
import { EvaluationComparisonChart } from "./comparison-chart";
import { buildMultiGroupChart, formatPrimaryGap, formatPrimaryValue } from "../../lib/evaluation-chart";
import { PRIMARY_METRICS, primaryMetricIds } from "../../lib/evaluation-metrics";

export function EvaluationNavigation({ active }: { active: "rag" | "agent" }) {
  return <nav className="experimentNavigation" aria-label="评测领域"><Link aria-current={active === "rag" ? "page" : undefined} href="/evaluations/rag">RAG实验<span>检索与回答</span></Link><Link aria-current={active === "agent" ? "page" : undefined} href="/evaluations/agent">Agent实验<span>任务与行为</span></Link></nav>;
}

export function BudgetFields({ value, setValue }: { value: EvaluationBudget; setValue: Dispatch<SetStateAction<EvaluationBudget>> }) {
  const fields = [["max_http_attempts", "HTTP尝试上限", 5000], ["max_elapsed_seconds", "实验总时限（秒）", 7200], ["case_timeout_seconds", "单题时限（秒）", 1200], ["concurrency", "并发单元", 4]] as const;
  return <fieldset className="experimentBudget"><legend>执行边界</legend><div className="evalFields">{fields.map(([key, label, max]) => <label key={key} className="field"><span>{label}</span><input aria-label={label} type="number" min={1} max={max} value={value[key]} onChange={event => setValue(previous => ({ ...previous, [key]: Number(event.target.value) }))} /></label>)}</div><p>单题超时保留已完成产物，不中止其余单元；全局预算耗尽则停止后续请求。</p></fieldset>;
}
export function budgetError(value: EvaluationBudget) {
  if (Object.values(value).some(item => !Number.isInteger(item) || item < 1)) return "预算参数应为正整数。";
  if (value.max_http_attempts > 5000 || value.max_elapsed_seconds > 7200 || value.case_timeout_seconds > 1200 || value.concurrency > 4) return "执行预算超出允许范围。";
  return "";
}

export function ModelSelection({ label, value, onChange, settings }: { label: string; value: string; onChange: (value: string) => void; settings: EvaluationSettings | null }) {
  const models = settings?.models ?? [], primary = settings?.default_model;
  const choices = models.filter(model => model.id !== primary);
  useEffect(() => { if (settings && value && !models.some(model => model.id === value)) onChange(""); }, [settings, value, onChange]);
  return <label className="field experimentModelField"><span>{label}</span>
    <select aria-label={label} value={value} onChange={event => onChange(event.target.value)} disabled={!primary}>
      <option value="">{primary ? `主模型：${primary}` : settings ? "服务端尚未配置主模型" : "正在读取模型列表"}</option>
      {choices.map(model => <option key={model.id} value={model.id}>{model.name}</option>)}
    </select>
    <small>{primary ? choices.length ? "候选来自服务端配置，统一使用当前模型服务连接。" : "当前复用主模型；新增候选后可在此切换。" : "请先完成服务端模型配置。"}</small>
  </label>;
}
export function ExecutionOptions({ mode = "live", setMode, liveOnly = false, judge, setJudge, settings, semantic }: { mode?: "replay" | "live"; setMode?: (value: "replay" | "live") => void; liveOnly?: boolean; judge: boolean; setJudge: (value: boolean) => void; settings: EvaluationSettings | null; semantic: boolean }) {
  const judgeLabel = settings?.judge_model ? `评审模型：${settings.judge_model}${settings.judge_reuses_default_model ? "（复用主模型）" : ""}。` : "";
  const judgeBudget = settings?.judge_limits ? `输出预算${settings.judge_limits.max_tokens}Token，每项理由最多${settings.judge_limits.max_reason_chars}字符。` : "";
  return <>
    {liveOnly ? <p className="evalHint">每次实验按当前配置重新检索；选择“检索与回答”时重新生成答案。执行前确认调用预算。</p> : <fieldset className="experimentMode">
      <legend>运行方式</legend>
      <label><input type="radio" name="experiment-mode" checked={mode === "replay"} onChange={() => setMode?.("replay")} /><span><strong>冻结回放</strong><small>重评已保存产物，不重跑模型或工具。</small></span></label>
      <label><input type="radio" name="experiment-mode" checked={mode === "live"} onChange={() => setMode?.("live")} /><span><strong>真实采集</strong><small>运行当前配置，调用模型或工具可能产生费用。</small></span></label>
    </fieldset>}
    {semantic && <div className="experimentOptions"><label>
      <input type="checkbox" checked={judge} disabled={!settings?.judge_available} onChange={event => setJudge(event.target.checked)} />
      <span><strong>独立Judge评审</strong><small>{settings?.judge_available ? `${judgeLabel}${judgeBudget}额外发送答案与证据，需确认费用；失败不清空规则分数。` : "独立评审未启用或模型未就绪；语义质量保持未评测。"}</small></span>
    </label></div>}
  </>;
}

export function CaseSelection(props: { dataset?: EvaluationDataset; caseIds: string[]; setCaseIds: Dispatch<SetStateAction<string[]>>; plan: EvaluationPlan; label?: string } & ({ inputs: EvaluationInput[]; inputRefs: Record<string, string>; setInputRefs: Dispatch<SetStateAction<Record<string, string>>> } | { inputs?: never; inputRefs?: never; setInputRefs?: never })) {
  const { dataset, caseIds, setCaseIds, plan, label = "评测样本", inputs = [], inputRefs = {}, setInputRefs = () => {} } = props;
  return <section className="experimentSection experimentCases"><div className="evalSectionHead"><div><h2>{label}</h2><p>{caseIds.length}个样本×{plan.variants.length}组配置×{plan.repetitions}次，计划{caseIds.length * plan.variants.length * plan.repetitions}个单元。</p></div><button className="textButton" type="button" onClick={() => setCaseIds(previous => previous.length === dataset?.cases.length ? [] : dataset?.cases.map(item => item.case_id) ?? [])}>{caseIds.length === dataset?.cases.length ? "清空选择" : "选择全部"}</button></div>{!dataset ? <p className="emptyInline">正在读取该领域的题集。</p> : <div className="evalTableScroll"><table className="evalTable"><thead><tr><th>选择</th><th>{plan.kind === "rag" ? "查询与标签" : "场景目标"}</th><th>标注状态</th>{plan.mode === "replay" && plan.variants.flatMap(variant => Array.from({ length: plan.repetitions }, (_, index) => <th key={`${variant.variant_id}:${index}`}>{variant.name}{plan.repetitions > 1 ? ` · 第${index + 1}次` : ""}</th>))}</tr></thead><tbody>{dataset.cases.map(item => <tr key={item.case_id}><td><input type="checkbox" aria-label={`选择${item.case_id}`} checked={caseIds.includes(item.case_id)} onChange={() => setCaseIds(previous => previous.includes(item.case_id) ? previous.filter(id => id !== item.case_id) : [...previous, item.case_id])} /></td><td><strong>{item.query}</strong><small>{item.case_id}{item.tags?.length ? ` / ${item.tags.join("、")}` : ""}</small></td><td><span className={`softBadge ${item.gold_status === "verified" ? "" : "amber"}`}>{stateName(item.gold_status ?? "draft")}</span></td>{plan.mode === "replay" && plan.variants.flatMap(variant => Array.from({ length: plan.repetitions }, (_, index) => { const trial = index + 1, slot = cellKey({ case_id: item.case_id, variant_id: variant.variant_id, trial }); return <td key={slot}><select aria-label={`${slot}冻结输入`} value={inputRefs[slot] ?? ""} disabled={!caseIds.includes(item.case_id)} onChange={event => setInputRefs(previous => { const next = { ...previous }; if (event.target.value) next[slot] = event.target.value; else delete next[slot]; return next; })}><option value="">自动匹配冻结产物</option>{inputs.filter(input => input.case_id === item.case_id && input.variant_id === variant.variant_id && (input.trial ?? 1) === trial && input.protocol === plan.protocol).map(input => <option key={input.input_id} value={input.input_id}>{input.created_at.slice(0, 16)} {input.provenance}</option>)}</select></td>; }))}</tr>)}</tbody></table></div>}</section>;
}

export function LaunchExperiment({ session, plan, validation }: { session: EvaluationSession; plan: EvaluationPlan; validation: string }) {
  return <section className="experimentLaunch"><div className="evalLaunch"><div><h2>确认实验条件</h2><p>预检不调用模型。切换配置、样本或预算后，需重新预检。</p>{validation && <p role="alert" className="formError">{validation}</p>}</div><div><button type="button" className="secondaryButton" disabled={session.busy || Boolean(validation) || !plan.dataset_id || !plan.case_ids.length || !plan.variants.length} onClick={() => void session.check()}>{session.busy ? "处理中" : "检查实验条件"}</button><button type="button" className="primaryButton" disabled={session.busy || Boolean(validation) || !session.checked?.ready} onClick={session.askRun}>{plan.mode === "live" ? "开始真实采集" : "开始冻结回放"}</button></div></div>{session.checked && <div className={`evalPreflight ${session.checked.ready ? "ready" : "blocked"}`} role="status"><strong>{session.checked.ready ? "已通过预检" : "暂不具备执行条件"}</strong><p>计划{session.checked.expected_cells}个单元；{plan.protocol}。</p>{session.checked.errors.map((value, index) => <p key={`e${index}`} className="formError">{value}</p>)}{session.checked.warnings.map((value, index) => <p key={`w${index}`}>{value}</p>)}</div>}{session.confirmation && session.confirmation.action !== "delete" && <ConfirmDialog title={session.confirmation.action === "cancel" ? "确认停止实验？" : plan.mode === "live" || plan.judge_enabled ? "确认发起真实调用？" : "确认开始冻结回放？"} description={session.confirmation.action === "cancel" ? "停止后续请求，保留已经完成的产物与评分。" : `${plan.case_ids.length}个样本、${plan.variants.length}组配置、每组${plan.repetitions}次。${plan.mode === "live" || plan.judge_enabled ? `最多${plan.budget.max_http_attempts}次业务HTTP尝试，实验总时限${plan.budget.max_elapsed_seconds}秒。${plan.judge_enabled ? "独立Judge将接收答案与证据并额外计费。" : ""}` : "只读取已保存产物，不会重新执行研究流程。"}`} confirmLabel={session.confirmation.action === "cancel" ? "停止实验" : "确认开始"} onCancel={() => session.setConfirmation(null)} onConfirm={() => void session.confirm()} />}</section>;
}

export function ExperimentHistory({ session }: { session: EvaluationSession }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const deleteTarget = session.confirmation?.action === "delete"
    ? session.experiments.find(item => item.experiment_id === session.confirmation?.identity) : undefined;
  useEffect(() => { if (session.notice) headingRef.current?.focus({ preventScroll: true }); }, [session.notice]);
  return <section className="experimentSection experimentHistory" aria-labelledby="experiment-records-heading">
    <div className="evalSectionHead"><div><h2 id="experiment-records-heading" ref={headingRef} tabIndex={-1}>实验记录</h2><p>只展示本领域v2实验。运行中请先停止，结束后可删除记录。</p></div></div>
    {session.notice && <p className="evalHint" role="status">{session.notice}</p>}
    {!session.experiments.length ? <p className="emptyInline">尚无实验记录。选择样本并完成预检后开始实验。</p> : <div className="evalExperimentList">
      {session.experiments.map(item => <div className="experimentRecordRow" key={item.experiment_id} data-experiment-id={item.experiment_id}>
        <button type="button" className="experimentRecordSelect" disabled={session.busy} aria-pressed={session.selected?.experiment_id === item.experiment_id} onClick={() => void session.pick(item.experiment_id)}>
          <span><strong>{protocolName(item.protocol)} / {item.plan.mode === "live" ? "真实采集" : "冻结回放"}</strong><small>{item.created_at.slice(0, 19).replace("T", " ")} / {item.experiment_id.slice(0, 8)}</small></span>
          <span className="experimentRecordCounts">{item.plan.case_ids.length}样本 / {item.plan.variants.length}配置 / {item.plan.repetitions}次</span>
          <span className={`softBadge ${["partial", "failed"].includes(item.status) ? "amber" : ""}`}>{stateName(item.status)}</span>
        </button>
        <button type="button" className="textButton experimentRecordDelete" aria-label={`删除实验记录${item.experiment_id}`} disabled={session.busy || isActiveExperiment(item.status)} title={isActiveExperiment(item.status) ? "请先停止实验，结束后再删除" : "删除记录，保留原文、索引与证据"} onClick={() => session.askDelete(item.experiment_id)}>{session.deletingId === item.experiment_id ? "正在删除" : "删除"}</button>
      </div>)}
    </div>}
    {deleteTarget && <ConfirmDialog title="确认删除实验记录？" description={`${protocolName(deleteTarget.protocol)}，${deleteTarget.experiment_id}。删除后不再显示该实验及其结果，原文、索引与证据保留。此页面不提供恢复操作。`} confirmLabel="确认删除" tone="danger" onCancel={() => session.setConfirmation(null)} onConfirm={() => void session.confirm()} />}
  </section>;
}

export function ExperimentResultHeader({ session }: { session: EvaluationSession }) {
  const value = session.selected; if (!value) return null; const usage = record(value.summary.provider_usage);
  return <><div className="evalSectionHead"><div><h2>{protocolName(value.protocol)}结果</h2><p>{value.experiment_id} / {stateName(value.status)}</p></div><div className="headerActions">{isActiveExperiment(value.status) && <button className="secondaryButton" disabled={session.busy} onClick={session.askCancel}>停止实验</button>}<a className="secondaryButton" download href={session.api.exportUrl(value.experiment_id, "json")}>下载JSON</a><a className="secondaryButton" download href={session.api.exportUrl(value.experiment_id, "csv")}>下载CSV</a></div></div><div className="experimentAccounting"><span>计划单元<strong>{value.summary.planned ?? value.plan.case_ids.length * value.plan.variants.length * value.plan.repetitions}</strong></span><span>执行完成<strong>{value.summary.completed ?? 0}</strong></span><span>失败 / 超时<strong>{value.summary.failed ?? 0} / {value.summary.timed_out ?? 0}</strong></span><span>评审失败 / 降级<strong>{value.summary.grader_failed ?? 0} / {value.summary.degraded ?? 0}</strong></span><span>HTTP尝试<strong>{asText(value.budget_ledger.http_attempts ?? 0)} / {value.plan.budget.max_http_attempts}</strong></span><span>已知费用<strong>{usage.known_cost === null || usage.known_cost === undefined ? "未记录" : metricValue(usage.known_cost, "usd")}</strong></span><span>费用记录覆盖<strong>{typeof usage.cost_usage_coverage === "number" ? `${Math.round(usage.cost_usage_coverage * 100)}%` : "未记录"}</strong></span><span>实验耗时<strong>{typeof value.budget_ledger.elapsed_seconds === "number" ? `${value.budget_ledger.elapsed_seconds.toFixed(1)}秒` : "未记录"}</strong></span></div><p className="evalHint">费用仅统计本次执行已记录的Provider调用；未知用量不填0，回放不重复计入原采集费用。</p>{value.stop_reason && <p className="inlineAlert neutralAlert">停止原因：{value.stop_reason}。未完成单元仍保留在计划分母中。</p>}</>;
}

export function MetricMatrix({ experiment, preferred = [] }: { experiment: EvaluationExperiment; preferred?: string[] }) {
  const variants = experiment.summary.variants ?? {};
  const available = [...new Set(Object.values(variants).flatMap(item => Object.keys(item.metrics ?? {})))];
  const metricIds = primaryMetricIds(experiment);
  return <div className="experimentMetricMatrix"><div className="evalSectionHead"><div><h3>配置与指标</h3><p>每项均显示可评数量/计划数量；未评测不是0分。不同评分单位分别解释，不合成总分。</p></div></div>{!metricIds.length ? <p className="emptyInline">尚无可汇总结果，实验结束或产物保存后自动更新。</p> : <div className="evalTableScroll"><table className="evalTable"><thead><tr><th>评分项目</th>{experiment.plan.variants.map(variant => <th key={variant.variant_id}>{variant.name}</th>)}</tr></thead><tbody>{metricIds.map(id => <tr key={id}><td>{metricName(id)}<small>{Object.values(variants).find(item => item.metrics[id])?.metrics[id].scoring_unit ?? ""}</small></td>{experiment.plan.variants.map(variant => { const item = variants[variant.variant_id]?.metrics[id]; return <td key={variant.variant_id}><span className={item?.mean === null || item?.mean === undefined ? "experimentMissing" : "experimentMetricValue"}>{metricValue(item?.mean, item?.unit)}</span><small>{item?.evaluated ?? 0}/{item?.planned ?? variants[variant.variant_id]?.planned ?? 0}可评</small></td>; })}</tr>)}</tbody></table></div>}</div>;
}

/** All groups share the same eligible case/trial intersection and value axis. */
export function WithinExperimentComparison({ experiment }: { experiment: EvaluationExperiment }) {
  const ordered = primaryMetricIds(experiment);
  const models = useMemo(() => primaryMetricIds(experiment).map(metricId => buildMultiGroupChart({ experiment, metricId })).filter(model => model !== null), [experiment]);
  const [metric, setMetric] = useState("");
  const metricIdentity = JSON.stringify(ordered);
  useEffect(() => { setMetric(previous => ordered.includes(previous) ? previous : ordered[0] ?? ""); }, [experiment.experiment_id, metricIdentity]);
  const selectedMetric = ordered.includes(metric) ? metric : ordered[0] ?? "";
  return <section className="withinExperimentComparison" aria-label="所有配置组效果比较">
    <div className="evalSectionHead"><div><h3>所有配置组效果比较</h3><p>先看各组数值与组间差距，再点击指标查看图表。每行只比较全部组共同可评的相同题目，不生成综合总分。</p></div><span className="softBadge">{ordered.length}项主指标 / {experiment.plan.variants.length}组配置</span></div>
    {!ordered.length ? <p className="emptyInline">尚无可展示的指标。已计划但未完成的单元不会被当作0分。</p> : <>
      <p className="metricOverviewGuide">差距按最大均值减最小均值计算，不是提升率；窄屏可横向滚动查看全部配置，指标名称固定。</p>
      <div className="metricPrimaryOverview metricAllGroupChart" tabIndex={0} role="region" aria-label="主指标数值对照，可横向滚动"><table className="metricPrimaryTable"><caption>组间差距为最大均值减最小均值，不是提升率。比例以百分比呈现，MRR仍为0—1排序分；不同指标的共同样本数可能不同。</caption><thead><tr><th scope="col">主指标</th>{experiment.plan.variants.map((variant, index) => <th scope="col" key={variant.variant_id} style={{ "--group-color": `var(--metric-group-${index % 6})` } as CSSProperties}><i aria-hidden="true" />{variant.name}</th>)}<th scope="col">组间差距</th></tr></thead><tbody>{models.map(model => <tr key={model.metricId} data-overview-metric={model.metricId} data-common={model.commonEvaluated} data-spread={model.range?.spread ?? "missing"} data-selected={selectedMetric === model.metricId}>
        <th scope="row"><button type="button" data-metric-id={model.metricId} aria-pressed={selectedMetric === model.metricId} title={metricDescription(model.metricId)} onClick={() => setMetric(model.metricId)}>{metricName(model.metricId)}</button><small>{model.directionLabel} / 共同可评{model.commonEvaluated}/{model.planned}</small></th>
        {model.groups.map(group => <td key={group.id} data-overview-group={group.id} data-value={group.value ?? "missing"}>{formatPrimaryValue(model.metricId, group.value, model.unit)}</td>)}
        <td className="metricPrimaryGap">{model.groups.length < 2 ? "单组不比较" : model.range?.spread === 0 ? "均值持平" : formatPrimaryGap(model.metricId, model.range?.spread ?? null, model.unit)}</td>
      </tr>)}</tbody></table></div>
      <p className="evalHint">固定主指标不随本次分差增删；其余诊断保留在JSON/CSV导出中。{ordered.length < PRIMARY_METRICS[experiment.protocol].length ? "旧记录未采集部分主指标，不用其他指标或0分补位。" : "缺评不计0，均值持平也不隐藏；只有少量题目时不作显著性结论。"}</p>
      <EvaluationComparisonChart experiment={experiment} metricId={selectedMetric} />
    </>}
  </section>;
}
export function ResultCells({ session, dataset, columns }: { session: EvaluationSession; dataset?: EvaluationDataset; columns: string[] }) {
  const experiment = session.selected; if (!experiment) return null;
  return <div className="experimentResultCells"><h3>逐题记录</h3>
    <div className="evalTableScroll"><table className="evalTable">
      <thead><tr><th>样本 / 配置</th><th>执行状态</th>{columns.map(key => <th key={key}>{metricName(key)}</th>)}<th>详情</th></tr></thead>
      <tbody>{experiment.results.map(item => <tr key={cellKey(item)} data-selected={session.selectedCase && cellKey(item) === cellKey(session.selectedCase)}>
        <td>{item.case_id} / {experiment.plan.variants.find(variant => variant.variant_id === item.variant_id)?.name ?? item.variant_id}<small>第{item.trial}次 / {dataset?.cases.find(value => value.case_id === item.case_id)?.query ?? ""}</small></td>
        <td>{stateName(item.status)}{item.degraded.length > 0 && <small className="formError">已降级</small>}<small>{(item.elapsed_ms / 1000).toFixed(2)}秒</small></td>
        {columns.map(key => <td key={key}>{metricValue(item.metrics[key]?.value, item.metrics[key]?.unit)}<small>{item.metrics[key] ? stateName(item.metrics[key].status) : "未评测"}</small></td>)}
        <td><button type="button" className="textButton" aria-haspopup="dialog" data-detail-case={item.case_id} data-detail-variant={item.variant_id} data-detail-trial={item.trial} onClick={() => void session.detail(item)}>查看详情</button></td>
      </tr>)}</tbody>
    </table></div>
  </div>;
}

export function ResultDetails({ session, children }: { session: EvaluationSession; children?: ReactNode }) {
  const value = session.selectedCase; if (!value) return null;
  const primary = session.selected ? primaryMetricIds(session.selected) : [];
  const variant = session.selected?.plan.variants.find(item => item.variant_id === value.variant_id)?.name ?? value.variant_id;
  return <EvaluationDetailDialog key={`${value.case_id}:${value.variant_id}:${value.trial}`} description={`${value.case_id} / ${variant} / 第${value.trial}次`} onClose={session.closeDetail}>
    <section className="experimentCaseDetail" aria-label="逐题评分依据"><div className="evalSectionHead"><div><h3>{value.case_id} / {variant} / 第{value.trial}次</h3><p>{stateName(value.status)}；以保存的真实产物为依据。</p></div></div>{value.error && <p role="alert" className="formError">{value.error}</p>}{value.degraded.length > 0 && <p className="inlineAlert neutralAlert">降级：{value.degraded.join("；")}</p>}<div className="experimentStages">{Object.entries(value.stage_status).map(([key, status]) => <span key={key}>{key}<strong>{stateName(status)}</strong></span>)}</div><div className="experimentMetricEvidence">{Object.entries(value.metrics).filter(([key]) => primary.includes(key)).map(([key, item]) => <article key={key}><div><strong>{metricName(key)}</strong><span>{formatPrimaryValue(key, typeof item.value === "boolean" ? Number(item.value) : item.value, item.unit)}</span></div><p>{stateName(item.status)} / {item.scoring_unit} / {item.grader_id}:{item.grader_version}</p>{item.reason && <p>{item.reason}</p>}{item.evidence_refs?.length > 0 && <small>依据：{item.evidence_refs.join("、")}</small>}</article>)}</div>{value.diagnostics && Object.keys(value.diagnostics).length > 0 && <DiagnosticsDetails value={value.diagnostics} />}{session.artifactLoading ? <p role="status">正在读取冻结产物。</p> : session.artifactError ? <p className="formError" role="alert">{session.artifactError}</p> : !value.input_id ? <p className="emptyInline">此单元尚未保存产物，不根据执行状态推测评分。</p> : children}</section>
  </EvaluationDetailDialog>;
}

function DiagnosticsDetails({ value }: { value: JsonRecord }) {
  return <section className="experimentDiagnostics"><h4>定位诊断</h4><p className="evalHint">辅助解释，不计入统一总分。</p><dl>{Object.entries(value).map(([key, item]) => <div key={key}><dt>{key}</dt><dd>{typeof item === "object" ? <pre>{JSON.stringify(item, null, 2)}</pre> : asText(item)}</dd></div>)}</dl></section>;
}

export function GoldDetails({ value, title = "参考标注" }: { value?: EvaluationCase; title?: string }) {
  if (!value) return <section className="experimentGold"><h4>{title}</h4><p>没有与本实验标签版本一致的参考标注，不用当前题集解释历史评分。</p></section>;
  const labels = Object.entries(value).filter(([key, content]) => !["case_id", "query", "kind", "tags"].includes(key) && content !== null && (!Array.isArray(content) || content.length));
  const names: Record<string, string> = { gold_status: "标注状态", source_gold_status: "来源标注", reference_status: "参考事实状态", anchors: "证据定位", reference_facts: "参考事实", reference_answer: "参考答案", reference_provenance: "原文核验依据", reference_version: "答案标注版本", expected_facts: "必要事实", expected_sources: "预期来源与锚点", relevant_source_ids: "相关来源", trajectory_assertions: "轨迹断言", assertions: "执行断言", output_rubric: "输出要求", report_rubric: "报告验收要求", objective: "任务目标", required_invariants: "工程约束", policy_requirements: "场景强制策略", requires_knowledge: "需要知识库", answerable: "可回答", protocol: "实验协议", qrels_status: "相关性标注状态", qrels: "相关性标注" };
  return <section className="experimentGold"><h4>{title}</h4><p>{value.query}</p><dl>{labels.map(([key, content]) => <div key={key}><dt>{names[key] ?? key}</dt><dd>{key === "expected_sources" && Array.isArray(content) ? content.map((source, index) => { const item = record(source); return <article className="experimentGoldAnchor" key={index}><strong>{asText(item.source_id)}{item.page_number ? ` / 第${asText(item.page_number)}页` : ""}</strong><p>{asText(item.anchor)}</p>{Boolean(item.heading) && <small>{asText(item.heading)}</small>}</article>; }) : Array.isArray(content) && content.every(item => typeof item === "string") ? <ul>{content.map((item, index) => <li key={index}>{item}</li>)}</ul> : typeof content === "string" ? stateName(content) : typeof content === "boolean" ? content ? "是" : "否" : <pre>{JSON.stringify(content, null, 2)}</pre>}</dd></div>)}</dl></section>;
}
export function MarkdownArtifact({ content }: { content: unknown }) { return typeof content === "string" && content.trim() ? <div className="experimentMarkdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown></div> : <p className="emptyInline">此产物没有保存可展示的文本。</p>; }


export function CheckList({ checks, title }: { checks: JsonRecord[]; title: string }) {
  return <section className="experimentChecks"><h4>{title}</h4>{checks.length ? checks.map((check, index) => { const status = asText(check.status), passed = typeof check.passed === "boolean" ? check.passed : typeof check.value === "boolean" ? check.value : null; return <article key={index}><strong>{asText(check.description ?? check.name ?? check.assertion ?? check.check_id ?? check.id ?? `检查${index + 1}`)}</strong><span className={passed === false ? "formError" : passed === true ? "experimentPositive" : "experimentMissing"}>{passed === null ? typeof check.value === "number" ? metricValue(check.value) : stateName(status || "not_evaluated") : passed ? "通过" : "未通过"}</span><p>{asText(check.reason ?? check.message ?? check.evidence)}</p>{Array.isArray(check.evidence_refs) && <small>{check.evidence_refs.join("、")}</small>}</article>; }) : <p className="emptyInline">没有足够的轨迹或参数证据，不能推断已通过。</p>}</section>;
}
