"""Answer/Judge output budgets and source-backed v2 references; no live requests."""
import json
from copy import deepcopy
from hashlib import sha256

import pytest
from pydantic import ValidationError

from atlasflow.config import Settings
from atlasflow.evaluation.contracts import fingerprint
from atlasflow.evaluation.dataset import DatasetRegistry
from atlasflow.evaluation.rag.contracts import RagGenerator, load_rag_dataset
from atlasflow.evaluation.rag.references import ReferenceAnnotations, apply_reference_annotations, load_reference_annotations
from atlasflow.evaluation_bindings_v2 import JUDGE_PROMPT_VERSION, build_v2_judge, generate_answer
from atlasflow.knowledge.domain import SearchResult
from backend.tests.fakes import make_test_settings


@pytest.mark.parametrize("changes", [
    {"evaluation_judge_max_tokens": 255}, {"evaluation_judge_max_tokens": 16385},
    {"evaluation_judge_max_reason_chars": 39}, {"evaluation_judge_max_reason_chars": 601},
])
def test_output_budgets_validate_bounds(changes):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **changes)


def test_answer_and_judge_defaults_are_larger_and_separate():
    settings = Settings(_env_file=None)
    assert RagGenerator().max_tokens == 4096
    assert RagGenerator().prompt_version == "rag-answer-v2.1"
    assert settings.evaluation_judge_max_tokens == 4096
    assert settings.evaluation_judge_max_reason_chars == 160
    assert settings.evaluation_max_http_attempts == 60
    assert settings.evaluation_max_seconds == 1200


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
async def test_configurable_judge_budget_short_reasons_and_identity(monkeypatch, finish_reason):
    calls = []

    class Client:
        def __init__(self, **_):
            pass

        async def chat(self, **kwargs):
            calls.append(kwargs)
            return {"content": json.dumps({"grades": [{"criterion_id": key, "value": 1,
                "reason": "grounded " * 100, "evidence_refs": ["E1"]} for key in (
                    "answer_correctness", "reference_fact_coverage", "faithfulness", "citation_support")]}),
                "_atlasflow_finish_reason": finish_reason}

    monkeypatch.setattr("atlasflow.evaluation_bindings_v2.OpenRouterClient", Client)
    settings = make_test_settings().model_copy(update={"evaluation_judge_enabled": True,
        "evaluation_judge_max_tokens": 8192, "evaluation_judge_max_reason_chars": 64})
    judge = build_v2_judge(settings)
    case = {"query": "question", "reference_status": "verified", "reference_facts": ["fact"]}
    artifact = {"content": {"answer": "answer [E1]", "context_evidence": [
        {"evidence_id": "E1", "content": "fact"}]}}
    if finish_reason == "length":
        with pytest.raises(ValueError, match="Judge response truncated"):
            await judge("rag-answer-v2", case, artifact)
    else:
        result = await judge("rag-answer-v2", case, artifact)
        assert len(result["metrics"]) == 4
        assert result["stage_status"]["semantic_grading"] == "completed"
        assert len(result["diagnostics"]["judge_reason_shortened"]) == 4
        assert all(len(metric["reason"]) == 64 for metric in result["metrics"].values())
        assert all(metric["grader_version"] == JUDGE_PROMPT_VERSION for metric in result["metrics"].values())
    assert calls[0]["max_tokens"] == 8192
    assert "64字符" in calls[0]["messages"][0]["content"]
    shape = json.loads(calls[0]["messages"][0]["content"].split("结构：", 1)[1])
    assert [grade["criterion_id"] for grade in shape["grades"]] == [
        "answer_correctness", "reference_fact_coverage", "faithfulness", "citation_support"]
    assert judge.identity["max_tokens"] == 8192 and judge.identity["max_reason_chars"] == 64


async def test_answer_keeps_truncation_guard_and_does_not_receive_reference_labels():
    calls = []

    class Client:
        async def chat(self, **kwargs):
            calls.append(kwargs)
            return {"content": "unfinished answer", "_atlasflow_finish_reason": "length"}

    from backend.tests.test_rag_evaluation_v2 import artifact, hit
    result = SearchResult.model_validate(artifact([hit(text="source evidence")])["content"])
    generator = RagGenerator().model_dump(mode="json")
    generator["model"] = "configured/model"
    case = next(case for case in load_rag_dataset()["cases"] if case["case_id"] == "R01")
    with pytest.raises(ValueError, match="answer generation was truncated"):
        await generate_answer(Client(), case["query"], result, generator)
    assert calls[0]["max_tokens"] == 4096
    assert "不扩写综述" in calls[0]["messages"][0]["content"]
    payload = json.loads(calls[0]["messages"][1]["content"])
    assert set(payload) == {"query", "evidence"}
    assert case["reference_answer"] not in calls[0]["messages"][1]["content"]


@pytest.mark.parametrize("prompt_version", ["rag-answer-v2", "rag-answer-v2.1"])
async def test_explicit_answer_budget_is_not_overridden_and_old_prompt_stays_compatible(prompt_version):
    calls = []

    class Client:
        async def chat(self, **kwargs):
            calls.append(kwargs)
            return {"content": "Answer [E1]", "_atlasflow_finish_reason": "stop"}

    from backend.tests.test_rag_evaluation_v2 import artifact, hit
    result = SearchResult.model_validate(artifact([hit(text="source evidence")])["content"])
    generator = RagGenerator(model="configured/model", max_tokens=1024,
                             prompt_version=prompt_version).model_dump(mode="json")
    answer = await generate_answer(Client(), "question", result, generator)
    assert calls[0]["max_tokens"] == answer["generator"]["max_tokens"] == 1024
    assert ("不扩写综述" in calls[0]["messages"][0]["content"]) is (prompt_version == "rag-answer-v2.1")


def test_references_upgrade_only_two_cases_without_modifying_v1():
    registry = DatasetRegistry()
    original = registry.get("llm-rag-v1").model_dump(mode="json")
    before = fingerprint(original)
    dataset = load_rag_dataset()
    by_id = {case["case_id"]: case for case in dataset["cases"]}
    assert dataset["version"] == "2.1" and len(dataset["cases"]) == 40
    assert {case["case_id"] for case in dataset["cases"] if case["reference_status"] == "verified"} == {"R01", "R04"}
    assert len(by_id["R01"]["reference_facts"]) == 3 and len(by_id["R04"]["reference_facts"]) == 2
    assert all(by_id[key]["reference_answer"] for key in ("R01", "R04"))
    assert fingerprint(registry.get("llm-rag-v1").model_dump(mode="json")) == before
    assert not any(case["expected_facts"] for case in original["cases"] if case["case_id"] in {"R01", "R04"})
    modified = deepcopy(dataset)
    modified["cases"][0]["reference_facts"][0] += " amended"
    assert fingerprint(modified) != fingerprint(dataset)


@pytest.mark.parametrize("changed", ["corpus", "query", "source", "case"])
def test_reference_annotations_reject_stale_corpus_queries_or_sources(changed):
    registry = DatasetRegistry()
    original = registry.get("llm-rag-v1").model_dump(mode="json")
    annotations = load_reference_annotations(registry.root).model_dump(mode="json")
    if changed == "corpus":
        annotations["corpus_manifest_hash"] = "0" * 64
    elif changed == "query":
        annotations["cases"][0]["query"] += " changed"
    elif changed == "source":
        annotations["cases"][0]["supports"][0]["content_hash"] = "0" * 64
    else:
        annotations["cases"][0]["case_id"] = "nonexistent"
    with pytest.raises(ValueError):
        apply_reference_annotations(original, ReferenceAnnotations.model_validate(annotations), "llm-rag-v1")


def test_every_curated_reference_anchor_matches_the_exact_primary_pdf():
    from pypdf import PdfReader
    registry = DatasetRegistry()
    dataset = registry.get("llm-rag-v1")
    annotations = load_reference_annotations(registry.root)
    sources = {source.source_id: source for case in dataset.cases for source in case.expected_sources}
    readers = {}
    for case in annotations.cases:
        for support in case.supports:
            path = registry.root / sources[support.source_id].source_path
            if not path.is_file():
                pytest.skip("original corpus PDFs are not distributed with source-only checkouts")
            if support.source_id not in readers:
                assert sha256(path.read_bytes()).hexdigest() == support.content_hash
                readers[support.source_id] = PdfReader(path)
            text = " ".join(readers[support.source_id].pages[support.page_number - 1].extract_text().split())
            assert " ".join(support.anchor.split()) in text


def test_invalid_later_reference_does_not_partially_upgrade_dataset():
    registry = DatasetRegistry()
    original = registry.get("llm-rag-v1").model_dump(mode="json")
    before = deepcopy(original)
    annotations = load_reference_annotations(registry.root).model_dump(mode="json")
    annotations["cases"][1]["query"] += " stale"
    with pytest.raises(ValueError, match="query/case"):
        apply_reference_annotations(original, ReferenceAnnotations.model_validate(annotations), "llm-rag-v1")
    assert original == before
