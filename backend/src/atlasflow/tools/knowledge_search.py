"""Internal knowledge search tool and its argument schema."""

from __future__ import annotations

from pydantic import BaseModel, Field

from atlasflow.rag import HybridRetriever
from atlasflow.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class KnowledgeSearchArguments(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)


class KnowledgeSearchTool(BaseTool):
    name = "knowledge_search"
    description = "Hybrid-search the internal knowledge base and return traceable evidence."
    risk_level = RiskLevel.LOW
    arguments_model = KnowledgeSearchArguments

    def __init__(self, retriever: HybridRetriever) -> None:
        self.retriever = retriever

    async def run(self, arguments: KnowledgeSearchArguments, context: ToolContext) -> ToolResult:
        evidence = await self.retriever.search(arguments.query, top_k=arguments.top_k)
        return ToolResult(
            success=True,
            data={"query": arguments.query, "matches": len(evidence)},
            evidence=evidence,
        )
