"""Actual app wiring and fresh RAG runs with injected test providers."""
import hashlib
import time

from atlasflow.bootstrap import ProviderBundle, build_container
from atlasflow.main import create_app
from fastapi.testclient import TestClient

from backend.tests.fakes import (
    FakeEmbeddingProvider,
    FakeModelGateway,
    FakeRerankProvider,
    make_test_settings,
)


class AnswerClient:
    def __init__(self):
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": "Attention connects queries, keys and values [E1].",
                "_atlasflow_finish_reason": "stop"}


def setup(tmp_path):
    text = "Attention connects each query to relevant key and value representations."
    path = tmp_path / "source.txt"
    path.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    gateway = FakeModelGateway()
    gateway.client = AnswerClient()
    gateway.model = "test/model"
    settings = make_test_settings()
    container = build_container(settings, providers=ProviderBundle(model=gateway,
        embedding=FakeEmbeddingProvider(), reranker=FakeRerankProvider()))
    container.evaluation_v2.datasets = [{"dataset_id": "test-rag-v2", "kind": "rag", "version": "2",
        "name": "Test evidence", "space": "user-default", "scoring_version": "rag-evidence-v2",
        "corpus_manifest_hash": digest, "source_content_hashes": {"attention": digest},
        "cases": [{"case_id": "R01", "query": "Attention", "requires_knowledge": True,
            "gold_status": "verified", "answerable": True, "expected_sources": [{
                "source_id": "attention", "content_hash": digest, "source_path": str(path),
                "anchor": "query to relevant key"}]}]}]
    plan = {"kind": "rag", "protocol": "rag-retrieval-v2", "dataset_id": "test-rag-v2",
        "mode": "live", "confirm_live": True, "space": "user-default", "case_ids": ["R01"],
        "variants": [{"variant_id": "lexical", "name": "Keywords", "retrieval_limit": 5,
                      "retrieval_tuning": {"vector_weight": 0, "lexical_weight": 1,
                                           "rerank_enabled": False, "query_expansion_enabled": False}}]}
    return container, gateway.client, settings, text, plan


def wait(client, identity):
    for _ in range(300):
        response = client.get(f"/api/v1/evaluations/rag/experiments/{identity}")
        assert response.status_code == 200, response.text
        record = response.json()
        if record["status"] not in {"planned", "running"}:
            return record
        time.sleep(0.01)
    raise AssertionError("isolated experiment did not finish")


def upload(client, text):
    result = client.post("/api/v1/documents", json={"title": "Attention", "source_id": "attention",
                                                      "content": text})
    assert result.status_code == 200, result.text


def test_rag_live_artifact_details_replay_rejection_and_workspace_isolation(tmp_path):
    container, chat, settings, text, plan = setup(tmp_path)
    with TestClient(create_app(settings=settings, container=container)) as client:
        upload(client, text)
        checked = client.post("/api/v1/evaluations/rag/preflight", json=plan)
        assert checked.status_code == 200 and checked.json()["ready"], checked.text
        result = client.post("/api/v1/evaluations/rag/experiments", json=plan)
        assert result.status_code == 202, result.text
        actual = wait(client, result.json()["experiment_id"])
        assert actual["status"] == "completed", actual
        row = actual["results"][0]
        assert row["metrics"]["source_coverage_at_5"]["value"] == 1
        assert row["metrics"]["anchor_coverage_at_5"]["value"] == 1
        assert "answer_correctness" not in row["metrics"]
        assert actual["budget_ledger"]["http_attempts"] == 0
        artifact_url = f"/api/v1/evaluations/rag/experiments/{actual['experiment_id']}/artifacts/{row['input_id']}"
        detail = client.get(artifact_url)
        assert detail.status_code == 200 and "relevant key" in detail.text
        replay_plan = {**plan, "mode": "replay", "confirm_live": False}
        replay = client.post("/api/v1/evaluations/rag/experiments", json=replay_plan)
        assert replay.status_code == 409, replay.text
        assert client.post("/api/v1/evaluations/rag/preflight", json=replay_plan).status_code == 422
        assert client.get("/api/v1/evaluations/rag/inputs").status_code == 404
        assert client.post("/api/v1/evaluations/rag/inputs", json={}).status_code == 404
        assert client.get("/api/v1/runs").json()["total"] == 0
        assert client.get(f"/api/v1/evaluations/agent/experiments/{actual['experiment_id']}").status_code == 404
        assert not chat.calls


def test_rag_answer_is_generated_fresh_on_every_run(tmp_path):
    container, chat, settings, text, plan = setup(tmp_path)
    plan["protocol"] = "rag-answer-v2"
    with TestClient(create_app(settings=settings, container=container)) as client:
        upload(client, text)
        result = client.post("/api/v1/evaluations/rag/experiments", json=plan)
        assert result.status_code == 202, result.text
        actual = wait(client, result.json()["experiment_id"])
        assert actual["status"] == "completed", actual
        row = actual["results"][0]
        assert len(chat.calls) == 1
        assert row["metrics"]["answer_present"]["value"] is True
        assert row["metrics"]["citation_validity"]["value"] == 1
        assert row["metrics"]["answer_correctness"]["value"] is None
        replay = client.post("/api/v1/evaluations/rag/experiments",
                             json={**plan, "mode": "replay", "confirm_live": False})
        assert replay.status_code == 409, replay.text
        assert len(chat.calls) == 1
        fresh = client.post("/api/v1/evaluations/rag/experiments", json=plan)
        repeated = wait(client, fresh.json()["experiment_id"])
        assert repeated["status"] == "completed"
        assert repeated["results"][0]["input_id"] != row["input_id"]
        assert len(chat.calls) == 2
        assert client.get("/api/v1/runs").json()["total"] == 0


def test_rag_preflight_needs_no_paid_confirmation_but_create_does(tmp_path):
    container, chat, settings, text, plan = setup(tmp_path)
    plan.pop("mode")
    plan["confirm_live"] = False
    with TestClient(create_app(settings=settings, container=container)) as client:
        upload(client, text)
        response = client.post("/api/v1/evaluations/rag/preflight", json=plan)
        assert response.status_code == 200 and response.json()["ready"], response.text
        response = client.post("/api/v1/evaluations/rag/experiments", json=plan)
        assert response.status_code == 409 and "confirm_live" in response.text
        assert client.get("/api/v1/evaluations/rag/experiments").json() == []
        assert not chat.calls


def test_legacy_history_is_read_only_and_cross_domain_contracts_rejected(tmp_path):
    container, _, settings, _, plan = setup(tmp_path)
    with TestClient(create_app(settings=settings, container=container)) as client:
        assert client.get("/api/v1/evaluations/legacy/experiments").json() == []
        assert client.post("/api/v1/evaluations/legacy/experiments", json=plan).status_code in {404, 405}
        assert client.post("/api/v1/evaluations/agent/preflight", json=plan).status_code == 422
        datasets = client.get("/api/v1/evaluations/rag/datasets").json()
        assert len(datasets) == 1 and datasets[0]["kind"] == "rag"
        settings = client.get("/api/v1/evaluations/agent/settings").json()
        assert settings["budget"]["case_timeout_seconds"] == 600


def test_actual_api_pairs_two_rag_groups_without_inventing_a_quality_gain(tmp_path):
    container, chat, settings, text, plan = setup(tmp_path)
    baseline = {**plan["variants"][0], "variant_id": "baseline"}
    # Identical controls deliberately establish that the UI must not invent a gain.
    candidate = {**baseline, "variant_id": "candidate", "name": "Repeated control"}
    plan["variants"] = [baseline, candidate]
    with TestClient(create_app(settings=settings, container=container)) as client:
        upload(client, text)
        started = client.post("/api/v1/evaluations/rag/experiments", json=plan)
        assert started.status_code == 202, started.text
        experiment = wait(client, started.json()["experiment_id"])
        endpoint = f"/api/v1/evaluations/rag/experiments/{experiment['experiment_id']}"
        response = client.get(endpoint + "/comparison?baseline_variant_id=baseline")
        assert response.status_code == 200, response.text
        comparison = response.json()
        assert comparison["baseline_variant_id"] == "baseline"
        anchor = comparison["comparisons"][0]["metrics"]["anchor_coverage_at_5"]
        assert anchor["paired"] == anchor["planned"] == 1
        assert anchor["baseline_mean"] == anchor["candidate_mean"] == 1
        assert anchor["mean_delta"] == 0 and anchor["tied"] == 1
        assert anchor["improved"] == anchor["regressed"] == 0
        posted = client.post(endpoint + "/compare", json={"baseline_variant_id": "baseline"})
        assert posted.status_code == 200 and posted.json() == comparison
        assert client.get(endpoint.replace("/rag/", "/agent/") + "/comparison").status_code == 404
        assert client.get("/api/v1/runs").json()["total"] == 0 and not chat.calls
