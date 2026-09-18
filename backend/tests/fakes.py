from __future__ import annotations

import hashlib

from atlasflow.bootstrap import ProviderBundle, build_container
from atlasflow.config import Settings
from atlasflow.providers import RerankResult
from atlasflow.schemas import Evidence
from atlasflow.tools import BaseTool, RiskLevel, ToolContext, ToolResult
from pydantic import BaseModel


class FakeEmbeddingProvider:
    async def embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * 16
            for character in text.lower():
                digest = hashlib.sha256(character.encode("utf-8")).digest()
                vector[int.from_bytes(digest[:2], "big") % len(vector)] += 1.0
            vectors.append(vector)
        return vectors


class FakeRerankProvider:
    async def rerank(
        self, query: str, documents: list[str], *, top_n: int
    ) -> list[RerankResult]:
        query_characters = set(query.lower())
        scored = [
            RerankResult(
                index=index,
                score=len(query_characters.intersection(document.lower()))
                / max(len(query_characters), 1),
            )
            for index, document in enumerate(documents)
        ]
        return sorted(scored, key=lambda item: item.score, reverse=True)[:top_n]


class FakeModelGateway:
    async def create_plan(self, query: str) -> list[str]:
        return ["检索证据", "分析证据", "检查引用"]

    async def write_draft(
        self, query: str, plan: list[str], evidence: list[Evidence]
    ) -> str:
        sources = "\n".join(
            f"- [S{index}] {item.title}" for index, item in enumerate(evidence, 1)
        )
        return f"# 报告\n\n{query} 的结论来自证据。 [S1]\n\n## 来源\n\n{sources}"

    async def critique(self, query: str, draft: str, evidence: list[Evidence]) -> list[str]:
        return []


class EmptyArguments(BaseModel):
    pass


class FakeWebSearchTool(BaseTool):
    name = "web_search"
    description = "Test-only web search double."
    risk_level = RiskLevel.LOW
    arguments_model = EmptyArguments

    async def run(self, arguments: EmptyArguments, context: ToolContext) -> ToolResult:
        return ToolResult(
            success=True,
            evidence=[
                Evidence(
                    source_id="test-web:1",
                    title="Test web source",
                    content="Test-only external evidence.",
                    uri="https://example.test/source",
                    score=1.0,
                    metadata={"provider": "test-double"},
                )
            ],
        )


def make_test_settings() -> Settings:
    return Settings(
        _env_file=None,
        llm_provider="openrouter",
        embedding_provider="openrouter",
        rerank_provider="openrouter",
        search_provider="openrouter",
        llm_model="test/model",
        embedding_model="test/embedding",
        rerank_model="test/rerank",
        llm_api_key="test-key",
        embedding_api_key="test-key",
        rerank_api_key="test-key",
        tool_timeout_seconds=2,
    )


def make_test_container(*, include_web: bool = True):
    providers = ProviderBundle(
        model=FakeModelGateway(),
        embedding=FakeEmbeddingProvider(),
        reranker=FakeRerankProvider(),
        web_search_tool=FakeWebSearchTool() if include_web else None,
    )
    return build_container(make_test_settings(), providers=providers)

