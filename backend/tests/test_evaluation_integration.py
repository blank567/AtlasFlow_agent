"""The application wiring evaluates frozen knowledge without writing business Runs."""
import hashlib
import time

from atlasflow.evaluation.contracts import EvaluationCase, EvaluationDataset, SourceAnchor
from atlasflow.evaluation.dataset import DatasetRegistry
from atlasflow.main import create_app
from fastapi.testclient import TestClient

from backend.tests.fakes import make_test_container, make_test_settings


def test_live_capture_replay_and_compare_use_isolated_store(tmp_path):
    text = "Attention connects each query to relevant key and value representations."
    source = tmp_path / "attention.txt"
    source.write_text(text, encoding="utf-8")
    content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    dataset = EvaluationDataset(dataset_id="integration", version="1", name="Integration",
        space="user-default", corpus_manifest_hash=content_hash,
        source_content_hashes={"attention": content_hash}, cases=[EvaluationCase(
            case_id="R01", kind="rag", query="Attention", gold_status="verified",
            expected_sources=[SourceAnchor(source_id="attention", content_hash=content_hash,
                source_path="attention.txt", anchor="query to relevant key")])])
    container = make_test_container()
    container.evaluation.datasets = DatasetRegistry([dataset], root=tmp_path)
    arm = {"arm_id": "lexical", "name": "关键词", "result_limit": 5,
           "tuning": {"lexical_weight": 1, "vector_weight": 0,
                      "rerank_enabled": False, "query_expansion_enabled": False}}
    plan = {"dataset_id": "integration", "mode": "live", "confirm_live": True,
            "arms": [arm], "case_ids": ["R01"], "langsmith_sync": False}
    with TestClient(create_app(settings=make_test_settings(), container=container)) as client:
        uploaded = client.post("/api/v1/documents", json={"title": "Attention",
            "content": text, "source_id": "attention"})
        assert uploaded.status_code == 200
        checked = client.post("/api/v1/evaluations/preflight", json=plan)
        assert checked.status_code == 200 and checked.json()["ready"]
        assert checked.json()["estimated_provider_requests"] == 0
        created = client.post("/api/v1/evaluations", json=plan)
        assert created.status_code == 202
        initial = wait(client, created.json()["experiment_id"])
        assert initial["status"] == "completed"
        assert initial["results"][0]["metrics"]["source_recall_at_5"] == 1
        assert initial["budget_ledger"]["http_attempts"] == 0
        assert "evaluation_context" in initial["snapshot"]
        assert client.get("/api/v1/runs").json()["total"] == 0
        assert len(client.get("/api/v1/evaluations/inputs").json()) == 1
        replay_plan = {**plan, "mode": "replay", "confirm_live": False}
        replay = client.post("/api/v1/evaluations", json=replay_plan)
        assert replay.status_code == 202
        result = wait(client, replay.json()["experiment_id"])
        assert result["results"][0]["replayed"] is True
        assert result["results"][0]["metrics"] == initial["results"][0]["metrics"]
        comparison = client.post("/api/v1/evaluations/compare", json={
            "baseline_id": initial["experiment_id"], "candidate_id": result["experiment_id"]})
        assert comparison.status_code == 200
        csv = client.get(f"/api/v1/evaluations/{result['experiment_id']}/export?format=csv")
        assert csv.status_code == 200 and "R01" in csv.text
        assert client.get("/api/v1/runs").json()["total"] == 0


def wait(client, experiment_id):
    for _ in range(200):
        result = client.get(f"/api/v1/evaluations/{experiment_id}").json()
        if result["status"] not in {"planned", "running"}:
            return result
        time.sleep(0.01)
    raise AssertionError("experiment did not finish")
