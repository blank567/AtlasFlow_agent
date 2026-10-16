"""Agent result aggregation preserves scoring coverage and repetition identity."""
from __future__ import annotations

from collections import defaultdict


def summarize_agent(rows: list[dict], expected_count: int) -> dict:
    variants = defaultdict(list)
    for row in rows:
        variants[row["variant_id"]].append(row)
    summaries = {}
    for variant, results in variants.items():
        names = {name for row in results for name in row.get("metrics", {})}
        metrics = {}
        for name in sorted(names):
            scored = [row["metrics"][name] for row in results
                      if row.get("metrics", {}).get(name, {}).get("status") == "evaluated"
                      and row["metrics"][name].get("value") is not None]
            values = [float(item["value"]) for item in scored]
            metrics[name] = {"mean": sum(values) / len(values) if values else None,
                             "evaluated_count": len(values), "case_count": len(results)}
        repeated = defaultdict(list)
        for row in results:
            repeated[row["case_id"]].append(row)
        complete_repeats = [items for items in repeated.values()
                            if all(item.get("metrics", {}).get("task_success", {}).get("value") is not None for item in items)]
        summaries[variant] = {
            "metrics": metrics, "result_count": len(results),
            "all_observed_trials_successful_ratio": (
                sum(all(item["metrics"]["task_success"]["value"] is True for item in items) for items in complete_repeats) / len(complete_repeats)
                if complete_repeats else None
            ),
            "fully_graded_scenario_count": len(complete_repeats),
            "stability_note": "仅统计已观测且全部获得语义评审的重复；不是Pass@k或Pass^k估计",
        }
    return {"expected_count": expected_count, "result_count": len(rows), "variants": summaries,
            "overall_score": None, "reason": "Agent四维并列，不与RAG指标或工程契约混合求总分"}
