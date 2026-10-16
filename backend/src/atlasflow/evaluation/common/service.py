from __future__ import annotations

import asyncio
import csv
import inspect
import io
import json
import math
from collections import Counter
from copy import deepcopy
from time import perf_counter

from atlasflow.evaluation.budget import (
    EvaluationBudget,
    EvaluationBudgetExceeded,
    evaluation_budget_context,
)
from atlasflow.evaluation.common.contracts import (
    CaseResult,
    MetricResult,
    as_dict,
    cell_key,
    fingerprint,
    make_experiment,
    new_id,
    now_iso,
)
from atlasflow.evaluation.contracts import EvaluationBudgetSpec
from atlasflow.evaluation.langsmith_adapter import private_evaluation_tracing, provider_capture
from atlasflow.observability import redact_sensitive_data


async def _await(value):
    return await value if inspect.isawaitable(value) else value


def _error(exc):
    return str(redact_sensitive_data(str(exc)))[:500]


def _config(variant):
    return {key: value for key, value in variant.items() if key not in {"variant_id", "name"}}


def _finite_number(value):
    if not isinstance(value, (int, float, bool)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


_METRIC_CONTRACT = ("unit", "direction", "scoring_unit", "grader_id", "grader_version")


def _variant_metric_pair(baseline, candidate, name):
    """Pair one metric only within the same immutable case/trial cell."""
    reasons = []
    for side, row in (("baseline", baseline), ("candidate", candidate)):
        if row is None:
            reasons.append(f"{side}_cell_missing")
        else:
            if row.get("status") != "completed":
                reasons.append(f"{side}_cell_{row.get('status', 'not_run')}")
            if row.get("degraded"):
                reasons.append(f"{side}_degraded")
    left = (baseline or {}).get("metrics", {}).get(name)
    right = (candidate or {}).get("metrics", {}).get(name)
    for side, metric in (("baseline", left), ("candidate", right)):
        if metric is None:
            reasons.append(f"{side}_metric_missing")
        else:
            if metric.get("status") != "evaluated":
                reasons.append(f"{side}_metric_{metric.get('status', 'not_run')}")
            if not _finite_number(metric.get("value")):
                reasons.append(f"{side}_metric_value_unavailable")
    if left and right and any(left.get(field) != right.get(field) for field in _METRIC_CONTRACT):
        reasons.append("metric_contract_mismatch")
    a = left.get("value") if left and _finite_number(left.get("value")) else None
    b = right.get("value") if right and _finite_number(right.get("value")) else None
    delta = float(b) - float(a) if not reasons else None
    if delta is not None and not math.isfinite(delta):
        reasons.append("metric_delta_nonfinite")
        delta = None
    outcome = None
    if delta is not None:
        direction = left.get("direction", "neutral")
        if math.isclose(delta, 0, rel_tol=0, abs_tol=1e-12):
            outcome = "tied"
        elif direction == "higher":
            outcome = "improved" if delta > 0 else "regressed"
        elif direction == "lower":
            outcome = "improved" if delta < 0 else "regressed"
        else:
            outcome = "unclassified"
    return {
        "baseline": a, "candidate": b, "delta": delta,
        "status": "excluded" if reasons else "paired", "outcome": outcome,
        "reason": "; ".join(reasons) if reasons else None, "reasons": reasons,
    }


def _identity(context, *, index=True):
    value = deepcopy(context)
    # Full source hashes are audit metadata. Runtime identity excludes ingestion
    # code, whose own immutable parser/chunker identity belongs to the index.
    value.get("environment_identity", {}).pop("full_code_hash", None)
    value.pop("validation_errors", None)
    value.pop("validation_warnings", None)
    if not index:
        snapshot = value.pop("snapshot", {})
        # Source identity is still fixed when explicitly comparing index generations.
        value["source_id_map"] = context.get("source_id_map", {})
        value["source_hash_map"] = sorted(context.get("source_hash_map", {}).values())
        value["source_manifest"] = context.get("corpus_manifest_hash")
        evaluation = value.get("evaluation_context", {})
        value["evaluation_context"] = {
            key: item for key, item in evaluation.items()
            if key not in {"embedding_compatibility", "requested_models"}
        }
        requested = evaluation.get("requested_models", {})
        value["non_index_models"] = {
            key: item for key, item in requested.items() if key != "embedding"
        }
        del snapshot
    return fingerprint(value)


def summarize(results: list[dict], planned: int, variants: list[dict]) -> dict:
    """Keep conditional quality separate from all failure and coverage counts."""
    counts = Counter(row["status"] for row in results)
    summary = {
        "planned": planned,
        "completed": counts["completed"],
        "failed": counts["failed"],
        "timed_out": counts["timed_out"],
        "budget_stopped": counts["budget_stopped"],
        "not_run": counts["not_run"] + max(0, planned - len(results)),
        "grader_failed": sum(row.get("stage_status", {}).get("judge") == "grader_failed" for row in results),
        "degraded": sum(bool(row.get("degraded")) for row in results),
        "variants": {},
    }
    captured = {}
    for row_index, row in enumerate(results):
        for call_index, call in enumerate(row.get("provider_calls", [])):
            captured[call.get("call_id") or f"{row_index}:{call_index}"] = call
    usages = [call.get("usage") or {} for call in captured.values()]
    costs = [usage["cost"] for usage in usages if usage.get("cost") is not None]
    tokens = [usage["total_tokens"] for usage in usages if usage.get("total_tokens") is not None]
    summary["provider_usage"] = {
        "provider_calls": len(captured),
        "known_cost": sum(costs) if costs else None,
        "cost_usage_coverage": len(costs) / len(captured) if captured else None,
        "known_tokens": sum(tokens) if tokens else None,
        "token_usage_coverage": len(tokens) / len(captured) if captured else None,
        "coverage_basis": "recorded_provider_calls_not_all_tool_http_attempts",
    }
    per_variant = planned // len(variants) if variants else 0
    for variant in variants:
        variant_id = variant["variant_id"]
        rows = [row for row in results if row["variant_id"] == variant_id]
        states = Counter(row["status"] for row in rows)
        metrics = {}
        for name in sorted({key for row in rows for key in row.get("metrics", {})}):
            items = [row.get("metrics", {}).get(name) for row in rows]
            items = [item for item in items if item is not None]
            valid = [
                item["value"] for item in items
                if item.get("status") == "evaluated"
                and isinstance(item.get("value"), (int, float, bool))
            ]
            first = items[0]
            metrics[name] = {
                "mean": sum(valid) / len(valid) if valid else None,
                "evaluated": len(valid),
                "planned": per_variant,
                "status_counts": dict(Counter(item.get("status", "not_run") for item in items)),
                "unit": first.get("unit", "ratio"),
                "direction": first.get("direction", "higher"),
                "scoring_unit": first.get("scoring_unit", "case"),
            }
        summary["variants"][variant_id] = {
            "planned": per_variant,
            "completed": states["completed"],
            "failed": states["failed"],
            "timed_out": states["timed_out"],
            "budget_stopped": states["budget_stopped"],
            "not_run": states["not_run"] + max(0, per_variant - len(rows)),
            "grader_failed": sum(row.get("stage_status", {}).get("judge") == "grader_failed" for row in rows),
            "degraded": sum(bool(row.get("degraded")) for row in rows),
            "metrics": metrics,
        }
    return summary


class V2MetadataLangSmithAdapter:
    """Strictly allowlisted metadata: never queries, labels, reasons or artifacts."""

    def __init__(self, client=None, project="atlasflow-evaluations-v2"):
        self.client = client
        self.project = project

    async def sync(self, experiment: dict):
        if self.client is None:
            return "not_configured"
        from datetime import datetime
        from uuid import uuid4

        identifier = uuid4()
        payload = {
            key: experiment[key] for key in
            ("experiment_id", "kind", "protocol", "dataset_hash", "status")
        }
        payload["content_redacted"] = True
        payload["budget"] = {
            key: value for key, value in experiment["budget_ledger"].items()
            if key in {"http_attempts", "max_http_attempts", "elapsed_seconds", "max_elapsed_seconds"}
        }
        payload["scores"] = [
            {
                "case_id": row["case_id"], "variant_id": row["variant_id"],
                "trial": row["trial"], "status": row["status"],
                "metrics": {
                    key: {"value": value.get("value"), "status": value.get("status")}
                    for key, value in row.get("metrics", {}).items()
                },
            } for row in experiment["results"]
        ]
        try:
            await asyncio.to_thread(
                self.client.create_run, id=identifier, name="atlasflow.evaluation.v2.summary",
                run_type="chain", inputs={"experiment_id": experiment["experiment_id"], "content_redacted": True},
                project_name=self.project,
                start_time=datetime.fromisoformat(experiment["created_at"]),
                extra={"metadata": {"protocol": experiment["protocol"], "dataset_hash": experiment["dataset_hash"]}},
            )
            await asyncio.to_thread(
                self.client.update_run, identifier, outputs=payload,
                end_time=datetime.fromisoformat(now_iso()),
            )
            await asyncio.to_thread(self.client.flush)
        except Exception:  # noqa: BLE001 - telemetry cannot invalidate saved local results
            return "failed"
        return "synced_metadata_only"


class V2EvaluationService:
    """Shared experiment lifecycle; injected domain evaluators own all quality semantics."""

    def __init__(
        self, store, *, datasets: list[dict], snapshot_resolver=None, executor=None,
        evaluator=None, judge=None, langsmith_adapter=None, default_budgets=None,
    ):
        self.store = store
        self.datasets = [as_dict(item) for item in datasets]
        self.snapshot_resolver = snapshot_resolver
        self.executor = executor
        self.evaluator = evaluator
        self.judge = judge
        self.langsmith_adapter = langsmith_adapter or V2MetadataLangSmithAdapter()
        self.default_budgets = default_budgets or {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._budgets: dict[str, EvaluationBudget] = {}
        self._write_lock = asyncio.Lock()

    async def initialize(self, *, recover_interrupted=False):
        await self.store.initialize()
        if recover_interrupted:
            for current in await self.store.list(limit=1000):
                if current["status"] in {"planned", "running"}:
                    self._fill_missing(current, "not_run", "process_interrupted")
                    current.update(status="partial", ended_at=now_iso(), stop_reason="process_interrupted")
                    await self.store.save(current)

    async def close(self):
        for budget in self._budgets.values():
            budget.stop("service_shutdown")
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.close()

    def list_datasets(self, kind):
        return [{**deepcopy(dataset), "dataset_hash": fingerprint(dataset)}
                for dataset in self.datasets if dataset.get("kind") == kind]

    def settings(self, kind):
        defaults = {
            "max_http_attempts": 60, "max_elapsed_seconds": 1200,
            "case_timeout_seconds": 600 if kind == "agent" else 90,
            "concurrency": 1 if kind == "agent" else 2,
        }
        defaults.update(self.default_budgets.get(kind, {}))
        return {
            "kind": kind,
            "protocols": ["rag-retrieval-v2", "rag-answer-v2"] if kind == "rag" else ["agent-task-v2", "agent-contract-v2"],
            "budget": defaults, "default_mode": "live" if kind == "rag" else "replay",
            "supported_modes": ["live"] if kind == "rag" else ["replay", "live"],
            "judge_available": self.judge is not None,
            "langsmith_available": getattr(self.langsmith_adapter, "client", None) is not None,
            "langsmith_content_mode": "metadata_only",
            **as_dict(getattr(self.executor, "extra_settings", {}).get(kind, {})),
        }

    def _plan(self, plan, kind=None):
        value = as_dict(plan)
        actual_kind = value.get("kind")
        if actual_kind not in {"rag", "agent"} or kind is not None and kind != actual_kind:
            raise ValueError("experiment kind does not match this workspace")
        if actual_kind == "rag":
            from atlasflow.evaluation.rag.contracts import RagExperimentPlan
            return RagExperimentPlan.model_validate(value).model_dump(mode="json")
        from atlasflow.evaluation.agent.contracts import AgentExperimentPlan
        return AgentExperimentPlan.model_validate(value).model_dump(mode="json")

    def _cases(self, plan):
        dataset = next((row for row in self.datasets if row.get("dataset_id") == plan["dataset_id"]), None)
        if dataset is None:
            raise KeyError("evaluation dataset not found")
        if dataset.get("kind") != plan["kind"]:
            raise ValueError("dataset kind does not match experiment kind")
        protocols = dataset.get("protocols")
        if protocols and plan["protocol"] not in protocols:
            raise ValueError("dataset does not support this evaluation protocol")
        cases = dataset.get("cases", dataset.get("scenarios", []))
        by_id = {item["case_id"]: item for item in cases if item.get("protocol", plan["protocol"]) == plan["protocol"]}
        ids = plan.get("case_ids") or list(by_id)[:10]
        if not ids or len(set(ids)) != len(ids) or any(key not in by_id for key in ids):
            raise ValueError("case selection is empty, duplicated, or contains unknown cases")
        return dataset, [deepcopy(by_id[key]) for key in ids]

    async def _resolved_variant(self, protocol, variant, context, case=None):
        resolve = getattr(self.executor, "resolve_variant", None)
        if resolve is not None:
            return as_dict(await _await(resolve(protocol, variant, context, case)))
        return _config(variant)

    @staticmethod
    def _knowledge_needed(plan, cases):
        if plan["kind"] == "rag":
            return True
        return any(case.get("requires_knowledge", False) for case in cases) or any(
            variant.get("policy", {}).get("knowledge_mode", "off") != "off" for variant in plan["variants"]
        )

    async def preflight(self, plan, kind=None):
        plan = self._plan(plan, kind)
        dataset, cases = self._cases(plan)
        errors, warnings = [], []
        dataset_hash = fingerprint(dataset)
        context = {
            "dataset_id": dataset["dataset_id"], "dataset_hash": dataset_hash,
            "corpus_manifest_hash": dataset.get("corpus_manifest_hash"),
            "scoring_version": dataset.get("scoring_version", plan["protocol"]),
            "judge_identity": getattr(self.judge, "identity", None) if plan.get("judge_enabled") else None,
            "environment_identity": as_dict(getattr(self.executor, "environment_identity", {})),
        }
        refs = dict(plan.get("input_refs", {}))
        cells = [(case, variant, trial) for case in cases for variant in plan["variants"]
                 for trial in range(1, plan.get("repetitions", 1) + 1)]
        if plan.get("judge_enabled") and self.judge is None:
            errors.append("independent Judge is not configured")
        if plan.get("langsmith_sync") and getattr(self.langsmith_adapter, "client", None) is None:
            warnings.append("LangSmith metadata synchronization is not configured")
        if plan["mode"] == "live":
            if self.executor is None:
                errors.append("domain live executor is not configured")
            if self._knowledge_needed(plan, cases):
                declared_spaces = {
                    variant.get("policy", {}).get("knowledge_space")
                    for variant in plan["variants"]
                    if variant.get("policy", {}).get("knowledge_mode") == "selected"
                    and variant.get("policy", {}).get("knowledge_space")
                }
                if len(declared_spaces) > 1 or plan.get("space") and any(space != plan["space"] for space in declared_spaces):
                    errors.append("Agent variants must use one explicit common knowledge space for a paired experiment")
                selected_space = plan.get("space") or next(iter(declared_spaces), None) or dataset.get("space")
                if self.snapshot_resolver is None:
                    errors.append("read-only knowledge snapshot resolver is not configured")
                else:
                    try:
                        resolved = as_dict(await self.snapshot_resolver(selected_space, plan.get("space_version_id")))
                        if "snapshot" in resolved:
                            context.update(resolved)
                        else:
                            context["snapshot"] = resolved
                        errors.extend(context.get("validation_errors", []))
                        warnings.extend(context.get("validation_warnings", []))
                        snapshot = context.get("snapshot", {})
                        source_ids = context.get("source_id_map", {})
                        source_hashes = context.get("source_hash_map", {})
                        documents = snapshot.get("documents")
                        if isinstance(documents, dict):
                            published = {
                                (source_ids.get(doc_id), source_hashes.get(state.get("version_id")))
                                for doc_id, state in documents.items()
                                if state.get("status") == "active" and state.get("version_id")
                            }
                            for case in cases:
                                for source in case.get("expected_sources", []):
                                    if (source["source_id"], source["content_hash"]) not in published:
                                        errors.append(f"gold source missing or changed in selected snapshot: {source['source_id']}")
                    except Exception as exc:  # noqa: BLE001 - no network work in preflight
                        errors.append(f"snapshot unavailable: {_error(exc)}")
            validator = getattr(self.executor, "validate_case", None)
            for case, variant, _ in cells:
                if validator is not None:
                    try:
                        problem = await _await(validator(plan["protocol"], case, variant, context))
                        if isinstance(problem, str):
                            errors.append(problem)
                        elif problem:
                            errors.extend(problem)
                    except ValueError as exc:
                        errors.append(str(exc))
            plan_validator = getattr(self.executor, "validate_plan", None)
            if plan_validator is not None:
                try:
                    problem = await _await(plan_validator(plan["protocol"], plan, cases, context))
                    if isinstance(problem, str):
                        errors.append(problem)
                    elif problem:
                        errors.extend(problem)
                except ValueError as exc:
                    errors.append(str(exc))
            if self._knowledge_needed(plan, cases):
                self._validate_semantic_identity(plan, context, errors, warnings)
            warnings.append("HTTP额度包含重试及工具请求，不保证整个实验矩阵都能完成。")
        else:
            inputs = await self.store.list_inputs(plan["kind"])
            coherent_context = None
            # Explicit refs determine the common context; never silently replace a bad ref.
            for case, variant, trial in cells:
                key = cell_key(case["case_id"], variant["variant_id"], trial)
                if key in refs:
                    try:
                        row = await self.store.get_input(refs[key])
                        await self._validate_input(row, plan, case, variant, trial, dataset_hash)
                        coherent_context = row["context"] if coherent_context is None else coherent_context
                        if _identity(coherent_context) != _identity(row["context"]):
                            errors.append("replay artifacts do not share the same frozen context")
                    except (KeyError, ValueError) as exc:
                        errors.append(f"invalid frozen input {key}: {_error(exc)}")
            candidates = [coherent_context] if coherent_context is not None else [row["context"] for row in inputs]
            selected = None
            for candidate in candidates:
                proposed = dict(refs)
                valid_context = True
                for case, variant, trial in cells:
                    key = cell_key(case["case_id"], variant["variant_id"], trial)
                    if key in proposed:
                        continue
                    found = None
                    for row in inputs:
                        if _identity(row["context"]) != _identity(candidate):
                            continue
                        try:
                            await self._validate_input(row, plan, case, variant, trial, dataset_hash)
                            found = row
                            break
                        except ValueError:
                            continue
                    if found is None:
                        valid_context = False
                        break
                    proposed[key] = found["input_id"]
                if valid_context:
                    selected = candidate
                    refs = proposed
                    break
            if selected is not None:
                context = deepcopy(selected)
                context["judge_identity"] = getattr(self.judge, "identity", None) if plan.get("judge_enabled") else None
            for case, variant, trial in cells:
                key = cell_key(case["case_id"], variant["variant_id"], trial)
                if key not in refs:
                    errors.append(f"frozen input missing: {key}")
            if plan.get("judge_enabled"):
                warnings.append("回放重新评审仍会发送独立Judge请求，并占用HTTP额度。")
        if any(case.get("gold_status", "draft") != "verified" for case in cases):
            warnings.append("未审核标签不进入正式质量指标均值。")
        return {
            "ready": not errors, "errors": list(dict.fromkeys(errors)), "warnings": list(dict.fromkeys(warnings)),
            "kind": plan["kind"], "protocol": plan["protocol"], "dataset_hash": dataset_hash,
            "context": context, "case_ids": [case["case_id"] for case in cases],
            "expected_cells": len(cells), "budget": plan["budget"], "resolved_input_refs": refs,
        }

    @staticmethod
    def _validate_semantic_identity(plan, context, errors, warnings):
        snapshot = context.get("snapshot", {})
        evaluation = context.get("evaluation_context", snapshot.get("evaluation_context", {}))
        compatibility = evaluation.get("embedding_compatibility", {})
        base = evaluation.get("retrieval_profile", {})
        for variant in plan["variants"]:
            tuning = {**base, **{key: value for key, value in variant.get("retrieval_tuning", {}).items() if value is not None}}
            wants_vector = (tuning.get("vector_weight") if tuning.get("vector_weight") is not None else 0.5) > 0
            bad = snapshot.get("identity_status") == "legacy_unknown" or compatibility.get("status") not in {None, "compatible", "verified"}
            if wants_vector and bad:
                message = f"{variant['variant_id']}: semantic index is unavailable or legacy_unknown; this is not a normal hybrid comparison"
                if plan.get("allow_degraded"):
                    warnings.append(message)
                    context.setdefault("degraded_variants", {}).setdefault(variant["variant_id"], []).append(message)
                else:
                    errors.append(message)

    async def _validate_input(self, row, plan, case, variant, trial, dataset_hash):
        if any((row.get("kind") != plan["kind"], row.get("protocol") != plan["protocol"],
                row.get("case_id") != case["case_id"], row.get("trial") != trial,
                row.get("dataset_hash") != dataset_hash)):
            raise ValueError("artifact kind/protocol/case/trial/dataset identity does not match")
        expected = await self._resolved_variant(plan["protocol"], variant, row.get("context", {}), case)
        if row.get("config_hash") != fingerprint(expected):
            raise ValueError("artifact configuration identity does not match")
        if row.get("content_hash") != fingerprint(row.get("artifact", {})):
            raise ValueError("artifact integrity check failed")
        if row.get("context_hash") != _identity(row.get("context", {})):
            raise ValueError("artifact context integrity check failed")
        if not row.get("verified_identity", False):
            raise ValueError("imported artifact configuration is unverified; use an attested live or validated import")
        if not row.get("complete", True):
            raise ValueError("partial-stage artifact cannot replace a completed experiment input")

    async def register_input(self, *, plan, case_id, variant_id, trial=1, artifact, provenance="imported", trusted=False, complete=True, experiment_id=None):
        plan = self._plan(plan)
        dataset, cases = self._cases(plan)
        case = next((row for row in cases if row["case_id"] == case_id), None)
        variant = next((row for row in plan["variants"] if row["variant_id"] == variant_id), None)
        if case is None or variant is None or not 1 <= trial <= plan.get("repetitions", 1):
            raise ValueError("artifact cell does not exist in selected experiment matrix")
        if not isinstance(artifact, dict) or "content" not in artifact:
            raise ValueError("a complete structured domain artifact with content is required")
        if any(key in artifact for key in ("path", "file_path", "artifact_path")):
            raise ValueError("local file paths are not accepted as artifacts")
        validator = getattr(self.executor, "validate_artifact", None)
        verified = trusted
        if not trusted and validator is not None:
            verified = bool(await _await(validator(plan["protocol"], case, variant, artifact)))
        context = deepcopy(artifact.get("context", {}))
        context.setdefault("dataset_hash", fingerprint(dataset))
        config = await self._resolved_variant(plan["protocol"], variant, context, case)
        if artifact.get("config_identity") is not None and artifact["config_identity"] != config:
            raise ValueError("artifact's observed configuration differs from resolved configuration")
        safe_artifact = redact_sensitive_data(deepcopy(artifact))
        value = {
            "input_id": new_id(), "created_at": now_iso(), "kind": plan["kind"], "protocol": plan["protocol"],
            "case_id": case_id, "variant_id": variant_id, "trial": trial,
            "dataset_hash": fingerprint(dataset), "config_hash": fingerprint(config),
            "config_identity": config, "context": context, "context_hash": _identity(context),
            "artifact": safe_artifact, "content_hash": fingerprint(safe_artifact),
            "provenance": provenance, "verified_identity": verified,
            "complete": complete, "experiment_id": experiment_id,
        }
        return await self.store.register_input(value)

    @staticmethod
    def input_metadata(row):
        return {key: deepcopy(value) for key, value in row.items() if key not in {"artifact", "context"}}

    async def list_inputs(self, kind):
        return [self.input_metadata(row) for row in await self.store.list_inputs(kind)]

    async def create(self, plan, kind=None, *, wait=False):
        plan = self._plan(plan, kind)
        if plan["mode"] == "live" and not plan["confirm_live"]:
            raise ValueError("live evaluation requires confirm_live=true")
        preflight = await self.preflight(plan)
        if not preflight["ready"]:
            raise ValueError("; ".join(preflight["errors"]))
        plan["case_ids"] = preflight["case_ids"]
        plan["input_refs"] = preflight["resolved_input_refs"]
        current = make_experiment(plan, dataset_hash=preflight["dataset_hash"], context=preflight["context"])
        current["summary"] = summarize([], preflight["expected_cells"], plan["variants"])
        await self.store.save(current)
        identifier = current["experiment_id"]
        task = asyncio.create_task(self._execute(current), name=f"evaluation-v2-{identifier}")
        self._tasks[identifier] = task
        task.add_done_callback(lambda _: self._tasks.pop(identifier, None))
        if wait:
            await task
            return await self.store.get(identifier)
        return current

    async def _patch(self, identifier, **updates):
        async with self._write_lock:
            current = await self.store.get(identifier)
            current.update(updates)
            await self.store.save(current)

    def _fill_missing(self, current, status, reason):
        # The immutable plan survives dataset revisions/removal across restarts.
        # Startup recovery must not reinterpret old labels through today's registry.
        cases = [{"case_id": case_id} for case_id in current["plan"].get("case_ids", [])]
        keys = {cell_key(row["case_id"], row["variant_id"], row["trial"]) for row in current["results"]}
        for case in cases:
            for variant in current["plan"]["variants"]:
                for trial in range(1, current["plan"].get("repetitions", 1) + 1):
                    if cell_key(case["case_id"], variant["variant_id"], trial) not in keys:
                        current["results"].append(CaseResult(case_id=case["case_id"], variant_id=variant["variant_id"], trial=trial, status=status, error=reason).model_dump(mode="json"))
        current["summary"] = summarize(current["results"], len(cases) * len(current["plan"]["variants"]) * current["plan"].get("repetitions", 1), current["plan"]["variants"])

    async def _execute(self, experiment):
        plan = experiment["plan"]
        _, cases = self._cases(plan)
        identifier = experiment["experiment_id"]
        expected = len(cases) * len(plan["variants"]) * plan.get("repetitions", 1)

        async def persist(ledger):
            await self._patch(identifier, budget_ledger=ledger)

        budget = EvaluationBudget(EvaluationBudgetSpec.model_validate(plan["budget"]), on_reserve=persist)
        self._budgets[identifier] = budget
        semaphore = asyncio.Semaphore(plan["budget"]["concurrency"])
        await self._patch(identifier, status="running")

        async def record(row):
            async with self._write_lock:
                current = await self.store.get(identifier)
                current["results"].append(row)
                current["summary"] = summarize(current["results"], expected, plan["variants"])
                current["budget_ledger"] = budget.ledger()
                await self.store.save(current)

        async def one(case, variant, trial):
            async with semaphore:
                started = perf_counter()
                calls = []
                row = CaseResult(case_id=case["case_id"], variant_id=variant["variant_id"], trial=trial, status="completed").model_dump(mode="json")
                if plan["kind"] == "rag" or case.get("requires_knowledge") or variant.get("policy", {}).get("knowledge_mode", "off") != "off":
                    row["degraded"] = list(experiment["context"].get("degraded_variants", {}).get(variant["variant_id"], []))
                stage = "execution"
                last_frozen = None

                async def freeze(artifact, *, complete):
                    artifact = deepcopy(artifact)
                    artifact["context"] = deepcopy(experiment["context"])
                    return await self.register_input(plan=plan, case_id=case["case_id"], variant_id=variant["variant_id"], trial=trial, artifact=artifact, provenance="live", trusted=True, complete=complete, experiment_id=identifier)

                async def stage_sink(artifact):
                    nonlocal last_frozen, stage
                    last_frozen = await freeze(as_dict(artifact), complete=False)
                    row["input_id"] = last_frozen["input_id"]
                    row["stage_status"].update(last_frozen["artifact"].get("stage_status", {}))
                    running = [name for name, status in row["stage_status"].items() if status == "running"]
                    if running:
                        stage = running[-1]

                try:
                    if budget.stop_reason:
                        raise EvaluationBudgetExceeded(budget.stop_reason)
                    if plan["mode"] == "replay":
                        frozen = await self.store.get_input(plan["input_refs"][cell_key(case["case_id"], variant["variant_id"], trial)])
                        row["stage_status"]["execution"] = "replayed"
                    else:
                        with provider_capture() as calls:
                            async with asyncio.timeout(min(plan["budget"]["case_timeout_seconds"], budget.remaining_seconds)):
                                parameters = inspect.signature(self.executor).parameters
                                kwargs = {"stage_sink": stage_sink} if "stage_sink" in parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()) else {}
                                artifact = as_dict(await self.executor(plan["protocol"], case, variant, experiment["context"], trial, **kwargs))
                        frozen = await freeze(artifact, complete=True)
                        last_frozen = frozen
                        row["stage_status"].update(frozen["artifact"].get("stage_status", {}))
                        for name in row["stage_status"]:
                            if row["stage_status"][name] == "running":
                                row["stage_status"][name] = "completed"
                        row["stage_status"]["execution"] = "completed"
                    row["input_id"] = frozen["input_id"]
                    stage = "scoring"
                    scored = as_dict(await _await(self.evaluator(plan["protocol"], case, frozen["artifact"], plan)))
                    self._merge_scores(row, scored)
                    row["stage_status"]["scoring"] = "completed"
                    row["degraded"] = list(dict.fromkeys([*row["degraded"], *frozen["artifact"].get("degraded", [])]))
                    if row["degraded"]:
                        # Preserve diagnostic values, but exclude them from normal quality means.
                        for metric in row["metrics"].values():
                            if metric["status"] == "evaluated":
                                metric["status"] = "degraded"
                    if plan.get("judge_enabled"):
                        stage = "judge"
                        await self._judge_one(row, plan, case, frozen["artifact"], budget, calls)
                except EvaluationBudgetExceeded as exc:
                    budget.stop(str(exc))
                    row["status"] = "budget_stopped"
                    row["error"] = str(exc)
                    row["stage_status"][stage] = "budget_stopped"
                except TimeoutError:
                    row["status"] = "timed_out"
                    row["error"] = "case_timeout"
                    row["stage_status"][stage] = "timed_out"
                except asyncio.CancelledError:
                    row["status"] = "not_run"
                    row["error"] = budget.stop_reason or "execution_interrupted"
                    row["stage_status"][stage] = "cancelled"
                    raise
                except Exception as exc:  # noqa: BLE001 - preserve every matrix cell and its denominator
                    row["status"] = "failed"
                    row["error"] = _error(exc)
                    row["stage_status"][stage] = "failed"
                finally:
                    if last_frozen is not None and row["status"] in {"failed", "timed_out", "budget_stopped", "not_run"} and not row["metrics"]:
                        # Retrieval completed before answer generation failed: retain its
                        # immutable evidence and rules without upgrading execution status.
                        state, stage_states = row["status"], dict(row["stage_status"])
                        try:
                            scored = as_dict(await _await(self.evaluator(plan["protocol"], case, last_frozen["artifact"], plan)))
                            self._merge_scores(row, scored)
                            row["stage_status"]["scoring"] = "completed"
                        except Exception:  # noqa: BLE001 - do not mask the original stage failure
                            row["stage_status"]["scoring"] = "failed"
                        row["status"] = state
                        row["stage_status"].update(stage_states)
                    row["degraded"] = list(dict.fromkeys(row["degraded"]))
                    if row["degraded"]:
                        for metric in row["metrics"].values():
                            if metric["status"] == "evaluated":
                                metric["status"] = "degraded"
                    row["elapsed_ms"] = int((perf_counter() - started) * 1000)
                    captured = [redact_sensitive_data(as_dict(call)) for call in calls]
                    if plan["mode"] == "live" and plan["kind"] == "agent" and last_frozen is not None:
                        captured.extend(call for segment in last_frozen["artifact"].get("content", {}).get("trace_segments", []) for call in segment.get("provider_calls", []))
                    row["provider_calls"] = list({call.get("call_id") or str(index): call for index, call in enumerate(captured)}.values())
                    await record(row)

        children = []
        try:
            with private_evaluation_tracing(), evaluation_budget_context(budget):
                children = [asyncio.create_task(one(case, variant, trial)) for case in cases for variant in plan["variants"] for trial in range(1, plan.get("repetitions", 1) + 1)]
                async with asyncio.timeout(plan["budget"]["max_elapsed_seconds"]):
                    await asyncio.gather(*children)
        except (asyncio.CancelledError, TimeoutError) as exc:
            budget.stop("cancelled" if isinstance(exc, asyncio.CancelledError) else "elapsed_budget_exhausted")
            for task in children:
                task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
        finally:
            current = await self.store.get(identifier)
            self._fill_missing(current, "not_run", budget.stop_reason or "execution_interrupted")
            counts = Counter(row["status"] for row in current["results"])
            current["status"] = "cancelled" if budget.stop_reason == "cancelled" else "partial" if budget.stop_reason or counts["not_run"] else "failed" if counts["failed"] or counts["timed_out"] else "completed"
            current.update(ended_at=now_iso(), budget_ledger=budget.ledger(), stop_reason=budget.stop_reason)
            await self.store.save(current)
            if plan.get("langsmith_sync"):
                status = await self.langsmith_adapter.sync(current)
                await self._patch(identifier, sync_status=status)
            self._budgets.pop(identifier, None)

    @staticmethod
    def _merge_scores(row, scored):
        if "metrics" not in scored:
            scored = {"metrics": scored}
        metrics = {
            name: MetricResult.model_validate(as_dict(metric)).model_dump(mode="json")
            for name, metric in scored.get("metrics", {}).items()
        }
        row["metrics"].update(metrics)
        row["checks"].extend(scored.get("checks", []))
        row["stage_status"].update(scored.get("stage_status", {}))
        row["diagnostics"].update(scored.get("diagnostics", {}))
        row["degraded"].extend(scored.get("degraded", []))
        if scored.get("status"):
            row["status"] = scored["status"]

    async def _judge_one(self, row, plan, case, artifact, budget, calls):
        judge_calls = []
        try:
            with provider_capture() as judge_calls:
                async with asyncio.timeout(min(plan["budget"]["case_timeout_seconds"], budget.remaining_seconds)):
                    scores = as_dict(await self.judge(plan["protocol"], case, artifact))
            self._merge_scores(row, scores)
            row["stage_status"]["judge"] = "completed"
        except EvaluationBudgetExceeded as exc:
            budget.stop(str(exc))
            row["stage_status"]["judge"] = "budget_stopped"
            row["checks"].append({"check_id": "judge", "status": "budget_stopped", "reason": str(exc)})
        except (TimeoutError, Exception) as exc:  # noqa: BLE001 - Judge is a separate optional stage
            row["stage_status"]["judge"] = "grader_failed"
            row["checks"].append({"check_id": "judge", "status": "grader_failed", "reason": _error(exc) or "judge_timeout"})
        finally:
            calls.extend(judge_calls)

    async def cancel(self, experiment_id, kind=None):
        current = await self.store.get(experiment_id, kind)
        if current["status"] not in {"planned", "running"}:
            raise ValueError("only active experiments can be cancelled")
        budget = self._budgets.get(experiment_id)
        if budget:
            budget.stop("cancelled")
        task = self._tasks.get(experiment_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        current = await self.store.get(experiment_id, kind)
        if current["status"] in {"planned", "running"}:
            self._fill_missing(current, "not_run", "cancelled")
            current.update(status="cancelled", ended_at=now_iso(), stop_reason="cancelled")
            await self.store.save(current)
        return current

    async def delete(self, experiment_id, kind=None):
        # Check workspace ownership before reporting the task's internal state.
        await self.store.get(experiment_id, kind, include_deleted=True)
        task = self._tasks.get(experiment_id)
        if task is not None and not task.done():
            raise ValueError("experiment is still executing or finalizing; cancel it or wait before deletion")
        return await self.store.delete(experiment_id, kind, now_iso())

    async def artifact(self, experiment_id, input_id, kind=None):
        current = await self.store.get(experiment_id, kind)
        frozen = await self.store.get_input(input_id)
        if input_id not in {row.get("input_id") for row in current["results"]} and frozen.get("experiment_id") != experiment_id:
            raise KeyError("artifact does not belong to this experiment")
        # Keep ownership metadata around the complete, sanitized domain artifact.
        return {**self.input_metadata(frozen), **redact_sensitive_data(frozen["artifact"])}

    async def compare_variants(self, experiment_id, baseline_variant_id=None, candidate_variant_id=None, kind=None):
        """Within-experiment paired effects, never differences of unrelated means."""
        experiment = await self.store.get(experiment_id, kind)
        plan = experiment["plan"]
        variants = {row["variant_id"]: row for row in plan["variants"]}
        if not variants:
            raise ValueError("experiment has no variants")
        if baseline_variant_id is None:
            baseline_variant_id = "baseline" if "baseline" in variants else next(iter(variants))
        if baseline_variant_id not in variants:
            raise ValueError("baseline variant does not belong to this experiment")
        if candidate_variant_id is not None and (
            candidate_variant_id not in variants or candidate_variant_id == baseline_variant_id
        ):
            raise ValueError("candidate must be another variant in this experiment")
        candidate_ids = [candidate_variant_id] if candidate_variant_id else [key for key in variants if key != baseline_variant_id]
        case_ids = plan.get("case_ids") or list(dict.fromkeys(row["case_id"] for row in experiment["results"]))
        planned_cells = [(case_id, trial) for case_id in case_ids for trial in range(1, plan.get("repetitions", 1) + 1)]
        allowed_cells = set(planned_cells)
        rows = {}
        for row in experiment["results"]:
            position = (row["case_id"], row["trial"])
            key = (row["variant_id"], *position)
            if key in rows:
                raise ValueError("duplicate case/variant/trial cells cannot be paired reliably")
            if position not in allowed_cells or row["variant_id"] not in variants:
                raise ValueError("stored result is outside the immutable experiment matrix")
            rows[key] = row
        comparisons = []
        for candidate_id in candidate_ids:
            selected = [row for key, row in rows.items() if key[0] in {baseline_variant_id, candidate_id}]
            names = sorted({name for row in selected for name in row.get("metrics", {})})
            contracts = {name: {} for name in names}
            pairs = []
            for case_id, trial in planned_cells:
                baseline = rows.get((baseline_variant_id, case_id, trial))
                candidate = rows.get((candidate_id, case_id, trial))
                metrics = {}
                for name in names:
                    metrics[name] = _variant_metric_pair(baseline, candidate, name)
                    if metrics[name]["status"] == "paired":
                        contract = {key: baseline["metrics"][name].get(key) for key in _METRIC_CONTRACT}
                        contracts[name][fingerprint(contract)] = contract
                pairs.append({
                    "case_id": case_id, "trial": trial,
                    "baseline_status": baseline.get("status") if baseline else "missing",
                    "candidate_status": candidate.get("status") if candidate else "missing",
                    "metrics": metrics,
                })
            aggregates = {}
            for name in names:
                # Do not pool heterogeneous graders/units even if each individual
                # cell happened to use the same grader on its two sides.
                if len(contracts[name]) > 1:
                    for pair in pairs:
                        item = pair["metrics"][name]
                        if item["status"] == "paired":
                            item.update(status="excluded", delta=None, outcome=None,
                                        reason="metric_contract_changes_across_pairs",
                                        reasons=["metric_contract_changes_across_pairs"])
                items = [pair["metrics"][name] for pair in pairs]
                valid = [item for item in items if item["status"] == "paired"]
                exclusions = Counter(reason for item in items if item["status"] == "excluded" for reason in item["reasons"])
                template = next((row["metrics"][name] for row in selected if name in row.get("metrics", {})), {})
                count = len(valid)
                outcomes = Counter(item["outcome"] for item in valid)
                aggregates[name] = {
                    "baseline_mean": math.fsum(float(item["baseline"]) / count for item in valid) if count else None,
                    "candidate_mean": math.fsum(float(item["candidate"]) / count for item in valid) if count else None,
                    "mean_delta": math.fsum(item["delta"] / count for item in valid) if count else None,
                    "paired": count, "planned": len(planned_cells),
                    "not_comparable": len(planned_cells) - count,
                    "excluded_reasons": dict(exclusions),
                    "improved": outcomes["improved"], "tied": outcomes["tied"],
                    "regressed": outcomes["regressed"], "unclassified": outcomes["unclassified"],
                    "unit": template.get("unit"), "direction": template.get("direction"),
                    "scoring_unit": template.get("scoring_unit"),
                }
            comparisons.append({
                "candidate_variant_id": candidate_id, "candidate_name": variants[candidate_id]["name"],
                "planned_pairs": len(planned_cells), "metrics": aggregates, "pairs": pairs,
            })
        return {
            "experiment_id": experiment_id, "kind": experiment["kind"], "protocol": experiment["protocol"],
            "status": experiment["status"], "comparison_focus": plan.get("comparison_focus", "general"),
            "baseline_variant_id": baseline_variant_id, "baseline_name": variants[baseline_variant_id]["name"],
            "planned_pairs": len(planned_cells), "comparisons": comparisons,
            "limitations": [
                "Only identical case/trial cells with completed, non-degraded, finite evaluated metrics are paired.",
                "Conditional paired means exclude failed/unavailable cells; coverage is not end-to-end success rate.",
                "Delta is candidate minus baseline; lower-is-better and neutral metrics are interpreted separately.",
                "Small-sample diagnostics, not statistical significance or a cross-domain overall score.",
            ],
        }

    async def compare(self, baseline_id, candidate_id, scope="configuration", kind=None):
        baseline = await self.store.get(baseline_id, kind)
        candidate = await self.store.get(candidate_id, kind)
        if baseline["kind"] != candidate["kind"] or baseline["protocol"] != candidate["protocol"]:
            raise ValueError("cross-kind or cross-protocol comparison is not allowed")
        if baseline["dataset_hash"] != candidate["dataset_hash"]:
            raise ValueError("dataset/gold/scoring identity mismatch")
        if scope not in {"configuration", "retrieval", "index", "agent"}:
            raise ValueError("unsupported comparison scope")
        if scope == "index" and baseline["kind"] != "rag":
            raise ValueError("index comparison is only available for RAG")
        if baseline["plan"].get("judge_enabled") != candidate["plan"].get("judge_enabled"):
            raise ValueError("Judge evaluation protocols differ")
        if _identity(baseline["context"], index=scope != "index") != _identity(candidate["context"], index=scope != "index"):
            raise ValueError("corpus/environment/Judge/index identities differ for selected comparison scope")
        if baseline["plan"].get("repetitions", 1) != candidate["plan"].get("repetitions", 1):
            raise ValueError("trial counts differ")
        if baseline["plan"].get("cutoffs") != candidate["plan"].get("cutoffs"):
            raise ValueError("retrieval scoring cutoffs differ")
        if set(baseline["plan"]["case_ids"]) != set(candidate["plan"]["case_ids"]):
            raise ValueError("paired comparison requires the same case selection")
        left = {cell_key(row["case_id"], row["variant_id"], row["trial"]): row for row in baseline["results"]}
        right = {cell_key(row["case_id"], row["variant_id"], row["trial"]): row for row in candidate["results"]}
        if set(left) != set(right):
            raise ValueError("paired comparison requires matching case/variant/trial cells")
        pairs = []
        for key, row in left.items():
            other = right[key]
            if scope == "agent" and any(item["plan"].get("comparison_focus") == "tools" for item in (baseline, candidate)):
                if not row.get("input_id") or not other.get("input_id"):
                    raise ValueError("tools comparison requires attested configuration for each paired artifact")
                a_input = await self.store.get_input(row["input_id"])
                b_input = await self.store.get_input(other["input_id"])
                if not all(item.get("verified_identity") and item.get("config_identity") for item in (a_input, b_input)):
                    raise ValueError("tools comparison requires attested configuration for each paired artifact")
                configs = [{field: value for field, value in item["config_identity"].items() if field != "tool_environment"} for item in (a_input, b_input)]
                if configs[0] != configs[1]:
                    raise ValueError("tools comparison must hold the actual model, policy and workflow fixed")
            if baseline["protocol"] == "rag-answer-v2" and scope in {"retrieval", "index"} and row.get("input_id") and other.get("input_id"):
                a_input = await self.store.get_input(row["input_id"])
                b_input = await self.store.get_input(other["input_id"])
                if a_input.get("config_identity", {}).get("generator") != b_input.get("config_identity", {}).get("generator"):
                    raise ValueError("retrieval/index comparison must hold the actual answer generator fixed")
            metrics = {}
            for name in sorted(set(row["metrics"]) | set(other["metrics"])):
                a, b = row["metrics"].get(name, {}), other["metrics"].get(name, {})
                if a and b and any(a.get(field) != b.get(field) for field in ("unit", "scoring_unit", "grader_id", "grader_version")):
                    raise ValueError("metric grader or scoring-unit versions differ")
                valid = a.get("status") == b.get("status") == "evaluated" and a.get("value") is not None and b.get("value") is not None
                metrics[name] = {"baseline": a.get("value"), "candidate": b.get("value"), "delta": float(b["value"]) - float(a["value"]) if valid else None, "status": "paired" if valid else "not_comparable"}
            pairs.append({"case_id": row["case_id"], "variant_id": row["variant_id"], "trial": row["trial"], "baseline_status": row["status"], "candidate_status": other["status"], "metrics": metrics})
        return {"baseline_id": baseline_id, "candidate_id": candidate_id, "scope": scope, "baseline": baseline["summary"], "candidate": candidate["summary"], "pairs": pairs, "limitations": ["Small-sample paired diagnostics; no statistical significance claim."]}

    async def export(self, experiment_id, format="json", kind=None):
        current = await self.store.get(experiment_id, kind)
        if format == "json":
            return json.dumps(current, ensure_ascii=False, indent=2)
        if format != "csv":
            raise ValueError("unsupported export format")
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["experiment_id", "kind", "protocol", "case_id", "variant_id", "trial", "status", "metric", "value", "metric_status", "unit", "scoring_unit", "reason"])
        for row in current["results"]:
            for name, value in (row["metrics"] or {"": {}}).items():
                writer.writerow([experiment_id, current["kind"], current["protocol"], row["case_id"], row["variant_id"], row["trial"], row["status"], name, value.get("value"), value.get("status"), value.get("unit"), value.get("scoring_unit"), value.get("reason") or row.get("error")])
        return stream.getvalue()
