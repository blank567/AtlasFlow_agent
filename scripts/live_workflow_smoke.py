from __future__ import annotations

import asyncio
import json

from atlasflow.bootstrap import build_container
from atlasflow.config import Settings


async def main() -> None:
    """Exercise every runtime Agent and provider without printing sensitive content."""

    settings = Settings()
    container = build_container(settings)
    run = await container.run_service.execute_and_wait(
        "AtlasFlow 如何通过工具调用和 RAG 提升多 Agent 结果可信度？"
    )
    print(
        json.dumps(
            {
                "status": run.status.value,
                "plan_version": run.plan.plan_version if run.plan else None,
                "plan_tasks": len(run.plan.tasks) if run.plan else 0,
                "plan_versions": len(run.plans),
                "research_results": len(run.research_results),
                "critic_reviews": len(run.critique_history),
                "draft_versions": len(run.draft_versions),
                "quality_scores": [item.score for item in run.quality_history],
                "routes": len(run.route_history),
                "report_characters": len(run.report or ""),
                "warnings": run.warnings,
                "metrics": run.metrics.model_dump(mode="json"),
                "error": run.error,
            },
            ensure_ascii=False,
        )
    )
    if run.status.value not in {"completed", "completed_with_warnings"}:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
