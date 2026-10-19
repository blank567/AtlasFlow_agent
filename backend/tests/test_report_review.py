"""Offline review evidence, materiality and paragraph-local delivery regressions."""

from copy import deepcopy

import pytest
from atlasflow.agents.contracts import QualityRoute, ReviewIssue, Severity
from atlasflow.agents.gateway import _ISSUE_SCHEMA, OpenRouterModelGateway, _quality_schema
from atlasflow.agents.report_publication import locate_issue_paragraphs
from atlasflow.agents.report_quality import apply_report_audit, calibrate_review_scope
from atlasflow.agents.report_review import (
    REVIEW_EVIDENCE_CHAR_BUDGET,
    build_review_context,
    validate_review_locations,
)
from atlasflow.agents.report_skill import (
    build_citation_catalog,
    build_report_context,
    cited_numbers,
)
from atlasflow.agents.report_structure import paragraph_index, report_paragraphs, report_parts
from atlasflow.schemas import RunStatus

from backend.tests.fakes import FakeModelGateway, make_test_container
from backend.tests.test_presentation_acceptance import decision, presentation_settings
from backend.tests.test_quality_output_recovery import DRAFT, RESULTS, Client, verdict
from backend.tests.test_report_catalog_selection import source
from backend.tests.test_warning_publication import BODY, draft, problem, publish


def scoped_issue(scope="local", ids=("P0003",), code="unsupported_claim", severity=Severity.CRITICAL):
    return ReviewIssue(
        severity=severity, code=code, message="该处具体年代未核验。", recommendation="移入局限。",
        scope=scope, paragraph_ids=list(ids), source_numbers=[1],
    )


def test_review_keeps_all_used_sources_while_writer_keeps_its_top_thirty():
    evidence = [source(n) for n in range(1, 101)]
    report = "资料" + "".join(f"[{n}]" for n in range(1, 56))
    writer = build_report_context(evidence, [], cited_report=report)
    review = build_review_context(report, evidence, [])
    assert writer.count("材料片段:") == 30
    assert review.count("材料片段:") == 55
    for number in range(1, 56):
        assert f"VERIFIED_EXCERPT_{number}." in review
    assert "https://sources.example.test/doc/100" not in review


def test_inherited_source_forty_one_and_evidence_after_nine_hundred_chars_are_visible():
    evidence = [source(n) for n in range(1, 101)]
    evidence[40] = evidence[40].model_copy(update={"content": "背景" * 600 + "ACTUAL_SUPPORTED_TEXT"})
    context = build_review_context("旧稿主张[41]。", evidence, [])
    assert "ACTUAL_SUPPORTED_TEXT" in context
    assert "[41] Known source 41" in context and "已存片段完整展示" in context
    assert "https://sources.example.test/doc/1\n" not in context


def test_repair_includes_alternative_citation_without_renumbering_or_unrelated_upload():
    evidence = [source(n) for n in range(1, 301)]
    issue = scoped_issue().model_copy(update={
        "source_numbers": [256, 257], "message": "原句引用[256][257]不对应。",
        "recommendation": "根据已有材料改为[260]。",
    })
    context = build_review_context("局部陈述[256][257]。", evidence, [], issues=[issue])
    assert all(f"VERIFIED_EXCERPT_{n}." in context for n in [256, 257, 260])
    assert context.count("材料片段:") == 3
    assert "VERIFIED_EXCERPT_1." not in context and "VERIFIED_EXCERPT_300." not in context


def test_character_budget_covers_every_citation_and_discloses_partial_visibility():
    evidence = [source(n).model_copy(update={"content": "x" * 8_000}) for n in range(1, 201)]
    context = build_review_context("".join(f"[{n}]" for n in range(1, 201)), evidence, [])
    snippets = [line.removeprefix("材料片段: ") for line in context.splitlines() if line.startswith("材料片段: ")]
    assert len(snippets) == 200
    assert sum(map(len, snippets)) <= REVIEW_EVIDENCE_CHAR_BUDGET
    assert all(snippets) and context.count("已存片段被截断") == 200
    assert "不等于来源不支持" in context and "不表示读取过整个网页" in context


def test_unknown_sources_and_code_citations_do_not_manufacture_evidence():
    context = build_review_context("已有[1]，未知[999]。`[998]`是代码。", [source(1)], [])
    assert "不存在的引用编号：999" in context and "998" not in context
    assert context.count("材料片段:") == 1
    assert build_review_context("直接计算。", [], [], fallback="SAFE_SUMMARY").endswith("SAFE_SUMMARY")


def test_source_order_and_direct_read_upgrade_are_preserved():
    first, second = source(1), source(2)
    second = second.model_copy(update={"metadata": {"provider": "http", "content_kind": "source_excerpt"}})
    context = build_review_context("已有证据[1]。", [first, second], [], source_order=[second.uri, first.uri])
    assert "[1] Known source 2" in context and "VERIFIED_EXCERPT_2." in context
    assert "直接读取网页的原文片段" in context and "VERIFIED_EXCERPT_1." not in context


def test_paragraph_ids_are_stable_with_code_masking_and_distinguish_limitations():
    code = "```md\n## 假标题\n引用[999]\n```"
    report = "## 分析\n\n有效发现[1]。\n\n" + code + "\n\n## 局限\n\n日期待核验。\n\n## 参考文献\n\n[1] 来源。"
    paragraphs = report_paragraphs(report)
    assert [item.id for item in paragraphs] == ["P0001", "L0001"]
    assert paragraphs[-1].in_limitations and paragraphs[-1].section == "局限"
    assert "假标题" not in paragraph_index(report)
    assert report_paragraphs(report) == paragraphs


def test_quality_schema_requires_scope_and_locations_without_mutating_critic_schema():
    original = deepcopy(_ISSUE_SCHEMA)
    schema = _quality_schema()
    fields = schema["properties"]["verdict"]["anyOf"][1]["properties"]["issues"]["items"]
    assert {"scope", "paragraph_ids", "source_numbers"}.issubset(fields["required"])
    assert _ISSUE_SCHEMA == original and "scope" not in _ISSUE_SCHEMA["properties"]


@pytest.mark.parametrize("ids", [["P9999"], ["P0001", "P0001"]])
async def test_bad_location_uses_existing_bounded_json_repair(ids):
    bad = verdict(issues=[scoped_issue(ids=ids).model_dump(mode="json")], instructions=["按原文修改。"])
    good = verdict(issues=[scoped_issue(ids=["P0001"]).model_dump(mode="json")], instructions=["按原文修改。"])
    # Workflow-only reported_severity is not requested from the model.
    for response in (bad, good):
        response["verdict"]["issues"][0].pop("reported_severity")
    client = Client([bad, good])
    result = await OpenRouterModelGateway(client, "test").evaluate_report("检查报告", DRAFT, RESULTS)
    assert len(client.requests) == 2 and result.issues[0].paragraph_ids == ["P0001"]
    assert "P0001" in client.requests[0]["messages"][1]["content"]


def test_local_issue_requires_existing_location_and_unique_references():
    for update in ({"paragraph_ids": []}, {"source_numbers": [1, 1]}):
        reviewed = decision(route=QualityRoute.REVISE, issues=[scoped_issue().model_copy(update=update)])
        with pytest.raises(ValueError):
            validate_review_locations(reviewed, BODY)


def test_local_critical_is_a_repair_not_a_global_block_and_original_severity_is_retained():
    original = decision(score=77, route=QualityRoute.REPLAN, issues=[scoped_issue()])
    snapshot = deepcopy(original.model_dump())
    calibrated = calibrate_review_scope(original, BODY, relaxed_review=True)
    assert calibrated.decision is QualityRoute.REVISE and calibrated.score == 77
    assert calibrated.issues[0].severity is Severity.WARNING
    assert calibrated.issues[0].reported_severity is Severity.CRITICAL
    assert original.model_dump() == snapshot
    assert calibrate_review_scope(original, BODY, relaxed_review=False) == original


def test_noncore_uncertainty_already_in_limitations_can_pass_without_another_revision():
    original = decision(score=70, route=QualityRoute.REVISE, issues=[scoped_issue(ids=["L0001"])])
    calibrated = calibrate_review_scope(original, BODY, relaxed_review=True)
    final = apply_report_audit(calibrated, [], threshold=50, relaxed_review=True)
    assert final.decision is QualityRoute.ACCEPT and final.score == 70
    assert final.issues[0].severity is Severity.INFO and final.issues[0].reported_severity is Severity.CRITICAL


@pytest.mark.parametrize("scope,code", [
    ("core", "unsupported_claim"), ("unknown", "unsupported_claim"),
    ("local", "report_update_missing_content"), ("local", "report_missing_fresh_evidence"),
])
def test_true_core_or_unknown_blockers_are_not_waived(scope, code):
    reviewed = decision(route=QualityRoute.REPLAN, issues=[scoped_issue(scope, ["L0001"], code)])
    assert calibrate_review_scope(reviewed, BODY, relaxed_review=True).issues[0].severity is Severity.CRITICAL


def test_model_info_cannot_mask_a_deterministic_core_invariant_of_the_same_code():
    model_issue = scoped_issue(ids=["L0001"], code="report_missing_fresh_evidence", severity=Severity.INFO)
    audit_issue = model_issue.model_copy(update={"scope": "core", "severity": Severity.CRITICAL})
    reviewed = apply_report_audit(
        decision(issues=[model_issue]), [audit_issue], threshold=50, relaxed_review=True,
    )
    assert reviewed.decision is QualityRoute.REPLAN
    assert reviewed.issues[0].severity is Severity.CRITICAL
    assert reviewed.score == 65


async def test_model_cannot_overwrite_workflow_owned_original_severity():
    issue = scoped_issue(ids=["P0001"]).model_dump(mode="json")
    client = Client([
        verdict(issues=[issue], instructions=["按材料局部修正。"]),
        verdict("accept"),
    ])
    result = await OpenRouterModelGateway(client, "test").evaluate_report("检查报告", DRAFT, RESULTS)
    assert result.decision is QualityRoute.ACCEPT and len(client.requests) == 2
    assert "reported_severity is workflow-owned" in client.requests[1]["messages"][-1]["content"]


def test_structured_location_moves_only_its_paragraph_not_shared_source_or_abstract():
    report = publish(issues=[scoped_issue(ids=["P0003"]) ]).content
    main, limits, _ = report_parts(report)
    assert "某寺始建于1234年" not in main and "某寺始建于1234年" in limits
    assert "湖畔步道与山谷观景台" in main and "文字题咏" in main
    assert "山水与文化可结合阅读" in main


def test_legacy_ellipsis_markup_and_citation_formatting_locate_only_the_claim():
    report = "## 正文\n\n正常保留的景观描述[1]。\n\n**某地文化保护**已采用数字化手段建立建筑数据库，另有AR与VR展示[1]。\n\n## 局限\n\n部分细节未核实。"
    issue = problem(message="正文“某地文化保护……另有AR与VR展示[1]”尚无依据。")
    assert locate_issue_paragraphs(issue, report) == {"P0002"}
    main, limits, _ = report_parts(publish(draft(report), issues=[issue]).content)
    assert "正常保留的景观描述" in main and "建筑数据库" not in main and "建筑数据库" in limits


def test_legacy_long_paraphrase_requires_a_unique_source_bound_match():
    report = "## 正文\n\n某阁始建于明代，曾为两州的禅林之首，现辟为地方博物馆[1]。\n\n其他研究结果继续保留[1]。"
    issue = problem(message="正文“某阁始建于明代，曾为两州禅林之首，现辟为地方博物馆[1]”待核实。")
    assert locate_issue_paragraphs(issue, report) == {"P0001"}
    assert locate_issue_paragraphs(issue, report + "\n\n某阁始建于明代，曾为两州的禅林之首，现辟为地方博物馆[1]。") == set()


def test_unlocated_issue_never_quarantines_all_other_body_paragraphs():
    issue = scoped_issue(scope="unknown", ids=[]).model_copy(update={"message": "无法定位的原审查事项。"})
    main, limits, _ = report_parts(publish(issues=[issue]).content)
    assert "湖畔步道与山谷观景台" in main and "文字题咏" in main
    assert "未能定位到具体段落" in limits
    assert "原稿陈述整体" not in limits


def test_known_citation_catalog_is_unchanged_by_review_or_publication():
    evidence = [source(n) for n in range(1, 40)]
    snapshot = deepcopy([item.model_dump() for item in evidence])
    before = build_citation_catalog(evidence, [])
    build_review_context("可核对[31][35]。", evidence, [])
    assert before == build_citation_catalog(evidence, [])
    assert [item.model_dump() for item in evidence] == snapshot
    assert cited_numbers("可核对[31][35]。") == {31, 35}


async def test_local_body_repair_then_declared_limitation_completes_once_without_warning():
    class LocalRepair(FakeModelGateway):
        async def synthesize_report(self, *args, **kwargs):
            value = await super().synthesize_report(*args, **kwargs)
            return value.model_copy(update={"content": value.content.replace(
                "## 结论", "非核心年代示例尚需核对。\n\n## 结论",
            )})

        async def revise_report(self, *args, **kwargs):
            value = await super().revise_report(*args, **kwargs)
            return value.model_copy(update={"content": value.content.replace(
                "## 局限", "## 局限\n\n非核心年代示例不作为已确认事实。\n\n",
            )})

        async def evaluate_report(self, query, candidate, results, **kwargs):
            index = self._called("evaluate_report")
            location = next(p for p in report_paragraphs(candidate.content)
                            if "非核心年代示例" in p.text)
            return decision(
                score=77 if index == 1 else 70,
                route=QualityRoute.REPLAN if index == 1 else QualityRoute.REVISE,
                issues=[scoped_issue(ids=[location.id])],
            ).model_copy(update={"draft_version": candidate.version})

    model = LocalRepair()
    run = await make_test_container(model=model, settings=presentation_settings()).run_service.execute_and_wait(
        "核心答案完整，非核心细节明确写入局限。"
    )
    assert run.status is RunStatus.COMPLETED and not run.warnings and not run.error, run.error
    assert run.metrics.replan_count == 0 and run.metrics.revision_count == 1
    assert model.call_counts["evaluate_report"] == 2 and model.call_counts["revise_report"] == 1
    assert [value.score for value in run.quality_history] == [77, 70]
    assert run.quality_history[0].issues[0].reported_severity is Severity.CRITICAL
    assert run.quality_history[-1].decision is QualityRoute.ACCEPT
