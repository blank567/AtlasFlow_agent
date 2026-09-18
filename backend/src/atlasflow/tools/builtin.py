from __future__ import annotations

import ast
import operator
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from atlasflow.providers.openrouter import OpenRouterClient
from atlasflow.rag import HybridRetriever
from atlasflow.schemas import Evidence
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

    async def run(
        self, arguments: KnowledgeSearchArguments, context: ToolContext
    ) -> ToolResult:
        evidence = await self.retriever.search(arguments.query, top_k=arguments.top_k)
        return ToolResult(
            success=True,
            data={"query": arguments.query, "matches": len(evidence)},
            evidence=evidence,
        )


class WebSearchArguments(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    max_results: int = Field(default=3, ge=1, le=10)


class OpenRouterWebSearchTool(BaseTool):
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
        annotations = message.get("annotations") or []
        for index, annotation in enumerate(annotations):
            if not isinstance(annotation, dict):
                continue
            citation = annotation.get("url_citation", annotation)
            if not isinstance(citation, dict):
                continue
            uri = citation.get("url")
            if not isinstance(uri, str) or not uri:
                continue
            evidence.append(
                Evidence(
                    source_id=f"web:{uri}",
                    title=str(citation.get("title") or uri),
                    content=str(citation.get("content") or "Web source returned without an excerpt."),
                    uri=uri,
                    score=max(0.0, 1.0 - index * 0.01),
                    metadata={"query": arguments.query, "provider": "openrouter"},
                )
            )
        answer = message.get("content", "")
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


class CalculatorArguments(BaseModel):
    expression: str = Field(min_length=1, max_length=200)


class CalculatorTool(BaseTool):
    name = "calculator"
    description = "Evaluate an arithmetic expression with a restricted AST interpreter."
    risk_level = RiskLevel.LOW
    arguments_model = CalculatorArguments

    _binary: ClassVar[dict[type[ast.operator], Any]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.Mod: operator.mod,
    }
    _unary: ClassVar[dict[type[ast.unaryop], Any]] = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    async def run(self, arguments: CalculatorArguments, context: ToolContext) -> ToolResult:
        try:
            value = self._evaluate(ast.parse(arguments.expression, mode="eval").body)
            return ToolResult(success=True, data={"result": value})
        except (TypeError, ValueError, ZeroDivisionError, SyntaxError) as exc:
            return ToolResult(success=False, error=str(exc), retryable=False)

    def _evaluate(self, node: ast.AST) -> float | int:
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in self._binary:
            left = self._evaluate(node.left)
            right = self._evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("Exponent is too large")
            return self._binary[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in self._unary:
            return self._unary[type(node.op)](self._evaluate(node.operand))
        raise ValueError("Only basic arithmetic expressions are allowed")
