from __future__ import annotations

import asyncio
import csv
import io
import json
from time import perf_counter

from atlasflow.evaluation.budget import (
    EvaluationBudget,
    EvaluationBudgetExceeded,
    evaluation_budget_context,
)
from atlasflow.evaluation.contracts import (
    EvaluationBudgetSpec,
    EvaluationCaseResult,
    EvaluationExperiment,
    EvaluationPlan,
    FrozenEvaluationInput,
    arm_fingerprint,
    default_arms,
    fingerprint,
    utc_now,
)
from atlasflow.evaluation.dataset import DatasetRegistry
from atlasflow.evaluation.langsmith_adapter import (
    MetadataLangSmithAdapter,
    private_evaluation_tracing,
    provider_capture,
)
from atlasflow.evaluation.metrics import evaluate_input, summarize
from atlasflow.evaluation.store import EvaluationStore
from atlasflow.observability import redact_sensitive_data


def _dump(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _index_binding(snapshot):
    return {
        key: snapshot.get(key)
        for key in ("space_id", "generation_id", "documents", "manifest_hash")
    }


def _index_identity(snapshot):
    return fingerprint(_index_binding(snapshot))


class EvaluationService:
    def __init__(
        self,
        store: EvaluationStore,
        datasets: DatasetRegistry,
        *,
        snapshot_resolver=None,
        retrieval_executor=None,
        agent_executor=None,
        run_reader=None,
        judge=None,
        langsmith_adapter=None,
        default_budget=None,
    ):
        self.store = store
        self.datasets = datasets
        self.snapshot_resolver = snapshot_resolver
        self.retrieval_executor = retrieval_executor
        self.agent_executor = agent_executor
        self.run_reader = run_reader
        self.judge = judge
        self.langsmith_adapter = langsmith_adapter or MetadataLangSmithAdapter()
        self.default_budget = default_budget or EvaluationBudgetSpec()
        self._tasks: dict[str, asyncio.Task] = {}
        self._budgets: dict[str, EvaluationBudget] = {}
        self._result_lock = asyncio.Lock()

    async def initialize(self, *, recover_interrupted=False):
        await self.store.initialize()
        if not recover_interrupted:
            return
        for experiment in await self.store.list(200):
            if experiment.status in {"running", "planned"}:
                results = list(experiment.results)
                try:
                    _, cases = self._cases(experiment.plan)
                    keys = {(row.case_id, row.arm_id) for row in results}
                    results.extend(
                        EvaluationCaseResult(
                            case_id=case.case_id,
                            arm_id=arm.arm_id,
                            kind=case.kind,
                            status="budget_stopped",
                            error="process_interrupted",
                            replayed=experiment.plan.mode == "replay",
                        )
                        for case in cases
                        for arm in experiment.plan.arms
                        if (case.case_id, arm.arm_id) not in keys
                    )
                    summary = summarize(results, len(cases) * len(experiment.plan.arms))
                except (KeyError, ValueError):
                    summary = experiment.summary
                await self.store.save(
                    experiment.model_copy(
                        update={
                            "status": "partial",
                            "ended_at": utc_now(),
                            "stop_reason": "process_interrupted",
                            "results": results,
                            "summary": summary,
                        }
                    )
                )

    async def close(self):
        for budget in self._budgets.values():
            budget.stop("service_shutdown")
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.close()

    def _cases(self, plan):
        dataset = self.datasets.get(plan.dataset_id)
        ids = plan.case_ids or [case.case_id for case in dataset.cases[:10]]
        by_id = {case.case_id: case for case in dataset.cases}
        if any(case_id not in by_id for case_id in ids):
            raise ValueError("unknown case id")
        return dataset, [by_id[case_id] for case_id in ids]

    async def preflight(self, plan: EvaluationPlan) -> dict:
        dataset, cases = self._cases(plan)
        dataset_hash = fingerprint(dataset)
        errors = self.datasets.validate_local_sources(dataset)
        warnings = []
        snapshot = {}
        available_inputs = await self.store.list_inputs()
        resolved_refs = dict(plan.input_refs)
        estimate = 0
        if plan.judge_enabled and self.judge is None:
            errors.append("independent Judge is not configured")
        if plan.mode == "live":
            if self.snapshot_resolver is None:
                errors.append("read-only snapshot resolver is not configured")
            else:
                try:
                    value = _dump(
                        await self.snapshot_resolver(
                            plan.space or dataset.space, plan.space_version_id
                        )
                    )
                    snapshot = _dump(value.get("snapshot")) if "snapshot" in value else value
                    for key in ("source_id_map", "source_hash_map", "evaluation_context"):
                        if key in value:
                            snapshot[key] = value[key]
                    hashes = snapshot.get("source_hash_map", {})
                    source_map = snapshot.get("source_id_map", {})
                    documents = snapshot.get("documents", {})
                    if "documents" in snapshot:
                        published_sources = {
                            source_map.get(doc_id)
                            for doc_id, state in documents.items()
                            if state.get("status") == "active" and state.get("version_id")
                        }
                        expected_sources = {
                            gold.source_id for case in cases for gold in case.expected_sources
                        }
                        for source in sorted(expected_sources - published_sources):
                            errors.append(
                                f"gold source not published in selected snapshot: {source}"
                            )
                    for doc_id, state in documents.items():
                        source = source_map.get(doc_id)
                        digest = hashes.get(state.get("version_id"))
                        if (
                            source in dataset.source_content_hashes
                            and digest != dataset.source_content_hashes[source]
                        ):
                            errors.append(f"snapshot source hash mismatch: {source}")
                except Exception as exc:  # noqa: BLE001 - preflight reports adapter failures without calls
                    errors.append(f"snapshot unavailable: {type(exc).__name__}")
            for case in cases:
                executor = self.retrieval_executor if case.kind == "rag" else self.agent_executor
                if executor is None:
                    errors.append(f"{case.kind} live executor is not configured")
                for arm in plan.arms:
                    try:
                        arm_fingerprint(
                            arm, snapshot.get("evaluation_context", {}).get("retrieval_profile")
                        )
                    except ValueError as exc:
                        errors.append(f"invalid resolved configuration {arm.arm_id}: {exc}")
                    if case.kind == "agent":
                        validator = getattr(self.agent_executor, "validate_case", None)
                        if validator:
                            reason = validator(case)
                            if reason:
                                errors.append(str(reason))
                        estimate = None
                        break
                    if estimate is not None:
                        estimate += int(arm.tuning.get("vector_weight", 1) > 0)
                        estimate += int(arm.tuning.get("rerank_enabled", True))
            warnings.append(
                "HTTP cap includes retries and tools; it does not guarantee the entire batch completes"
            )
            if snapshot.get("identity_status") == "legacy_unknown":
                warnings.append(
                    "legacy index identity is unknown; hybrid retrieval will degrade to lexical"
                )
                if any(arm.tuning.get("lexical_weight", 1) == 0 for arm in plan.arms):
                    errors.append("pure semantic evaluation requires a verified index identity")
            compatibility = snapshot.get("evaluation_context", {}).get(
                "embedding_compatibility", {}
            )
            if compatibility.get("status") not in {None, "compatible"}:
                for arm in plan.arms:
                    if arm.tuning.get("vector_weight", 1) <= 0:
                        continue
                    if arm.tuning.get("lexical_weight", 1) == 0:
                        errors.append(
                            f"pure semantic evaluation cannot use index: {compatibility['status']}"
                        )
                    else:
                        warnings.append(
                            f"hybrid arm {arm.arm_id} will degrade to lexical: {compatibility['status']}"
                        )
        else:
            # Prefer a coherent saved experiment snapshot, never arbitrary latest
            # per-case results from unrelated corpus/config generations.
            eligible = {}
            for case in cases:
                for arm in plan.arms:
                    key = f"{case.case_id}:{arm.arm_id}"
                    candidates = []
                    for frozen in available_inputs:
                        try:
                            self._validate_input(frozen, case, arm, dataset_hash)
                        except ValueError:
                            continue
                        candidates.append(frozen)
                    eligible[key] = candidates
            fixed = []
            for input_id in resolved_refs.values():
                try:
                    fixed.append(await self.store.get_input(input_id))
                except KeyError:
                    pass
            identities = (
                [_index_identity(fixed[0].snapshot)]
                if fixed
                else list(dict.fromkeys(_index_identity(row.snapshot) for row in available_inputs))
            )
            for identity in identities:
                if all(
                    key in resolved_refs
                    or any(_index_identity(row.snapshot) == identity for row in rows)
                    for key, rows in eligible.items()
                ):
                    for key, rows in eligible.items():
                        if key not in resolved_refs:
                            resolved_refs[key] = next(
                                row.input_id
                                for row in rows
                                if _index_identity(row.snapshot) == identity
                            )
                    break
            for case in cases:
                for arm in plan.arms:
                    key = f"{case.case_id}:{arm.arm_id}"
                    input_id = resolved_refs.get(key)
                    if not input_id:
                        errors.append(f"frozen input missing: {key}")
                        continue
                    try:
                        frozen = await self.store.get_input(input_id)
                        self._validate_input(frozen, case, arm, dataset_hash)
                        if not snapshot:
                            snapshot = frozen.snapshot
                        elif _index_identity(snapshot) != _index_identity(frozen.snapshot):
                            errors.append("replay inputs do not share the same frozen snapshot")
                    except (KeyError, ValueError) as exc:
                        errors.append(f"invalid frozen input {key}: {exc}")
        if any(case.gold_status == "draft" for case in cases):
            warnings.append(
                "draft labels are excluded from effect scores, never presented as verified gold"
            )
        if plan.judge_enabled:
            warnings.append(
                "Judge sends selected case, evidence and report to its explicitly configured provider"
            )
        comparison_key = fingerprint(
            {
                "dataset_hash": dataset_hash,
                "case_ids": [case.case_id for case in cases],
                "corpus": dataset.corpus_manifest_hash,
                "scoring": dataset.scoring_version,
            }
        )
        return {
            "ready": not errors,
            "errors": list(dict.fromkeys(errors)),
            "warnings": warnings,
            "selected_cases": len(cases),
            "case_ids": [case.case_id for case in cases],
            "arms": [_dump(arm) for arm in plan.arms],
            "estimated_provider_requests": estimate,
            "snapshot": snapshot,
            "dataset_hash": dataset_hash,
            "comparison_key": comparison_key,
            "resolved_input_refs": resolved_refs,
            "budget": _dump(plan.budget),
            "available_inputs": [self.input_metadata(row) for row in available_inputs],
            "defaults": {
                "mode": "replay",
                "arms": [_dump(arm) for arm in default_arms()],
                "budget": _dump(self.default_budget),
            },
        }

    def settings(self):
        return {
            "budget": _dump(self.default_budget),
            "judge_available": self.judge is not None,
            "langsmith_available": self.langsmith_adapter.client is not None,
            "default_mode": "replay",
            "langsmith_content_mode": "metadata_only",
        }

    @staticmethod
    def _validate_input(frozen, case, arm, dataset_hash):
        if (
            frozen.case_id != case.case_id
            or frozen.kind != case.kind
            or frozen.arm_id != arm.arm_id
        ):
            raise ValueError("input case/kind/arm does not match")
        resolved_hash = arm_fingerprint(
            arm, frozen.snapshot.get("evaluation_context", {}).get("retrieval_profile")
        )
        # Read the first pre-release raw-arm snapshots without fabricating an
        # observed configuration. New inputs always use resolved parameters.
        if frozen.dataset_hash != dataset_hash or frozen.config_hash not in {
            resolved_hash,
            fingerprint(arm),
        }:
            raise ValueError("input dataset/config hash mismatch")
        if frozen.content_hash != fingerprint(frozen.content):
            raise ValueError("input content hash mismatch")

    @staticmethod
    def input_metadata(row):
        return row.model_dump(mode="json", exclude={"content", "source_id_map", "source_hash_map"})

    async def register_input(
        self,
        *,
        dataset_id: str,
        case_id: str,
        arm,
        run_id=None,
        content=None,
        snapshot=None,
        source_id_map=None,
        source_hash_map=None,
        provenance=None,
    ):
        dataset = self.datasets.get(dataset_id)
        case = next((item for item in dataset.cases if item.case_id == case_id), None)
        if case is None:
            raise ValueError("unknown case id")
        if run_id:
            if case.kind != "agent" or self.run_reader is None:
                raise ValueError(
                    "Run import is available only for Agent cases with a configured reader"
                )
            content = _dump(await self.run_reader(run_id))
            provenance = "imported_run"
        if not content:
            raise ValueError("a complete SearchResult or RunRecord is required")
        content = redact_sensitive_data(_dump(content))
        if case.kind == "agent" and snapshot is None:
            snapshot = content.get("retrieval_snapshot") or {}
        if case.kind == "rag":
            from atlasflow.knowledge.domain import SearchResult

            parsed = SearchResult.model_validate(content)
        else:
            from atlasflow.schemas import RunRecord

            parsed = RunRecord.model_validate_json(json.dumps(content))
        if " ".join(parsed.query.split()) != " ".join(case.query.split()):
            raise ValueError("frozen result query does not match the evaluation case")
        value = FrozenEvaluationInput(
            case_id=case_id,
            arm_id=arm.arm_id,
            kind=case.kind,
            content=content,
            content_hash=fingerprint(content),
            dataset_hash=fingerprint(dataset),
            config_hash=arm_fingerprint(
                arm, (snapshot or {}).get("evaluation_context", {}).get("retrieval_profile")
            ),
            source_run_id=run_id,
            snapshot=snapshot or {},
            source_id_map=source_id_map or {},
            source_hash_map=source_hash_map or {},
            provenance=provenance or "imported_search",
        )
        return await self.store.register_input(value)

    async def create(self, plan: EvaluationPlan, *, wait=False) -> EvaluationExperiment:
        preflight = await self.preflight(plan)
        if not preflight["ready"]:
            raise ValueError("; ".join(preflight["errors"]))
        if plan.mode == "replay":
            plan = plan.model_copy(update={"input_refs": preflight["resolved_input_refs"]})
        plan = plan.model_copy(update={"case_ids": preflight["case_ids"]})
        experiment = EvaluationExperiment(
            status="planned",
            plan=plan,
            dataset_hash=preflight["dataset_hash"],
            comparison_key=preflight["comparison_key"],
            snapshot=preflight["snapshot"],
        )
        await self.store.save(experiment)
        task = asyncio.create_task(
            self._execute(experiment), name=f"evaluation-{experiment.experiment_id}"
        )
        self._tasks[experiment.experiment_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(experiment.experiment_id, None))
        if wait:
            await task
            return await self.store.get(experiment.experiment_id)
        return experiment

    async def _execute(self, experiment):
        plan = experiment.plan
        dataset, cases = self._cases(plan)

        async def save_reservation(ledger):
            async with self._result_lock:
                current = await self.store.get(experiment.experiment_id)
                await self.store.save(current.model_copy(update={"budget_ledger": ledger}))

        budget = EvaluationBudget(plan.budget, on_reserve=save_reservation)
        self._budgets[experiment.experiment_id] = budget
        experiment = experiment.model_copy(update={"status": "running"})
        await self.store.save(experiment)
        expected_count = len(cases) * len(plan.arms)
        semaphore = asyncio.Semaphore(plan.budget.concurrency)

        async def record(row):
            async with self._result_lock:
                current = await self.store.get(experiment.experiment_id)
                results = [*current.results, row]
                await self.store.save(
                    current.model_copy(
                        update={
                            "results": results,
                            "summary": summarize(results, expected_count),
                            "budget_ledger": budget.ledger(),
                        }
                    )
                )

        async def run_one(case, arm):
            async with semaphore:
                started = perf_counter()
                calls = []
                try:
                    if budget.stop_reason:
                        raise EvaluationBudgetExceeded(budget.stop_reason)
                    if plan.mode == "replay":
                        frozen = await self.store.get_input(
                            plan.input_refs[f"{case.case_id}:{arm.arm_id}"]
                        )
                    else:
                        executor = (
                            self.retrieval_executor if case.kind == "rag" else self.agent_executor
                        )
                        with provider_capture() as calls:
                            async with asyncio.timeout(
                                min(plan.budget.case_timeout_seconds, budget.remaining_seconds)
                            ):
                                args = (case, arm, experiment.snapshot)
                                if case.kind == "agent":
                                    args += (experiment.experiment_id,)
                                raw = _dump(await executor(*args))
                        envelope = (
                            raw if isinstance(raw, dict) and "result" in raw else {"result": raw}
                        )
                        frozen = await self.register_input(
                            dataset_id=dataset.dataset_id,
                            case_id=case.case_id,
                            arm=arm,
                            content=_dump(envelope["result"]),
                            snapshot=experiment.snapshot,
                            source_id_map=envelope.get(
                                "source_id_map", experiment.snapshot.get("source_id_map")
                            ),
                            source_hash_map=envelope.get(
                                "source_hash_map", experiment.snapshot.get("source_hash_map")
                            ),
                            provenance="live",
                        )
                    row = evaluate_input(case, frozen, replayed=plan.mode == "replay")
                    if plan.judge_enabled:
                        metrics = dict(row.metrics)
                        availability = dict(row.availability)
                        judge_calls = []
                        try:
                            with provider_capture() as judge_calls:
                                async with asyncio.timeout(
                                    min(plan.budget.case_timeout_seconds, budget.remaining_seconds)
                                ):
                                    scores = await self.judge(case, frozen)
                            for key in ("answer_correctness", "citation_support"):
                                score = scores.get(key)
                                if (
                                    isinstance(score, (int, float))
                                    and not isinstance(score, bool)
                                    and 0 <= score <= 1
                                ):
                                    metrics[key] = score
                                    availability[key] = "independent_judge"
                        except EvaluationBudgetExceeded as exc:
                            budget.stop(str(exc))
                            for key in ("answer_correctness", "citation_support"):
                                availability[key] = "budget_stopped"
                        except TimeoutError:
                            for key in ("answer_correctness", "citation_support"):
                                availability[key] = "grader_failed:case_timeout"
                        except Exception:  # noqa: BLE001 - Judge cannot erase retrieved evidence
                            for key in ("answer_correctness", "citation_support"):
                                availability[key] = "grader_failed"
                        finally:
                            calls.extend(judge_calls)
                        row = row.model_copy(
                            update={"metrics": metrics, "availability": availability}
                        )
                except EvaluationBudgetExceeded as exc:
                    budget.stop(str(exc))
                    row = EvaluationCaseResult(
                        case_id=case.case_id,
                        arm_id=arm.arm_id,
                        kind=case.kind,
                        status="budget_stopped",
                        error=str(exc),
                        replayed=plan.mode == "replay",
                    )
                except TimeoutError:
                    row = EvaluationCaseResult(
                        case_id=case.case_id,
                        arm_id=arm.arm_id,
                        kind=case.kind,
                        status="failed",
                        error="case_timeout",
                        replayed=plan.mode == "replay",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - preserve each failed case and its denominator
                    row = EvaluationCaseResult(
                        case_id=case.case_id,
                        arm_id=arm.arm_id,
                        kind=case.kind,
                        status="failed",
                        error=str(redact_sensitive_data(str(exc)))[:500],
                        replayed=plan.mode == "replay",
                    )
                    if case.kind == "rag" and case.answerable and case.gold_status == "verified":
                        row = row.model_copy(
                            update={
                                "metrics": {
                                    "source_recall_at_5": 0.0,
                                    "mrr": 0.0,
                                    "anchor_hit_rate": 0.0,
                                    "context_anchor_hit_rate": 0.0,
                                },
                                "availability": {
                                    key: "retrieval_failed"
                                    for key in (
                                        "source_recall_at_5",
                                        "mrr",
                                        "anchor_hit_rate",
                                        "context_anchor_hit_rate",
                                    )
                                },
                            }
                        )
                call_data = [_dump(call) for call in calls]
                if case.kind == "agent" and "frozen" in locals() and plan.mode == "live":
                    call_data.extend(
                        call
                        for segment in frozen.content.get("trace_segments", [])
                        for call in segment.get("provider_calls", [])
                    )
                unique_calls = {call.get("call_id", fingerprint(call)): call for call in call_data}
                await record(
                    row.model_copy(
                        update={
                            "elapsed_ms": int((perf_counter() - started) * 1000),
                            "provider_calls": list(unique_calls.values()),
                        }
                    )
                )

        children = []
        try:
            with private_evaluation_tracing(), evaluation_budget_context(budget):
                children = [
                    asyncio.create_task(run_one(case, arm)) for case in cases for arm in plan.arms
                ]
                async with asyncio.timeout(plan.budget.max_elapsed_seconds):
                    await asyncio.gather(*children)
        except (asyncio.CancelledError, TimeoutError) as exc:
            budget.stop(
                "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else "elapsed_budget_exhausted"
            )
            for task in children:
                task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
        finally:
            current = await self.store.get(experiment.experiment_id)
            keys = {(row.case_id, row.arm_id) for row in current.results}
            missing = [
                EvaluationCaseResult(
                    case_id=case.case_id,
                    arm_id=arm.arm_id,
                    kind=case.kind,
                    status="budget_stopped",
                    error=budget.stop_reason or "execution_interrupted",
                    replayed=plan.mode == "replay",
                )
                for case in cases
                for arm in plan.arms
                if (case.case_id, arm.arm_id) not in keys
            ]
            results = [*current.results, *missing]
            status = (
                "cancelled"
                if budget.stop_reason == "cancelled"
                else "partial"
                if budget.stop_reason or missing
                else "failed"
                if any(row.status == "failed" for row in results)
                else "completed"
            )
            final = current.model_copy(
                update={
                    "status": status,
                    "results": results,
                    "ended_at": utc_now(),
                    "summary": summarize(results, expected_count),
                    "budget_ledger": budget.ledger(),
                    "stop_reason": budget.stop_reason,
                }
            )
            await self.store.save(final)
            if plan.langsmith_sync:
                sync = await self.langsmith_adapter.sync(final)
                await self.store.save(final.model_copy(update={"sync_status": sync}))
            self._budgets.pop(experiment.experiment_id, None)

    async def cancel(self, experiment_id):
        current = await self.store.get(experiment_id)
        if current.status not in {"planned", "running"}:
            raise ValueError("only active evaluations can be cancelled")
        budget = self._budgets.get(experiment_id)
        if budget:
            budget.stop("cancelled")
        task = self._tasks.get(experiment_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        current = await self.store.get(experiment_id)
        if current.status in {"planned", "running"}:
            current = current.model_copy(
                update={"status": "cancelled", "ended_at": utc_now(), "stop_reason": "cancelled"}
            )
            await self.store.save(current)
        return current

    async def compare(self, baseline_id, candidate_id, scope="retrieval"):
        baseline = await self.store.get(baseline_id)
        candidate = await self.store.get(candidate_id)
        if baseline.comparison_key != candidate.comparison_key:
            raise ValueError("experiments have incompatible dataset/gold/corpus/scoring identities")

        same_index = _index_identity(baseline.snapshot) == _index_identity(candidate.snapshot)
        if scope not in {"retrieval", "index"}:
            raise ValueError("comparison scope must be retrieval or index")
        if scope == "retrieval" and not same_index:
            raise ValueError(
                "retrieval comparison requires the same index snapshot; select index scope explicitly"
            )
        return {
            "baseline_id": baseline_id,
            "candidate_id": candidate_id,
            "comparison_key": baseline.comparison_key,
            "baseline": baseline.summary,
            "candidate": candidate.summary,
            "scope": scope,
            "snapshots_differ": not same_index,
            "evaluation_context_differ": fingerprint(baseline.snapshot.get("evaluation_context"))
            != fingerprint(candidate.snapshot.get("evaluation_context")),
            "limitations": ["small-sample exploratory comparison; not statistical significance"],
        }

    async def export(self, experiment_id, format="json"):
        experiment = await self.store.get(experiment_id)
        if format == "json":
            return experiment.model_dump_json(indent=2)
        if format != "csv":
            raise ValueError("unsupported export format")
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(
            ["experiment_id", "case_id", "arm_id", "status", "replayed", "metrics", "stop_reason"]
        )
        for row in experiment.results:
            writer.writerow(
                [
                    experiment_id,
                    row.case_id,
                    row.arm_id,
                    row.status,
                    row.replayed,
                    json.dumps(row.metrics, ensure_ascii=False),
                    experiment.stop_reason or row.error or "",
                ]
            )
        return stream.getvalue()
