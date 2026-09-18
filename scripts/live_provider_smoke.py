from __future__ import annotations

import asyncio
import json

from atlasflow.bootstrap import build_openrouter_providers
from atlasflow.config import Settings
from atlasflow.tools import RiskLevel, ToolContext
from atlasflow.tools.builtin import WebSearchArguments


async def main() -> None:
    providers = build_openrouter_providers(Settings())
    plan = await providers.model.create_plan("为一个多 Agent 项目制定三步测试计划")
    vectors = await providers.embedding.embed(
        ["AtlasFlow retrieval test"], input_type="search_document"
    )
    ranked = await providers.reranker.rerank(
        "agent retrieval",
        ["agent retrieval evidence", "unrelated weather"],
        top_n=1,
    )
    if providers.web_search_tool is None:
        web_success = False
        web_evidence_count = 0
        web_error = "web search tool is not configured"
    else:
        web = await providers.web_search_tool.run(
            WebSearchArguments(query="OpenRouter official documentation", max_results=1),
            ToolContext(
                run_id="live-smoke",
                agent_name="smoke",
                approved_risks={RiskLevel.LOW},
            ),
        )
        web_success = web.success
        web_evidence_count = len(web.evidence)
        web_error = web.error

    print(
        json.dumps(
            {
                "chat_ok": len(plan) >= 3,
                "embedding_ok": len(vectors) == 1,
                "embedding_dimensions": len(vectors[0]),
                "rerank_ok": len(ranked) == 1,
                "web_search_ok": web_success,
                "web_evidence_count": web_evidence_count,
                "web_error": web_error,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

