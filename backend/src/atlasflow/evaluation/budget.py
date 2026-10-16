from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic
from typing import Any

from atlasflow.evaluation.contracts import EvaluationBudgetSpec


class EvaluationBudgetExceeded(BaseException):
    """A stop, never a retryable provider/tool failure."""


class EvaluationBudget:
    def __init__(self, spec: EvaluationBudgetSpec, on_reserve=None):
        self.spec = spec
        self.started = monotonic()
        self.lock = asyncio.Lock()
        self.attempts: list[dict[str, Any]] = []
        self.stop_reason: str | None = None
        self.on_reserve = on_reserve

    @property
    def remaining_seconds(self) -> float:
        return max(0, self.spec.max_elapsed_seconds - (monotonic() - self.started))

    async def reserve(self, kind: str, operation: str) -> None:
        async with self.lock:
            if self.stop_reason:
                raise EvaluationBudgetExceeded(self.stop_reason)
            if not self.remaining_seconds:
                self.stop_reason = "elapsed_budget_exhausted"
            elif len(self.attempts) >= self.spec.max_http_attempts:
                self.stop_reason = "http_attempt_budget_exhausted"
            if self.stop_reason:
                raise EvaluationBudgetExceeded(self.stop_reason)
            self.attempts.append(
                {
                    "sequence": len(self.attempts) + 1,
                    "kind": kind[:80],
                    "operation": operation[:120],
                }
            )
            if self.on_reserve is not None:
                try:
                    await self.on_reserve(self.ledger())
                except Exception as exc:
                    self.stop_reason = "budget_ledger_persistence_failed"
                    raise EvaluationBudgetExceeded(self.stop_reason) from exc

    def stop(self, reason: str) -> None:
        self.stop_reason = self.stop_reason or reason

    def ledger(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for attempt in self.attempts:
            kinds[attempt["kind"]] = kinds.get(attempt["kind"], 0) + 1
        return {
            "http_attempts": len(self.attempts),
            "by_kind": kinds,
            "max_http_attempts": self.spec.max_http_attempts,
            "elapsed_seconds": round(monotonic() - self.started, 3),
            "max_elapsed_seconds": self.spec.max_elapsed_seconds,
            "stop_reason": self.stop_reason,
        }


_CURRENT_BUDGET: ContextVar[EvaluationBudget | None] = ContextVar("evaluation_budget", default=None)


@contextmanager
def evaluation_budget_context(budget: EvaluationBudget):
    token = _CURRENT_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _CURRENT_BUDGET.reset(token)


async def reserve_http_attempt(kind: str, operation: str) -> None:
    budget = _CURRENT_BUDGET.get()
    if budget is not None:
        await budget.reserve(kind, operation)


def current_evaluation_budget() -> EvaluationBudget | None:
    return _CURRENT_BUDGET.get()
