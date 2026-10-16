"""Subprocess-only deterministic graph driver. Never calls a remote model.

The parent test supplies a unique disposable schema and an E-drive SQLite path.
It kills this process externally: no graceful close simulates the crash window.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from atlasflow.agents.contracts import RunPolicy
from atlasflow.agents.workflow import ResearchWorkflow
from atlasflow.execution import ResumeRunRequest, create_postgres_execution
from atlasflow.schemas import RunStatus
from atlasflow.service import RunService
from atlasflow.storage import SQLiteRunStore

from backend.tests.fakes import FakeModelGateway, GatewayScenario
from backend.tests.test_execution_recovery import wait_status


async def drive(args):
    runtime = create_postgres_execution(os.environ["ATLASFLOW_TEST_DATABASE_URL"],
        schema=args.schema, profile_hash="subprocess-fixture-v1")
    store = SQLiteRunStore(args.store, recover_interrupted=False, defer_open=True)

    class CrashGateway(FakeModelGateway):
        async def analyze_task(self, query, task, dependencies=(), **kwargs):
            if args.phase == "start" and task.task_id != "p1-t1":
                await asyncio.sleep(300)
            return await super().analyze_task(query, task, dependencies, **kwargs)

    gateway = CrashGateway(GatewayScenario(research_delay_seconds=0.001))
    await runtime.start()
    await store.open()
    workflow = ResearchWorkflow(model=gateway, event_sink=store.append_event,
                                execution=runtime, trace_segment_sink=store.upsert_trace_segment)
    service = RunService(store, workflow, execution=runtime)
    try:
        if args.phase == "start":
            run = await service.create("subprocess partial-wave crash fixture",
                                       policy=RunPolicy(knowledge_mode="off"))
            print(json.dumps({"run_id": run.id}), flush=True)
            await service._tasks[run.id]
        else:
            await service.reconcile_startup()
            recovery = await service.inspect_recovery(args.run_id)
            assert recovery.available, recovery.reason
            await service.resume(args.run_id, ResumeRunRequest(
                request_id="subprocess-manual-resume", expected_checkpoint_id=recovery.checkpoint_id,
                acknowledged_unknown_call_ids=[call.call_id for call in recovery.unknown_calls]))
            final = await wait_status(store, args.run_id, RunStatus.terminal())
            task = service._tasks.get(args.run_id)
            if task is not None:
                await task
            print(json.dumps({"status": final.status.value, "run_id": final.id,
                              "research_calls": gateway.call_counts.get("analyze_task", 0),
                              "planner_calls": gateway.call_counts.get("create_plan", 0),
                              "tasks": len(final.research_results),
                              "event_ids": [event.event_id for event in final.events],
                              "sequences": [event.sequence for event in final.events]}), flush=True)
    finally:
        await service.close()
        await store.close()
        await runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--phase", choices=["start", "resume"], required=True)
    parser.add_argument("--run-id")
    asyncio.run(drive(parser.parse_args()))
