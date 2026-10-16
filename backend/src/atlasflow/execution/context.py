"""Safe per-node context; HTTP hooks never receive raw request content."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from atlasflow.execution.contracts import ExecutionClaim, HttpAttemptToken


@dataclass
class NodeContext:
    runtime: Any
    claim: ExecutionClaim
    node_key: str
    counters: dict[str, int] = field(default_factory=dict)
    http_groups: dict[tuple[str, str, str | None], str] = field(default_factory=dict)

    def slot(self, kind: str, name: str) -> str:
        key = f"{kind}:{name}"
        ordinal = self.counters.get(key, 0)
        self.counters[key] = ordinal + 1
        return f"{self.node_key}:{key}:{ordinal}"


CURRENT_EXECUTION: ContextVar[NodeContext | None] = ContextVar("atlasflow_execution", default=None)
CURRENT_INVOCATION: ContextVar[str | None] = ContextVar("atlasflow_invocation_scope", default=None)


async def before_http_attempt(
    *, endpoint: str, requested_model: str | None = None, attempt_number: int = 1
) -> HttpAttemptToken | None:
    context = CURRENT_EXECUTION.get()
    if context is None:
        return None
    parent = CURRENT_INVOCATION.get() or context.claim.owner_token
    lease = await context.runtime.begin_invocation(
        context, kind="http", name=endpoint,
        inputs={"endpoint": endpoint, "model": requested_model, "attempt": attempt_number},
        replayable=False,
        slot=context.slot("http", f"{parent}:{endpoint}"),
    )
    group_key = (parent, endpoint, requested_model)
    if attempt_number == 1 or group_key not in context.http_groups:
        context.http_groups[group_key] = context.slot("http_request", f"{parent}:{endpoint}")
    await context.runtime.annotate_http(lease,
        request_group=context.http_groups[group_key], attempt_number=attempt_number,
        requested_model=requested_model)
    await context.runtime.dispatch(lease)
    return HttpAttemptToken(lease=lease)


async def after_http_attempt(
    token: HttpAttemptToken | None, *, outcome: Literal["succeeded", "failed", "unknown"],
    request_id: str | None = None, http_status: int | None = None,
    usage: dict[str, Any] | None = None,
) -> None:
    context = CURRENT_EXECUTION.get()
    if token is None or context is None:
        return
    await context.runtime.finish_http(
        token.lease, outcome=outcome, request_id=request_id, http_status=http_status, usage=usage
    )
