"""Internal knowledge search tool and its argument schema."""

from __future__ import annotations

from pydantic import BaseModel, Field

from atlasflow.knowledge.application import KnowledgePlatform
from atlasflow.knowledge.domain import KnowledgeFilter
from atlasflow.schemas import Evidence
from atlasflow.tools.base import BaseTool, Capability, RiskLevel, ToolContext, ToolResult


class KnowledgeSearchArguments(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    space: str | None = Field(default=None, max_length=100)
    filters: KnowledgeFilter | None = None
    result_limit: int = Field(default=5, ge=1, le=20)


class KnowledgeSearchTool(BaseTool):
    allowed_agents = ("researcher", "critic", "quality_gate")
    name = "knowledge_search"
    description = (
        "Search an authorized knowledge space with lexical, vector, fusion and reranking; "
        "returns source-located evidence and an explicit sufficiency decision."
    )
    risk_level = RiskLevel.LOW
    arguments_model = KnowledgeSearchArguments
    capabilities = (
        Capability(
            id="knowledge_search",
            description="检索已导入、可追溯且有权限访问的知识空间",
            supports_fresh_data=False,
        ),
    )
    cache_identical_calls = True

    def __init__(self, platform: KnowledgePlatform) -> None:
        self.platform = platform

    async def run(self, arguments: KnowledgeSearchArguments, context: ToolContext) -> ToolResult:
        result = await self.platform.search(
            arguments.query,
            space=arguments.space,
            filters=arguments.filters,
            result_limit=arguments.result_limit,
        )
        evidence = [
            Evidence(
                source_id=hit.chunk.id,
                title=hit.chunk.title,
                content=hit.chunk.content,
                uri=(
                    str(hit.chunk.metadata.get("canonical_uri"))
                    if hit.chunk.metadata.get("canonical_uri")
                    else None
                ),
                score=hit.rerank_score if hit.rerank_score is not None else hit.fusion_score,
                metadata={
                    **hit.chunk.metadata,
                    "document_id": hit.chunk.document_id,
                    "version_id": hit.chunk.version_id,
                    "heading_path": hit.chunk.heading_path,
                    "page_number": hit.chunk.page_number,
                    "lexical_score": hit.lexical_score,
                    "vector_score": hit.vector_score,
                    "fusion_score": hit.fusion_score,
                    "rerank_score": hit.rerank_score,
                },
            )
            for hit in result.hits
        ]
        return ToolResult(
            success=True,
            data={
                "query": arguments.query,
                "matches": len(evidence),
                "decision": result.decision.model_dump(mode="json"),
                "trace": result.trace.model_dump(mode="json"),
            },
            evidence=evidence,
        )
