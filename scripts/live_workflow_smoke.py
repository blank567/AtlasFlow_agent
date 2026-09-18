from __future__ import annotations

import asyncio
import json

from atlasflow.bootstrap import build_container
from atlasflow.config import Settings


async def main() -> None:
    """Exercise every runtime Agent and provider without printing sensitive content."""

    settings = Settings(max_agent_iterations=1)
    container = build_container(settings)
    run = await container.run_service.execute_and_wait(
        "AtlasFlow 如何通过工具调用和 RAG 提升多 Agent 结果可信度？"
    )
    print(
        json.dumps(
            {
                "status": run.status.value,
                "plan_steps": len(run.plan),
                "tools": [call.tool_name for call in run.tool_calls],
                "successful_tools": sum(call.success for call in run.tool_calls),
                "evidence_count": len(run.evidence),
                "report_characters": len(run.report or ""),
                "critic_issues": len(run.critiques),
                "error": run.error,
            },
            ensure_ascii=False,
        )
    )
    if run.status.value != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
