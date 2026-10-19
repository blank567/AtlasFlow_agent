"""Conversation orchestration: classify, explain, or explicitly dispatch a new Run."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from atlasflow.agents.report_skill import (
    build_citation_catalog,
    build_report_context,
    cited_numbers,
    render_academic_references,
    report_prose,
)
from atlasflow.conversations.contracts import (
    ConfirmTurn,
    ContinuationInput,
    ConversationAnswer,
    ConversationConflict,
    ConversationDecision,
    ConversationDeleted,
    ConversationDetail,
    CreateConversation,
    DeleteConversation,
    ReportLink,
    Requirement,
    SendMessage,
)
from atlasflow.observability import redact_sensitive_data, trace_segment
from atlasflow.schemas import RunRecord, RunStatus
from atlasflow.service import RunNotFoundError

if TYPE_CHECKING:
    from atlasflow.agents.gateway import ModelGateway
    from atlasflow.conversations.storage import SQLiteConversationStore
    from atlasflow.service import RunService


class ConversationService:
    def __init__(self, store: SQLiteConversationStore, runs: RunService, model: ModelGateway):
        self.store, self.runs, self.model = store, runs, model
        self._tasks: dict[str, asyncio.Task] = {}
        self._operation_locks: dict[str, asyncio.Lock] = {}

    def _lock(self, conversation_id):
        return self._operation_locks.setdefault(conversation_id, asyncio.Lock())

    async def create(self, payload: CreateConversation):
        return await self.store.create(payload)

    async def delete(
        self, conversation_id: str, payload: DeleteConversation
    ) -> ConversationDeleted:
        if payload.confirmation_conversation_id != conversation_id:
            raise ConversationConflict("删除确认与目标会话不一致")
        async with self._lock(conversation_id):
            run_ids = await self.store.begin_delete(conversation_id, payload.expected_version)
            detail = await self.store.detail(conversation_id)
            # Routing/answer requests have no Run but still need cancellation.
            # Drain callbacks before deleting their parent message/turn rows.
            tasks = [self._tasks[turn.id] for turn in detail.turns if turn.id in self._tasks]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            async with self.runs.conversation_deletion(conversation_id, run_ids):
                count = await self.store.delete(conversation_id, run_ids)
        return ConversationDeleted(conversation_id=conversation_id, deleted_run_count=count)

    async def _sync(self, conversation_id: str):
        detail = await self.store.detail(conversation_id)
        if detail.conversation.deleting:
            return
        turn = next((t for t in detail.turns if t.id == detail.conversation.active_turn_id), None)
        if not turn or not turn.run_id:
            return
        try:
            run = await self.runs.store.get(turn.run_id)
        except RunNotFoundError:
            await self.store.finish(
                conversation_id,
                turn.id,
                status="failed",
                kind="error",
                content="关联运行已删除；会话消息保留，请重新发起研究。",
                error="associated Run deleted",
            )
            return
        if run.status in RunStatus.terminal():
            if run.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS}:
                content = "报告已生成，可在报告工作区阅读、下载或继续追问。"
                if run.warnings:
                    content += "\n\n仍有效的警告：\n" + "\n".join(
                        f"- {warning}" for warning in run.warnings
                    )
                await self.store.finish(
                    conversation_id,
                    turn.id,
                    status=run.status.value,
                    content=content,
                    kind="report",
                    current_report=run.id,
                )
            else:
                await self.store.finish(
                    conversation_id,
                    turn.id,
                    status=run.status.value,
                    kind="error",
                    content=(
                        "本轮执行已取消。"
                        if run.status is RunStatus.CANCELLED
                        else "本轮执行失败，已保留运行记录与可用草稿。"
                    )
                    + (f"\n\n{run.error}" if run.error else ""),
                    error=run.error,
                )
        else:
            await self.store.sync_status(
                conversation_id,
                turn.id,
                "running" if run.status is RunStatus.PENDING else run.status.value,
            )

    async def detail(self, conversation_id: str) -> ConversationDetail:
        await self._sync(conversation_id)
        detail = await self.store.detail(conversation_id)
        for turn in detail.turns:
            if not turn.run_id:
                continue
            try:
                run = await self.runs.store.get(turn.run_id)
            except RunNotFoundError:
                detail.reports.append(
                    ReportLink(
                        run_id=turn.run_id,
                        turn_id=turn.id,
                        title="关联运行已删除",
                        status="deleted",
                        source_run_id=None,
                        created_at=turn.created_at,
                    )
                )
                continue
            if turn.id == detail.conversation.active_turn_id:
                detail.active_run = run
            if run.report:
                detail.reports.append(
                    ReportLink(
                        run_id=run.id,
                        turn_id=turn.id,
                        title=run.query,
                        status=run.status.value,
                        source_run_id=run.source_run_id,
                        created_at=run.created_at,
                    )
                )
        return detail

    async def send(self, conversation_id: str, payload: SendMessage) -> ConversationDetail:
        async with self._lock(conversation_id):
            return await self._send(conversation_id, payload)

    async def _send(self, conversation_id: str, payload: SendMessage) -> ConversationDetail:
        # Reconcile a finished Run before checking the single active-turn invariant.
        await self._sync(conversation_id)
        turn, created = await self.store.reserve_message(conversation_id, payload)
        if created:
            task = asyncio.create_task(self._route(conversation_id, turn.id))
            self._tasks[turn.id] = task

            def forget(completed):
                self._tasks.pop(turn.id, None)
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(forget)
        return await self.detail(conversation_id)

    async def _eligible_reports(self, detail):
        eligible = {}
        for report in detail.reports:
            if report.status not in {
                RunStatus.COMPLETED.value,
                RunStatus.COMPLETED_WITH_WARNINGS.value,
            }:
                continue
            try:
                run = await self.runs.store.get(report.run_id)
            except RunNotFoundError:
                continue
            if run.report and run.plan and run.research_results:
                eligible[run.id] = run
        return eligible

    @staticmethod
    def _source_records(run):
        inherited = (
            ContinuationInput.model_validate(run.continuation_input).base_tool_calls
            if run.continuation_input
            else []
        )
        return list({item.call_id: item for item in [*inherited, *run.tool_calls]}.values())

    @staticmethod
    def _validate_decision(decision, context):
        allowed = {item["run_id"] for item in context["reports"]}
        if decision.base_run_id and decision.base_run_id not in allowed:
            raise ConversationConflict("路由选择的报告不属于当前会话或尚未通过验收")
        selected = context.get("selected_base_run_id")
        if (
            selected
            and (decision.action in {"answer", "revise"}
                 or (decision.action == "research" and decision.base_run_id is not None))
            and decision.base_run_id != selected
        ):
            raise ConversationConflict("路由没有使用用户明确选择的报告")
        valid = set(context["user_message_ids"])
        for patch in decision.requirement_updates:
            if (
                context["user_message"]["id"] not in patch.source_message_ids
                or not set(patch.source_message_ids) <= valid
            ):
                raise ConversationConflict("需求变更必须引用本轮真实用户消息，不能依据助手建议")

    async def _route(self, conversation_id, turn_id):
        async def sink(_, segment):
            await self.store.save_trace(conversation_id, turn_id, segment.model_dump(mode="json"))

        try:
            async with trace_segment(
                atlasflow_run_id=turn_id,
                name="atlasflow.conversation.turn",
                kind="conversation",
                metadata={"conversation_id": conversation_id, "turn_id": turn_id},
                sink=sink,
            ) as trace:
                detail = await self.detail(conversation_id)
                turn = next(t for t in detail.turns if t.id == turn_id)
                message = next(m for m in detail.messages if m.id == turn.user_message_id)
                eligible = await self._eligible_reports(detail)
                if turn.requested_base_run_id and turn.requested_base_run_id not in eligible:
                    raise ConversationConflict("所选报告不可用；请选择当前会话内已完成的报告")
                context = {
                    "user_message": message.model_dump(mode="json"),
                    "user_message_ids": [m.id for m in detail.messages if m.role == "user"],
                    "recent_messages": [
                        {"id": m.id, "role": m.role, "content": m.content[:1600]}
                        for m in detail.messages[-12:]
                    ],
                    "requirements": {
                        k: v.model_dump() for k, v in detail.conversation.requirements.items()
                    },
                    "reports": [
                        {
                            "run_id": run.id,
                            "question": run.query,
                            "status": run.status.value,
                            "created_at": run.created_at.isoformat(),
                            "warnings": run.warnings,
                            "requirements_snapshot": {
                                k: v.model_dump()
                                for k, v in next(
                                    (
                                        t.requirements_snapshot
                                        for t in detail.turns
                                        if t.run_id == run.id
                                    ),
                                    {},
                                ).items()
                            },
                            "abstract": (run.report or "")[:600],
                        }
                        for run in eligible.values()
                    ],
                    "current_report_run_id": detail.conversation.current_report_run_id,
                    "selected_base_run_id": turn.requested_base_run_id,
                }
                decision = ConversationDecision.model_validate(
                    await self.model.route_conversation(context)
                )
                self._validate_decision(decision, context)
                requirements = (
                    {} if decision.reset_requirements else dict(detail.conversation.requirements)
                )
                for patch in decision.requirement_updates:
                    if patch.value is None:
                        requirements.pop(patch.key, None)
                    else:
                        requirements[patch.key] = Requirement(
                            value=patch.value, source_message_ids=patch.source_message_ids
                        )
                if len(requirements) > 40:
                    raise ConversationConflict("会话约束过多，请新建会话以明确研究范围")
                if decision.action == "clarify":
                    await self.store.save_decision(
                        conversation_id,
                        turn_id,
                        decision,
                        requirements,
                        "为明确研究范围，请补充：\n\n"
                        + "\n".join(f"- {q}" for q in decision.clarification_questions),
                        "awaiting_clarification",
                    )
                elif decision.action == "answer":
                    saved = await self.store.save_decision(
                        conversation_id, turn_id, decision, requirements, None, "routing"
                    )
                    if saved:
                        base = eligible[decision.base_run_id]
                        source_records = self._source_records(base)
                        source_order = base.draft_versions[-1].citation_source_urls if base.draft_versions else []
                        catalog = build_citation_catalog(base.evidence, source_records, source_order=source_order)
                        answer = ConversationAnswer.model_validate(
                            await self.model.answer_conversation(
                                {
                                    "request": decision.resolved_request,
                                    "report": base.report,
                                    "source_run_id": base.id,
                                    "report_created_at": base.created_at.isoformat(),
                                    "warnings": base.warnings,
                                    "report_requirements": {
                                        k: v.model_dump()
                                        for k, v in next(
                                            (
                                                t.requirements_snapshot
                                                for t in detail.turns
                                                if t.run_id == base.id
                                            ),
                                            {},
                                        ).items()
                                    },
                                    "evidence": build_report_context(base.evidence, source_records, source_order=source_order),
                                    "allowed_citation_numbers": [item.number for item in catalog],
                                    "allowed_source_urls": [item.url for item in catalog],
                                }
                            )
                        )
                        if cited_numbers(answer.content) - {item.number for item in catalog}:
                            raise ConversationConflict(
                                "回答包含来源目录中不存在的引用，未发布该回答"
                            )
                        if re.search(r"https?://", report_prose(answer.content)):
                            raise ConversationConflict(
                                "回答正文必须使用编号引用，不能自行添加来源链接"
                            )
                        content = (
                            render_academic_references(
                                answer.content, base.evidence, source_records, source_order=source_order,
                            )
                            if cited_numbers(answer.content)
                            else answer.content
                        )
                        if base.warnings:
                            content = (
                                "> 来源报告仍有有效警告："
                                + "；".join(base.warnings)
                                + "\n\n"
                                + content
                            )
                        await self.store.finish(
                            conversation_id, turn_id, status="completed", content=content
                        )
                else:
                    label = (
                        "报告表达修订，不调用工具"
                        if decision.action == "revise"
                        else "研究执行，可能调用模型与工具"
                    )
                    await self.store.save_decision(
                        conversation_id,
                        turn_id,
                        decision,
                        requirements,
                        f"{decision.resolved_request}\n\n{decision.rationale}\n\n执行方式：{label}。请核验当前需求后确认执行。",
                        "awaiting_confirmation",
                    )
                trace.set_outputs(
                    {
                        "action": decision.action,
                        "base_run_id": decision.base_run_id,
                        "conversation_id": conversation_id,
                        "turn_id": turn_id,
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface failure, never start research as fallback.
            detail = str(redact_sensitive_data(str(exc)))[:1000]
            await self.store.finish(
                conversation_id,
                turn_id,
                status="failed",
                kind="error",
                content=f"本轮处理失败，未自动重试或启动研究。\n\n{detail}",
                error=detail,
            )

    async def confirm(self, conversation_id: str, turn_id: str, payload: ConfirmTurn):
        async with self._lock(conversation_id):
            return await self._confirm(conversation_id, turn_id, payload)

    async def _confirm(self, conversation_id: str, turn_id: str, payload: ConfirmTurn):
        if payload.confirmation_turn_id != turn_id:
            raise ConversationConflict("执行确认与当前轮次不一致")
        detail = await self.detail(conversation_id)
        turn = next((t for t in detail.turns if t.id == turn_id), None)
        if turn is None:
            from atlasflow.conversations.contracts import ConversationNotFoundError

            raise ConversationNotFoundError(turn_id)
        if turn.run_id:
            return detail  # Network retries do not re-dispatch unknown external work.
        if not turn.decision or turn.action not in {"research", "revise"}:
            raise ConversationConflict("该轮次没有可确认的执行建议")
        base = None
        if turn.decision.base_run_id:
            eligible = await self._eligible_reports(detail)
            base = eligible.get(turn.decision.base_run_id)
            if base is None:
                raise ConversationConflict("来源报告已不可用，请重新发送需求")
        mode = "report_revision" if turn.action == "revise" else "research"
        seed = ContinuationInput(conversation_id=conversation_id, turn_id=turn_id, mode=mode)
        if base:
            if (base.policy.knowledge_mode, base.policy.knowledge_space) != (
                detail.conversation.policy.knowledge_mode,
                detail.conversation.policy.knowledge_space,
            ):
                raise ConversationConflict("来源报告知识空间不同，不能静默继承其检索证据")
            seed = seed.model_copy(
                update={
                    "base_run_id": base.id,
                    "base_query": base.query,
                    "base_report": base.report,
                    "base_citation_source_urls": base.draft_versions[-1].citation_source_urls if base.draft_versions else [],
                    "base_plan": base.plan,
                    "base_results": [
                        r for r in base.research_results if r.plan_version == base.plan.plan_version
                    ],
                    "base_evidence": [
                        item.model_copy(
                            update={"metadata": {**item.metadata, "origin_run_id": base.id}}
                        )
                        for item in base.evidence
                    ],
                    "base_tool_calls": self._source_records(base),
                    "base_warnings": base.warnings,
                }
            )
        record = RunRecord(
            id=str(uuid5(NAMESPACE_URL, f"atlasflow:conversation:{turn_id}")),
            query=turn.decision.resolved_request,
            source_run_id=seed.base_run_id,
            policy=detail.conversation.policy,
            auto_approve=detail.conversation.auto_approve,
            conversation_id=conversation_id,
            conversation_turn_id=turn_id,
            execution_mode=mode,
            continuation_input=seed.model_dump(mode="json"),
        )
        attached = await self.store.attach_run(
            conversation_id, turn_id, payload.expected_version, record
        )
        if attached:
            await self.runs.launch_prepared(record.id)
        return await self.detail(conversation_id)

    async def cancel(self, conversation_id: str, turn_id: str):
        async with self._lock(conversation_id):
            return await self._cancel(conversation_id, turn_id)

    async def _cancel(self, conversation_id: str, turn_id: str):
        detail = await self.detail(conversation_id)
        turn = next((t for t in detail.turns if t.id == turn_id), None)
        if turn is None or detail.conversation.active_turn_id != turn_id:
            raise ConversationConflict("该轮次不是当前活跃轮次")
        if turn.run_id:
            await self.runs.cancel(turn.run_id, request_id=f"conversation-cancel:{turn.id}")
            return await self.detail(conversation_id)
        task = self._tasks.get(turn.id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.store.finish(
            conversation_id,
            turn_id,
            status="cancelled",
            kind="notice",
            content="本轮已停止，未启动研究；已发出的模型请求仍可能产生费用。",
        )
        return await self.detail(conversation_id)

    async def reconcile_startup(self):
        for conversation_id, turn_id, status in await self.store.unfinished():
            detail = await self.store.detail(conversation_id)
            turn = next(t for t in detail.turns if t.id == turn_id)
            if turn.run_id:
                await self._sync(conversation_id)
            elif status == "routing":
                await self.store.finish(
                    conversation_id,
                    turn_id,
                    status="failed",
                    kind="error",
                    content="上次会话请求因服务重启中断，输入已保存。请求是否在远端完成未知；不会自动重新调用，请确认后重新发送。",
                    error="conversation interrupted by restart",
                )

    async def close(self):
        tasks = list(self._tasks.items())
        for _, task in tasks:
            task.cancel()
        await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        for turn_id, _ in tasks:
            # Startup reconciliation preserves inputs and marks unknown requests; no paid retry here.
            self._tasks.pop(turn_id, None)
