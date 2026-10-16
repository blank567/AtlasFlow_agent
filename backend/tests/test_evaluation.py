from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from hashlib import sha256
from types import SimpleNamespace

import pytest
from atlasflow.evaluation.api import router
from atlasflow.evaluation.budget import (
    EvaluationBudget,
    EvaluationBudgetExceeded,
    evaluation_budget_context,
    reserve_http_attempt,
)
from atlasflow.evaluation.contracts import (
    EvaluationArm,
    EvaluationBudgetSpec,
    EvaluationCase,
    EvaluationDataset,
    EvaluationExperiment,
    EvaluationPlan,
    FrozenEvaluationInput,
    SourceAnchor,
    all_rag_arms,
    arm_fingerprint,
    fingerprint,
)
from atlasflow.evaluation.dataset import DatasetRegistry
from atlasflow.evaluation.langsmith_adapter import MetadataLangSmithAdapter, safe_summary
from atlasflow.evaluation.metrics import evaluate_input, summarize
from atlasflow.evaluation.runner import EvaluationService
from atlasflow.evaluation.store import EvaluationStore
from atlasflow.knowledge.domain import (
    KnowledgeChunk,
    RetrievalDecision,
    RetrievalHit,
    RetrievalTrace,
    SearchResult,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError


def fixtures(tmp_path):
    raw = "A key-value cache stores past keys and values."
    (tmp_path / "source.md").write_text(raw, encoding="utf-8")
    digest = sha256(raw.encode()).hexdigest()
    case = EvaluationCase(
        case_id="R01",
        kind="rag",
        query="What does KVCache store?",
        gold_status="verified",
        expected_sources=[
            SourceAnchor(
                source_id="kv-cache", content_hash=digest, source_path="source.md", anchor=raw
            )
        ],
    )
    dataset = EvaluationDataset(
        dataset_id="test-rag",
        name="Fixture",
        version="1",
        space="test-space",
        corpus_manifest_hash="test",
        source_content_hashes={"kv-cache": digest},
        cases=[case],
    )
    arm = EvaluationArm(
        arm_id="baseline", name="Fixture", tuning={"vector_weight": 0, "rerank_enabled": False}
    )
    chunk = KnowledgeChunk(
        document_id="doc",
        version_id="version",
        generation_id="generation",
        chunk_kind="child",
        title="Cache",
        content=raw,
        token_count=12,
        lexical_text=raw,
    )
    result = SearchResult(
        query=case.query,
        hits=[RetrievalHit(chunk=chunk)],
        decision=RetrievalDecision(status="partial", evidence_count=1, coverage=1),
        trace=RetrievalTrace(profile_id="fixture", generation_id="generation"),
    )
    return case, dataset, arm, result, digest


async def prepared(tmp_path, **kwargs):
    case, dataset, arm, result, digest = fixtures(tmp_path)
    service = EvaluationService(
        EvaluationStore(tmp_path / "eval.sqlite3"),
        DatasetRegistry([dataset], root=tmp_path),
        **kwargs,
    )
    await service.initialize()
    frozen = await service.register_input(
        dataset_id=dataset.dataset_id,
        case_id=case.case_id,
        arm=arm,
        content=result.model_dump(mode="json"),
        snapshot={"generation_id": "generation"},
        source_id_map={"doc": "kv-cache"},
        source_hash_map={"version": digest},
        provenance="fixture",
    )
    plan = EvaluationPlan(
        dataset_id=dataset.dataset_id, arms=[arm], input_refs={"R01:baseline": frozen.input_id}
    )
    return service, frozen, plan


def test_explicit_live_and_judge_confirmation():
    arm = EvaluationArm(arm_id="a", name="A")
    with pytest.raises(ValidationError, match="confirm_live"):
        EvaluationPlan(dataset_id="a", arms=[arm], mode="live")
    with pytest.raises(ValidationError, match="confirm_judge"):
        EvaluationPlan(dataset_id="a", arms=[arm], judge_enabled=True)
    assert len(all_rag_arms()) == 6
    assert EvaluationBudgetSpec().max_http_attempts == 60


def test_resolved_config_fingerprint_sparse_ui_defaults_and_environment():
    from atlasflow.knowledge.domain import RetrievalProfile

    sparse = EvaluationArm(
        arm_id="baseline", name="Different label", tuning={"rerank_enabled": False}
    )
    defaults = RetrievalProfile().model_dump(
        exclude={"id", "status", "min_relevance", "context_token_budget", "result_limit"}
    )
    complete = EvaluationArm(
        arm_id="baseline", name="UI", tuning={**defaults, "rerank_enabled": False}
    )
    assert arm_fingerprint(sparse) == arm_fingerprint(complete)
    profile = RetrievalProfile(query_expansion_limit=3).model_dump(mode="json")
    assert arm_fingerprint(sparse, profile) != arm_fingerprint(complete, profile)
    complete3 = complete.model_copy(
        update={"tuning": {**complete.tuning, "query_expansion_limit": 3}}
    )
    assert arm_fingerprint(sparse, profile) == arm_fingerprint(complete3, profile)


async def test_budget_atomic_concurrent_attempts_and_no_production_effect():
    await reserve_http_attempt("provider", "outside")
    budget = EvaluationBudget(EvaluationBudgetSpec(max_http_attempts=60))
    sent = []

    async def send(index):
        await reserve_http_attempt("provider" if index % 2 else "tool", "fake")
        sent.append(index)

    with evaluation_budget_context(budget):
        rows = await asyncio.gather(*(send(index) for index in range(100)), return_exceptions=True)
    assert len(sent) == 60
    assert sum(isinstance(row, EvaluationBudgetExceeded) for row in rows) == 40
    assert budget.ledger()["by_kind"] == {"tool": 30, "provider": 30}
    assert not issubclass(EvaluationBudgetExceeded, Exception)


async def test_elapsed_and_cancel_guard():
    budget = EvaluationBudget(EvaluationBudgetSpec(max_elapsed_seconds=1))
    budget.started -= 2
    with pytest.raises(EvaluationBudgetExceeded, match="elapsed"):
        await budget.reserve("provider", "embedding")
    cancelled = EvaluationBudget(EvaluationBudgetSpec())
    cancelled.stop("cancelled")
    with pytest.raises(EvaluationBudgetExceeded, match="cancelled"):
        await cancelled.reserve("tool", "weather")


async def test_budget_persistence_failure_blocks_before_sending():
    persisted = []

    async def save(ledger):
        persisted.append(ledger["http_attempts"])
        raise OSError("disk unavailable")

    budget = EvaluationBudget(EvaluationBudgetSpec(), on_reserve=save)
    with (
        evaluation_budget_context(budget),
        pytest.raises(EvaluationBudgetExceeded, match="persistence_failed"),
    ):
        await reserve_http_attempt("provider", "not-sent")
    assert persisted == [1]


async def test_live_attempt_reservation_is_durable_before_response(tmp_path):
    _, dataset, arm, result, _ = fixtures(tmp_path)
    reached = asyncio.Event()
    release = asyncio.Event()

    async def snapshot(*_):
        return {"generation_id": "fixed"}

    async def executor(*_):
        await reserve_http_attempt("provider", "embedding")
        reached.set()
        await release.wait()
        return result

    service = EvaluationService(
        EvaluationStore(tmp_path / "durable.sqlite3"),
        DatasetRegistry([dataset], root=tmp_path),
        snapshot_resolver=snapshot,
        retrieval_executor=executor,
    )
    await service.initialize()
    value = await service.create(
        EvaluationPlan(dataset_id=dataset.dataset_id, arms=[arm], mode="live", confirm_live=True)
    )
    await reached.wait()
    current = await service.store.get(value.experiment_id)
    assert current.results == [] and current.budget_ledger["http_attempts"] == 1
    await service.cancel(value.experiment_id)
    assert (await service.store.get(value.experiment_id)).budget_ledger["http_attempts"] == 1
    await service.close()


async def test_replay_zero_calls_and_recomputable_source_scores(tmp_path):
    async def forbidden(*_):
        raise AssertionError("replay must not call providers")

    service, frozen, plan = await prepared(tmp_path, retrieval_executor=forbidden, judge=forbidden)
    check = await service.preflight(plan)
    assert check["ready"] and check["estimated_provider_requests"] == 0
    value = await service.create(plan, wait=True)
    assert value.status == "completed"
    row = value.results[0]
    assert row.metrics["source_recall_at_5"] == 1 and row.metrics["mrr"] == 1
    assert row.metrics["anchor_hit_rate"] == 1
    assert row.metrics["answer_correctness"] is None
    assert value.budget_ledger["http_attempts"] == 0
    assert (
        evaluate_input(
            service.datasets.get(plan.dataset_id).cases[0], frozen, replayed=True
        ).metrics
        == row.metrics
    )
    assert (tmp_path / "artifacts" / value.experiment_id / "cases.jsonl").is_file()
    await service.close()


async def test_preflight_missing_inputs_never_fabricates(tmp_path):
    service, _, plan = await prepared(tmp_path)
    missing = plan.model_copy(
        update={"input_refs": {}, "arms": [plan.arms[0].model_copy(update={"result_limit": 4})]}
    )
    check = await service.preflight(missing)
    assert not check["ready"] and "frozen input missing" in check["errors"][0]
    with pytest.raises(ValueError, match="frozen input missing"):
        await service.create(missing)
    assert not await service.store.list()
    await service.close()


async def test_replay_automatically_selects_registered_matching_input(tmp_path):
    service, frozen, plan = await prepared(tmp_path)
    implicit = plan.model_copy(update={"input_refs": {}})
    check = await service.preflight(implicit)
    assert check["ready"] and check["resolved_input_refs"] == {"R01:baseline": frozen.input_id}
    experiment = await service.create(implicit, wait=True)
    assert experiment.plan.input_refs["R01:baseline"] == frozen.input_id
    assert experiment.plan.case_ids == ["R01"]
    await service.close()


async def test_live_recovery_case_is_blocked_before_calls(tmp_path):
    _, dataset, arm, _result, _ = fixtures(tmp_path)

    async def executor(*_):
        raise AssertionError("unsupported case must not execute")

    executor.validate_case = lambda _: "real restart required"
    case = EvaluationCase(
        case_id="A01",
        kind="agent",
        query="Recover",
        answerable=False,
        required_invariants=["recovery"],
    )
    dataset = dataset.model_copy(update={"cases": [case]})

    async def snapshot(*_):
        return {"generation_id": "x", "evaluation_context": {"query_fingerprint": "fixed"}}

    service = EvaluationService(
        EvaluationStore(tmp_path / "blocked.sqlite3"),
        DatasetRegistry([dataset], root=tmp_path),
        snapshot_resolver=snapshot,
        agent_executor=executor,
    )
    await service.initialize()
    check = await service.preflight(
        EvaluationPlan(dataset_id=dataset.dataset_id, arms=[arm], mode="live", confirm_live=True)
    )
    assert not check["ready"] and "real restart required" in check["errors"]
    assert check["snapshot"]["evaluation_context"]["query_fingerprint"] == "fixed"
    await service.close()


async def test_live_preflight_blocks_missing_gold_and_incompatible_pure_vector(tmp_path):
    _, dataset, arm, _, digest = fixtures(tmp_path)
    observed = {"generation_id": "g", "documents": {}, "source_id_map": {}, "source_hash_map": {}}

    async def snapshot(*_):
        return observed

    async def forbidden(*_):
        raise AssertionError("preflight cannot call retrieval")

    service = EvaluationService(
        EvaluationStore(tmp_path / "preflight.sqlite3"),
        DatasetRegistry([dataset], root=tmp_path),
        snapshot_resolver=snapshot,
        retrieval_executor=forbidden,
    )
    await service.initialize()
    plan = EvaluationPlan(dataset_id=dataset.dataset_id, arms=[arm], mode="live", confirm_live=True)
    check = await service.preflight(plan)
    assert not check["ready"] and any(
        "gold source not published" in message for message in check["errors"]
    )
    observed.update(
        documents={"doc": {"version_id": "v", "status": "active"}},
        source_id_map={"doc": "kv-cache"},
        source_hash_map={"v": digest},
        evaluation_context={"embedding_compatibility": {"status": "service_unavailable"}},
    )
    semantic = EvaluationArm(
        arm_id="semantic", name="Semantic", tuning={"lexical_weight": 0, "vector_weight": 1}
    )
    check = await service.preflight(plan.model_copy(update={"arms": [semantic]}))
    assert not check["ready"] and any("pure semantic" in message for message in check["errors"])
    hybrid = EvaluationArm(
        arm_id="hybrid", name="Hybrid", tuning={"lexical_weight": 1, "vector_weight": 1}
    )
    check = await service.preflight(plan.model_copy(update={"arms": [hybrid]}))
    assert check["ready"] and any("degrade to lexical" in message for message in check["warnings"])
    await service.close()


async def test_default_budget_settings_and_index_comparison_scope(tmp_path):
    service, _, plan = await prepared(
        tmp_path, default_budget=EvaluationBudgetSpec(max_http_attempts=12)
    )
    assert service.settings()["budget"]["max_http_attempts"] == 12
    a = await service.create(plan, wait=True)
    b = (await service.create(plan, wait=True)).model_copy(
        update={"snapshot": {"generation_id": "other"}}
    )
    await service.store.save(b)
    with pytest.raises(ValueError, match="same index snapshot"):
        await service.compare(a.experiment_id, b.experiment_id)
    assert (await service.compare(a.experiment_id, b.experiment_id, "index"))["scope"] == "index"
    await service.close()


async def test_inputs_immutable_and_query_binding(tmp_path):
    service, frozen, _plan = await prepared(tmp_path)
    with pytest.raises(ValueError, match="cannot be overwritten"):
        await service.store.register_input(frozen.model_copy(update={"case_id": "other"}))
    case, dataset, arm, result, _ = fixtures(tmp_path)
    wrong = result.model_copy(update={"query": "another question"})
    with pytest.raises(ValueError, match="query does not match"):
        await service.register_input(
            dataset_id=dataset.dataset_id,
            case_id=case.case_id,
            arm=arm,
            content=wrong.model_dump(mode="json"),
        )
    with pytest.raises(ValidationError, match="content hash mismatch"):
        FrozenEvaluationInput.model_validate(
            {**frozen.model_dump(mode="json"), "content_hash": "bad"}
        )
    await service.close()


async def test_wrong_dataset_config_source_hash_does_not_match(tmp_path):
    service, frozen, plan = await prepared(tmp_path)
    case = service.datasets.get(plan.dataset_id).cases[0]
    wrong_hash = frozen.model_copy(update={"source_hash_map": {"version": "a" * 64}})
    assert evaluate_input(case, wrong_hash, replayed=True).metrics["source_recall_at_5"] == 0
    unknown = frozen.model_copy(update={"source_id_map": {}})
    result = evaluate_input(case, unknown, replayed=True)
    assert result.metrics["source_recall_at_5"] is None
    assert result.availability["source_recall_at_5"] == "source_identity_missing"
    altered = plan.model_copy(
        update={"arms": [plan.arms[0].model_copy(update={"result_limit": 4})]}
    )
    assert not (await service.preflight(altered))["ready"]
    await service.close()


async def test_no_answer_has_no_recall_division(tmp_path):
    service, frozen, _plan = await prepared(tmp_path)
    case = EvaluationCase(
        case_id="R01", kind="rag", query=frozen.content["query"], answerable=False
    )
    row = evaluate_input(case, frozen, replayed=True)
    assert row.metrics["source_recall_at_5"] is None
    assert row.availability["source_recall_at_5"] == "not_applicable"
    await service.close()


async def test_source_ranking_uses_unique_sources_and_child_context_are_separate(tmp_path):
    service, frozen, plan = await prepared(tmp_path)
    case = service.datasets.get(plan.dataset_id).cases[0]
    raw = SearchResult.model_validate(frozen.content)
    distractor = raw.hits[0].chunk.model_copy(
        update={"document_id": "other", "version_id": "other-v", "content": "irrelevant"}
    )
    result = raw.model_copy(
        update={"hits": [RetrievalHit(chunk=distractor) for _ in range(5)] + raw.hits}
    )
    content = result.model_dump(mode="json")
    modified = frozen.model_copy(
        update={
            "content": content,
            "content_hash": fingerprint(content),
            "source_id_map": {**frozen.source_id_map, "other": "irrelevant"},
            "source_hash_map": {**frozen.source_hash_map, "other-v": "b" * 64},
        }
    )
    row = evaluate_input(case, modified, replayed=True)
    assert row.metrics["source_recall_at_5"] == 1 and row.metrics["mrr"] == 0.5
    child = raw.hits[0].chunk.model_copy(
        update={"content": "The child does not include the target."}
    )
    context_result = raw.model_copy(
        update={
            "hits": [RetrievalHit(chunk=child, context_excerpt=case.expected_sources[0].anchor)]
        }
    )
    content = context_result.model_dump(mode="json")
    context = frozen.model_copy(update={"content": content, "content_hash": fingerprint(content)})
    row = evaluate_input(case, context, replayed=True)
    assert row.metrics["anchor_hit_rate"] == 0 and row.metrics["context_anchor_hit_rate"] == 1
    assert row.availability["anchor_hit_rate"] == "evaluated_child_text"
    await service.close()


async def test_live_shared_stop_preserves_partial_and_frozen_input(tmp_path):
    _case, dataset, arm, result, digest = fixtures(tmp_path)

    async def snapshot(*_):
        return {
            "generation_id": "generation",
            "source_id_map": {"doc": "kv-cache"},
            "source_hash_map": {"version": digest},
        }

    sends = []

    async def retrieve(*_):
        await reserve_http_attempt("provider", "embedding")
        sends.append(1)
        await reserve_http_attempt("tool", "web-fetch-redirect")
        sends.append(2)
        return result

    service = EvaluationService(
        EvaluationStore(tmp_path / "eval.sqlite3"),
        DatasetRegistry([dataset], root=tmp_path),
        snapshot_resolver=snapshot,
        retrieval_executor=retrieve,
    )
    await service.initialize()
    plan = EvaluationPlan(
        dataset_id=dataset.dataset_id,
        arms=[arm],
        mode="live",
        confirm_live=True,
        budget=EvaluationBudgetSpec(max_http_attempts=1),
    )
    value = await service.create(plan, wait=True)
    assert value.status == "partial" and len(sends) == 1
    assert value.results[0].status == "budget_stopped"
    assert value.budget_ledger["http_attempts"] == 1
    await service.close()


async def test_optional_judge_under_same_budget_and_availability(tmp_path):
    async def judge(*_):
        await reserve_http_attempt("provider", "judge")
        return {"answer_correctness": 0.8, "citation_support": 0.7}

    service, _, plan = await prepared(tmp_path, judge=judge)
    value = await service.create(
        plan.model_copy(update={"judge_enabled": True, "confirm_judge": True}), wait=True
    )
    assert value.results[0].metrics["answer_correctness"] == 0.8
    assert value.results[0].availability["citation_support"] == "independent_judge"
    assert value.budget_ledger["http_attempts"] == 1
    await service.close()


async def test_cancel_and_startup_do_not_resume_network(tmp_path):
    service, _, plan = await prepared(tmp_path)
    value = await service.create(plan)
    stopped = await service.cancel(value.experiment_id)
    assert stopped.status == "cancelled"
    stranded = EvaluationExperiment(
        status="running", plan=plan, dataset_hash="x", comparison_key="x"
    )
    await service.store.save(stranded)
    await service.close()
    other = EvaluationService(EvaluationStore(tmp_path / "eval.sqlite3"), service.datasets)
    await other.initialize()
    assert (await other.store.get(stranded.experiment_id)).status == "running"
    await other.close()
    recovery = EvaluationService(EvaluationStore(tmp_path / "eval.sqlite3"), service.datasets)
    await recovery.initialize(recover_interrupted=True)
    restored = await recovery.store.get(stranded.experiment_id)
    assert restored.status == "partial" and restored.stop_reason == "process_interrupted"
    await recovery.close()


async def test_compare_identity_and_csv_unknown_usage(tmp_path):
    service, _, plan = await prepared(tmp_path)
    a = await service.create(plan, wait=True)
    b = await service.create(plan, wait=True)
    assert (await service.compare(a.experiment_id, b.experiment_id))[
        "comparison_key"
    ] == a.comparison_key
    incompatible = b.model_copy(update={"comparison_key": "different"})
    await service.store.save(incompatible)
    with pytest.raises(ValueError, match="incompatible"):
        await service.compare(a.experiment_id, b.experiment_id)
    assert "R01" in await service.export(a.experiment_id, "csv")
    summary = summarize(a.results, 1)
    assert summary["arms"]["baseline"]["known_cost"] is None
    assert summary["arms"]["baseline"]["cost_usage_coverage"] is None
    await service.close()


async def test_metadata_only_and_remote_failure_keep_local_result(tmp_path):
    class Client:
        def create_run(self, **kwargs):
            serialized = json.dumps(kwargs, default=str)
            assert "KVCache" not in serialized and "key-value" not in serialized
            raise ConnectionError("offline")

    service, _, plan = await prepared(
        tmp_path, langsmith_adapter=MetadataLangSmithAdapter(Client())
    )
    value = await service.create(plan.model_copy(update={"langsmith_sync": True}), wait=True)
    assert value.status == "completed" and value.sync_status == "failed"
    assert "What does" not in json.dumps(safe_summary(value))
    assert value.results[0].metrics["source_recall_at_5"] == 1
    await service.close()


def test_datasets_counts_stable_hashes_and_no_fake_results():
    registry = DatasetRegistry()
    rag = registry.get("llm-rag-v1")
    agent = registry.get("llm-agent-v1")
    assert len(rag.cases) == 40 and len(agent.cases) == 10
    assert len([case for case in rag.cases if case.gold_status == "verified"]) == 34
    assert all(case.gold_status == "draft" for case in rag.cases if not case.answerable)
    assert all(
        source.source_path.startswith("data/knowledge/corpus/")
        for case in rag.cases
        for source in case.expected_sources
    )
    assert all(not hasattr(case, "rankings") for case in rag.cases)


def test_packaged_dataset_fallback_and_local_priority(tmp_path, monkeypatch):
    from atlasflow.evaluation import dataset as dataset_module

    _, definition, _, _, _ = fixtures(tmp_path)
    package = tmp_path / "package"
    (package / "data").mkdir(parents=True)
    (package / "data/rag.json").write_text(definition.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(dataset_module.resources, "files", lambda _: package)
    fallback = DatasetRegistry(root=tmp_path)
    assert fallback.get(definition.dataset_id).name == "Fixture"
    local = tmp_path / "evals/rag"
    local.mkdir(parents=True)
    local_definition = definition.model_copy(update={"name": "Local definition"})
    (local / "dataset.json").write_text(local_definition.model_dump_json(), encoding="utf-8")
    assert DatasetRegistry(root=tmp_path).get(definition.dataset_id).name == "Local definition"


def test_cli_replay_preflight_and_smoke_refuse_implicit_live(tmp_path):
    from pathlib import Path

    project = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "PYTHONPATH": str(project / "backend/src"),
        "LANGSMITH_TRACING": "false",
        "PYTHONIOENCODING": "utf-8",
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
    }
    check = subprocess.run(
        [
            sys.executable,
            "scripts/evaluate.py",
            "preflight",
            "--case",
            "R01",
            "--store",
            str(tmp_path / "cli.sqlite3"),
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert check.returncode == 2
    output = json.loads(check.stdout)
    assert output["ready"] is False and output["estimated_provider_requests"] == 0
    assert output["available_inputs"] == []
    smoke = subprocess.run(
        [sys.executable, "evals/run_local.py"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert smoke.returncode != 0 and "without --live" in smoke.stderr


async def test_agent_run_freeze_is_read_only_and_contract_scores_are_not_self_grade(tmp_path):
    from backend.tests.fakes import make_test_container

    container = make_test_container()
    query = "Evaluate this deterministic test workflow, not live model quality."
    run = await container.run_service.execute_and_wait(query)
    case = EvaluationCase(case_id="A01", kind="agent", query=query, answerable=False)
    _, source_dataset, arm, _, _ = fixtures(tmp_path)
    dataset = source_dataset.model_copy(update={"cases": [case]})
    service = EvaluationService(
        EvaluationStore(tmp_path / "agent.sqlite3"),
        DatasetRegistry([dataset], root=tmp_path),
        run_reader=container.store.get,
    )
    await service.initialize()
    frozen = await service.register_input(
        dataset_id=dataset.dataset_id, case_id="A01", arm=arm, run_id=run.id
    )
    value = await service.create(
        EvaluationPlan(dataset_id=dataset.dataset_id, arms=[arm]), wait=True
    )
    row = value.results[0]
    assert row.replayed is True and row.metrics["run_completed"] is True
    assert row.metrics["answer_correctness"] is None and row.metrics["citation_support"] is None
    assert row.input_id == frozen.input_id and (await container.store.get(run.id)) == run
    assert "content" not in service.input_metadata(frozen)
    await service.close()
    await container.run_service.close()
    await container.knowledge.close()
    await container.store.close()


def test_api_preflight_default_and_input_metadata(tmp_path):
    _, dataset, arm, _, _ = fixtures(tmp_path)
    evaluation = EvaluationService(
        EvaluationStore(tmp_path / "api.sqlite3"), DatasetRegistry([dataset], root=tmp_path)
    )

    @asynccontextmanager
    async def lifespan(_):
        await evaluation.initialize()
        yield
        await evaluation.close()

    app = FastAPI(lifespan=lifespan)
    app.state.container = SimpleNamespace(evaluation=evaluation)
    app.include_router(router)
    with TestClient(app) as client:
        assert (
            client.get("/api/v1/evaluations/datasets").json()[0]["dataset_id"] == dataset.dataset_id
        )
        assert (
            client.get("/api/v1/evaluations/settings").json()["budget"]["max_http_attempts"] == 60
        )
        response = client.post(
            "/api/v1/evaluations/preflight",
            json={"dataset_id": dataset.dataset_id, "arms": [arm.model_dump(mode="json")]},
        )
        assert response.status_code == 200 and not response.json()["ready"]
        assert client.get("/api/v1/evaluations/inputs").json() == []
        invalid = client.post(
            "/api/v1/evaluations",
            json={
                "dataset_id": dataset.dataset_id,
                "arms": [arm.model_dump(mode="json")],
                "mode": "live",
            },
        )
        assert invalid.status_code == 422
