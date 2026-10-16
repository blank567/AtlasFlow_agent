from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path

from atlasflow.agents.tool_runtime import ToolRuntime
from atlasflow.agents.workflow import ResearchWorkflow
from atlasflow.bootstrap import _workflow_options, build_openrouter_providers
from atlasflow.config import Settings
from atlasflow.evaluation.budget import EvaluationBudget, evaluation_budget_context
from atlasflow.evaluation.contracts import EvaluationBudgetSpec
from atlasflow.evaluation.langsmith_adapter import private_evaluation_tracing
from atlasflow.knowledge.application import KnowledgePlatform
from atlasflow.knowledge.infrastructure import LocalBlobStore
from atlasflow.knowledge.infrastructure.postgres_repository import PostgresKnowledgeRepository
from atlasflow.knowledge.retrieval import RetrievalService
from atlasflow.service import InMemoryRunStore, RunService
from atlasflow.tools.catalog import build_tool_registry
from atlasflow.tools.knowledge_search import KnowledgeSearchTool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run AtlasFlow's live regression dataset against configured providers."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Acknowledge that this command makes real network calls and may incur charges.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if not args.live:
        raise SystemExit(
            "Refusing to call real providers without --live. "
            "Run `python evals/run_local.py --live` after checking API quotas and costs."
        )
    dataset_path = Path(__file__).with_name("dataset.jsonl")
    cases = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    settings = Settings(knowledge_worker_enabled=False, langsmith_tracing=False)
    providers = build_openrouter_providers(settings)
    repository = PostgresKnowledgeRepository(settings.knowledge_database_url)
    knowledge = KnowledgePlatform(
        repository=repository,
        blob_store=LocalBlobStore(settings.knowledge_blob_root),
        embedding_provider=providers.embedding,
        retrieval=RetrievalService(
            repository=repository,
            embedding_provider=providers.embedding,
            rerank_provider=providers.reranker,
        ),
        default_space=settings.knowledge_default_space,
        worker_enabled=False,
    )
    registry = build_tool_registry(settings, web_search=providers.web_search_tool)
    registry.register(KnowledgeSearchTool(knowledge))
    store = InMemoryRunStore()
    runtime = ToolRuntime(
        registry=registry,
        client=providers.model.client,
        model=providers.model.model,
        event_sink=store.append_event,
    )
    workflow = ResearchWorkflow(
        model=providers.model,
        event_sink=store.append_event,
        tool_runtime=runtime,
        trace_segment_sink=store.upsert_trace_segment,
        **_workflow_options(settings),
    )
    service = RunService(store, workflow)
    budget = EvaluationBudget(EvaluationBudgetSpec())
    passed = 0
    # Read-only connection initialization; never ensure/create a default space
    # or start ingestion workers, and never write the user's production Run store.
    try:
        await repository.initialize()
        with private_evaluation_tracing(), evaluation_budget_context(budget):
            async with asyncio.timeout(budget.spec.max_elapsed_seconds):
                for case in cases:
                    run = await service.execute_and_wait(case["query"])
                    plan_size = len(run.plan.tasks) if run.plan else 0
                    status_ok = run.status.value in case["accepted_statuses"]
                    plan_ok = case["min_tasks"] <= plan_size <= case["max_tasks"]
                    quorum_ok = len(run.research_results) >= math.ceil(
                        plan_size * case["min_research_ratio"]
                    )
                    report_ok = bool(run.report and run.report.strip())
                    audit_ok = bool(run.route_history and run.events)
                    case_passed = status_ok and plan_ok and quorum_ok and report_ok and audit_ok
                    passed += int(case_passed)
                    print(
                        json.dumps(
                            {
                                "query": case["query"],
                                "status": run.status.value,
                                "status_ok": status_ok,
                                "plan_ok": plan_ok,
                                "quorum_ok": quorum_ok,
                                "report_ok": report_ok,
                                "audit_ok": audit_ok,
                                "plan_tasks": plan_size,
                                "research_results": len(run.research_results),
                                "quality_scores": [item.score for item in run.quality_history],
                                "passed": case_passed,
                            },
                            ensure_ascii=False,
                        )
                    )
    finally:
        await service.close()
        await repository.close()
    print(f"\nPassed {passed}/{len(cases)} cases")
    if passed != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
