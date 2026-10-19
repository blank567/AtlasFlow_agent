"""Offline multi-turn integration: no model API, production database or paid runs."""

from __future__ import annotations

import asyncio
import json
from collections import deque

import pytest
from atlasflow.agents.contracts import DraftVersion, QualityRoute
from atlasflow.agents.gateway import OpenRouterModelGateway
from atlasflow.bootstrap import ProviderBundle, build_container
from atlasflow.conversations.contracts import (
    ConfirmTurn,
    ConversationAnswer,
    ConversationConflict,
    ConversationDecision,
    CreateConversation,
    SendMessage,
)
from atlasflow.main import create_app
from atlasflow.schemas import RunStatus, ToolCallRecord
from fastapi.testclient import TestClient

from backend.tests.fakes import (
    FakeEmbeddingProvider,
    FakeModelGateway,
    FakeRerankProvider,
    GatewayScenario,
    make_test_container,
    make_test_settings,
)


class ConversationModel(FakeModelGateway):
    def __init__(self, routes=("research",), patches=(), scenario=None):
        super().__init__(scenario)
        self.routes = deque(routes)
        self.patches = deque(patches)
        self.contexts = []
        self.answer_contexts = []

    async def route_conversation(self, context):
        json.dumps(context, ensure_ascii=False)  # Real providers must receive JSON primitives.
        self._called("route_conversation")
        self.contexts.append(context)
        action = self.routes.popleft()
        base = context["selected_base_run_id"] or (
            context["reports"][-1]["run_id"] if context["reports"] else None
        )
        updates = self.patches.popleft() if self.patches else {}
        requirements = {k: v["value"] for k, v in context["requirements"].items()}
        requirements.update(updates)
        return ConversationDecision(
            action=action,
            resolved_request=(f"{context['user_message']['content']}；完整约束：{requirements}"),
            rationale="离线会话路由夹具",
            base_run_id=base,
            clarification_questions=["请补充目的地和天数"] if action == "clarify" else [],
            requirement_updates=[
                {"key": k, "value": v, "source_message_ids": [context["user_message"]["id"]]}
                for k, v in updates.items()
            ],
        )

    async def answer_conversation(self, context):
        self._called("answer_conversation")
        self.answer_contexts.append(context)
        return ConversationAnswer(content="这是根据已有报告给出的解释，没有重新查询资料。")

    async def rewrite_report(self, query, draft, results, *, instructions, tool_context=""):
        self._called("rewrite_report")
        return DraftVersion(
            version=draft.version + 1,
            plan_version=draft.plan_version,
            content=draft.content.replace("## 分析", "## 分析与论证"),
            based_on_task_ids=draft.based_on_task_ids,
        )


async def wait_turn(service, conversation_id, wanted=None):
    for _ in range(300):
        detail = await service.detail(conversation_id)
        if detail.turns:
            status = detail.turns[-1].status
            if (status in wanted) if wanted else (status != "routing"):
                return detail
        await asyncio.sleep(0.01)
    raise AssertionError("conversation did not settle")


async def send(service, conversation_id, content, request_id="request-1", base_run_id=None):
    detail = await service.detail(conversation_id)
    payload = SendMessage(
        content=content,
        request_id=request_id,
        expected_version=detail.conversation.version,
        base_run_id=base_run_id,
    )
    await service.send(conversation_id, payload)
    return await wait_turn(service, conversation_id), payload


async def confirm(service, detail):
    turn = detail.turns[-1]
    return await service.confirm(
        detail.conversation.id,
        turn.id,
        ConfirmTurn(expected_version=detail.conversation.version, confirmation_turn_id=turn.id),
    )


@pytest.mark.asyncio
async def test_clarify_research_answer_followup_rewrite_keeps_history_and_independent_runs():
    model = ConversationModel(
        ("clarify", "research", "answer", "research", "revise"),
        patches=(
            {},
            {"目的地": "北京", "天数": "三天", "预算": "3000元"},
            {},
            {"天数": "五天"},
            {},
        ),
    )
    container = make_test_container(model=model)
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=True))
    detail, _ = await send(service, conversation.id, "做一份旅游报告")
    assert detail.turns[-1].status == "awaiting_clarification"
    assert (await container.store.list())[1] == 0
    detail, _ = await send(service, conversation.id, "北京三天，两个人，预算3000元", "request-2")
    assert detail.turns[-1].status == "awaiting_confirmation"
    assert (await container.store.list())[1] == 0
    await confirm(service, detail)
    detail = await wait_turn(service, conversation.id, {"completed"})
    first = detail.reports[-1].run_id
    original = await container.store.get(first)
    assert original.conversation_id == conversation.id
    assert detail.conversation.requirements["天数"].confirmed

    before = model.call_counts.copy()
    detail, _ = await send(service, conversation.id, "解释一下第二天为什么这样安排", "request-3")
    assert detail.turns[-1].status == "completed" and not detail.turns[-1].run_id
    assert model.call_counts["create_plan"] == before["create_plan"]
    assert (await container.store.list())[1] == 1
    assert detail.messages[-1].run_id == first

    detail, _ = await send(
        service, conversation.id, "改成五天，预算不变，重新核验交通", "request-4"
    )
    assert "3000元" in detail.turns[-1].decision.resolved_request
    assert not detail.conversation.requirements["天数"].confirmed
    await confirm(service, detail)
    detail = await wait_turn(service, conversation.id, {"completed"})
    second = detail.reports[-1].run_id
    assert (await container.store.get(second)).source_run_id == first
    assert detail.turns[1].requirements_snapshot["天数"].value == "三天"
    assert detail.turns[-1].requirements_snapshot["天数"].value == "五天"

    before = model.call_counts.copy()
    detail, _ = await send(service, conversation.id, "内容不变，改成正式论文式报告", "request-5")
    await confirm(service, detail)
    detail = await wait_turn(service, conversation.id, {"completed"})
    third = await container.store.get(detail.reports[-1].run_id)
    assert third.execution_mode == "report_revision" and third.source_run_id == second
    assert third.metrics.tool_calls == 0 and not third.tool_calls
    assert third.metrics.total_tasks == 0 and third.metrics.successful_tasks == 0
    assert model.call_counts["create_plan"] == before["create_plan"]
    assert model.call_counts["analyze_task"] == before["analyze_task"]
    assert model.call_counts["rewrite_report"] == 1
    assert not any(event.agent in {"planner", "researcher", "critic"} for event in third.events)
    assert (await container.store.get(first)).report == original.report
    assert (await container.store.list())[1] == 3
    assert len(detail.messages) == 13  # 5 user, 5 route/answer, 3 Run outcomes.
    assert all(turn.trace_segments for turn in detail.turns)
    await service.close()
    await container.run_service.close()


@pytest.mark.asyncio
async def test_duplicate_message_and_confirm_do_not_duplicate_model_requests_or_runs():
    model = ConversationModel()
    container = make_test_container(model=model)
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=True))
    detail, payload = await send(service, conversation.id, "分析Transformer架构")
    duplicate = await service.send(conversation.id, payload)
    assert len(duplicate.turns) == 1 and model.call_counts["route_conversation"] == 1
    turn = detail.turns[-1]
    request = ConfirmTurn(
        expected_version=detail.conversation.version, confirmation_turn_id=turn.id
    )
    await asyncio.gather(
        service.confirm(conversation.id, turn.id, request),
        service.confirm(conversation.id, turn.id, request),
    )
    await wait_turn(service, conversation.id, {"completed"})
    assert (await container.store.list())[1] == 1 and model.call_counts["create_plan"] == 1
    with pytest.raises(ConversationConflict, match="request_id"):
        await service.send(conversation.id, payload.model_copy(update={"content": "另一个任务"}))
    await container.run_service.close()


@pytest.mark.asyncio
async def test_stale_version_rejected_and_unexecuted_proposal_can_be_refined():
    model = ConversationModel(("research", "clarify"))
    container = make_test_container(model=model)
    service = container.conversations
    conversation = await service.create(CreateConversation())
    detail, _ = await send(service, conversation.id, "分析Transformer")
    with pytest.raises(ConversationConflict, match="已更新"):
        await service.send(
            conversation.id, SendMessage(content="补充范围", request_id="stale", expected_version=0)
        )
    detail, _ = await send(service, conversation.id, "先不要研究，帮我明确范围", "refinement")
    assert detail.turns[0].status == "cancelled"
    assert detail.turns[-1].status == "awaiting_clarification"
    assert (await container.store.list())[1] == 0


@pytest.mark.asyncio
async def test_explicit_approval_is_not_replaced_by_chat_message():
    container = make_test_container(model=ConversationModel())
    service = container.conversations
    conversation = await service.create(CreateConversation())
    detail, _ = await send(service, conversation.id, "分析多Agent系统")
    await confirm(service, detail)
    detail = await wait_turn(service, conversation.id, {"waiting_approval"})
    with pytest.raises(ConversationConflict, match="审批"):
        await service.send(
            conversation.id,
            SendMessage(
                content="好的",
                request_id="not-approval",
                expected_version=detail.conversation.version,
            ),
        )
    run = await container.store.get(detail.active_run.id)
    assert run.status is RunStatus.WAITING_APPROVAL
    await service.cancel(conversation.id, detail.turns[-1].id)
    assert (await service.detail(conversation.id)).turns[-1].status == "cancelled"


@pytest.mark.asyncio
async def test_revision_has_no_tools_preserves_warnings_and_navigation_across_versions():
    model = ConversationModel(("research", "revise", "answer", "revise"))
    container = make_test_container(model=model)
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=True))
    detail, _ = await send(service, conversation.id, "分析旅行安排")
    await confirm(service, detail)
    detail = await wait_turn(service, conversation.id, {"completed"})
    base = await container.store.get(detail.reports[-1].run_id)
    nav = "https://uri.amap.com/navigation?from=1&to=2"
    record = ToolCallRecord(
        tool_name="map_route", arguments={}, success=True, duration_ms=1, navigation_url=nav
    )
    content = (
        base.report.replace("测试结论成立。", "测试结论成立[1]。")
        + f"\n\n## 参考文献\n\n[1] 导航 [访问链接]({nav})"
    )
    await container.store.update(
        base.id,
        report=content,
        tool_calls=[record],
        warnings=["票价尚未核实"],
        status=RunStatus.COMPLETED_WITH_WARNINGS,
    )

    class ForbiddenRuntime:
        class Budget:
            def clear(self, run_id):
                pass

        budget = Budget()

        def clear_run(self, run_id):
            self.budget.clear(run_id)

        async def gather(self, **kwargs):
            pytest.fail("expression-only revisions must not call ToolRuntime")

    container.run_service.workflow.tool_runtime = ForbiddenRuntime()
    detail, _ = await send(service, conversation.id, "只改成正式表达", "rewrite-1")
    await confirm(service, detail)
    detail = await wait_turn(service, conversation.id, {"completed_with_warnings"})
    revised = await container.store.get(detail.reports[-1].run_id)
    assert "票价尚未核实" in revised.warnings
    assert revised.metrics.tool_calls == 0
    assert nav in revised.report
    detail, _ = await send(service, conversation.id, "解释一下导航来源", "answer")
    assert "票价尚未核实" in detail.messages[-1].content
    detail, _ = await send(service, conversation.id, "再调整标题，不改变事实", "rewrite-2")
    await confirm(service, detail)
    detail = await wait_turn(service, conversation.id, {"completed_with_warnings"})
    latest = await container.store.get(detail.reports[-1].run_id)
    assert nav in latest.report and latest.metrics.tool_calls == 0


@pytest.mark.asyncio
async def test_model_cannot_choose_another_conversation_report_or_fabricate_memory_sources():
    class BadModel(ConversationModel):
        async def route_conversation(self, context):
            return ConversationDecision(
                action="research",
                resolved_request="研究",
                rationale="测试",
                base_run_id="other-conversation-report",
            )

    container = make_test_container(model=BadModel())
    service = container.conversations
    conversation = await service.create(CreateConversation())
    detail, _ = await send(service, conversation.id, "分析一个问题")
    assert detail.turns[-1].status == "failed"
    assert (await container.store.list())[1] == 0
    container.conversations.model = ConversationModel(("research",))
    with pytest.raises(ConversationConflict, match="当前会话"):
        await send(
            service, conversation.id, "查询报告", "other", base_run_id="other-conversation-report"
        )

    class BadMemory(ConversationModel):
        async def route_conversation(self, context):
            return ConversationDecision(
                action="research",
                resolved_request="研究",
                rationale="测试",
                requirement_updates=[
                    {"key": "预算", "value": "99999元", "source_message_ids": ["assistant-made-up"]}
                ],
            )

    service.model = BadMemory()
    detail, _ = await send(service, conversation.id, "预算3000元", "bad-memory")
    assert detail.turns[-1].status == "failed" and not detail.conversation.requirements


@pytest.mark.asyncio
async def test_revision_request_for_new_evidence_does_not_silently_replan():
    model = ConversationModel(
        ("research", "revise"),
        scenario=GatewayScenario(quality_routes=[QualityRoute.ACCEPT, QualityRoute.REPLAN]),
    )
    container = make_test_container(model=model)
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=True))
    detail, _ = await send(service, conversation.id, "分析Transformer")
    await confirm(service, detail)
    detail = await wait_turn(service, conversation.id, {"completed"})
    detail, _ = await send(service, conversation.id, "只改文风", "rewrite")
    await confirm(service, detail)
    detail = await wait_turn(service, conversation.id, {"completed_with_warnings"})
    assert model.call_counts["create_plan"] == 1
    assert "另行确认补充研究" in (await container.store.get(detail.reports[-1].run_id)).warnings[-1]


def test_api_persistence_version_validation_and_restart_never_retries_unknown_chat(tmp_path):
    path = tmp_path / "conversation.sqlite3"
    settings = make_test_settings().model_copy(update={
        "database_path": str(path), "evaluation_storage_root": str(tmp_path / "evaluations")
    })

    def build(model):
        return build_container(
            settings,
            ProviderBundle(
                model=model, embedding=FakeEmbeddingProvider(), reranker=FakeRerankProvider()
            ),
        )

    model = ConversationModel(("clarify",))
    container = build(model)
    with TestClient(create_app(settings=settings, container=container)) as client:
        created = client.post("/api/v1/conversations", json={"title": "持久化会话"})
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        sent = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "做一份旅游报告", "request_id": "persist", "expected_version": 0},
        )
        assert sent.status_code == 202
        for _ in range(100):
            detail = client.get(f"/api/v1/conversations/{conversation_id}").json()
            if detail["turns"][-1]["status"] != "routing":
                break
            import time

            time.sleep(0.01)
        assert detail["turns"][-1]["status"] == "awaiting_clarification"
        assert (
            client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={"content": "新消息", "request_id": "stale", "expected_version": 0},
            ).status_code
            == 409
        )
        assert client.get("/api/v1/conversations/missing").status_code == 404
        assert (
            client.post(
                "/api/v1/conversations",
                json={"policy": {"knowledge_mode": "selected", "knowledge_space": "missing-space"}},
            ).status_code
            == 422
        )
        assert client.get("/api/v1/conversations").json()["total"] == 1
        assert (
            client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={"content": "", "request_id": "empty", "expected_version": 0},
            ).status_code
            == 422
        )
    next_model = ConversationModel(())
    with TestClient(create_app(settings=settings, container=build(next_model))) as client:
        restored = client.get(f"/api/v1/conversations/{conversation_id}").json()
        assert len(restored["messages"]) == 2
        assert not next_model.call_counts


@pytest.mark.asyncio
async def test_confirmation_and_cancellation_are_serialized_without_orphaned_run():
    container = make_test_container(model=ConversationModel())
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=True))
    detail, _ = await send(service, conversation.id, "研究执行竞态")
    entered, release = asyncio.Event(), asyncio.Event()
    original = container.run_service.launch_prepared

    async def slow_launch(run_id):
        entered.set()
        await release.wait()
        return await original(run_id)

    container.run_service.launch_prepared = slow_launch
    starting = asyncio.create_task(confirm(service, detail))
    await entered.wait()
    stopping = asyncio.create_task(service.cancel(conversation.id, detail.turns[-1].id))
    await asyncio.sleep(0.01)
    assert not stopping.done(), "cancel must wait for the Run binding/dispatch operation"
    release.set()
    await starting
    stopped = await stopping
    assert not stopped.conversation.active_turn_id
    run = await container.store.get(stopped.turns[-1].run_id)
    assert run.status is RunStatus.CANCELLED
    assert (await container.store.list())[1] == 1


@pytest.mark.asyncio
async def test_atomic_reservation_rolls_back_identity_when_projection_write_fails():
    container = make_test_container(model=ConversationModel())
    service = container.conversations
    conversation = await service.create(CreateConversation())
    detail, _ = await send(service, conversation.id, "测试原子写入")
    original = container.store.insert_prepared

    def fail_after_insert(connection, record):
        original(connection, record)
        raise RuntimeError("simulated projection failure")

    container.store.insert_prepared = fail_after_insert
    with pytest.raises(RuntimeError, match="projection failure"):
        await confirm(service, detail)
    assert (await container.store.list())[1] == 0
    restored = await service.detail(conversation.id)
    assert restored.turns[-1].run_id is None
    assert restored.turns[-1].status == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_restart_preserves_unknown_chat_request_without_automatic_model_retry(tmp_path):
    settings = make_test_settings().model_copy(
        update={"database_path": str(tmp_path / "unknown.sqlite3")}
    )

    def build(model):
        return build_container(
            settings,
            ProviderBundle(
                model=model, embedding=FakeEmbeddingProvider(), reranker=FakeRerankProvider()
            ),
        )

    first = build(ConversationModel())
    conversation = await first.conversations.create(CreateConversation())
    await first.conversations.store.reserve_message(
        conversation.id,
        SendMessage(content="输入已保存，远端结果未知", request_id="unknown", expected_version=0),
    )
    await first.store.close()
    model = ConversationModel(())
    second = build(model)
    await second.conversations.reconcile_startup()
    restored = await second.conversations.detail(conversation.id)
    assert restored.turns[-1].status == "failed"
    assert "不会自动重新调用" in restored.messages[-1].content
    assert restored.messages[0].content == "输入已保存，远端结果未知"
    assert not model.call_counts
    await second.store.close()


@pytest.mark.asyncio
async def test_explicit_old_report_keeps_original_requirement_snapshot():
    model = ConversationModel(
        ("research", "research", "answer"), patches=({"天数": "三天"}, {"天数": "五天"}, {})
    )
    container = make_test_container(model=model)
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=True))
    detail, _ = await send(service, conversation.id, "北京三天")
    await confirm(service, detail)
    detail = await wait_turn(service, conversation.id, {"completed"})
    old_id = detail.reports[-1].run_id
    detail, _ = await send(service, conversation.id, "改成五天", "five-days")
    await confirm(service, detail)
    await wait_turn(service, conversation.id, {"completed"})
    detail, _ = await send(
        service, conversation.id, "解释第一份报告", "explain-old", base_run_id=old_id
    )
    assert detail.turns[-1].status == "completed"
    assert model.answer_contexts[-1]["source_run_id"] == old_id
    assert model.answer_contexts[-1]["report_requirements"]["天数"]["value"] == "三天"
    assert detail.conversation.requirements["天数"].value == "五天"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["citation", "url"])
async def test_unverified_chat_answer_is_not_published(invalid):
    class UnsafeAnswer(ConversationModel):
        async def answer_conversation(self, context):
            return ConversationAnswer(
                content="无来源论断[999]"
                if invalid == "citation"
                else "来源https://unverified.test/example"
            )

    container = make_test_container(model=UnsafeAnswer(("research", "answer")))
    service = container.conversations
    conversation = await service.create(CreateConversation(auto_approve=True))
    detail, _ = await send(service, conversation.id, "分析Transformer")
    await confirm(service, detail)
    await wait_turn(service, conversation.id, {"completed"})
    detail, _ = await send(service, conversation.id, "解释报告", "unsafe-answer")
    assert detail.turns[-1].status == "failed"
    assert not any(
        "无来源论断" in m.content or "unverified.test" in m.content
        for m in detail.messages
        if m.role == "assistant"
    )
    assert (await container.store.list())[1] == 1


@pytest.mark.asyncio
async def test_conversation_sse_snapshot_has_version_and_can_resume_from_cursor():
    from atlasflow.conversations.api import events
    from starlette.requests import Request

    container = make_test_container()
    conversation = await container.conversations.create(CreateConversation())
    app = create_app(settings=make_test_settings(), container=container)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/events",
            "headers": [(b"last-event-id", b"0")],
            "app": app,
        },
        receive,
    )
    response = await events(conversation.id, request, 0)
    stream = response.body_iterator
    assert "retry:" in await anext(stream)
    snapshot = await anext(stream)
    assert "event: conversation_snapshot\nid: 0\n" in snapshot
    assert conversation.id in snapshot
    await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["report", "source"])
async def test_router_gateway_repairs_invalid_reference_before_returning_decision(bad):
    class Client:
        def __init__(self):
            self.calls = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            payload = ConversationDecision(
                action="research",
                resolved_request="完整研究需求",
                rationale="测试",
                base_run_id="report-1",
                requirement_updates=[
                    {"key": "预算", "value": "3000元", "source_message_ids": ["user-1"]}
                ],
            ).model_dump(mode="json")
            if len(self.calls) == 1:
                if bad == "report":
                    payload["base_run_id"] = "other-session"
                else:
                    payload["requirement_updates"][0]["source_message_ids"] = ["assistant-1"]
            return {"content": json.dumps(payload), "_atlasflow_finish_reason": "stop"}

    client = Client()
    model = OpenRouterModelGateway(client, "test/model")
    result = await model.route_conversation(
        {
            "reports": [{"run_id": "report-1"}],
            "selected_base_run_id": None,
            "user_message": {"id": "user-1", "content": "预算3000元"},
            "user_message_ids": ["user-1"],
        }
    )
    assert result.base_run_id == "report-1" and len(client.calls) == 2
    schema = client.calls[0]["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_trace_only_updates_do_not_expire_unchanged_message_or_execution_confirmation():
    container = make_test_container(model=ConversationModel(("clarify", "research")))
    service = container.conversations
    conversation = await service.create(CreateConversation())
    detail, _ = await send(service, conversation.id, "先明确研究范围")
    old_version = detail.conversation.version
    await service.store.save_trace(
        conversation.id, detail.turns[-1].id, {"segment_id": "late-clarify-link"}
    )
    await service.send(
        conversation.id,
        SendMessage(
            content="分析Transformer", request_id="after-trace", expected_version=old_version
        ),
    )
    detail = await wait_turn(service, conversation.id)
    assert detail.turns[-1].status == "awaiting_confirmation"
    old_version = detail.conversation.version
    await service.store.save_trace(
        conversation.id, detail.turns[-1].id, {"segment_id": "late-proposal-link"}
    )
    confirmed = await service.confirm(
        conversation.id,
        detail.turns[-1].id,
        ConfirmTurn(expected_version=old_version, confirmation_turn_id=detail.turns[-1].id),
    )
    assert confirmed.turns[-1].run_id
    await wait_turn(service, conversation.id, {"waiting_approval"})
    await service.cancel(conversation.id, detail.turns[-1].id)


def test_legacy_conversation_json_keeps_strict_dates_and_protects_current_content_version():
    from atlasflow.conversations.contracts import Conversation
    record = Conversation(title="旧会话", version=7, policy=CreateConversation().policy)
    payload = record.model_dump(mode="json")
    payload.pop("content_version")
    restored = Conversation.model_validate_json(json.dumps(payload))
    assert restored.version == restored.content_version == 7
    assert restored.created_at == record.created_at
