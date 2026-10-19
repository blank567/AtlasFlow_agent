"""Warnings retain a complete report, without claiming factual/quality acceptance."""

from copy import deepcopy

import pytest
from atlasflow.agents.contracts import (
    DraftVersion,
    QualityDecision,
    QualityRoute,
    ReviewIssue,
    Severity,
)
from atlasflow.agents.report_publication import build_warning_publication, usable_manuscript
from atlasflow.agents.report_skill import report_prose
from atlasflow.agents.workflow import ResearchWorkflow
from atlasflow.schemas import Evidence, RunStatus

from backend.tests.test_cumulative_reports import CumulativeConversationModel, update_context
from backend.tests.test_report_checkpoint_types import discard_event, restored_state

BODY = (
    "## 摘要\n\n山水与文化可结合阅读[1]。\n\n"
    "## 自然景观\n\n湖畔步道与山谷观景台属于旧稿保留内容[1]。\n\n"
    "## 历史文化\n\n某寺始建于1234年，来源仍待核对[1]。\n\n"
    "文字题咏是本次新增文化线索[1]。\n\n"
    "## 结论\n\n可结合山水与文化安排参观[1]。\n\n"
    "## 局限\n\n具体年代仍需核验。\n\n## 参考文献\n\n旧稿目录。"
)


def source(key="base"):
    return Evidence(
        id=key,
        source_id=key,
        title="本地合成资料",
        content="湖畔步道与文字题咏的合成材料。",
        uri=f"https://example.test/{key}",
    )


def draft(content=BODY, version=2):
    return DraftVersion(
        version=version,
        plan_version=1,
        content=content,
        based_on_task_ids=["culture-1", "culture-2"],
        citation_source_urls=[source().uri],
    )


def problem(code="unsupported_claim", severity=Severity.CRITICAL, message=None):
    return ReviewIssue(
        severity=severity,
        code=code,
        task_id=None,
        message=message or "历史文化中“某寺始建于1234年”尚无对应来源。",
        recommendation="核对年代原文或暂不作为确定事实使用。",
    )


def publish(candidate=None, **kwargs):
    return build_warning_publication(
        candidate or draft(),
        [source()],
        [],
        reason="修订预算用尽，保留完整报告。",
        version=3,
        **kwargs,
    )


def test_critical_claim_moves_to_limitations_while_supported_body_and_scope_survive():
    report = publish(issues=[problem()]).content
    assert all(
        heading in report for heading in ["## 自然景观", "## 历史文化", "## 结论", "## 局限"]
    )
    assert "\n湖畔步道与山谷观景台" in report  # Unaffected body is not erased/quarantined.
    main, limits = report.split("\n\n## 局限\n\n", 1)
    assert "某寺始建于1234年" not in main
    assert "> 某寺始建于1234年" in report
    assert "### 待核实事项" in limits and "未获得完整质量验收" in limits
    assert "文字题咏是本次新增文化线索" in report
    assert "已获取资料摘录" not in report and "### 核验说明" in limits
    assert "待核实陈述" not in report and "> 交付说明" not in main


def test_annotations_do_not_require_blank_lines_after_headings():
    report = publish(
        draft("## 历史文化\n某寺始建于1234年[1]。\n## 结论\n尚待核验。"), issues=[problem()]
    ).content
    assert report.startswith("## 历史文化\n") and "\n## 结论\n" in report
    assert "> ## 历史文化" not in report and "> 某寺始建于1234年" in report
    assert "某寺始建于1234年" not in report.split("\n\n## 局限\n\n")[0]


@pytest.mark.parametrize("withhold", [False, True])
def test_unlocated_critical_or_untrusted_review_preserves_body_and_limits_conclusions(withhold):
    report = publish(
        issues=[] if withhold else [problem(message="存在未能准确定位的重大事实冲突。")],
        withhold_conclusions=withhold,
    ).content
    main, limits = report.split("\n\n## 局限\n\n", 1)
    assert "湖畔步道与山谷观景台" in main
    assert "文字题咏是本次新增文化线索" in main
    assert "可结合山水与文化安排参观" not in main
    assert "摘要与结论不作确定性承诺" in limits
    assert "整体按待核验材料保留" not in report


def test_omission_warning_is_reported_without_erasing_or_quarantining_existing_material():
    report = publish(
        issues=[problem(code="missing_limitation", message="局限未说明材料的时效范围。")]
    ).content
    assert "\n湖畔步道与山谷观景台" in report
    assert "重点核验：局限未说明材料的时效范围" in report
    assert "### 待核实事项" not in report


@pytest.mark.parametrize("token", ["[999]", "[deadbeef]", "[S1]", "[task:T1]"])
def test_unknown_references_are_removed_not_replaced_by_fabricated_evidence(token):
    report = publish(
        draft(f"## 历史文化\n\n某寺的年代{token}尚待核验。\n\n## 结论\n\n资料有限。")
    ).content
    assert token not in report_prose(report)
    assert "> 某寺的年代尚待核验" in report
    assert "### 待核实事项" in report
    assert "某寺的年代" not in report.split("\n\n## 局限\n\n")[0]


def test_source_order_and_old_navigation_numbers_survive_later_evidence():
    from atlasflow.schemas import ToolCallRecord

    navigation = "https://uri.amap.com/navigation?from=a&to=b"
    candidate = draft("## 摘要\n\n导航见[2]，旧来源见[1]。\n\n## 结论\n\n仅为参考。")
    candidate.citation_source_urls = [source().uri, navigation]
    call = ToolCallRecord(
        call_id="navigation",
        tool_name="map_route",
        arguments={},
        success=True,
        duration_ms=1,
        navigation_urls=[navigation],
    )
    report = build_warning_publication(
        candidate, [source(), source("review")], [call], reason="核验提示。", version=3
    )
    assert report.citation_source_urls == [source().uri, navigation, source("review").uri]
    assert "[2] 高德地图导航" in report.content and "旧来源见[1]" in report.content


def test_code_and_inline_examples_remain_intact_while_invalid_body_references_are_removed():
    code = '```python\nprint("[999]")\n```'
    report = publish(
        draft("## 说明\n\n" + code + "\n\n使用`[deadbeef]`仅为代码示例。\n\n正文[999]需核对。")
    ).content
    assert code in report and "`[deadbeef]`" in report
    assert "正文需核对" in report and "[999]" not in report_prose(report)


def test_existing_draft_and_review_objects_are_never_mutated_or_given_a_fake_pass():
    candidate = draft()
    issue = problem()
    original = deepcopy((candidate.model_dump(), issue.model_dump()))
    result = publish(candidate, issues=[issue])
    assert (candidate.model_dump(), issue.model_dump()) == original
    assert result.version == 3 and candidate.version == 2
    assert result.based_on_task_ids == candidate.based_on_task_ids


def test_review_citations_do_not_create_new_bibliography_entries():
    report = publish(
        issues=[problem(message="“某寺始建于1234年”所引[999]与[deadbeef]不可核验。")]
    ).content
    assert "[999]" not in report_prose(report) and "[deadbeef]" not in report_prose(report)
    assert "引用999" in report and "内部引用标记" in report


def test_missing_baseline_is_kept_as_labelled_historical_material_not_new_current_facts():
    context = update_context()
    report = publish(
        draft("## 历史文化\n\n新文化线索[1]。"),
        report_update=context,
        missing_baseline_sections=["B1"],
    ).content
    assert "湖畔步道" in report and "新文化线索" in report
    assert "来源报告补充（历史基线）" in report and "不是本轮已确认结论" in report
    assert "预算3000元" not in report  # B2 was not missing, so do not reinsert obsolete budget.


@pytest.mark.asyncio
async def test_finalizer_uses_only_latest_matching_review_not_fixed_issues_from_earlier_draft():
    workflow = ResearchWorkflow(model=CumulativeConversationModel(), event_sink=discard_event)
    state = restored_state()
    state.pop("report_update")
    state["evidence"] = [source()]
    state["draft_versions"] = [draft(version=1), draft()]
    old = QualityDecision(
        decision=QualityRoute.REVISE,
        plan_version=1,
        draft_version=1,
        score=40,
        rationale="旧稿存在尚未定位的事实冲突。",
        issues=[problem(message="旧稿重大事实冲突。")],
        revision_instructions=["修正旧稿。"],
    )
    latest = QualityDecision(
        decision=QualityRoute.REVISE,
        plan_version=1,
        draft_version=2,
        score=59,
        rationale="最新稿只需补充局限。",
        issues=[
            problem(
                code="missing_limitation", severity=Severity.WARNING, message="补充局限的时效说明。"
            )
        ],
        revision_instructions=["补充时效说明。"],
    )
    state.update(
        quality_history=[old, latest],
        requested_final_status=RunStatus.COMPLETED_WITH_WARNINGS.value,
    )
    result = await workflow._finalizer(state)
    assert result["final_status"] == RunStatus.COMPLETED_WITH_WARNINGS.value
    assert "\n湖畔步道与山谷观景台" in result["report"]
    assert "旧稿重大事实冲突" not in result["report"]
    assert "### 待核实事项" not in result["report"]
    assert [item.score for item in state["quality_history"]] == [40, 59]
    assert len(state["draft_versions"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("has_older_quality", [False, True])
async def test_unreviewed_newer_draft_does_not_inherit_an_older_draft_quality_pass(
    has_older_quality,
):
    workflow = ResearchWorkflow(model=CumulativeConversationModel(), event_sink=discard_event)
    state = restored_state()
    state.pop("report_update")
    state["evidence"] = [source()]
    state.update(
        draft_versions=[draft()],
        delivery_fallback_reason="新稿审查未完成。",
        requested_final_status=RunStatus.COMPLETED_WITH_WARNINGS.value,
        quality_history=[
            QualityDecision(
                decision=QualityRoute.ACCEPT,
                plan_version=1,
                draft_version=1,
                score=90,
                rationale="仅上一稿通过验收。",
            )
        ]
        if has_older_quality
        else [],
    )
    result = await workflow._finalizer(state)
    main, limits = result["report"].split("\n\n## 局限\n\n", 1)
    assert "湖畔步道与山谷观景台" in main
    assert "摘要与结论不作确定性承诺" in limits
    assert result["final_status"] == RunStatus.COMPLETED_WITH_WARNINGS.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content", ["...", "## 摘要\n\n## 结论", "## 参考文献\n\n[1] 不应作为研究正文。"]
)
async def test_empty_or_headings_only_manuscript_uses_findings_instead_of_failing(content):
    assert not usable_manuscript(content)
    with pytest.raises(ValueError, match="empty manuscript"):
        publish(draft(content))
    workflow = ResearchWorkflow(model=CumulativeConversationModel(), event_sink=discard_event)
    state = restored_state()
    state.pop("report_update")
    state.update(
        draft_versions=[draft(content)],
        delivery_fallback_reason="没有可用成稿。",
        requested_final_status=RunStatus.COMPLETED_WITH_WARNINGS.value,
    )
    output = await workflow._finalizer(state)
    assert output["final_status"] == RunStatus.COMPLETED_WITH_WARNINGS.value
    assert "## 研究发现" in output["report"] and "文化参观" in output["report"]
