"""Report excerpts stay bounded while cumulative source identities remain visible."""

import re

import pytest
from atlasflow.agents.report_skill import (
    build_citation_catalog,
    build_report_context,
    render_academic_references,
)
from atlasflow.schemas import Evidence


def source(number: int, *, inherited: bool = True, **metadata) -> Evidence:
    return Evidence(
        id=f"source-{number}",
        source_id=f"source-{number}",
        title=f"Known source {number}",
        content=f"VERIFIED_EXCERPT_{number}.",
        uri=f"https://sources.example.test/doc/{number}",
        metadata={**({"origin_run_id": "base-run"} if inherited else {}), **metadata},
    )


def excerpts(context: str) -> dict[int, str]:
    """Read the detailed blocks, excluding entries in the compact complete index."""

    return {
        int(number): content
        for number, content in re.findall(
            r"^\[(\d+)\] [^\n]+\nURL: [^\n]+\n获取方式: [^\n]+\n材料片段: ([^\n]*)",
            context,
            re.MULTILINE,
        )
    }


def test_new_preferred_unesco_source_and_old_citation_both_have_excerpts():
    old = [source(number) for number in range(1, 81)]
    new = source(81, inherited=False).model_copy(update={
        "title": "UNESCO generic fixture",
        "uri": "https://whc.unesco.org/en/list/fixture",
        "content": "NEW_UNESCO_FIXTURE_EVIDENCE.",
    })
    evidence = [*old, new]
    order = [item.uri for item in old]
    context = build_report_context(
        evidence,
        [],
        source_order=order,
        preferred_source_urls=[new.uri],
        cited_report="The existing supported claim remains.[3]",
    )

    selected = excerpts(context)
    assert len(selected) == 30
    assert selected[3] == "VERIFIED_EXCERPT_3."
    assert selected[81] == "NEW_UNESCO_FIXTURE_EVIDENCE."
    assert "[81] UNESCO generic fixture" in context
    assert "仅有索引链接的来源不得用于推断未取得的新事实" in context
    assert "不得自行追加或重排编号" in context


def test_explicit_source_order_keeps_sparse_numbers_through_context_and_render():
    old = [source(number) for number in range(1, 81)]
    new = source(81, inherited=False)
    order = [item.uri for item in reversed(old)]
    evidence = [new, *old]
    context = build_report_context(
        evidence, [], source_order=order,
        preferred_source_urls=[new.uri], cited_report="Old claim.[3]",
    )
    assert excerpts(context)[3] == "VERIFIED_EXCERPT_78."
    assert excerpts(context)[81] == "VERIFIED_EXCERPT_81."
    catalog = build_citation_catalog(evidence, [], source_order=order)
    assert catalog[2].url == old[77].uri
    assert catalog[80].url == new.uri
    report = render_academic_references(
        "Old claim.[3] New supported claim.[81]", evidence, [], source_order=order
    )
    assert "[3] Known source 78[EB/OL]" in report
    assert "[81] Known source 81[EB/OL]" in report


def test_more_than_thirty_cited_and_preferred_sources_share_budget_and_complete_index():
    old = [source(number) for number in range(1, 81)]
    new = [source(number, inherited=False) for number in range(81, 121)]
    evidence = [*old, *new]
    context = build_report_context(
        evidence, [],
        preferred_source_urls=[item.uri for item in new],
        cited_report=" ".join(f"Existing claim.[{number}]" for number in range(1, 41)),
    )
    selected = excerpts(context)
    assert len(selected) == 30
    assert len(set(selected) & set(range(1, 41))) == 15
    assert len(set(selected) & set(range(81, 121))) == 15
    # Excerpt selection does not hide any valid citation identity, even when
    # neither important group can fit completely into the excerpt budget.
    for number, item in enumerate(evidence, 1):
        assert f"[{number}] {item.title} | URL: {item.uri}" in context
    assert "VERIFIED_EXCERPT_40." not in context
    assert "VERIFIED_EXCERPT_120." not in context


def test_current_evidence_precedes_inherited_prefix_in_preferred_urls():
    old = [source(number) for number in range(1, 81)]
    new = source(81, inherited=False)
    context = build_report_context(
        [*old, new], [],
        preferred_source_urls=[*(item.uri for item in old), new.uri],
        cited_report="Existing claim.[79]",
    )
    assert excerpts(context)[81] == new.content
    assert excerpts(context)[79] == old[78].content


def test_without_explicit_preferences_current_sources_do_not_starve_behind_citations():
    old = [source(number) for number in range(1, 81)]
    new = source(81, inherited=False)
    context = build_report_context(
        [*old, new], [],
        cited_report=" ".join(f"Existing claim.[{number}]" for number in range(1, 41)),
    )
    assert len(excerpts(context)) == 30
    assert 1 in excerpts(context) and 81 in excerpts(context)


def test_preferences_and_unknown_citations_cannot_manufacture_source_identities():
    evidence = [source(number) for number in range(1, 82)]
    unknown = "https://unknown.example.test/fabricated"
    context = build_report_context(
        evidence, [], preferred_source_urls=[unknown, evidence[-1].uri, evidence[-1].uri],
        cited_report="Known claim.[3] Unsupported citation.[999]",
    )
    assert unknown not in context and "[999]" not in context
    assert set(re.findall(r"URL: (https://[^\n]+)", context)) == {
        item.uri for item in evidence
    }
    assert 3 in excerpts(context) and 81 in excerpts(context)
    assert len(excerpts(context)) == 30


@pytest.mark.parametrize("origin", ["base-run", "", None])
def test_origin_tagged_fallback_is_not_treated_as_new_evidence(origin):
    old = [source(number, origin_run_id=origin) for number in range(1, 82)]
    new = source(82, inherited=False)
    context = build_report_context(
        [*old, new], [], cited_report=" ".join(f"Claim.[{n}]" for n in range(1, 41))
    )
    assert 82 in excerpts(context)
    assert len(excerpts(context)) == 30


def test_plain_small_report_and_empty_fallback_remain_compatible():
    evidence = [source(1), source(2, inherited=False)]
    context = build_report_context(evidence, [], fallback="Safe task summary.")
    assert context.startswith("引用目录（系统生成，编号固定；只能引用与论断语义相符的条目）：")
    assert excerpts(context) == {1: evidence[0].content, 2: evidence[1].content}
    assert "完整来源索引" not in context
    assert context.endswith("其他安全工具摘要：\nSafe task summary.")
    assert build_report_context([], [], fallback="Safe task summary.") == "Safe task summary."


def test_acquisition_labels_and_same_url_upgrades_survive_excerpt_selection():
    old = [source(number) for number in range(1, 81)]
    original = source(81, provider="http", content_kind="source_excerpt")
    searched_again = original.model_copy(update={
        "id": "search-81", "content": "LOWER_QUALITY_SEARCH_OVERVIEW.",
        "metadata": {"provider": "openrouter", "content_kind": "search_response_citation"},
    })
    fetched = original.model_copy(update={"content": "DIRECTLY_READ_EXCERPT."})
    evidence = [*old, searched_again, fetched]
    context = build_report_context(evidence, [], preferred_source_urls=[original.uri])
    catalog = build_citation_catalog(evidence, [])
    assert len(catalog) == 81 and catalog[-1].number == 81
    assert excerpts(context)[81] == "DIRECTLY_READ_EXCERPT."
    assert "LOWER_QUALITY_SEARCH_OVERVIEW" not in context
    assert "直接读取网页的原文片段（不等于阅读全文）" in context
    assert "获取方式未标注（不能据此声称已直接读取原网页）" in context
