"""Durable safe-result journal, claims and transactional outbox.

Checkpoint writes are deliberately independent. Journal replay and outbox receipts
cover the intervening windows; no cross-database transaction is claimed.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from atlasflow.execution.context import CURRENT_EXECUTION, NodeContext
from atlasflow.execution.contracts import (
    ExecutionCancelled,
    ExecutionClaim,
    ExecutionConflict,
    InvocationLease,
    UnknownInvocation,
    decode_safe,
    digest,
    encode_safe,
)
from atlasflow.execution.ports import ExecutionRepository, RunBundle


class ExecutionToolLimit(RuntimeError):
    pass


class ExecutionRuntime:
    def __init__(self, repository: ExecutionRepository, checkpointer: Any,
                 profile_hash: str = "research-v1") -> None:
        self.repository = repository
        self.checkpointer = checkpointer
        self.profile_hash = profile_hash
        self.instance_id = str(uuid4())
        self.store: Any = None
        self.claims: dict[str, ExecutionClaim] = {}
        self.budget_counts: dict[str, int] = {}
        self.ready = False
        self._delivery_locks: dict[str, asyncio.Lock] = {}

    def delivery_lock(self, run_id: str) -> asyncio.Lock:
        return self._delivery_locks.setdefault(run_id, asyncio.Lock())

    async def start(self) -> None:
        await self.repository.start()
        self.ready = True

    def bind_store(self, store: Any) -> None:
        self.store = store

    async def close(self) -> None:
        await self.repository.close()
        self.ready = False

    async def bind_run(self, run_id: str, *, tool_limit: int,
                       retrieval_snapshot: dict[str, Any] | None = None) -> None:
        await self.repository.bind(run_id, {
            "profile_hash": self.profile_hash, "state_schema_version": 1,
            "owner": None, "fencing_epoch": 0, "cancel_requested": False,
            "deleted": False, "tool_limit": tool_limit, "tool_reserved": 0,
            "recovery_count": 0, "retrieval_snapshot": retrieval_snapshot,
        })
        bundle = await self.repository.read(run_id)
        if bundle is None:
            raise ExecutionConflict("execution binding not found")
        if bundle.control["profile_hash"] != self.profile_hash:
            raise ExecutionConflict("execution configuration is incompatible; create a new Run")
        self.budget_counts[run_id] = bundle.control["tool_reserved"]

    @staticmethod
    def _assert_claim(bundle: RunBundle, claim: ExecutionClaim) -> None:
        c = bundle.control
        if c.get("deleted") or c.get("cancel_requested"):
            raise ExecutionCancelled("Run cancelled or deleted")
        if c.get("owner") != claim.owner_token or c.get("fencing_epoch") != claim.fencing_epoch:
            raise ExecutionConflict("execution owner was fenced")

    async def claim(self, run_id: str, *, request_id: str, payload: dict[str, Any],
                    acknowledged: list[str] = (), recovery: bool = False,
                    checkpoint_id: str | None = None) -> tuple[ExecutionClaim | None, bool]:
        def operation(bundle: RunBundle):
            c = bundle.control
            if c.get("deleted") or c.get("cancel_requested"):
                raise ExecutionConflict("Run cancelled or deleted")
            if c["profile_hash"] != self.profile_hash:
                raise ExecutionConflict("execution configuration incompatible; create a new Run")
            previous = bundle.commands.get(request_id)
            request_hash = digest(payload)
            if previous:
                if previous["hash"] != request_hash:
                    raise ExecutionConflict("request_id reused with a different payload")
                return None, True
            if c.get("owner"):
                raise ExecutionConflict("Run already has an active execution")
            unknown = self._active_unknown(bundle)
            if set(unknown) - set(acknowledged):
                raise UnknownInvocation("confirm unknown calls before retry; a repeat may incur cost")
            for invocation_id in unknown:
                bundle.invocations[invocation_id]["acknowledged"] = True
            c["fencing_epoch"] += 1
            claim = ExecutionClaim(run_id=run_id, fencing_epoch=c["fencing_epoch"])
            c["owner"] = claim.owner_token
            c["instance_id"] = self.instance_id
            if recovery:
                c["recovery_count"] += 1
            bundle.commands[request_id] = {"hash": request_hash, "payload": encode_safe(payload),
                                           "status": "accepted", "claim": claim.model_dump(),
                                           "checkpoint_id": checkpoint_id,
                                           "order": len(bundle.commands)}
            return claim, False
        claim, repeated = await self.repository.mutate(run_id, operation)
        if claim is not None:
            self.claims[run_id] = claim
        return claim, repeated

    async def complete_command(self, claim: ExecutionClaim, *, recovered_approval: str | None = None) -> None:
        def operation(bundle: RunBundle):
            self._assert_claim(bundle, claim)
            for key, command in bundle.commands.items():
                if command.get("claim", {}).get("owner_token") == claim.owner_token or key == recovered_approval:
                    command["status"] = "completed"
        await self.repository.mutate(claim.run_id, operation)

    @staticmethod
    def pending_approval(bundle: RunBundle, checkpoint_id: str | None) -> tuple[str, dict[str, Any]] | None:
        for key, command in sorted(bundle.commands.items(), key=lambda pair: pair[1].get("order", 0), reverse=True):
            payload = decode_safe(command["payload"])
            if command["status"] == "accepted" and payload.get("kind") == "approval" and \
                    command.get("checkpoint_id") == checkpoint_id:
                return key, payload
        return None

    async def release(self, claim: ExecutionClaim) -> None:
        def operation(bundle: RunBundle):
            c = bundle.control
            if c.get("owner") == claim.owner_token and c["fencing_epoch"] == claim.fencing_epoch:
                c["owner"] = None
        await self.repository.mutate(claim.run_id, operation)
        if self.claims.get(claim.run_id) == claim:
            self.claims.pop(claim.run_id, None)

    async def assert_active(self, claim: ExecutionClaim) -> None:
        bundle = await self.repository.read(claim.run_id)
        if bundle is None:
            raise ExecutionConflict("execution binding missing")
        self._assert_claim(bundle, claim)

    async def cancel(self, run_id: str, *, deleted: bool = False,
                     request_id: str | None = None, under_delivery_lock: bool = False) -> None:
        if not under_delivery_lock:
            async with self.delivery_lock(run_id):
                await self.cancel(run_id, deleted=deleted, request_id=request_id,
                                  under_delivery_lock=True)
            return
        def operation(bundle: RunBundle):
            c = bundle.control
            c["cancel_requested"] = True
            c["deleted"] = c.get("deleted", False) or deleted
            c["fencing_epoch"] += 1
            c["owner"] = None
            if request_id:
                payload = {"kind": "cancel"}
                previous = bundle.commands.get(request_id)
                if previous and previous["hash"] != digest(payload):
                    raise ExecutionConflict("request_id reused with a different payload")
                bundle.commands.setdefault(request_id, {"hash": digest(payload),
                    "payload": payload, "status": "completed", "claim": {},
                    "order": len(bundle.commands)})
            for event in bundle.outbox.values():
                projected = decode_safe(event["patch"]).get("status")
                if deleted or (projected is not None and str(projected) != "cancelled"):
                    event["delivered"] = True
                    event["discarded"] = True
        await self.repository.mutate(run_id, operation)
        self.claims.pop(run_id, None)

    @staticmethod
    def _active_unknown(bundle: RunBundle) -> list[str]:
        # A committed node output makes its inner requests irrelevant to retry:
        # graph replay will return the node update without touching providers.
        completed = {item["node_key"] for item in bundle.invocations.values()
                     if item["kind"] == "node_result" and item["status"] == "succeeded"}
        return [key for key, item in bundle.invocations.items()
                if item["status"] == "unknown" and not item.get("acknowledged")
                and item.get("node_key") not in completed]

    async def recover_owner(self, run_id: str) -> RunBundle:
        def operation(bundle: RunBundle):
            c = bundle.control
            c["owner"] = None
            c["fencing_epoch"] += 1
            for item in bundle.invocations.values():
                if item["status"] == "dispatched":
                    item["status"] = "unknown"
                elif item["status"] == "reserved":
                    item["status"] = "failed"
                    item["not_sent"] = True
                    if item.get("tool_budget_counted"):
                        c["tool_reserved"] -= 1
                        item["tool_budget_counted"] = False
            return bundle
        bundle = await self.repository.mutate(run_id, operation)
        self.budget_counts[run_id] = bundle.control["tool_reserved"]
        self.claims.pop(run_id, None)
        return bundle

    async def begin_invocation(self, context: NodeContext, *, kind: str, name: str,
                               inputs: Any, replayable: bool = True,
                               consumes_tool_budget: bool = False,
                               slot: str | None = None) -> InvocationLease:
        slot = slot or context.slot(kind, name)
        input_hash = digest(inputs)
        claim = context.claim
        def operation(bundle: RunBundle):
            self._assert_claim(bundle, claim)
            matches = sorted((item for item in bundle.invocations.values() if item["slot"] == slot),
                             key=lambda item: item.get("attempt", 0))
            for item in matches:
                if item["input_hash"] != input_hash and kind in {"llm", "node_result"}:
                    raise ExecutionConflict("durable invocation input changed")
            for item in reversed(matches):
                if item["input_hash"] != input_hash:
                    continue
                if item["status"] == "succeeded" and item.get("replayable"):
                    return InvocationLease(run_id=claim.run_id, invocation_id=item["id"],
                                           slot=slot, claim=claim, replay=item["result"], replayed=True)
                if item["status"] in {"reserved", "dispatched"}:
                    raise ExecutionConflict("invocation already active")
                if item["status"] == "unknown" and not item.get("acknowledged"):
                    raise UnknownInvocation("unknown invocation requires explicit acknowledgment")
            c = bundle.control
            if consumes_tool_budget:
                if c["tool_reserved"] >= c["tool_limit"]:
                    raise ExecutionToolLimit("Run tool budget exhausted")
                c["tool_reserved"] += 1
            invocation_id = str(uuid4())
            bundle.invocations[invocation_id] = {
                "id": invocation_id, "slot": slot, "input_hash": input_hash,
                "kind": kind, "name": name, "node_key": context.node_key,
                "status": "reserved", "replayable": replayable, "result": None,
                "tool_budget_counted": consumes_tool_budget, "acknowledged": False,
                "created_at": datetime.now(UTC).isoformat(),
                "attempt": len(matches),
            }
            return InvocationLease(run_id=claim.run_id, invocation_id=invocation_id,
                                   slot=slot, claim=claim)
        lease = await self.repository.mutate(claim.run_id, operation)
        bundle = await self.repository.read(claim.run_id)
        self.budget_counts[claim.run_id] = bundle.control["tool_reserved"]
        return lease

    async def dispatch(self, lease: InvocationLease) -> None:
        def operation(bundle: RunBundle):
            self._assert_claim(bundle, lease.claim)
            bundle.invocations[lease.invocation_id]["status"] = "dispatched"
        await self.repository.mutate(lease.run_id, operation)

    async def confirm(self, lease: InvocationLease, result: Any, *, replayable: bool | None = None) -> None:
        safe = encode_safe(result)
        def operation(bundle: RunBundle):
            self._assert_claim(bundle, lease.claim)
            invocation = bundle.invocations[lease.invocation_id]
            invocation.update(status="succeeded", result=safe)
            if replayable is not None:
                invocation["replayable"] = replayable
        await self.repository.mutate(lease.run_id, operation)

    async def has_tool_replay(self, context: NodeContext) -> bool:
        bundle = await self.repository.read(context.claim.run_id)
        return bundle is not None and any(item["node_key"] == context.node_key
            and item["kind"] == "tool_decision" and item["status"] == "succeeded"
            and item["replayable"] for item in bundle.invocations.values())

    async def fail(self, lease: InvocationLease) -> None:
        def operation(bundle: RunBundle):
            self._assert_claim(bundle, lease.claim)
            bundle.invocations[lease.invocation_id]["status"] = "failed"
        await self.repository.mutate(lease.run_id, operation)

    async def finish_http(self, lease: InvocationLease, *, outcome: str,
                          request_id: str | None, http_status: int | None,
                          usage: dict[str, Any] | None) -> None:
        import math
        safe_usage = {key: value for key, value in (usage or {}).items()
                      if key in {"prompt_tokens", "completion_tokens", "total_tokens", "cost"}
                      and isinstance(value, (int, float)) and not isinstance(value, bool)
                      and math.isfinite(value) and value >= 0}
        def operation(bundle: RunBundle):
            self._assert_claim(bundle, lease.claim)
            bundle.invocations[lease.invocation_id].update(
                status=outcome, request_id=request_id, http_status=http_status,
                usage=safe_usage or None)
        await self.repository.mutate(lease.run_id, operation)
        if outcome == "unknown":
            raise UnknownInvocation("external request outcome unknown; confirm before retry (possible duplicate cost)")

    async def annotate_http(self, lease: InvocationLease, *, request_group: str,
                            attempt_number: int, requested_model: str | None) -> None:
        def operation(bundle: RunBundle):
            self._assert_claim(bundle, lease.claim)
            bundle.invocations[lease.invocation_id].update(
                request_group=request_group, attempt_number=attempt_number,
                requested_model=requested_model)
        await self.repository.mutate(lease.run_id, operation)

    @staticmethod
    def provider_summary(bundle: RunBundle) -> dict[str, Any]:
        attempts = [item for item in bundle.invocations.values() if item["kind"] == "http"
                    and item["status"] != "reserved" and not item.get("not_sent")]
        usage = [item["usage"] for item in attempts if item.get("usage")]
        costs = [item["cost"] for item in usage if "cost" in item]
        tokens = [item["total_tokens"] for item in usage if "total_tokens" in item]
        return {
            "http_attempts": len(attempts),
            "provider_requests": len({item.get("request_group", item["id"]) for item in attempts}),
            "model_calls": len({item.get("request_group", item["id"]) for item in attempts
                               if item["name"] == "/chat/completions"}),
            "unknown_attempts": sum(item["status"] == "unknown" for item in attempts),
            "known_cost": sum(costs) if costs else None,
            "known_tokens": sum(tokens) if tokens else None,
            "token_usage_coverage": len(tokens) / len(attempts) if attempts else None,
            "cost_usage_coverage": len(costs) / len(attempts) if attempts else None,
        }

    async def emit(self, run_id: str, event: Any, *, patch: dict[str, Any] | None = None,
                   logical_key: str | None = None) -> None:
        context = CURRENT_EXECUTION.get()
        key = logical_key or (context.slot("event", event.event_type.value) if context else event.event_id)
        event_id = str(uuid5(NAMESPACE_URL, f"atlasflow:{run_id}:{key}"))
        safe_event = event.model_copy(update={"event_id": event_id}).model_dump(mode="json")
        def operation(bundle: RunBundle):
            if bundle.control.get("deleted"):
                return
            projected_status = (patch or {}).get("status")
            if bundle.control.get("cancel_requested") and projected_status is not None and \
                    str(projected_status) != "cancelled":
                return
            if context:
                self._assert_claim(bundle, context.claim)
            bundle.outbox.setdefault(event_id, {"event": encode_safe(safe_event),
                                               "patch": encode_safe(patch or {}), "delivered": False,
                                               "order": len(bundle.outbox)})
        await self.repository.mutate(run_id, operation)
        await self.flush_run(run_id)

    async def flush_run(self, run_id: str) -> None:
        async with self.delivery_lock(run_id):
            await self._flush_run_locked(run_id)

    async def _flush_run_locked(self, run_id: str) -> None:
        if self.store is None:
            raise ExecutionConflict("Run projection store not bound")
        bundle = await self.repository.read(run_id)
        if bundle is None or bundle.control.get("deleted"):
            return
        for event_id, envelope in sorted(bundle.outbox.items(), key=lambda pair: pair[1]["order"]):
            if envelope["delivered"]:
                continue
            projected_status = decode_safe(envelope["patch"]).get("status")
            if bundle.control.get("cancel_requested") and projected_status is not None and \
                    str(projected_status) != "cancelled":
                continue
            await self.store.apply_outbox(envelope["event"], decode_safe(envelope["patch"]))
            def delivered(current: RunBundle, event_id=event_id):
                current.outbox[event_id]["delivered"] = True
            await self.repository.mutate(run_id, delivered)

    async def journal_node(self, name: str, function: Callable, state: dict[str, Any],
                           config: dict[str, Any]) -> Any:
        claim = self.claims.get(state["run_id"])
        if claim is None:
            raise ExecutionConflict("graph execution has no claim")
        metadata = config.get("metadata", {})
        node_key = str(metadata.get("langgraph_checkpoint_ns") or
                       config.get("configurable", {}).get("checkpoint_ns") or
                       digest({"name": name, "state": state}))
        context = NodeContext(self, claim, node_key)
        token = CURRENT_EXECUTION.set(context)
        try:
            lease = await self.begin_invocation(context, kind="node_result", name=name,
                                                inputs=state, slot=f"{node_key}:node_result")
            if lease.replayed:
                return decode_safe(lease.replay)
            # A node marker is not itself an external request. Interrupts retain
            # it as reserved and are safely released during startup reconciliation.
            try:
                result = await function(state)
            except BaseException:
                await self.fail(lease)
                raise
            await self.confirm(lease, result)
            return decode_safe(encode_safe(result))
        finally:
            CURRENT_EXECUTION.reset(token)


class DurableToolBudget:
    """The count is durable; the synchronous mirror is only a conservative hint."""
    def __init__(self, runtime: ExecutionRuntime) -> None:
        self.runtime = runtime

    def remaining(self, run_id: str, *, already_used: int, limit: int) -> int:
        return max(0, limit - max(already_used, self.runtime.budget_counts.get(run_id, 0)))

    def clear(self, run_id: str) -> None:
        # Terminal cleanup never restores a Run's consumed allowance.
        pass

    async def reserve(self, run_id: str, *, already_used: int, limit: int) -> bool:
        raise ExecutionConflict("durable budget must be reserved with an invocation")
