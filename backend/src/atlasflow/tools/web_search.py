"""OpenRouter web search tool and its argument schema."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from atlasflow.providers.openrouter import OpenRouterClient
from atlasflow.schemas import Evidence
from atlasflow.tools.base import BaseTool, Capability, RiskLevel, ToolContext, ToolResult


class WebSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    max_results: int = Field(default=3, ge=1, le=10)


class OpenRouterWebSearchTool(BaseTool):
    planning_safe = True
    cache_identical_calls = True
    capabilities = (Capability("web_search", "检索公开网页及可引用来源，包括当前信息", True),)
    model_calls_per_execution = 1
    name = "web_search"
    description = "Search current public web sources through OpenRouter server tools."
    risk_level = RiskLevel.LOW
    arguments_model = WebSearchArguments

    def __init__(self, client: OpenRouterClient, model: str) -> None:
        self.client = client
        self.model = model

    async def run(self, arguments: WebSearchArguments, context: ToolContext) -> ToolResult:
        message = await self.client.chat(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Use web search for the user's research query. Return a concise factual summary "
                        "based only on retrieved pages. Preserve URL citations."
                    ),
                },
                {"role": "user", "content": arguments.query},
            ],
            temperature=0.0,
            max_tokens=1200,
            max_tool_calls=1,
            tools=[
                {
                    "type": "openrouter:web_search",
                    "parameters": {
                        "engine": "auto",
                        "max_results": arguments.max_results,
                        "max_total_results": arguments.max_results,
                        "max_uses": 1,
                        "max_characters": 1800,
                    },
                }
            ],
        )
        evidence: list[Evidence] = []
        answer = message.get("content")
        answer = answer if isinstance(answer, str) else ""
        annotations = message.get("annotations") or []
        for index, annotation in enumerate(annotations if isinstance(annotations, list) else []):
            if not isinstance(annotation, dict):
                continue
            citation = annotation.get("url_citation", annotation)
            if not isinstance(citation, dict):
                continue
            uri = citation.get("url")
            if not isinstance(uri, str) or not uri:
                continue
            try:
                parts = urlsplit(uri)
            except ValueError:
                continue
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                continue
            excerpt = citation.get("content")
            content_kind = "source_excerpt"
            if not isinstance(excerpt, str) or not excerpt.strip():
                start, end = citation.get("start_index"), citation.get("end_index")
                excerpt = (
                    answer[start:end]
                    if type(start) is int and type(end) is int and 0 <= start < end <= len(answer)
                    else "检索服务返回该来源链接，未提供原文摘录；请结合搜索摘要核验。"
                )
                content_kind = "search_response_citation"
            evidence.append(
                Evidence(
                    source_id=f"web:{uri}",
                    title=str(citation.get("title") or uri),
                    content=excerpt[:1600],
                    uri=uri,
                    score=max(0.0, 1.0 - index * 0.01),
                    metadata={
                        "query": arguments.query,
                        "provider": "openrouter",
                        "content_kind": content_kind,
                        "retrieved_at": datetime.now(UTC).isoformat(),
                    },
                )
            )
        if not evidence:
            return ToolResult(
                success=False,
                data={"provider": "openrouter", "summary": answer},
                error="OpenRouter web search returned no URL citations",
                retryable=False,
            )
        return ToolResult(
            success=True,
            data={"provider": "openrouter", "summary": answer},
            evidence=evidence,
        )
