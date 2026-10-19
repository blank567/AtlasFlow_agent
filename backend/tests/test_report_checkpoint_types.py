"""Production serializer and legacy checkpoint regressions, entirely offline."""

from copy import deepcopy

import pytest
from atlasflow.agents.contracts import (
    BaselineCoverage,
    CriterionAssessment,
    DraftVersion,
    QualityDecision,
    QualityRoute,
    ReportBaselineSection,
    ReportUpdateContext,
    ReviewIssue,
    RunPolicy,
    Severity,
)
from atlasflow.agents.report_update import restore_report_update_context
from atlasflow.agents.workflow import _CHECKPOINT_TYPES, ResearchWorkflow
from atlasflow.conversations.contracts import ContinuationInput
from atlasflow.execution.context import NodeContext
from atlasflow.execution.contracts import (
    checkpoint_msgpack_modules,
    checkpoint_types,
    decode_safe,
    encode_safe,
)
from atlasflow.execution.memory import MemoryExecutionRepository
from atlasflow.execution.postgres import create_postgres_execution
from atlasflow.execution.runtime import ExecutionRuntime
from atlasflow.schemas import RunStatus
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel, ValidationError

from backend.tests.test_cumulative_reports import (
    HISTORY_REPORT,
    UPDATED_REPORT,
    CumulativeConversationModel,
    plan_and_results,
    update_context,
)

CONTRACTS = [
    update_context(),
    ReportBaselineSection(section_id="B1", title="自然风光", content="湖畔步道。"),
    BaselineCoverage(
        section_id="B1", status="retained", location="山水与文化", reason="旧主题仍保留。"
    ),
    CriterionAssessment(
        criterion_index=1, status="partial", finding_indices=[1], reason="资料有限。"
    ),
]


@pytest.mark.parametrize("original", CONTRACTS, ids=lambda model: type(model).__name__)
@pytest.mark.parametrize("codec", ["journal", "checkpoint"])
def test_new_contracts_roundtrip_through_shared_production_codecs(original, codec):
    assert checkpoint_types()[type(original).__name__] is type(original)
    if codec == "journal":
        restored = decode_safe(encode_safe(original))
    else:
        serializer = JsonPlusSerializer(allowed_msgpack_modules=checkpoint_msgpack_modules())
        restored = serializer.loads_typed(serializer.dumps_typed(original))
    assert type(restored) is type(original)
    assert restored.model_dump(mode="json") == original.model_dump(mode="json")
    if isinstance(restored, ReportUpdateContext):
        assert all(isinstance(section, ReportBaselineSection) for section in restored.sections)


def test_memory_and_postgres_use_the_same_explicit_registry():
    expected = checkpoint_msgpack_modules()
    assert _CHECKPOINT_TYPES == expected
    assert len(expected) == len(set(expected))
    assert all(
        (model.__module__, model.__name__) in expected for model in checkpoint_types().values()
    )


def test_unknown_model_still_cannot_enter_the_durable_journal():
    class UnregisteredContract(BaseModel):
        text: str

    with pytest.raises(ValueError, match="unregistered durable type"):
        encode_safe(UnregisteredContract(text="不能放开任意类型反序列化"))


@pytest.mark.asyncio
async def test_cumulative_node_result_replays_typed_context_from_the_durable_journal():
    runtime = ExecutionRuntime(MemoryExecutionRepository(), None, profile_hash="offline")
    await runtime.start()
    try:
        await runtime.bind_run("offline-replay", tool_limit=40)
        claim, _ = await runtime.claim(
            "offline-replay",
            request_id="initial",
            payload={"kind": "initial"},
        )
        payload = {"report_update": update_context()}
        node = NodeContext(runtime, claim, "cumulative-synthesizer")
        lease = await runtime.begin_invocation(
            node, kind="node_result", name="synthesizer", inputs={}
        )
        await runtime.dispatch(lease)
        await runtime.confirm(lease, payload)
        replay = await runtime.begin_invocation(
            NodeContext(runtime, claim, "cumulative-synthesizer"),
            kind="node_result",
            name="synthesizer",
            inputs={},
        )
        assert replay.replayed
        restored = decode_safe(replay.replay)["report_update"]
        assert restored == payload["report_update"]
        assert isinstance(restored.sections[0], ReportBaselineSection)
    finally:
        await runtime.close()


@pytest.mark.parametrize("transport", ["typed", "dict", "none"])
def test_restore_context_parses_nested_sections_without_mutating_transport(transport):
    expected = update_context()
    value = expected if transport == "typed" else expected.model_dump(mode="json")
    if transport == "none":
        value = None
    before = deepcopy(value)
    restored = restore_report_update_context(value)
    assert value == before
    if value is None:
        assert restored is None
    else:
        assert restored == expected
        assert all(isinstance(section, ReportBaselineSection) for section in restored.sections)


@pytest.mark.parametrize("invalid", [{}, {"base_run_id": "old"}, [], "old-report"])
def test_malformed_restored_context_cannot_silently_disable_cumulative_review(invalid):
    with pytest.raises(ValidationError):
        restore_report_update_context(invalid)


async def discard_event(*args, **kwargs):
    pass


def restored_state():
    context = update_context()
    plan, results = plan_and_results()
    state = ResearchWorkflow._initial_state(
        run_id="offline-restored-run",
        query=context.update_request,
        auto_approve=True,
        policy=RunPolicy(knowledge_mode="off"),
    )
    state.update(
        plan=plan,
        plans=[plan],
        research_results=results,
        report_update=context.model_dump(mode="json"),
    )
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["synthesize", "revise", "quality", "fallback"])
async def test_all_report_consumers_accept_legacy_dict_context(stage):
    gateway = CumulativeConversationModel()
    workflow = ResearchWorkflow(model=gateway, event_sink=discard_event)
    state = restored_state()
    if stage in {"revise", "quality"}:
        state["draft_versions"] = [
            DraftVersion(
                version=1,
                plan_version=1,
                content=HISTORY_REPORT if stage == "revise" else UPDATED_REPORT,
                based_on_task_ids=[item.task_id for item in state["research_results"]],
            )
        ]
    if stage == "revise":
        state["synthesis_mode"] = "revise"
        state["quality_history"] = [
            QualityDecision(
                decision=QualityRoute.REVISE,
                plan_version=1,
                draft_version=1,
                score=59,
                rationale="补回旧主题。",
                revision_instructions=["补回自然风光。"],
                issues=[
                    ReviewIssue(
                        severity=Severity.WARNING,
                        code="report_update_missing_content",
                        message="自然风光遗漏。",
                        recommendation="补回旧主题。",
                    )
                ],
            )
        ]
    if stage == "fallback":
        state.update(
            requested_final_status=RunStatus.COMPLETED_WITH_WARNINGS.value,
            delivery_fallback_reason="模型服务不可用，仅交付已有资料。",
        )
        output = await workflow._finalizer(state)
        assert "湖畔步道" in output["report"]
        assert output["draft_versions"][0].citation_source_urls == []
    else:
        output = await (
            workflow._quality_gate(state) if stage == "quality" else workflow._synthesizer(state)
        )
        assert not output.get("fatal_error")
        assert gateway.report_calls
        assert all(isinstance(call[1], ReportUpdateContext) for call in gateway.report_calls)
        assert all(
            isinstance(section, ReportBaselineSection)
            for call in gateway.report_calls
            for section in call[1].sections
        )
    assert isinstance(state["report_update"], dict)


class LegacyDictSerializer(JsonPlusSerializer):
    """Emulate old/JSON checkpoints returning a mapping, not an approved model."""

    def loads_typed(self, value):
        def untype(item):
            if isinstance(item, ReportUpdateContext):
                return item.model_dump(mode="json")
            if isinstance(item, dict):
                return {key: untype(child) for key, child in item.items()}
            if isinstance(item, list):
                return [untype(child) for child in item]
            if isinstance(item, tuple):
                return tuple(untype(child) for child in item)
            return item

        return untype(super().loads_typed(value))


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["production_postgres_serializer", "legacy_dict"])
async def test_approval_checkpoint_restores_full_report_update_without_any_database(transport):
    # Production constructors are lazy. Never open the pool, connect or initialize tables.
    runtime = create_postgres_execution("postgresql://offline:unused@127.0.0.1:1/not_used")
    try:
        serializer = runtime.checkpointer.serde
        context = update_context()
        assert isinstance(
            serializer.loads_typed(serializer.dumps_typed(context)), ReportUpdateContext
        )
        if transport == "legacy_dict":
            serializer = LegacyDictSerializer(allowed_msgpack_modules=checkpoint_msgpack_modules())
        gateway = CumulativeConversationModel()
        gateway.planned_scopes.append("scenery")
        workflow = ResearchWorkflow(
            model=gateway,
            event_sink=discard_event,
            checkpointer=MemorySaver(serde=serializer),
        )
        seed = ContinuationInput(
            conversation_id="offline-conversation",
            turn_id="offline-turn",
            mode="research",
            base_run_id=context.base_run_id,
            base_query=context.base_query,
            base_report=context.base_report,
        )
        paused = await workflow.start(
            run_id="offline-approval",
            query=context.update_request,
            auto_approve=False,
            policy=RunPolicy(knowledge_mode="off"),
            continuation_input=seed.model_dump(mode="json"),
        )
        assert paused.paused
        restored = paused.state["report_update"]
        assert isinstance(restored, dict if transport == "legacy_dict" else ReportUpdateContext)
        finished = await workflow.resume(run_id="offline-approval", action="approve")
        assert finished.final and finished.state["final_status"] == RunStatus.COMPLETED.value
        assert all(word in finished.state["report"] for word in ("湖畔步道", "地方博物馆"))
        assert all(isinstance(call[1], ReportUpdateContext) for call in gateway.report_calls)
        assert not runtime.ready  # Serializer-only test: no PostgreSQL service was contacted.
    finally:
        await runtime.close()
