"""Report-first delivery accepts useful answers and isolates verification gaps.

All data/models are offline fixtures. Neither a reference nor a high score here
constitutes a factual claim about the real world.
"""

from copy import deepcopy

import pytest
from atlasflow.agents.contracts import QualityDecision, QualityRoute, Severity
from atlasflow.agents.gateway import OpenRouterModelGateway, _quality_schema
from atlasflow.agents.report_publication import consolidate_report_limitations
from atlasflow.agents.report_quality import apply_report_audit, audit_report
from atlasflow.config import Settings
from atlasflow.schemas import RunStatus
from pydantic import ValidationError

from backend.tests.fakes import FakeModelGateway, make_test_container
from backend.tests.test_presentation_acceptance import decision, issue, presentation_settings
from backend.tests.test_quality_output_recovery import DRAFT, RESULTS, Client, verdict
from backend.tests.test_report_warning_regression import research
from backend.tests.test_warning_publication import BODY, draft, problem, publish


def test_default_threshold_is_fifty_in_settings_schema_and_gateway():
    assert Settings.model_fields["quality_threshold"].default == 50
    assert OpenRouterModelGateway(Client([]), "test").quality_threshold == 50
    assert _quality_schema()["properties"]["verdict"]["anyOf"][0]["properties"]["score"]["minimum"] == 50


@pytest.mark.parametrize("score", [50, 55, 59])
async def test_low_but_valid_score_keeps_actual_value_and_accepts_in_one_request(score):
    client = Client([verdict("accept", score=score)])
    reviewed = await OpenRouterModelGateway(client, "test").evaluate_report("核心回答", DRAFT, RESULTS)
    assert reviewed.score == score and reviewed.decision is QualityRoute.ACCEPT
    assert len(client.requests) == 1
    prompt = client.requests[0]["messages"][0]["content"]
    assert "至少50" in prompt and "report_limitation_note" in prompt
    assert "非核心未核实细节" in prompt and "关键无依据事实" in prompt


@pytest.mark.parametrize("threshold", [49, 101])
def test_invalid_threshold_cannot_disable_review(threshold):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, quality_threshold=threshold)
    with pytest.raises(ValueError, match="between 50 and 100"):
        OpenRouterModelGateway(Client([]), "test", quality_threshold=threshold)


@pytest.mark.parametrize("code", ["report_limitation_note", "report_optional_detail"])
def test_optional_notes_pass_without_a_fake_score_increase_or_issue_removal(code):
    original = decision(score=55, route=QualityRoute.REVISE, issues=[issue(code)])
    snapshot = original.model_dump()
    reviewed = apply_report_audit(original, [], threshold=50, relaxed_review=True)
    assert reviewed.decision is QualityRoute.ACCEPT and reviewed.score == 55
    assert reviewed.issues == original.issues and original.model_dump() == snapshot
    strict = apply_report_audit(original, [], threshold=80, relaxed_review=False)
    assert strict.decision is QualityRoute.REVISE


@pytest.mark.parametrize("code", ["report_unknown_citation", "report_unknown_source", "report_missing_source"])
def test_lower_standard_does_not_accept_fake_or_missing_references(code):
    reviewed = apply_report_audit(decision(score=55), [issue(code)], threshold=50, relaxed_review=True)
    assert reviewed.decision is QualityRoute.REVISE and reviewed.score == 55


def test_critical_fact_still_blocks_even_with_a_nonblocking_code():
    reviewed = apply_report_audit(
        decision(score=55), [issue("report_optional_detail", Severity.CRITICAL)],
        threshold=50, relaxed_review=True,
    )
    assert reviewed.decision is QualityRoute.REPLAN and reviewed.score == 55
    assert reviewed.issues[0].severity is Severity.CRITICAL


def test_soft_deterministic_format_checks_are_informational_in_presentation_mode():
    plan, results, evidence = research()
    before = audit_report("已有真实来源[1]，但表达可以更完整。", plan, results, evidence, [])
    after = audit_report(
        "已有真实来源[1]，但表达可以更完整。", plan, results, evidence, [], relaxed_review=True,
    )
    assert next(item for item in before if item.code == "report_missing_sections").severity is Severity.WARNING
    assert next(item for item in after if item.code == "report_missing_sections").severity is Severity.INFO
    assert all(item.severity is not Severity.INFO for item in after if item.code == "report_citation_missing_reference")


def test_limitations_merge_and_research_gaps_are_idempotent_without_changing_body():
    content = consolidate_report_limitations(BODY, ["外部资料仅覆盖部分地区。", "外部资料仅覆盖部分地区。"])
    assert content.count("## 局限\n") == 1
    assert content.count("外部资料仅覆盖部分地区。") == 1
    assert content.index("外部资料仅覆盖部分地区。") > content.index("## 局限")
    assert content.index("外部资料仅覆盖部分地区。") < content.index("## 参考文献")
    assert content.split("## 局限")[0] == BODY.split("## 局限")[0]
    assert consolidate_report_limitations(content, ["外部资料仅覆盖部分地区。"]) == content


def test_duplicate_limitation_sections_merge_without_losing_following_sections():
    report = (
        "## 分析\n\n有用的核心发现。\n\n## 局限\n\n旧资料有限。\n\n"
        "## 结论\n\n保留结论。\n\n## 局限与待核实事项\n\n时间范围未重验。\n\n"
        "## 参考文献\n\n原有条目。"
    )
    result = consolidate_report_limitations(report)
    assert result.count("## 局限\n") == 1 and "## 局限与待核实事项" not in result
    assert result.index("## 结论") < result.index("## 局限")
    assert "旧资料有限。" in result and "时间范围未重验。" in result and result.endswith("原有条目。")


@pytest.mark.parametrize("content", ["...", "## 摘要\n\n## 结论", "## 参考文献\n\n[1] 条目。"])
def test_boilerplate_never_turns_an_empty_manuscript_into_an_answer(content):
    assert consolidate_report_limitations(content, ["有待核验"]) == content


def test_missing_limitation_added_before_references_but_code_examples_untouched():
    code = "```md\n## 局限\n## 参考文献\n```"
    content = "## 分析\n\n已有核心发现。\n\n" + code + "\n\n## 参考文献\n\n[1] 条目。"
    result = consolidate_report_limitations(content, ["资料尚未更新。"])
    assert code in result
    assert result.index("资料尚未更新。") < result.rindex("## 参考文献")
    assert result.rindex("## 局限") > result.index(code)


def test_disputed_text_and_review_details_only_appear_in_limitations():
    report = publish(issues=[problem()]).content
    main, limits = report.split("\n\n## 局限\n\n", 1)
    assert "某寺始建于1234年" not in main and "某寺始建于1234年" in limits
    assert "湖畔步道与山谷观景台" in main and "文字题咏是本次新增文化线索" in main
    assert "重点核验" not in main and "重点核验" in limits
    assert "待核实陈述" not in report and report.count("## 局限\n") == 1
    assert report.index("## 局限") < report.index("## 参考文献")


def test_localized_noncritical_uncertainty_also_moves_out_of_main_assertions():
    report = publish(issues=[problem(severity=Severity.WARNING)]).content
    main, limits = report.split("\n\n## 局限\n\n", 1)
    assert "某寺始建于1234年" not in main and "某寺始建于1234年" in limits
    assert "可结合山水与文化安排参观" in main
    assert "待核实事项" in limits


def test_existing_limitations_also_remove_invalid_reference_markers():
    report = publish(draft(BODY.replace("具体年代仍需核验。", "具体年代[999]和[S1]仍需核验。"))).content
    assert "[999]" not in report and "[S1]" not in report
    assert "具体年代和仍需核验。" in report


async def test_declared_optional_gaps_pass_once_with_the_reviewed_text_preserved():
    class OptionalGapModel(FakeModelGateway):
        async def analyze_task(self, *args, **kwargs):
            researched = await super().analyze_task(*args, **kwargs)
            return researched.model_copy(update={"limitations": ["开放时间细节未核验，不作具体承诺。"]})

        async def synthesize_report(self, *args, **kwargs):
            candidate = await super().synthesize_report(*args, **kwargs)
            return candidate.model_copy(update={"content": candidate.content.replace("## 局限", "## 局限与待核实事项")})

        async def evaluate_report(self, query, candidate, results, **kwargs):
            self._called("evaluate_report")
            assert "## 局限\n" in candidate.content
            main, limits = candidate.content.split("## 局限\n", 1)
            assert "开放时间细节未核验" not in main and "开放时间细节未核验" in limits
            self.reviewed = deepcopy(candidate)
            return QualityDecision(
                decision=QualityRoute.REVISE, plan_version=candidate.plan_version,
                draft_version=candidate.version, score=55, rationale="核心回答可用，局限已说明。",
                issues=[issue("report_limitation_note")], revision_instructions=["可选补充更多背景。"],
            )

    gateway = OptionalGapModel()
    run = await make_test_container(model=gateway, settings=presentation_settings()).run_service.execute_and_wait(
        "生成有用的核心报告，不要求补全可选信息。"
    )
    assert run.status is RunStatus.COMPLETED and not run.warnings and not run.error, run.error
    assert run.report == gateway.reviewed.content and run.metrics.revision_count == 0
    assert run.quality_history[-1].score == 55 and run.quality_history[-1].issues
    assert gateway.call_counts["evaluate_report"] == 1 and gateway.call_counts.get("revise_report", 0) == 0
