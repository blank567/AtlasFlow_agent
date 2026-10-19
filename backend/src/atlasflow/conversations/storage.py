"""Three business tables in the existing SQLite file; no second connection/cache."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from atlasflow.conversations.contracts import (
    Conversation,
    ConversationConflict,
    ConversationDecision,
    ConversationDetail,
    ConversationNotFoundError,
    CreateConversation,
    Message,
    Requirement,
    SendMessage,
    Turn,
)
from atlasflow.execution.contracts import digest
from atlasflow.schemas import RunRecord, RunStatus, utc_now

if TYPE_CHECKING:
    from atlasflow.storage import SQLiteRunStore

_DDL = (
    """CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY, version INTEGER NOT NULL, payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS conversation_turns (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, request_id TEXT NOT NULL,
        status TEXT NOT NULL, run_id TEXT NULL, payload_json TEXT NOT NULL,
        UNIQUE(conversation_id, request_id), UNIQUE(run_id),
        FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS conversation_messages (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL,
        sequence INTEGER NOT NULL, payload_json TEXT NOT NULL,
        UNIQUE(conversation_id, sequence),
        FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE)""",
    "CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_conversation_turns_parent ON conversation_turns(conversation_id)",
)


class SQLiteConversationStore:
    def __init__(self, runs: SQLiteRunStore):
        self.runs = runs
        self._ready = False

    async def initialize(self):
        if self._ready:
            return
        async with self.runs.transaction() as connection:
            for statement in _DDL:
                connection.execute(statement)
        self._ready = True

    @staticmethod
    def _conversation(connection, conversation_id, *, allow_deleting=False) -> Conversation:
        row = connection.execute(
            "SELECT payload_json FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise ConversationNotFoundError(conversation_id)
        value = Conversation.model_validate_json(row[0])
        if value.deleting and not allow_deleting:
            raise ConversationConflict("会话正在删除，不能发送消息或启动执行；请完成或重试删除")
        return value

    @staticmethod
    def _turn(connection, conversation_id, turn_id) -> Turn:
        row = connection.execute(
            "SELECT payload_json FROM conversation_turns WHERE id=? AND conversation_id=?",
            (turn_id, conversation_id),
        ).fetchone()
        if row is None:
            raise ConversationNotFoundError(turn_id)
        return Turn.model_validate_json(row[0])

    @staticmethod
    def _save_conversation(connection, conversation: Conversation, *, semantic: bool = True):
        conversation.version += 1
        if semantic:
            conversation.content_version = conversation.version
        conversation.updated_at = utc_now()
        connection.execute(
            "UPDATE conversations SET version=?,payload_json=?,updated_at=? WHERE id=?",
            (
                conversation.version,
                conversation.model_dump_json(),
                conversation.updated_at.isoformat(),
                conversation.id,
            ),
        )

    @staticmethod
    def _save_turn(connection, turn: Turn):
        turn.updated_at = utc_now()
        connection.execute(
            "UPDATE conversation_turns SET status=?,run_id=?,payload_json=? WHERE id=?",
            (turn.status, turn.run_id, turn.model_dump_json(), turn.id),
        )

    @staticmethod
    def _append(connection, message: Message):
        message.sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM conversation_messages WHERE conversation_id=?",
            (message.conversation_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO conversation_messages VALUES (?,?,?,?,?)",
            (
                message.id,
                message.conversation_id,
                message.turn_id,
                message.sequence,
                message.model_dump_json(),
            ),
        )

    async def create(self, payload: CreateConversation) -> Conversation:
        await self.initialize()
        conversation = Conversation(
            title=payload.title, policy=payload.policy, auto_approve=payload.auto_approve
        )
        async with self.runs.transaction() as connection:
            connection.execute(
                "INSERT INTO conversations VALUES (?,?,?,?)",
                (
                    conversation.id,
                    0,
                    conversation.model_dump_json(),
                    conversation.updated_at.isoformat(),
                ),
            )
        return conversation

    async def list(self, *, page: int = 1, page_size: int = 30):
        await self.initialize()
        async with self.runs.transaction() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM conversations ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size),
            ).fetchall()
            count = connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        return [Conversation.model_validate_json(row[0]) for row in rows], count

    async def detail(self, conversation_id: str) -> ConversationDetail:
        await self.initialize()
        async with self.runs.transaction() as connection:
            conversation = self._conversation(connection, conversation_id, allow_deleting=True)
            messages = [
                Message.model_validate_json(row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM conversation_messages WHERE conversation_id=? ORDER BY sequence",
                    (conversation_id,),
                ).fetchall()
            ]
            turns = [
                Turn.model_validate_json(row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM conversation_turns WHERE conversation_id=? ORDER BY rowid",
                    (conversation_id,),
                ).fetchall()
            ]
        return ConversationDetail(conversation=conversation, messages=messages, turns=turns)

    @staticmethod
    def _deletion_run_ids(connection, conversation_id: str) -> list[str]:
        """Own actual Runs, not report/source references or standalone clones."""

        linked = {
            row[0]
            for row in connection.execute(
                "SELECT run_id FROM conversation_turns WHERE conversation_id=? AND run_id IS NOT NULL",
                (conversation_id,),
            ).fetchall()
        }
        rows = connection.execute(
            """SELECT p.run_id,p.snapshot_json FROM run_projections p
               WHERE json_extract(p.snapshot_json,'$.conversation_id')=?
                  OR EXISTS (SELECT 1 FROM conversation_turns t
                             WHERE t.conversation_id=? AND t.run_id=p.run_id)""",
            (conversation_id, conversation_id),
        ).fetchall()
        for row in rows:
            if json.loads(row[1]).get("conversation_id") != conversation_id:
                raise ConversationConflict("关联Run归属不一致，已阻止删除；未删除任何本地记录")
            linked.add(row[0])
        return sorted(linked)

    async def begin_delete(self, conversation_id: str, expected_version: int) -> list[str]:
        await self.initialize()
        async with self.runs.transaction() as connection:
            conversation = self._conversation(connection, conversation_id, allow_deleting=True)
            run_ids = self._deletion_run_ids(connection, conversation_id)
            if not conversation.deleting:
                if not conversation.content_version <= expected_version <= conversation.version:
                    raise ConversationConflict("会话已更新，请重新核对删除范围后确认")
                conversation.deleting = True
                self._save_conversation(connection, conversation)
            return run_ids

    async def delete(self, conversation_id: str, run_ids: list[str]) -> int:
        """One SQLite commit for messages, turns, Runs, projections and events.

        Caller must hold RunService's deletion guard and fence durable work
        BEFORE this transaction. Any failed validation rolls back ALL rows.
        """

        async with self.runs.transaction() as connection:
            conversation = self._conversation(connection, conversation_id, allow_deleting=True)
            if not conversation.deleting or self._deletion_run_ids(
                connection, conversation_id
            ) != sorted(set(run_ids)):
                raise ConversationConflict("删除范围已变化，未删除记录，请重试")
            for run_id in run_ids:
                row = connection.execute(
                    "SELECT status FROM run_projections WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is not None and row[0] not in RunStatus.terminal():
                    raise ConversationConflict("关联Run尚未停止，未删除本地记录，请重试")
            deleted = sum(
                connection.execute("DELETE FROM runs WHERE id=?", (run_id,)).rowcount
                for run_id in run_ids
            )
            connection.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            return deleted

    async def reserve_message(
        self, conversation_id: str, payload: SendMessage
    ) -> tuple[Turn, bool]:
        await self.initialize()
        fingerprint = digest(payload.model_dump(mode="json"))
        async with self.runs.transaction() as connection:
            conversation = self._conversation(connection, conversation_id)
            prior = connection.execute(
                "SELECT payload_json FROM conversation_turns WHERE conversation_id=? AND request_id=?",
                (conversation_id, payload.request_id),
            ).fetchone()
            if prior:
                turn = Turn.model_validate_json(prior[0])
                if turn.request_hash != fingerprint:
                    raise ConversationConflict("request_id已用于其他消息，请勿重复使用")
                return turn, False
            if not conversation.content_version <= payload.expected_version <= conversation.version:
                raise ConversationConflict("会话已更新，请刷新后重试；未发送的文本不会丢失")
            if conversation.active_turn_id:
                active = self._turn(connection, conversation_id, conversation.active_turn_id)
                if active.status != "awaiting_confirmation":
                    raise ConversationConflict("当前轮次尚未结束，请先完成审批、恢复或停止执行")
                active.status = "cancelled"  # Editing an unexecuted proposal is safe.
                active.error = "已由新的用户消息替代，未启动研究"
                self._save_turn(connection, active)
            if (
                payload.base_run_id
                and not connection.execute(
                    "SELECT 1 FROM conversation_turns WHERE conversation_id=? AND run_id=?",
                    (conversation_id, payload.base_run_id),
                ).fetchone()
            ):
                raise ConversationConflict("只能选择当前会话中的报告")
            message = Message(
                conversation_id=conversation_id, turn_id="", role="user", content=payload.content
            )
            turn = Turn(
                conversation_id=conversation_id,
                user_message_id=message.id,
                request_id=payload.request_id,
                request_hash=fingerprint,
                requested_base_run_id=payload.base_run_id,
                requirements_snapshot=conversation.requirements,
            )
            message.turn_id = turn.id
            connection.execute(
                "INSERT INTO conversation_turns VALUES (?,?,?,?,?,?)",
                (
                    turn.id,
                    conversation_id,
                    turn.request_id,
                    turn.status,
                    None,
                    turn.model_dump_json(),
                ),
            )
            self._append(connection, message)
            conversation.active_turn_id = turn.id
            if conversation.title == "新会话":
                conversation.title = payload.content[:48]
            self._save_conversation(connection, conversation)
        return turn, True

    async def save_decision(
        self,
        conversation_id: str,
        turn_id: str,
        decision: ConversationDecision,
        requirements: dict[str, Requirement],
        content: str | None,
        status: str,
    ) -> bool:
        async with self.runs.transaction() as connection:
            conversation = self._conversation(connection, conversation_id)
            turn = self._turn(connection, conversation_id, turn_id)
            if conversation.active_turn_id != turn_id or turn.status != "routing":
                return False
            turn.decision, turn.action, turn.status = decision, decision.action, status
            turn.requirements_snapshot = requirements
            conversation.requirements = requirements
            if status not in {"routing", "awaiting_confirmation"}:
                conversation.active_turn_id = None
            self._save_turn(connection, turn)
            if content:
                self._append(
                    connection,
                    Message(
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        role="assistant",
                        kind="clarification" if status == "awaiting_clarification" else "proposal",
                        content=content,
                    ),
                )
            self._save_conversation(connection, conversation)
            return True

    async def finish(
        self,
        conversation_id: str,
        turn_id: str,
        *,
        status: str,
        content: str,
        kind: str = "text",
        error: str | None = None,
        current_report: str | None = None,
    ) -> bool:
        async with self.runs.transaction() as connection:
            conversation = self._conversation(connection, conversation_id, allow_deleting=True)
            if conversation.deleting:
                return False
            turn = self._turn(connection, conversation_id, turn_id)
            if turn.status in {"completed", "completed_with_warnings", "failed", "cancelled"}:
                return False
            turn.status, turn.error = status, error
            self._save_turn(connection, turn)
            self._append(
                connection,
                Message(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    role="assistant",
                    kind=kind,
                    content=content,
                    run_id=turn.run_id
                    or (turn.decision.base_run_id if turn.decision and kind == "text" else None),
                ),
            )
            if conversation.active_turn_id == turn_id:
                conversation.active_turn_id = None
            if current_report:
                conversation.current_report_run_id = current_report
            self._save_conversation(connection, conversation)
            return True

    async def attach_run(
        self, conversation_id: str, turn_id: str, expected_version: int, record: RunRecord
    ) -> bool:
        """Reserve the deterministic Run and link it to its turn atomically."""
        async with self.runs.transaction() as connection:
            conversation = self._conversation(connection, conversation_id)
            turn = self._turn(connection, conversation_id, turn_id)
            if turn.run_id:
                return False
            if (
                not conversation.content_version <= expected_version <= conversation.version
                or conversation.active_turn_id != turn_id
            ):
                raise ConversationConflict("会话已更新，请重新核验执行建议")
            if turn.status != "awaiting_confirmation":
                raise ConversationConflict("该轮次不在等待执行确认")
            if (
                record.source_run_id
                and not connection.execute(
                    "SELECT 1 FROM runs WHERE id=?", (record.source_run_id,)
                ).fetchone()
            ):
                raise ConversationConflict("来源报告已删除，请重新发送需求")
            self.runs.insert_prepared(connection, record)
            turn.run_id, turn.status = record.id, "running"
            for requirement in conversation.requirements.values():
                requirement.confirmed = True
            turn.requirements_snapshot = conversation.requirements
            self._save_turn(connection, turn)
            self._save_conversation(connection, conversation)
            return True

    async def sync_status(self, conversation_id: str, turn_id: str, status: str):
        async with self.runs.transaction() as connection:
            conversation = self._conversation(connection, conversation_id, allow_deleting=True)
            if conversation.deleting:
                return
            turn = self._turn(connection, conversation_id, turn_id)
            if turn.status == status:
                return
            turn.status = status
            self._save_turn(connection, turn)
            self._save_conversation(connection, conversation)

    async def save_trace(self, conversation_id, turn_id, segment):
        async with self.runs.transaction() as connection:
            conversation = self._conversation(connection, conversation_id, allow_deleting=True)
            if conversation.deleting:
                return
            turn = self._turn(connection, conversation_id, turn_id)
            turn.trace_segments = [
                item for item in turn.trace_segments if item["segment_id"] != segment["segment_id"]
            ] + [segment]
            self._save_turn(connection, turn)
            self._save_conversation(connection, conversation, semantic=False)

    async def unfinished(self) -> list[tuple[str, str, str]]:
        await self.initialize()
        async with self.runs.transaction() as connection:
            return [
                tuple(row)
                for row in connection.execute(
                    "SELECT conversation_id,id,status FROM conversation_turns WHERE status IN ('routing','running','waiting_approval','interrupted')"
                ).fetchall()
            ]
