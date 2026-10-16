from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from atlasflow.evaluation.budget import reserve_http_attempt
from atlasflow.evaluation.common.api import create_v2_router
from atlasflow.evaluation.common.contracts import MetricResult
from atlasflow.evaluation.common.service import V2EvaluationService, V2MetadataLangSmithAdapter
from atlasflow.evaluation.common.store import V2EvaluationStore
from atlasflow.evaluation.contracts import fingerprint
from fastapi import FastAPI
from fastapi.testclient import TestClient


def dataset(kind="rag", count=1):
    return {
        "dataset_id": f"fixture-{kind}", "kind": kind, "scoring_version": f"{kind}-v2",
        "corpus_manifest_hash": "fixture-source-hash",
        "cases": [{"case_id": f"C{i}", "query": f"Question {i}", "gold_status": "verified"} for i in range(count)],
    }


def plan(kind="rag", **updates):
    value = {
        "kind": kind, "dataset_id": f"fixture-{kind}",
        "protocol": "rag-retrieval-v2" if kind == "rag" else "agent-task-v2",
        "mode": "live", "confirm_live": True,
        "variants": [{"variant_id": "base", "name": "Baseline"}],
        "budget": {"max_http_attempts": 4, "max_elapsed_seconds": 30, "case_timeout_seconds": 1, "concurrency": 1},
    }
    if kind == "rag":
        value["variants"][0]["retrieval_tuning"] = {"vector_weight": 0, "rerank_enabled": False}
    value.update(updates)
    return value


async def snapshot(*_):
    return {"snapshot": {"generation_id": "g1", "identity_status": "verified"}, "evaluation_context": {"embedding_compatibility": {"status": "compatible"}}}


async def execute(protocol, case, variant, context, trial):
    return {"content": {"query": case["query"], "report": "actual fixture report"}, "events": []}


def score(*_):
    return {"metrics": {"evidence": MetricResult(value=0.75, status="evaluated")}}


async def service(tmp_path, *, kind="rag", count=1, executor=execute, judge=None, store=None):
    value = V2EvaluationService(store or V2EvaluationStore(tmp_path / "eval.sqlite3"), datasets=[dataset(kind, count)], snapshot_resolver=snapshot, executor=executor, evaluator=score, judge=judge)
    await value.initialize()
    return value


async def test_public_dataset_hash_matches_experiment_labels_and_does_not_mutate_catalog(tmp_path):
    runtime = await service(tmp_path)
    expected_hash = fingerprint(dataset())
    public = runtime.list_datasets("rag")[0]
    assert public["dataset_hash"] == expected_hash
    public["cases"][0]["query"] = "client-side change"
    assert runtime.list_datasets("rag")[0]["cases"][0]["query"] == "Question 0"
    preflight = await runtime.preflight(plan())
    result = await runtime.create(plan(), wait=True)
    assert preflight["dataset_hash"] == result["dataset_hash"] == expected_hash
    await runtime.close()


async def test_live_replay_immutable_artifact_and_no_second_executor(tmp_path):
    calls = []
    async def executor(*args):
        calls.append(args)
        return await execute(*args)
    runtime = await service(tmp_path, kind="agent", executor=executor)
    result = await runtime.create(plan("agent"), wait=True)
    assert result["status"] == "completed"
    assert result["summary"]["variants"]["base"]["metrics"]["evidence"]["mean"] == .75
    artifact_id = result["results"][0]["input_id"]
    original = await runtime.store.get_input(artifact_id)
    with pytest.raises(ValueError, match="overwritten"):
        await runtime.store.register_input({**original, "provenance": "changed"})
    replay = plan("agent", mode="replay", confirm_live=False)
    preflight = await runtime.preflight(replay)
    assert preflight["ready"]
    assert preflight["resolved_input_refs"] == {"C0:base:1": artifact_id}
    replayed = await runtime.create(replay, wait=True)
    assert replayed["results"][0]["input_id"] == artifact_id
    assert len(calls) == 1
    assert replayed["budget_ledger"]["http_attempts"] == 0
    assert (await runtime.compare(result["experiment_id"], replayed["experiment_id"]))["pairs"][0]["metrics"]["evidence"]["delta"] == 0
    await runtime.close()


async def test_judge_failure_preserves_artifact_and_rule_score(tmp_path):
    async def judge(*_):
        raise ValueError("judge unavailable")
    runtime = await service(tmp_path, judge=judge)
    result = await runtime.create(plan(protocol="rag-answer-v2", judge_enabled=True, confirm_judge=True), wait=True)
    row = result["results"][0]
    assert row["status"] == "completed"
    assert row["input_id"]
    assert row["metrics"]["evidence"]["value"] == .75
    assert row["stage_status"]["judge"] == "grader_failed"
    assert result["summary"]["grader_failed"] == 1
    await runtime.close()


async def test_judge_budget_does_not_erase_scoring(tmp_path):
    async def judge(*_):
        for _ in range(5):
            await reserve_http_attempt("model", "judge")
        return {"metrics": {}}
    runtime = await service(tmp_path, judge=judge)
    result = await runtime.create(plan(protocol="rag-answer-v2", judge_enabled=True, confirm_judge=True), wait=True)
    row = result["results"][0]
    assert result["status"] == "partial"
    assert row["metrics"]["evidence"]["value"] == .75
    assert row["stage_status"]["judge"] == "budget_stopped"
    assert row["input_id"]
    assert result["budget_ledger"]["http_attempts"] == 4
    await runtime.close()


@pytest.mark.parametrize("failure", ["timeout", "provider", "budget"])
async def test_generation_failure_preserves_retrieval_stage(tmp_path, failure):
    async def executor(protocol, case, variant, context, trial, *, stage_sink):
        await stage_sink({"content": {"query": case["query"], "retrieval": {"hits": []}}, "stage_status": {"retrieval": "completed", "generation": "running"}})
        if failure == "timeout":
            await asyncio.sleep(1.1)
        elif failure == "budget":
            for _ in range(5):
                await reserve_http_attempt("model", "answer")
        else:
            raise RuntimeError("provider failed")
    runtime = await service(tmp_path, executor=executor)
    result = await runtime.create(plan(protocol="rag-answer-v2"), wait=True)
    row = result["results"][0]
    assert row["status"] == {"timeout": "timed_out", "provider": "failed", "budget": "budget_stopped"}[failure]
    assert row["stage_status"]["generation"] == {"timeout": "timed_out", "provider": "failed", "budget": "budget_stopped"}[failure]
    assert row["metrics"]["evidence"]["value"] == .75
    frozen = await runtime.store.get_input(row["input_id"])
    assert not frozen["complete"]
    assert (await runtime.artifact(result["experiment_id"], frozen["input_id"]))["content"]["retrieval"] == {"hits": []}
    with pytest.raises(ValueError, match="live"):
        await runtime.preflight(plan(protocol="rag-answer-v2", mode="replay", confirm_live=False))
    await runtime.close()


async def test_local_timeout_continues_other_cells(tmp_path):
    async def executor(protocol, case, *args):
        if case["case_id"] == "C0":
            await asyncio.sleep(1.1)
        return await execute(protocol, case, *args)
    runtime = await service(tmp_path, count=2, executor=executor)
    result = await runtime.create(plan(), wait=True)
    assert [row["status"] for row in result["results"]] == ["timed_out", "completed"]
    assert result["stop_reason"] is None
    assert result["summary"]["timed_out"] == 1
    assert result["summary"]["variants"]["base"]["metrics"]["evidence"]["evaluated"] == 1
    assert result["summary"]["variants"]["base"]["metrics"]["evidence"]["planned"] == 2
    await runtime.close()


async def test_agent_does_not_require_knowledge_snapshot_and_trials_are_distinct(tmp_path):
    runtime = await service(tmp_path, kind="agent")
    runtime.snapshot_resolver = None
    configuration = plan("agent", repetitions=3)
    preflight = await runtime.preflight(configuration)
    assert preflight["ready"] and preflight["expected_cells"] == 3
    result = await runtime.create(configuration, wait=True)
    assert [row["trial"] for row in result["results"]] == [1, 2, 3]
    assert len({row["input_id"] for row in result["results"]}) == 3
    replay = await runtime.create({**configuration, "mode": "replay", "confirm_live": False}, wait=True)
    assert len(replay["results"]) == 3
    assert len((await runtime.compare(result["experiment_id"], replay["experiment_id"], "agent"))["pairs"]) == 3
    await runtime.close()


async def test_legacy_unknown_requires_explicit_degraded_opt_in(tmp_path):
    runtime = await service(tmp_path)
    async def legacy(*_):
        return {"snapshot": {"identity_status": "legacy_unknown"}}
    runtime.snapshot_resolver = legacy
    configuration = plan(variants=[{"variant_id": "hybrid", "name": "Hybrid"}])
    assert not (await runtime.preflight(configuration))["ready"]
    assert (await runtime.preflight({**configuration, "allow_degraded": True}))["ready"]
    await runtime.close()


async def test_sparse_none_tuning_respects_lexical_only_snapshot_profile(tmp_path):
    runtime = await service(tmp_path)
    async def lexical(*_):
        return {"snapshot": {"identity_status": "legacy_unknown"}, "evaluation_context": {"retrieval_profile": {"vector_weight": 0, "lexical_weight": 1}}}
    runtime.snapshot_resolver = lexical
    configuration = plan(variants=[{"variant_id": "base", "name": "Lexical by profile"}])
    assert (await runtime.preflight(configuration))["ready"]
    await runtime.close()


async def test_unverified_import_not_used_as_a_fabricated_baseline(tmp_path):
    runtime = await service(tmp_path, kind="agent")
    value = await runtime.register_input(plan=plan("agent"), case_id="C0", variant_id="base", artifact={"content": {"query": "Question 0"}})
    assert not value["verified_identity"]
    preflight = await runtime.preflight(plan("agent", mode="replay", confirm_live=False, input_refs={"C0:base:1": value["input_id"]}))
    assert not preflight["ready"]
    assert any("unverified" in error for error in preflight["errors"])
    await runtime.close()


async def test_artifact_ownership_and_cross_protocol_comparison(tmp_path):
    runtime = await service(tmp_path)
    a = await runtime.create(plan(), wait=True)
    b = await runtime.create(plan(protocol="rag-answer-v2"), wait=True)
    with pytest.raises(ValueError, match="cross-kind or cross-protocol"):
        await runtime.compare(a["experiment_id"], b["experiment_id"])
    with pytest.raises(KeyError, match="belong"):
        await runtime.artifact(a["experiment_id"], b["results"][0]["input_id"])
    with pytest.raises(KeyError, match="workspace"):
        await runtime.store.get(a["experiment_id"], "agent")
    await runtime.close()


async def test_http_reservation_persisted_before_executor_send(tmp_path):
    observed = []
    runtime = None
    async def executor(*args):
        await reserve_http_attempt("model", "fixture")
        current = (await runtime.store.list())[0]
        observed.append(current["budget_ledger"]["http_attempts"])
        return await execute(*args)
    runtime = await service(tmp_path, executor=executor)
    await runtime.create(plan(), wait=True)
    assert observed == [1]
    await runtime.close()


async def test_restart_marks_only_v2_active_partial_and_preserves_v1_json(tmp_path):
    path = tmp_path / "eval.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE evaluation_experiments(id TEXT PRIMARY KEY,payload TEXT)")
    db.execute("INSERT INTO evaluation_experiments VALUES('legacy','untouched')")
    db.commit()
    db.close()
    runtime = await service(tmp_path)
    result = await runtime.create(plan(), wait=True)
    result.update(status="running", results=[])
    await runtime.store.save(result)
    await runtime.close()
    runtime = await service(tmp_path)
    await runtime.initialize(recover_interrupted=True)
    restored = await runtime.store.get(result["experiment_id"])
    assert restored["status"] == "partial"
    assert restored["stop_reason"] == "process_interrupted"
    assert restored["results"][0]["status"] == "not_run"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT payload FROM evaluation_experiments").fetchone()[0] == "untouched"
    await runtime.close()


def test_api_rejects_cross_kind_before_any_execution(tmp_path):
    runtime = V2EvaluationService(V2EvaluationStore(":memory:"), datasets=[dataset()], snapshot_resolver=snapshot, executor=execute, evaluator=score)
    app = FastAPI()
    app.state.container = SimpleNamespace(evaluation_v2=runtime)
    app.include_router(create_v2_router("rag"))
    app.include_router(create_v2_router("agent"))
    with TestClient(app) as client:
        assert client.get("/api/v1/evaluations/rag/datasets").json()[0]["kind"] == "rag"
        response = client.post("/api/v1/evaluations/agent/preflight", json=plan())
        assert response.status_code == 422
        assert "workspace" in response.json()["detail"]


async def test_metadata_sync_never_exports_query_labels_reasons_or_artifact():
    requests = []
    class Client:
        def create_run(self, **kwargs):
            requests.append(kwargs)
        def update_run(self, *args, **kwargs):
            requests.append(kwargs)
        def flush(self):
            pass
    adapter = V2MetadataLangSmithAdapter(Client())
    experiment = {
        "experiment_id": "exp", "kind": "rag", "protocol": "rag-answer-v2", "dataset_hash": "hash", "status": "completed",
        "created_at": "2026-10-15T00:00:00+00:00", "budget_ledger": {},
        "plan": {"query": "SECRET_QUERY"},
        "results": [{"case_id": "C0", "variant_id": "base", "trial": 1, "status": "completed", "metrics": {"evidence": {"value": .5, "status": "evaluated", "reason": "SECRET_REASON"}}, "artifact": "SECRET_CONTENT"}],
    }
    assert await adapter.sync(experiment) == "synced_metadata_only"
    assert "SECRET" not in json.dumps(requests, default=str)


def test_store_rejects_c_drive():
    with pytest.raises(ValueError, match="drive C"):
        V2EvaluationStore("C:/eval.sqlite3")


def test_metric_finite_and_null_distinct():
    assert MetricResult(value=0, status="evaluated").value == 0
    assert MetricResult(status="missing_gold").value is None
    with pytest.raises(ValueError):
        MetricResult(value=float("nan"), status="evaluated")


async def test_scenario_protocols_are_not_mixed_by_default(tmp_path):
    runtime = await service(tmp_path, kind="agent", count=2)
    runtime.datasets[0]["cases"][0]["protocol"] = "agent-task-v2"
    runtime.datasets[0]["cases"][1]["protocol"] = "agent-contract-v2"
    assert (await runtime.preflight(plan("agent")))["case_ids"] == ["C0"]
    assert (await runtime.preflight(plan("agent", protocol="agent-contract-v2")))["case_ids"] == ["C1"]
    with pytest.raises(ValueError, match="unknown cases"):
        await runtime.preflight(plan("agent", case_ids=["C1"]))
    await runtime.close()


async def test_generation_success_closes_running_stage(tmp_path):
    async def executor(protocol, case, variant, context, trial, *, stage_sink):
        artifact = await execute(protocol, case, variant, context, trial)
        await stage_sink({**artifact, "stage_status": {"retrieval": "completed", "generation": "running"}})
        return artifact
    runtime = await service(tmp_path, executor=executor)
    result = await runtime.create(plan(protocol="rag-answer-v2"), wait=True)
    assert result["results"][0]["stage_status"]["generation"] == "completed"
    assert (await runtime.store.get_input(result["results"][0]["input_id"]))["complete"]
    await runtime.close()


def test_usage_summary_distinguishes_unknown_zero_and_partial_coverage():
    from atlasflow.evaluation.common.service import summarize
    variants = [{"variant_id": "base"}]
    row = {"case_id": "C0", "variant_id": "base", "trial": 1, "status": "completed", "metrics": {}}
    assert summarize([row], 1, variants)["provider_usage"]["known_cost"] is None
    result = summarize([{**row, "provider_calls": [
        {"call_id": "1", "usage": {"cost": 0, "total_tokens": 100}},
        {"call_id": "1", "usage": {"cost": 0, "total_tokens": 100}},
        {"call_id": "2", "usage": None},
    ]}], 1, variants)["provider_usage"]
    assert result["provider_calls"] == 2
    assert result["known_cost"] == 0
    assert result["cost_usage_coverage"] == .5
    assert result["known_tokens"] == 100


async def test_v1_judge_exception_preserves_retrieval_metrics(tmp_path):
    from atlasflow.evaluation.contracts import EvaluationPlan
    async def judge(*_):
        raise ValueError("judge failed")
    runtime, data, arm, result = await _v1_fixture(tmp_path, judge=judge)
    frozen = await runtime.register_input(dataset_id=data.dataset_id, case_id="C0", arm=arm, content=result.model_dump(mode="json"), source_id_map={"doc": "source"}, source_hash_map={"ver": data.source_content_hashes["source"]}, provenance="fixture")
    configuration = EvaluationPlan(dataset_id=data.dataset_id, arms=[arm], input_refs={"C0:base": frozen.input_id}, judge_enabled=True, confirm_judge=True)
    result = await runtime.create(configuration, wait=True)
    row = result.results[0]
    assert row.status == "completed"
    assert row.metrics["source_recall_at_5"] == 1
    assert row.availability["answer_correctness"] == "grader_failed"
    await runtime.close()


async def test_v1_single_cell_timeout_does_not_stop_batch(tmp_path):
    from atlasflow.evaluation.contracts import EvaluationBudgetSpec, EvaluationPlan
    runtime, data, arm, search = await _v1_fixture(tmp_path)
    slow = arm.model_copy(update={"arm_id": "slow"})
    fast = arm.model_copy(update={"arm_id": "fast"})
    async def executor(case, selected_arm, snapshot):
        if selected_arm.arm_id == "slow":
            await asyncio.sleep(1.1)
        return search
    runtime.snapshot_resolver = snapshot
    runtime.retrieval_executor = executor
    result = await runtime.create(EvaluationPlan(dataset_id=data.dataset_id, mode="live", confirm_live=True, arms=[slow, fast], budget=EvaluationBudgetSpec(case_timeout_seconds=1, max_elapsed_seconds=10, concurrency=1)), wait=True)
    assert [row.status for row in result.results] == ["failed", "completed"]
    assert result.stop_reason is None
    await runtime.close()


async def _comparison_fixture(tmp_path, *, count=3, kind="rag", repetitions=1):
    runtime = await service(tmp_path, kind=kind, count=count)
    configuration = plan(kind, variants=[{"variant_id": "baseline", "name": "Baseline"}, {"variant_id": "candidate", "name": "Candidate"}], repetitions=repetitions)
    result = await runtime.create(configuration, wait=True)
    return runtime, result


def _metric(value, direction="higher", **kwargs):
    return MetricResult(value=value, status="evaluated", direction=direction, **kwargs).model_dump(mode="json")


async def test_variant_comparison_uses_only_matching_case_trial_denominators(tmp_path):
    runtime, result = await _comparison_fixture(tmp_path)
    by_cell = {(row["case_id"], row["variant_id"]): row for row in result["results"]}
    by_cell["C0", "baseline"]["metrics"] = {"quality": _metric(1)}
    by_cell["C0", "candidate"]["metrics"] = {"quality": _metric(.5)}
    by_cell["C1", "baseline"]["metrics"] = {"quality": _metric(0)}
    by_cell["C1", "candidate"]["metrics"] = {"quality": _metric(1)}
    by_cell["C1", "candidate"]["status"] = "failed"
    by_cell["C2", "baseline"]["metrics"] = {"quality": _metric(None)}
    by_cell["C2", "candidate"]["metrics"] = {"quality": _metric(1)}
    await runtime.store.save(result)
    response = await runtime.compare_variants(result["experiment_id"])
    assert response["baseline_variant_id"] == "baseline"
    metric = response["comparisons"][0]["metrics"]["quality"]
    assert metric["baseline_mean"] == 1
    assert metric["candidate_mean"] == .5
    assert metric["mean_delta"] == -.5
    assert metric["paired"] == 1 and metric["planned"] == 3
    assert metric["regressed"] == 1
    assert metric["not_comparable"] == 2
    assert metric["excluded_reasons"]["candidate_cell_failed"] == 1
    assert metric["excluded_reasons"]["baseline_metric_value_unavailable"] == 1
    await runtime.close()


async def test_variant_comparison_interprets_lower_and_neutral_metrics(tmp_path):
    runtime, result = await _comparison_fixture(tmp_path, count=1)
    for row in result["results"]:
        is_base = row["variant_id"] == "baseline"
        row["metrics"] = {
            "latency": _metric(100 if is_base else 80, "lower", unit="ms"),
            "tool_calls": _metric(1 if is_base else 2, "neutral", unit="count"),
            "equal": _metric(0),
        }
    await runtime.store.save(result)
    metrics = (await runtime.compare_variants(result["experiment_id"]))["comparisons"][0]["metrics"]
    assert metrics["latency"]["mean_delta"] == -20
    assert metrics["latency"]["improved"] == 1 and metrics["latency"]["regressed"] == 0
    assert metrics["tool_calls"]["mean_delta"] == 1
    assert metrics["tool_calls"]["unclassified"] == 1
    assert metrics["tool_calls"]["improved"] == metrics["tool_calls"]["regressed"] == 0
    assert metrics["equal"]["tied"] == 1
    await runtime.close()


async def test_variant_comparison_excludes_degraded_missing_and_incompatible_graders(tmp_path):
    runtime, result = await _comparison_fixture(tmp_path, count=3)
    for row in result["results"]:
        row["metrics"] = {"quality": _metric(.5)}
        if row["variant_id"] == "candidate":
            if row["case_id"] == "C0":
                row["degraded"] = ["semantic_lane_unavailable"]
            elif row["case_id"] == "C1":
                row["metrics"]["quality"]["status"] = "missing_gold"
            else:
                row["metrics"]["quality"]["grader_version"] = "different-version"
    await runtime.store.save(result)
    metric = (await runtime.compare_variants(result["experiment_id"]))["comparisons"][0]["metrics"]["quality"]
    assert metric["paired"] == 0 and metric["planned"] == 3
    assert metric["baseline_mean"] is metric["candidate_mean"] is metric["mean_delta"] is None
    assert metric["excluded_reasons"] == {"candidate_degraded": 1, "candidate_metric_missing_gold": 1, "metric_contract_mismatch": 1}
    await runtime.close()


async def test_variant_comparison_does_not_pool_different_metric_contracts(tmp_path):
    runtime, result = await _comparison_fixture(tmp_path, count=2)
    for row in result["results"]:
        row["metrics"] = {"quality": _metric(.5, grader_version=row["case_id"])}
    await runtime.store.save(result)
    metric = (await runtime.compare_variants(result["experiment_id"]))["comparisons"][0]["metrics"]["quality"]
    assert metric["paired"] == 0
    assert metric["excluded_reasons"] == {"metric_contract_changes_across_pairs": 2}
    await runtime.close()


async def test_agent_variant_comparison_pairs_trials_not_list_order(tmp_path):
    runtime, result = await _comparison_fixture(tmp_path, count=1, kind="agent", repetitions=3)
    for row in result["results"]:
        row["metrics"] = {"task_success": _metric(row["trial"] / 10 + (0 if row["variant_id"] == "baseline" else .2))}
    result["results"].reverse()
    # A missing trial must not borrow another trial's successful result.
    result["results"] = [row for row in result["results"] if not (row["variant_id"] == "candidate" and row["trial"] == 2)]
    await runtime.store.save(result)
    response = await runtime.compare_variants(result["experiment_id"], candidate_variant_id="candidate", kind="agent")
    metric = response["comparisons"][0]["metrics"]["task_success"]
    assert metric["paired"] == 2 and metric["planned"] == 3
    assert metric["baseline_mean"] == pytest.approx(.2)
    assert metric["candidate_mean"] == pytest.approx(.4)
    assert metric["mean_delta"] == pytest.approx(.2)
    assert metric["excluded_reasons"]["candidate_cell_missing"] == 1
    await runtime.close()


def test_variant_metric_pair_rejects_nonfinite_values_and_overflow_delta():
    from atlasflow.evaluation.common.service import _variant_metric_pair
    for invalid in (None, float("nan"), float("inf"), "1.0"):
        left = {"status": "completed", "metrics": {"quality": {"status": "evaluated", "value": invalid}}}
        right = {"status": "completed", "metrics": {"quality": {"status": "evaluated", "value": 1}}}
        assert _variant_metric_pair(left, right, "quality")["status"] == "excluded"
    left = {"status": "completed", "metrics": {"quality": _metric(-1e308)}}
    right = {"status": "completed", "metrics": {"quality": _metric(1e308)}}
    assert _variant_metric_pair(left, right, "quality")["reasons"] == ["metric_delta_nonfinite"]


async def test_variant_comparison_api_get_post_and_workspace_isolation(tmp_path):
    runtime, result = await _comparison_fixture(tmp_path, count=1)
    app = FastAPI()
    app.state.container = SimpleNamespace(evaluation_v2=runtime)
    app.include_router(create_v2_router("rag"))
    app.include_router(create_v2_router("agent"))
    prefix = f"/api/v1/evaluations/rag/experiments/{result['experiment_id']}"
    before = len(await runtime.store.list_inputs())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        get = await client.get(prefix + "/comparison?baseline_variant_id=baseline")
        post = await client.post(prefix + "/compare", json={"baseline_variant_id": "baseline"})
        assert get.status_code == post.status_code == 200
        assert get.json() == post.json()
        assert (await client.get(prefix + "/comparison?baseline_variant_id=missing")).status_code == 422
        assert (await client.post(prefix + "/compare", json={"candidate_variant_id": "baseline"})).status_code == 422
        assert (await client.get(prefix.replace("/rag/", "/agent/") + "/comparison")).status_code == 404
    assert len(await runtime.store.list_inputs()) == before
    await runtime.close()


async def test_executor_plan_validator_and_settings_catalog_are_shared_without_replay_calls(tmp_path):
    seen = []
    async def executor(*args):
        return await execute(*args)
    executor.extra_settings = {"agent": {"tools": [{"name": "calculator", "risk": "low"}]}}
    runtime = await service(tmp_path, kind="agent", executor=executor)
    result = await runtime.create(plan("agent"), wait=True)
    async def validate(protocol, configuration, cases, context):
        seen.append(protocol)
        return ["effective toolsets must differ"]
    executor.validate_plan = validate
    assert runtime.settings("agent")["tools"][0]["name"] == "calculator"
    assert not (await runtime.preflight(plan("agent")))["ready"]
    assert seen == ["agent-task-v2"]
    assert (await runtime.preflight({**result["plan"], "mode": "replay", "confirm_live": False}))["ready"]
    assert seen == ["agent-task-v2"]
    await runtime.close()


async def test_cross_experiment_tools_comparison_rejects_model_policy_confounds(tmp_path):
    async def executor(*args):
        return await execute(*args)
    def resolve(protocol, variant, context, case):
        return {"model": variant.get("model") or "model-A", "policy": variant["policy"], "workflow_version": "current", "tool_environment": variant["tool_allowlist"]}
    executor.resolve_variant = resolve
    runtime = await service(tmp_path, kind="agent", executor=executor)
    configuration = plan("agent", comparison_focus="tools", variants=[
        {"variant_id": "baseline", "name": "Baseline", "tool_allowlist": ["calculator"]},
        {"variant_id": "candidate", "name": "Candidate", "tool_allowlist": []},
    ])
    first = await runtime.create(configuration, wait=True)
    changed_model = {**configuration, "variants": [{**variant, "model": "model-B"} for variant in configuration["variants"]]}
    second = await runtime.create(changed_model, wait=True)
    with pytest.raises(ValueError, match="actual model, policy and workflow fixed"):
        await runtime.compare(first["experiment_id"], second["experiment_id"], "agent")
    third = await runtime.create(configuration, wait=True)
    assert (await runtime.compare(first["experiment_id"], third["experiment_id"], "agent"))["pairs"]
    await runtime.close()


async def _v1_fixture(tmp_path, **kwargs):
    from hashlib import sha256

    from atlasflow.evaluation.contracts import (
        EvaluationArm,
        EvaluationCase,
        EvaluationDataset,
        SourceAnchor,
    )
    from atlasflow.evaluation.dataset import DatasetRegistry
    from atlasflow.evaluation.runner import EvaluationService
    from atlasflow.evaluation.store import EvaluationStore
    from atlasflow.knowledge.domain import (
        KnowledgeChunk,
        RetrievalDecision,
        RetrievalHit,
        RetrievalTrace,
        SearchResult,
    )
    raw = "The fixture evidence stores keys and values."
    (tmp_path / "fixture.md").write_text(raw, encoding="utf-8")
    digest = sha256(raw.encode()).hexdigest()
    data = EvaluationDataset(dataset_id="v1-fixture", name="Fixture", version="1", space="fixture", corpus_manifest_hash="fixture", source_content_hashes={"source": digest}, cases=[EvaluationCase(case_id="C0", kind="rag", query="Question 0", gold_status="verified", expected_sources=[SourceAnchor(source_id="source", content_hash=digest, source_path="fixture.md", anchor=raw)])])
    arm = EvaluationArm(arm_id="base", name="Baseline", tuning={"vector_weight": 0, "rerank_enabled": False})
    chunk = KnowledgeChunk(document_id="doc", version_id="ver", generation_id="gen", chunk_kind="child", title="Fixture", content=raw, token_count=10, lexical_text=raw)
    result = SearchResult(query="Question 0", hits=[RetrievalHit(chunk=chunk)], decision=RetrievalDecision(status="partial", evidence_count=1, coverage=1), trace=RetrievalTrace(profile_id="fixture", generation_id="gen"))
    runtime = EvaluationService(EvaluationStore(tmp_path / "v1.sqlite3"), DatasetRegistry([data], root=tmp_path), **kwargs)
    await runtime.initialize()
    return runtime, data, arm, result
