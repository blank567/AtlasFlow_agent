from atlasflow.agents.report_skill import (
    academic_report_instructions,
    build_citation_catalog,
    build_report_context,
    cited_numbers,
    render_academic_references,
)
from atlasflow.schemas import Evidence, ToolCallRecord


def _evidence(source_id: str, title: str, url: str, content: str = "证据正文") -> Evidence:
    return Evidence(source_id=source_id, title=title, uri=url, content=content)


def test_project_skill_loads_report_depth_and_citation_contract() -> None:
    instructions = academic_report_instructions()

    assert "1,500–3,500" in instructions
    assert "[1]" in instructions
    assert "Do not emit `[task:T1]`" in instructions


def test_citation_catalog_deduplicates_urls_and_keeps_navigation_last() -> None:
    evidence = [
        _evidence("s1", "来源甲", "https://example.test/a"),
        _evidence("s2", "重复来源", "https://example.test/a"),
        _evidence("s3", "来源乙", "https://example.test/b"),
    ]
    records = [
        ToolCallRecord(
            tool_name="map_itinerary",
            agent="researcher",
            arguments={"mode": "walking"},
            success=True,
            duration_ms=10,
            navigation_urls=["https://uri.amap.com/navigation?from=a&to=b"],
        )
    ]

    catalog = build_citation_catalog(evidence, records)

    assert [item.number for item in catalog] == [1, 2, 3]
    assert [item.title for item in catalog] == [
        "来源甲",
        "来源乙",
        "高德地图导航（第 1 段）",
    ]
    assert catalog[-1].kind == "navigation"


def test_report_context_and_bibliography_use_only_verified_cited_numbers() -> None:
    long_excerpt = "甲" * 700 + "关键证据"
    evidence = [
        _evidence("s1", "来源甲", "https://example.test/a", long_excerpt),
        _evidence("s2", "来源乙", "https://example.test/b"),
    ]
    context = build_report_context(evidence, [])
    report = (
        "## 摘要\n\n结论由来源甲支持。[1]\n\n"
        "## 分析\n\n无效编号必须保留给质量门发现。[99]\n\n"
        "## 结论\n\n结论。\n\n"
        "## 局限\n\n仍有限制。\n\n"
        "## 来源\n\n模型自行编写的条目"
    )

    rendered = render_academic_references(report, evidence, [])

    assert "关键证据" in context
    assert cited_numbers(rendered) == {1, 99}
    assert "## 参考文献" in rendered and "## 来源" not in rendered
    assert "[1] 来源甲[EB/OL]. [访问链接](https://example.test/a)" in rendered
    assert "[2] 来源乙" not in rendered
    assert "[99]" in rendered.split("## 参考文献", 1)[0]
    assert "模型自行编写的条目" not in rendered
