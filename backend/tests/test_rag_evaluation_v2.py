"""RAG protocols score actual evidence prefixes and never infer answer quality."""
import json
from types import SimpleNamespace

import pytest
from atlasflow.evaluation.rag.contracts import RagExperimentPlan, load_rag_dataset
from atlasflow.evaluation.rag.metrics import evaluate_rag
from atlasflow.evaluation_bindings_v2 import build_v2_judge, generate_answer
from atlasflow.knowledge.domain import SearchResult
from pydantic import ValidationError

HASH = "a" * 64


def hit(identity="s1", text="irrelevant text", *, context=None, page=1):
    return {"chunk": {"id": identity + "-" + text[:8], "document_id": identity,
        "version_id": identity + "-v", "generation_id": "generation", "chunk_kind": "child",
        "title": identity, "content": text, "token_count": 10, "lexical_text": text,
        "page_number": page, "metadata": {"source_id": identity, "content_hash": HASH}},
        "context_excerpt": context}


def artifact(hits, *, depth=5, degraded=None):
    return {"content": {"query": "attention?", "hits": hits,
        "decision": {"status": "sufficient" if hits else "insufficient", "coverage": 1,
                     "evidence_count": len(hits)},
        "trace": {"profile_id": "test", "generation_id": "generation", "degraded": degraded or [],
                  "applied_parameters": {"result_limit": depth}, "duration_ms": 10}},
        "captured_retrieval_limit": depth}


def case(**changes):
    return {"case_id": "R1", "query": "attention?", "gold_status": "verified", "answerable": True,
            "expected_sources": [{"source_id": "s1", "content_hash": HASH, "page_number": 1,
                                  "anchor": "query key value"}], **changes}


def score(value, gold=None, **plan):
    return evaluate_rag("rag-retrieval-v2", gold or case(), value, {"cutoffs": [1, 3, 5], **plan})


def test_domain_contract_rejects_agent_fields_and_fake_answer_grading():
    base = {"variants": [{"variant_id": "baseline", "name": "关键词",
                          "retrieval_tuning": {"vector_weight": 0}}]}
    parsed = RagExperimentPlan.model_validate(base)
    assert parsed.kind == "rag" and parsed.mode == "live"
    for change in ({"kind": "agent"}, {"judge_enabled": True, "confirm_judge": True},
                   {"cutoffs": [10]}, {"repetitions": 2}, {"mode": "replay"},
                   {"input_refs": {"R01:baseline:1": "old-input"}}):
        with pytest.raises(ValidationError):
            RagExperimentPlan.model_validate({**base, **change})
    with pytest.raises(ValidationError):
        RagExperimentPlan.model_validate({"variants": [{**base["variants"][0], "policy": {}}]})
    answer = RagExperimentPlan.model_validate({**base, "protocol": "rag-answer-v2"})
    assert answer.variants[0].generator.prompt_version == "rag-answer-v2.1"
    assert answer.variants[0].generator.max_tokens == 4096


def test_source_hit_is_not_answer_evidence_hit():
    result = score(artifact([hit()]))["metrics"]
    assert result["source_coverage_at_5"]["value"] == 1
    assert result["anchor_coverage_at_5"]["value"] == 0
    assert result["anchor_mrr_at_5"]["value"] == 0
    assert result["ndcg_at_5"]["value"] is None
    assert "answer_correctness" not in result


def test_parent_context_does_not_raise_child_hit_score():
    result = score(artifact([hit(context="query key value")]))["metrics"]
    assert result["anchor_coverage_at_5"]["value"] == 0
    assert result["context_anchor_coverage_at_5"]["value"] == 1


def test_later_child_of_fifth_source_is_not_lost_and_cutoffs_are_real_prefixes():
    hits = [hit(f"s{i}") for i in range(1, 6)] + [hit("s5", "query key value")]
    gold = case(expected_sources=[{"source_id": "s5", "content_hash": HASH,
                                   "page_number": 1, "anchor": "query key value"}])
    metrics = score(artifact(hits, depth=10), gold, cutoffs=[5, 10])["metrics"]
    assert metrics["source_coverage_at_5"]["value"] == 1
    assert metrics["anchor_coverage_at_5"]["value"] == 0
    assert metrics["anchor_coverage_at_10"]["value"] == 1
    assert metrics["anchor_mrr_at_10"]["value"] == pytest.approx(1 / 6)


def test_capture_depth_differs_from_real_fewer_hits():
    low = score(artifact([hit(text="query key value")]), cutoffs=[10])["metrics"]
    assert low["anchor_coverage_at_10"]["value"] is None
    assert low["anchor_coverage_at_10"]["status"] == "insufficient_capture_depth"
    empty = score(artifact([], depth=10), cutoffs=[10])["metrics"]
    assert empty["anchor_coverage_at_10"]["value"] == 0


def test_degraded_scores_remain_diagnostics_not_clean_scores():
    result = score(artifact([hit(text="query key value")], degraded=["embedding_unavailable"]))
    assert result["metrics"]["anchor_coverage_at_5"]["value"] == 1
    assert result["metrics"]["anchor_coverage_at_5"]["status"] == "degraded"
    assert result["stage_status"]["retrieval"] == "degraded"


def test_draft_gold_and_unknown_source_are_not_zero():
    draft = score(artifact([]), case(gold_status="draft"))["metrics"]
    assert draft["anchor_coverage_at_5"]["value"] is None
    value = artifact([hit()])
    value["content"]["hits"][0]["chunk"]["metadata"] = {}
    unknown = score(value)["metrics"]
    assert unknown["anchor_coverage_at_5"]["value"] is None
    assert unknown["anchor_coverage_at_5"]["status"] == "source_identity_missing"


def test_explicit_binary_qrels_can_score_ndcg_without_multilevel_labels():
    hits = [hit("s2"), hit("s1", "query key value")]
    gold = case(qrels={hits[1]["chunk"]["id"]: 1}, qrels_status="verified")
    metrics = score(artifact(hits), gold)["metrics"]
    assert metrics["ndcg_at_5"]["value"] == pytest.approx(1 / 1.584962500721156)
    assert metrics["unjudged_fraction_at_5"]["value"] == 0.5


def test_answer_citation_format_is_not_semantic_support():
    source = artifact([hit(text="query key value")])
    source["content"] = {"retrieval": source["content"], "answer": "A [E1]. B [E99].",
                          "context_evidence": [{"evidence_id": "E1", "content": "query key value"}]}
    result = evaluate_rag("rag-answer-v2", case(), source, {"cutoffs": [5]})["metrics"]
    assert result["citation_validity"]["value"] == 0.5
    assert result["answer_correctness"]["value"] is None
    assert result["faithfulness"]["value"] is None


def test_partial_generation_preserves_retrieval_metrics():
    source = artifact([hit(text="query key value")])
    source["content"] = {"retrieval": source["content"], "generation_status": "not_completed"}
    result = evaluate_rag("rag-answer-v2", case(), source, {"cutoffs": [5]})
    assert result["metrics"]["anchor_coverage_at_5"]["value"] == 1
    assert result["metrics"]["answer_present"]["value"] is None
    assert result["stage_status"]["generation"] == "not_completed"


@pytest.mark.asyncio
async def test_generator_captures_only_exact_context_sent_to_the_model():
    class Client:
        async def chat(self, **kwargs):
            self.messages = kwargs["messages"]
            return {"content": "Evidence [E1]", "_atlasflow_finish_reason": "stop"}
    client = Client()
    result = SearchResult.model_validate(artifact([hit(text="x" * 2000)])["content"])
    generated = await generate_answer(client, "attention?", result,
        {"model": "configured-model", "max_context_chars": 1000, "max_tokens": 200,
         "prompt_version": "rag-answer-v2"})
    assert generated["citations"] == ["E1"]
    assert generated["context_evidence"][0]["truncated"]
    assert len(generated["context_evidence"][0]["content"]) == 1000
    assert json.loads(client.messages[1]["content"])["evidence"] == generated["context_evidence"]


def test_v2_labels_do_not_invent_reference_answers_or_modify_v1():
    value = load_rag_dataset()
    assert value["dataset_id"] == "llm-rag-v2"
    assert len(value["cases"]) == 40
    assert value["label_policy"] == "positive_evidence_only"
    assert all(c["reference_status"] == "missing" for c in value["cases"] if not c["reference_facts"])


@pytest.mark.asyncio
async def test_judge_rejects_duplicate_grades_and_keeps_partial_semantic_status(monkeypatch):
    grade = {"criterion_id": "faithfulness", "value": 1, "reason": "Supported by E1", "evidence_refs": ["E1"]}
    output = {"grades": [grade, grade]}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def chat(self, **kwargs):
            return {"content": json.dumps(output), "_atlasflow_finish_reason": "stop"}

    monkeypatch.setattr("atlasflow.evaluation_bindings_v2.OpenRouterClient", Client)
    settings = SimpleNamespace(evaluation_judge_enabled=True, evaluation_judge_model="independent-model",
        evaluation_judge_api_key="", llm_api_key="unused-test-key", evaluation_judge_base_url="",
        llm_base_url="https://configured.invalid/v1", provider_timeout_seconds=10, max_tool_retries=0,
        evaluation_judge_max_tokens=4096, evaluation_judge_max_reason_chars=160)
    judge = build_v2_judge(settings)
    source = {"content": {"answer": "Answer [E1]", "context_evidence": [
        {"evidence_id": "E1", "content": "Evidence"}]}}
    with pytest.raises(ValueError, match="duplicate criterion"):
        await judge("rag-answer-v2", case(), source)
    output["grades"] = [grade]
    result = await judge("rag-answer-v2", case(), source)
    assert result["metrics"]["faithfulness"]["value"] == 1
    assert result["stage_status"]["semantic_grading"] == "partial"
    output["grades"] = []
    assert (await judge("rag-answer-v2", case(), source))["stage_status"]["semantic_grading"] == "not_run"
