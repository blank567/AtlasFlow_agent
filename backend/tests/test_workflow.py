from __future__ import annotations

import asyncio

import pytest
from atlasflow.agents.contracts import (
    CritiqueRoute,
    QualityRoute,
    ResearchPlan,
    ResearchTask,
    RunPolicy,
)
from atlasflow.schemas import ApprovalAction, ApprovalRequest, RunEventType, RunStatus

from backend.tests.fakes import (
    FakeModelGateway,
    GatewayScenario,
    make_test_container,
)


async def _wait_for_terminal(container, run_id: str):
    for _ in range(200):
        run = await container.store.get(run_id)
        if run.status in RunStatus.terminal():
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("run did not reach a terminal state")


@pytest.mark.asyncio
async def test_direct_path_is_auditable_and_has_one_terminal_event() -> None:
    container = make_test_container()

    run = await container.run_service.execute_and_wait("分析多 Agent 编排设计")

    assert run.status is RunStatus.COMPLETED
    assert run.plan is not None
    assert len(run.plan.tasks) == 3
    assert len(run.research_results) == 3
    assert run.report and "[task:p1-t1]" in run.report
    assert run.metrics.total_tasks == 3
    assert run.metrics.successful_tasks == 3
    assert run.metrics.failed_tasks == 0
    assert [event.sequence for event in run.events] == list(
        range(1, len(run.events) + 1)
    )
    assert sum(
        event.event_type is RunEventType.RUN_STARTED for event in run.events
    ) == 1
    terminal = [
        event
        for event in run.events
        if event.event_type
        in {
            RunEventType.RUN_COMPLETED,
            RunEventType.RUN_DEGRADED,
            RunEventType.RUN_FAILED,
            RunEventType.RUN_CANCELLED,
        }
    ]
    assert len(terminal) == 1
    assert terminal[0] is run.events[-1]
    assert {route.from_node for route in run.route_history} >= {
        "supervisor",
        "planner",
        "research_gate",
        "critic",
        "synthesizer",
        "quality_gate",
        "finalizer",
    }


@pytest.mark.asyncio
async def test_researchers_really_run_concurrently_with_cap_three() -> None:
    gateway = FakeModelGateway(
        GatewayScenario(initial_task_count=5, research_delay_seconds=0.05)
    )
    container = make_test_container(model=gateway)

    run = await container.run_service.execute_and_wait("验证动态并行")

    assert run.status is RunStatus.COMPLETED
    assert 2 <= gateway.peak_researchers <= 3
    assert run.metrics.peak_concurrency == gateway.peak_researchers == 3


@pytest.mark.asyncio
async def test_research_retry_succeeds_on_second_attempt() -> None:
    scenario = GatewayScenario(transient_research_failures={"p1-t1": 1})
    container = make_test_container(scenario=scenario)

    run = await container.run_service.execute_and_wait("验证研究重试")

    assert run.status is RunStatus.COMPLETED
    assert any(
        error.task_id == "p1-t1" and error.retryable for error in run.errors
    )
    result = next(item for item in run.research_results if item.task_id == "p1-t1")
    assert result.attempt == 2


@pytest.mark.asyncio
async def test_partial_failure_above_quorum_is_degraded() -> None:
    scenario = GatewayScenario(permanent_research_failures={"p1-t3"})
    container = make_test_container(scenario=scenario)

    run = await container.run_service.execute_and_wait("验证 Quorum 降级")

    assert run.status is RunStatus.COMPLETED_WITH_WARNINGS
    assert len(run.research_results) == 2
    assert run.metrics.successful_tasks == 2
    assert run.metrics.failed_tasks == 1
    assert run.warnings
    assert run.events[-1].event_type is RunEventType.RUN_DEGRADED


@pytest.mark.asyncio
async def test_below_quorum_replans_once_then_fails() -> None:
    failures = {"p1-t2", "p1-t3", "p2-t2", "p2-t3"}
    scenario = GatewayScenario(permanent_research_failures=failures)
    container = make_test_container(scenario=scenario)

    run = await container.run_service.execute_and_wait("验证低于 Quorum")

    assert run.status is RunStatus.FAILED
    assert len(run.plans) == 2
    assert run.metrics.replan_count == 1
    assert run.error and "Quorum" in run.error
    assert run.events[-1].event_type is RunEventType.RUN_FAILED


@pytest.mark.asyncio
async def test_policy_drives_exact_two_to_three_to_accept_flow() -> None:
    policy = RunPolicy(
        initial_task_count=2,
        required_supplement_rounds=1,
        supplement_task_count=1,
    )
    gateway = FakeModelGateway(
        GatewayScenario(
            critique_routes=[CritiqueRoute.SUPPLEMENT, CritiqueRoute.ACCEPT]
        )
    )
    container = make_test_container(model=gateway)

    run = await container.run_service.execute_and_wait(
        "黄山风景介绍",
        policy=policy,
    )

    assert run.status is RunStatus.COMPLETED
    assert run.policy == policy
    assert gateway.plan_policies == [policy]
    assert run.plan_lineage is not None
    assert run.plan_lineage.base_plan.task_ids == ("p1-t1", "p1-t2")
    assert run.plan_lineage.active_plan().task_ids == (
        "p1-t1",
        "p1-t2",
        "p1-t3",
    )
    assert len(run.plan_lineage.supplements) == 1
    assert run.plan_lineage.supplements[0].round == 1
    assert run.plan_lineage.supplements[0].tasks[0].task_id == "p1-t3"
    assert run.plan_lineage.plan_version == 1
    assert run.plan_lineage.revision == 1
    assert run.plan is not None and run.plan.task_ids == (
        "p1-t1",
        "p1-t2",
        "p1-t3",
    )
    assert [plan.task_ids for plan in gateway.review_plans] == [
        ("p1-t1", "p1-t2"),
        ("p1-t1", "p1-t2", "p1-t3"),
    ]
    assert len(run.research_results) == 3
    assert len(run.critique_history) == 2
    assert [item.decision for item in run.critique_history] == [
        CritiqueRoute.SUPPLEMENT,
        CritiqueRoute.ACCEPT,
    ]
    assert run.review_contexts == gateway.review_contexts
    assert len(run.review_contexts) == 2
    first_review, second_review = run.review_contexts
    assert first_review.review_round == 1
    assert first_review.plan_version == 1
    assert first_review.plan_revision == 0
    assert first_review.initial_task_ids == ["p1-t1", "p1-t2"]
    assert first_review.supplemental_task_ids == []
    assert first_review.completed_task_ids == ["p1-t1", "p1-t2"]
    assert first_review.current_task_count == first_review.expected_task_count == 2
    assert second_review.review_round == 2
    assert second_review.plan_version == 1
    assert second_review.plan_revision == 1
    assert second_review.initial_task_ids == ["p1-t1", "p1-t2"]
    assert second_review.supplemental_task_ids == ["p1-t3"]
    assert second_review.completed_task_ids == ["p1-t1", "p1-t2", "p1-t3"]
    assert second_review.current_task_count == second_review.expected_task_count == 3
    assert second_review.supplement_rounds_used == 1
    assert second_review.supplement_rounds_remaining == 0
    assert gateway.research_attempts == {
        "p1-t1": 1,
        "p1-t2": 1,
        "p1-t3": 1,
    }
    assert run.metrics.supplement_rounds == 1
    assert run.metrics.replan_count == 0


@pytest.mark.asyncio
async def test_critic_replan_is_global_and_versioned() -> None:
    scenario = GatewayScenario(
        critique_routes=[CritiqueRoute.REPLAN, CritiqueRoute.ACCEPT]
    )
    container = make_test_container(scenario=scenario)

    run = await container.run_service.execute_and_wait("验证 Critic 重规划")

    assert run.status is RunStatus.COMPLETED
    assert [plan.plan_version for plan in run.plans] == [1, 2]
    assert run.plan is not None and run.plan.plan_version == 2
    assert run.metrics.replan_count == 1


@pytest.mark.asyncio
async def test_critic_replan_without_critical_issue_is_rejected() -> None:
    gateway = FakeModelGateway(
        GatewayScenario(
            critique_routes=[CritiqueRoute.REPLAN],
            invalid_replan_without_critical_issue=True,
        )
    )
    container = make_test_container(model=gateway)

    run = await container.run_service.execute_and_wait("验证无效 Critic 重规划")

    assert run.status is RunStatus.FAILED
    assert run.metrics.replan_count == 0
    assert gateway.call_counts["create_plan"] == 1
    assert run.error and "critical issue" in run.error
    assert not any(
        route.from_node == "critic" and route.to_node == "planner"
        for route in run.route_history
    )


@pytest.mark.asyncio
async def test_quality_gate_revises_then_accepts() -> None:
    scenario = GatewayScenario(
        quality_routes=[QualityRoute.REVISE, QualityRoute.ACCEPT]
    )
    container = make_test_container(scenario=scenario)

    run = await container.run_service.execute_and_wait("验证报告修订")

    assert run.status is RunStatus.COMPLETED
    assert len(run.draft_versions) == 2
    assert [item.version for item in run.draft_versions] == [1, 2]
    assert run.metrics.revision_count == 1


@pytest.mark.asyncio
async def test_quality_gate_can_trigger_replan() -> None:
    scenario = GatewayScenario(
        quality_routes=[QualityRoute.REPLAN, QualityRoute.ACCEPT]
    )
    container = make_test_container(scenario=scenario)

    run = await container.run_service.execute_and_wait("验证质量重规划")

    assert run.status is RunStatus.COMPLETED
    assert len(run.plans) == 2
    assert run.plan is not None and run.plan.plan_version == 2
    assert run.metrics.replan_count == 1


@pytest.mark.asyncio
async def test_supplement_history_does_not_become_a_revision_of_replanned_plan() -> None:
    policy = RunPolicy(
        initial_task_count=2,
        required_supplement_rounds=1,
        supplement_task_count=1,
    )
    gateway = FakeModelGateway(
        GatewayScenario(
            critique_routes=[
                CritiqueRoute.SUPPLEMENT,
                CritiqueRoute.ACCEPT,
                CritiqueRoute.ACCEPT,
            ],
            quality_routes=[QualityRoute.REPLAN, QualityRoute.ACCEPT],
        )
    )
    container = make_test_container(model=gateway)

    run = await container.run_service.execute_and_wait(
        "验证跨计划版本的补充来源",
        policy=policy,
    )

    assert run.status is RunStatus.COMPLETED
    assert [lineage.plan_version for lineage in run.plan_lineages] == [1, 2]
    assert [lineage.revision for lineage in run.plan_lineages] == [1, 0]
    assert run.plan is not None and run.plan.plan_version == 2
    assert run.plan.task_ids == ("p2-t1", "p2-t2")
    replanned_review = run.review_contexts[-1]
    assert replanned_review.review_round == 3
    assert replanned_review.plan_version == 2
    assert replanned_review.plan_revision == 0
    assert replanned_review.initial_task_ids == ["p2-t1", "p2-t2"]
    assert replanned_review.supplemental_task_ids == []
    assert replanned_review.supplement_rounds_used == 1
    assert replanned_review.supplement_rounds_remaining == 0
    assert replanned_review.current_task_count == 2
    assert replanned_review.expected_task_count == 2
    assert run.metrics.supplement_rounds == 1
    assert run.metrics.replan_count == 1


@pytest.mark.asyncio
async def test_revision_budget_exhaustion_keeps_report_with_warning() -> None:
    scenario = GatewayScenario(
        quality_routes=[
            QualityRoute.REVISE,
            QualityRoute.REVISE,
            QualityRoute.REVISE,
        ]
    )
    container = make_test_container(scenario=scenario)

    run = await container.run_service.execute_and_wait("验证质量预算")

    assert run.status is RunStatus.COMPLETED_WITH_WARNINGS
    assert len(run.draft_versions) == 3
    assert run.metrics.revision_count == 2
    assert run.report == run.draft_versions[-1].content
    assert run.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["create_plan", "synthesize_report", "evaluate_report"],
)
async def test_core_agent_failure_ends_run_as_failed(method: str) -> None:
    container = make_test_container(
        scenario=GatewayScenario(method_failures={method: 1})
    )

    run = await container.run_service.execute_and_wait(f"验证 {method} 失败")

    assert run.status is RunStatus.FAILED
    assert run.error and method in run.error
    assert run.events[-1].event_type is RunEventType.RUN_FAILED


@pytest.mark.asyncio
async def test_human_approval_can_approve_edit_and_cancel() -> None:
    approve_container = make_test_container()
    waiting = await approve_container.run_service.execute_and_wait(
        "等待批准", auto_approve=False
    )
    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert waiting.plan is not None
    await approve_container.run_service.resolve_approval(
        waiting.id, ApprovalRequest(action=ApprovalAction.APPROVE)
    )
    approved = await _wait_for_terminal(approve_container, waiting.id)
    assert approved.status is RunStatus.COMPLETED

    edit_container = make_test_container()
    waiting = await edit_container.run_service.execute_and_wait(
        "等待编辑", auto_approve=False
    )
    assert waiting.plan is not None
    edited = ResearchPlan(
        plan_version=waiting.plan.plan_version,
        rationale="人工编辑后的两任务计划",
        tasks=[
            ResearchTask(
                task_id="human-1",
                title="人工任务一",
                objective="检查第一维度",
                success_criteria=["完成第一维度"],
                priority=1,
                dependencies=[],
                plan_version=waiting.plan.plan_version,
            ),
            ResearchTask(
                task_id="human-2",
                title="人工任务二",
                objective="检查第二维度",
                success_criteria=["完成第二维度"],
                priority=2,
                dependencies=["human-1"],
                plan_version=waiting.plan.plan_version,
            ),
        ],
    )
    await edit_container.run_service.resolve_approval(
        waiting.id,
        ApprovalRequest(action=ApprovalAction.EDIT, edited_plan=edited),
    )
    edited_run = await _wait_for_terminal(edit_container, waiting.id)
    assert edited_run.status is RunStatus.COMPLETED
    assert edited_run.plan is not None
    assert edited_run.plan.task_ids == ("human-1", "human-2")

    cancel_container = make_test_container()
    waiting = await cancel_container.run_service.execute_and_wait(
        "等待取消", auto_approve=False
    )
    await cancel_container.run_service.resolve_approval(
        waiting.id, ApprovalRequest(action=ApprovalAction.CANCEL)
    )
    cancelled = await _wait_for_terminal(cancel_container, waiting.id)
    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.report is None
    assert cancelled.events[-1].event_type is RunEventType.RUN_CANCELLED
