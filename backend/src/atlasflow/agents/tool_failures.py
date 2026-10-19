"""Run-scoped transient-failure circuits restored from existing safe tool records."""

from collections.abc import Sequence

from atlasflow.schemas import ToolCallRecord
from atlasflow.tools.base import BaseTool


class ToolFailureGuard:
    """Share completed outcomes across parallel stages; no network or database writes.

    Records are bounded by the Run tool budget. Persisted prior_calls restore the
    circuit on recovery, while live observations cover parallel branches whose
    graph-state snapshots do not yet contain each other's completed calls.
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[tuple[str, str], ToolCallRecord]] = {}

    def observe(self, run_id: str, records: Sequence[ToolCallRecord]) -> None:
        for record in records:
            if record.reused_from_call_id:
                continue
            key = (record.call_id, record.created_at.isoformat())
            self._records.setdefault(run_id, {})[key] = record

    def blocking_record(
        self, run_id: str, tool: BaseTool | type[BaseTool], arguments: dict
    ) -> ToolCallRecord | None:
        limit = tool.transient_failure_limit
        scope = tool.transient_failure_scope(arguments) if limit else None
        if not scope:
            return None
        consecutive = 0
        for record in sorted(
            self._records.get(run_id, {}).values(), key=lambda item: item.created_at.timestamp(),
            reverse=True,
        ):
            if record.tool_name != tool.name or tool.transient_failure_scope(record.arguments) != scope:
                continue
            if record.success or record.retryable is not True:
                break
            consecutive += 1
            if consecutive >= limit:
                return record
        return None

    def clear(self, run_id: str) -> None:
        self._records.pop(run_id, None)
