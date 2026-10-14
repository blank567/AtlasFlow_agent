from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest
from atlasflow.knowledge.retrieval.bilingual_query_expander import (
    BilingualQueryExpander,
    QueryExpansion,
)
from atlasflow.knowledge.retrieval.query_pipeline import QueryPipeline


async def test_normalization_is_separate_from_original_preserving_expansion():
    pipeline = QueryPipeline()
    original = "  attention 机制\n KV cache  "
    assert await pipeline.normalize(original) == "attention 机制 KV cache"
    expansion = await pipeline.expand(original)
    assert expansion.queries[0] == original
    assert expansion.matched_terms == ["attention", "kv_cache"]
    assert any("注意力机制" in query and "键值缓存" in query for query in expansion.queries[1:])
    assert expansion.method == "bilingual-terms-v1"


async def test_chinese_query_adds_english_without_extra_nested_concepts():
    expansion = await QueryPipeline().expand("自注意力机制和低秩适配的区别")
    assert expansion.matched_terms == ["self_attention", "lora"]
    assert expansion.queries == [
        "自注意力机制和低秩适配的区别",
        "自注意力机制和低秩适配的区别 self-attention; low-rank adaptation",
    ]


async def test_english_acronyms_add_long_names_and_chinese():
    expansion = await QueryPipeline().expand("RAG uses LoRA and KVCache")
    assert expansion.matched_terms == ["rag", "lora", "kv_cache"]
    assert expansion.queries == [
        "RAG uses LoRA and KVCache",
        "RAG uses LoRA and KVCache retrieval-augmented generation; low-rank adaptation; KV cache",
        "RAG uses LoRA and KVCache 检索增强生成; 低秩适配; 键值缓存",
    ]


async def test_exact_aliases_not_substrings_and_ascii_boundaries_not_chinese_boundaries():
    pipeline = QueryPipeline()
    for unrelated in ("paragraph flora", "pragmatic floral", "myRAG toolLoRA KVCacheable", "RAG_1"):
        expansion = await pipeline.expand(unrelated)
        assert expansion.queries == [unrelated]
        assert expansion.matched_terms == []
    expansion = await pipeline.expand("什么是RAG？怎么用LoRA微调？")
    assert expansion.matched_terms == ["rag", "lora", "fine_tuning"]


async def test_longest_phrase_does_not_match_attention_inside_self_attention():
    pipeline = QueryPipeline()
    for query in ("SELF ATTENTION", "self-attention", "自注意力", "多头注意力机制"):
        expansion = await pipeline.expand(query)
        assert len(expansion.matched_terms) == 1
        assert "attention" not in expansion.matched_terms
    assert (await pipeline.expand("supervised fine-tuning")).matched_terms == ["sft"]
    assert (await pipeline.expand("量化低秩适配")).matched_terms == ["qlora"]


async def test_url_paths_and_parameters_do_not_trigger_glossary_matches():
    pipeline = QueryPipeline()
    query = "阅读 https://example.test/rag?topic=LoRA 和 www.example.test/attention"
    result = await pipeline.expand(query)
    assert result.queries == [query]
    assert result.matched_terms == []
    query = "检索增强生成参考 https://example.test/retrieval-augmented-generation"
    result = await pipeline.expand(query)
    assert result.matched_terms == ["rag"]
    assert result.queries[1] == f"{query} retrieval-augmented generation"


async def test_proper_names_do_not_get_invented_or_overly_broad_translations():
    pipeline = QueryPipeline()
    for query in ("FlashAttention", "BM25", "最佳匹配算法"):
        result = await pipeline.expand(query)
        assert result.queries == [query]
    assert (await pipeline.expand("FlashAttention")).matched_terms == ["flash_attention"]
    assert (await pipeline.expand("BM25")).matched_terms == ["bm25"]
    assert (await pipeline.expand("最佳匹配算法")).matched_terms == []
    assert "flash_attention" not in (await pipeline.expand("闪存注意力")).matched_terms


async def test_disabled_does_not_invoke_expander():
    class NeverCalled:
        async def expand(self, query: str, *, limit: int) -> QueryExpansion:
            raise AssertionError("disabled expansion must not invoke the provider")

    expansion = await QueryPipeline(expander=NeverCalled()).expand("RAG", enabled=False)
    assert expansion == QueryExpansion(queries=["RAG"], matched_terms=[], method="disabled")


async def test_custom_async_expander_is_injectable():
    class CustomExpander:
        async def expand(self, query: str, *, limit: int) -> QueryExpansion:
            return QueryExpansion(
                queries=[query, f"{query} custom-{limit}"],
                matched_terms=["custom"],
                method="custom-v1",
            )

    expansion = await QueryPipeline(expander=CustomExpander()).expand("query", limit=3)
    assert expansion.queries == ["query", "query custom-3"]
    assert expansion.method == "custom-v1"


async def test_no_matches_and_identical_canonical_aliases_keep_single_query():
    pipeline = QueryPipeline()
    no_match = await pipeline.expand("如何参观黄山？")
    assert no_match.queries == ["如何参观黄山？"]
    assert no_match.matched_terms == []
    result = await pipeline.expand("attention mechanism 注意力机制")
    assert result.queries == ["attention mechanism 注意力机制"]
    assert result.matched_terms == ["attention"]


async def test_case_whitespace_and_duplicate_aliases_dedupe_concepts():
    expansion = await QueryPipeline().expand("RAG rag 检索增强生成 retrieval   augmented generation")
    assert expansion.matched_terms == ["rag"]
    keys = [" ".join(query.split()).casefold() for query in expansion.queries]
    assert len(keys) == len(set(keys))
    assert len(expansion.queries) == 2


@pytest.mark.parametrize("limit", [1, 2, 3, 4])
async def test_limit_counts_additional_variants_not_original(limit):
    expansion = await QueryPipeline().expand("RAG LoRA KVCache", limit=limit)
    assert len(expansion.queries) <= limit + 1
    assert expansion.queries[0] == "RAG LoRA KVCache"
    if limit == 1:
        assert len(expansion.queries) == 2


@pytest.mark.parametrize("limit", [0, 5, True, 1.5, "2"])
async def test_invalid_limit_is_explicit(limit):
    with pytest.raises(ValueError, match="between 1 and 4"):
        await QueryPipeline().expand("RAG", limit=limit)


async def test_expansion_length_is_bounded_but_original_is_never_truncated():
    pipeline = QueryPipeline()
    original = "RAG " + "x" * 4460
    result = await pipeline.expand(original)
    assert result.queries[0] == original
    assert all(len(query) <= 4500 for query in result.queries)
    oversized = "RAG " + "x" * 4500
    result = await pipeline.expand(oversized)
    assert result.queries == [oversized]
    assert result.matched_terms == []


async def test_match_budget_is_bounded_and_deterministic():
    pipeline = QueryPipeline()
    query = "RAG LoRA QLoRA SFT DPO RLHF PEFT KVCache MoE Transformer"
    first = await pipeline.expand(query)
    second = await pipeline.expand(query)
    assert first == second
    assert first.matched_terms == ["rag", "lora", "qlora", "sft", "dpo", "rlhf", "peft", "kv_cache"]


@pytest.mark.parametrize("query", ["", " \n\t "])
async def test_blank_queries_are_rejected(query):
    pipeline = QueryPipeline()
    with pytest.raises(ValueError, match="empty"):
        await pipeline.normalize(query)
    with pytest.raises(ValueError, match="empty"):
        await pipeline.expand(query)
    with pytest.raises(ValueError, match="empty"):
        await pipeline.expand(query, enabled=False)


def test_expansion_record_prevents_field_reassignment():
    result = QueryExpansion(queries=["original"], matched_terms=[])
    with pytest.raises(FrozenInstanceError):
        result.method = "mutated"


async def test_custom_glossary_adds_concepts_and_replaces_id(tmp_path):
    path = tmp_path / "terms.json"
    path.write_text(
        json.dumps(
            {"terms": [
                {"id": "graph_rag", "zh": ["图检索增强"], "en": ["GraphRAG"]},
                {"id": "lora", "zh": ["低秩适配", "领域适配"], "en": ["domain adaptation", "LoRA"]},
            ]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    expander = BilingualQueryExpander.from_file(path)
    result = await expander.expand("图检索增强和领域适配", limit=2)
    assert result.matched_terms == ["graph_rag", "lora"]
    assert "GraphRAG; domain adaptation" in result.queries[1]
    assert (await expander.expand("RAG", limit=2)).matched_terms == ["rag"]
    assert (await expander.expand("low-rank adaptation", limit=2)).matched_terms == []


async def test_duplicate_aliases_within_one_concept_are_deduplicated():
    expander = BilingualQueryExpander(
        terms=[{"id": "custom", "zh": ["领域术语", "领域术语"], "en": ["custom term", "CUSTOM   TERM"]}]
    )
    result = await expander.expand("CUSTOM term custom term", limit=2)
    assert result.matched_terms == ["custom"]
    assert result.queries == ["CUSTOM term custom term", "CUSTOM term custom term 领域术语"]


@pytest.mark.parametrize("payload", [
    [],
    {"terms": [], "other": True},
    {"terms": "RAG"},
    {"terms": [{"id": "custom", "zh": [], "en": ["custom"]}]},
    {"terms": [{"id": "UPPER", "zh": ["术语"], "en": ["custom"]}]},
    {"terms": [{"id": "custom", "zh": ["术语"], "en": [123]}]},
    {"terms": [{"id": "custom", "zh": ["术语"], "en": [" "]}]},
    {"terms": [{"id": "custom", "zh": ["术语"], "en": ["custom"], "extra": "ignored?"}]},
    {"terms": [
        {"id": "custom", "zh": ["术语"], "en": ["custom"]},
        {"id": "custom", "zh": ["词语"], "en": ["different"]},
    ]},
    {"terms": [{"id": "custom", "zh": ["术语"], "en": ["RAG"]}]},
])
def test_malformed_or_ambiguous_glossary_fails_explicitly(tmp_path, payload):
    path = tmp_path / "terms.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="bilingual glossary"):
        BilingualQueryExpander.from_file(path)


def test_invalid_json_and_oversized_file_are_not_silently_ignored(tmp_path):
    path = tmp_path / "terms.json"
    path.write_text('{"terms": [', encoding="utf-8")
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        BilingualQueryExpander.from_file(path)
    path.write_bytes(b"x" * (BilingualQueryExpander.MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        BilingualQueryExpander.from_file(path)


def test_glossary_term_and_alias_budgets_are_validated():
    with pytest.raises(ValueError, match="at most"):
        BilingualQueryExpander(terms=[{}] * (BilingualQueryExpander.MAX_TERMS + 1))
    with pytest.raises(ValueError, match="nonempty alias list"):
        BilingualQueryExpander(
            terms=[{"id": "custom", "zh": ["词语"], "en": ["x"] * 33}]
        )
    with pytest.raises(ValueError, match="invalid alias"):
        BilingualQueryExpander(
            terms=[{"id": "custom", "zh": ["词语"], "en": ["x" * 121]}]
        )


async def test_expand_never_reads_glossary_again(tmp_path, monkeypatch):
    path = tmp_path / "terms.json"
    path.write_text('{"terms": []}', encoding="utf-8")
    expander = BilingualQueryExpander.from_file(path)

    def forbidden_read(*args, **kwargs):
        raise AssertionError("retrieval expansion must not perform filesystem I/O")

    monkeypatch.setattr(type(path), "read_text", forbidden_read)
    assert (await expander.expand("RAG", limit=2)).matched_terms == ["rag"]
