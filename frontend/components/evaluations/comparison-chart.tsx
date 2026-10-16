"use client";

import { CSSProperties, useId } from "react";
import { buildMultiGroupChart, chartPosition, formatPrimaryValue } from "../../lib/evaluation-chart";
import { EvaluationExperiment } from "../../lib/evaluations-v2";

const groupStyle = (index: number): CSSProperties => ({ "--group-color": `var(--metric-group-${index % 6})` } as CSSProperties);

/** Every displayed mean uses the complete all-group intersection, never mixed cohorts. */
export function EvaluationComparisonChart({ experiment, metricId }: { experiment: EvaluationExperiment; metricId: string }) {
  const model = buildMultiGroupChart({ experiment, metricId });
  const titleId = useId(), explanationId = useId();
  if (!model) return <div className="metricChartEmpty" role="status">尚未保存此指标。请选择另一指标或等待评测完成。</div>;
  const reading = (value: number | null, unit: string) => formatPrimaryValue(metricId, value, unit);
  const position = (value: number) => (chartPosition(value, model.domain) ?? 0) * 100;
  const zero = position(0), groupsById = new Map(model.groups.map(group => [group.id, group]));
  const axis = [model.domain[0], model.domain[0] / 2 + model.domain[1] / 2, model.domain[1]];
  return <section className="metricComparisonChart metricAllGroupChart" data-metric-id={metricId} data-direction={model.direction} data-unit={model.unit} data-paired={model.commonEvaluated} data-planned={model.planned} data-excluded={model.excluded} aria-labelledby={titleId} aria-describedby={explanationId}>
    <header className="metricChartHeading"><div><h4 id={titleId}>{model.title}</h4><p>{model.description}</p></div><span>{model.directionLabel}</span></header>
    <div className="metricChartCohort"><span>共同可评<strong>{model.commonEvaluated}/{model.planned}</strong></span><span>未纳入均值<strong>{model.excluded}</strong></span><p>所有组使用相同题目与重复记录，统一分母、统一刻度。缺评不当作0分。</p></div>
    <ul className="metricGroupLegend" aria-label="配置组颜色图例">{model.groups.map(group => <li key={group.id} style={groupStyle(group.colorIndex)}><i aria-hidden="true" /><span>{group.label}</span></li>)}</ul>
    <div className="metricChartPanels">
      <figure className="metricMeanChart" aria-label="所有配置组的共同样本均值"><figcaption><strong>各组均值</strong><span>同一值轴、同一批共同可评样本，不指定基线或候选。</span></figcaption>
        <div className="metricChartMeanRows">{model.groups.map(group => {
          const point = group.value === null ? null : position(group.value);
          return <div className="metricChartMeanRow" key={group.id} style={groupStyle(group.colorIndex)} data-variant-id={group.id} data-value={group.value ?? "missing"}>
            <span className="metricChartGroupName"><i aria-hidden="true" /><span>{group.label}<small>本组可评{group.available}/{group.planned}</small></span></span>
            <div className="metricMeanPlot" role="img" aria-label={`${group.label}均值：${reading(group.value, model.unit)}；共同可评${model.commonEvaluated}/${model.planned}`}>
              <div className="metricMeanTrack" aria-hidden="true" /><span className="metricMeanZero" style={{ left: `${zero}%` }} aria-hidden="true" />
              {group.value !== null && group.value !== 0 && point !== null && <span data-chart-bar="mean" className="metricGroupBar" style={{ left: `${Math.min(zero, point)}%`, width: `${Math.abs(point - zero)}%` }} aria-hidden="true" />}
              {group.value === 0 && <span data-zero-marker="mean" className="metricGroupZero" style={{ left: `${zero}%` }} aria-hidden="true" />}
              {group.value === null && <span className="metricMeanMissing">缺少共同可评数据</span>}
            </div><strong className="metricChartValue">{reading(group.value, model.unit)}</strong>
          </div>;
        })}</div>
        <div className="metricMeanAxis" aria-hidden="true">{axis.map((value, index) => <span key={index}>{reading(value, model.unit)}</span>)}</div>
        <p className="metricMeanCohortNote">每组均值均来自相同的{model.commonEvaluated}个单元。增加一组配置可能缩小共同样本，应结合覆盖数解释。</p>
      </figure>
      <section className="metricComparisonExplanation" id={explanationId} aria-label="指标结果解释"><h4>如何理解这次结果</h4>{model.interpretation.map((line, index) => <p key={index}>{line}</p>)}</section>
    </div>
    <figure className="metricMultiCaseChart" aria-label="所有配置组的逐题表现"><figcaption><div><strong>逐题表现</strong><span>各组使用同一值轴。颜色对应配置组；点越靠右表示数值越大，不一定代表效果更好。</span></div></figcaption>
      <div className="metricMultiCaseAxis" aria-hidden="true">{axis.map((value, index) => <span key={index}>{reading(value, model.unit)}</span>)}</div>
      <div className="metricMultiCaseRows">{model.rows.map(row => <div className="metricMultiCaseRow" key={`${row.caseId}:${row.trial}`} data-case-id={row.caseId} data-trial={row.trial} data-pair-status={row.status}>
        <span className="metricMultiCaseLabel">{row.caseId}<small>第{row.trial}次</small><small>{row.status === "common" ? "共同样本" : "未共同可评"}</small></span>
        <div className="metricMultiCaseObservation">{row.status === "common" ? <>
          <div className="metricMultiCasePlot" style={{ height: Math.max(38, model.groups.length * 18 + 8) }} role="img" aria-label={`${row.caseId}第${row.trial}次：${row.values.map(value => `${groupsById.get(value.variantId)?.label ?? value.variantId}，${reading(value.value, model.unit)}`).join("；")}`}>
            {[0, 25, 50, 75, 100].map(value => <span key={value} className="metricMultiGridLine" style={{ left: `${value}%` }} aria-hidden="true" />)}
            {row.values.map((value, index) => {
              const group = groupsById.get(value.variantId); if (value.value === null || !group) return null;
              return <div className="metricGroupPointLane" key={value.variantId} style={{ top: index * 18 + 4 }} aria-hidden="true"><span className="metricGroupPoint" data-group-point="true" data-group-id={value.variantId} data-value={value.value} style={{ ...groupStyle(group.colorIndex), left: `${position(value.value)}%` }} /></div>;
            })}
          </div>
          <ul className="metricMultiCaseValues">{row.values.map(value => {
            const group = groupsById.get(value.variantId); return <li key={value.variantId} style={groupStyle(group?.colorIndex ?? 0)}><i aria-hidden="true" /><span>{group?.label ?? value.variantId}</span><strong>{reading(value.value, model.unit)}</strong></li>;
          })}</ul>
        </> : <p className="metricMultiCaseExcluded">{row.reasons.join("；") || "缺少所有组共同可评的依据"}。本题不纳入各组均值，不绘制为0。</p>}</div>
      </div>)}</div>
    </figure>
    <div className="metricChartLimitations"><strong>解释边界</strong>{model.limitations.map((line, index) => <p key={index}>{line}</p>)}</div>
  </section>;
}
