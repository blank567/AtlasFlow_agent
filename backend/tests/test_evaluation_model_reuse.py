"""Configured chat choices and shared Judge models; no real provider requests."""
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from atlasflow.bootstrap import ProviderBundle, build_container
from atlasflow.config import Settings, configured_chat_models, resolve_evaluation_judge_model
from atlasflow.evaluation.rag.contracts import RagGenerator
from atlasflow.evaluation_bindings_v2 import build_v2_judge
from backend.tests.fakes import FakeEmbeddingProvider, FakeModelGateway, FakeRerankProvider, make_test_settings


PRIMARY = "~deepseek/deepseek-flash-latest"


@pytest.mark.parametrize("override, expected", [("", PRIMARY), ("  ", PRIMARY),
                                                ("other/judge", "other/judge")])
def test_judge_model_follows_primary_unless_overridden(override, expected):
    settings = Settings(_env_file=None, llm_model=PRIMARY, evaluation_judge_model=override)
    assert resolve_evaluation_judge_model(settings) == expected


def test_configured_model_choices_are_trimmed_unique_and_include_primary():
    settings = Settings(_env_file=None, llm_model=PRIMARY,
                        llm_model_options=[PRIMARY, "other/model", " other/model "])
    assert configured_chat_models(settings) == [PRIMARY, "other/model"]
    assert configured_chat_models(settings, "injected/model") == ["injected/model", PRIMARY, "other/model"]
    assert RagGenerator(model=PRIMARY).model == PRIMARY


@pytest.mark.parametrize("identifier", ["", "https://other.invalid/v1", "bad model", "../model"])
def test_model_choices_reject_blank_urls_and_invalid_identifiers(identifier):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_model_options=[identifier])
    with pytest.raises(ValidationError):
        RagGenerator(model=identifier)


def test_disabled_judge_creates_no_client(monkeypatch):
    def unexpected(**_):
        raise AssertionError("Disabled Judge must not construct a client")
    monkeypatch.setattr("atlasflow.evaluation_bindings_v2.OpenRouterClient", unexpected)
    settings = make_test_settings().model_copy(update={"llm_model": PRIMARY, "evaluation_judge_enabled": False})
    assert build_v2_judge(settings) is None


@pytest.mark.parametrize("override", ["", "other/judge"])
async def test_both_judges_use_effective_model_and_v2_records_actual_identity(monkeypatch, override):
    requests, clients = [], []

    class Client:
        def __init__(self, **kwargs):
            clients.append(kwargs)
            self.legacy = kwargs.get("app_name") == "AtlasFlow evaluation Judge"

        async def chat(self, **kwargs):
            requests.append(kwargs)
            content = ({"answer_correctness": .8, "citation_support": .9}
                       if self.legacy else {"grades": [{
                           "criterion_id": "faithfulness", "value": 1,
                           "reason": "Supported by E1", "evidence_refs": ["E1"]}]})
            return {"content": json.dumps(content), "_atlasflow_finish_reason": "stop"}

    monkeypatch.setattr("atlasflow.evaluation_bindings.OpenRouterClient", Client)
    monkeypatch.setattr("atlasflow.evaluation_bindings_v2.OpenRouterClient", Client)
    settings = make_test_settings().model_copy(update={
        "llm_model": PRIMARY, "llm_model_options": ["other/model"],
        "evaluation_judge_enabled": True, "evaluation_judge_model": override,
        "evaluation_judge_api_key": "", "evaluation_judge_base_url": "",
    })
    container = build_container(settings, providers=ProviderBundle(
        model=FakeModelGateway(), embedding=FakeEmbeddingProvider(), reranker=FakeRerankProvider()))
    expected = override or PRIMARY
    for kind in ("rag", "agent"):
        public = container.evaluation_v2.settings(kind)
        assert public["default_model"] == PRIMARY
        assert [model["id"] for model in public["models"]] == [PRIMARY, "other/model"]
        assert public["models"][0]["is_default"]
        assert public["judge_available"] and public["judge_model"] == expected
        assert public["judge_reuses_default_model"] is (not override)
        assert public["judge_limits"] == {"max_tokens": 4096, "max_reason_chars": 160}
        if kind == "rag":
            assert public["answer_defaults"]["max_tokens"] == 4096
            assert public["answer_defaults"]["prompt_version"] == "rag-answer-v2.1"
        assert settings.llm_api_key not in json.dumps(public)
        assert settings.llm_base_url not in json.dumps(public)
    assert clients and all(client["api_key"] == settings.llm_api_key for client in clients)
    assert all(client["base_url"] == settings.llm_base_url for client in clients)
    await container.evaluation.judge(SimpleNamespace(query="question", expected_facts=[], expected_sources=[]),
                                     SimpleNamespace(content={"answer": "answer"}))
    artifact = {"content": {"answer": "answer [E1]", "context_evidence": [
        {"evidence_id": "E1", "content": "evidence"}]}}
    result = await container.evaluation_v2.judge("rag-answer-v2", {"query": "question"}, artifact)
    assert [request["model"] for request in requests] == [expected, expected]
    assert [request["max_tokens"] for request in requests] == [4096, 4096]
    assert container.evaluation_v2.judge.identity["model"] == expected
    assert result["metrics"]["faithfulness"]["grader_id"] == expected
