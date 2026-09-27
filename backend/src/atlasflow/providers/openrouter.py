from __future__ import annotations

import asyncio
import math
from collections import deque
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx

from atlasflow.observability import (
    ProviderCallMetric,
    ProviderCallStatus,
    ProviderUsage,
    record_provider_call,
    redact_sensitive_data,
)
from atlasflow.providers.base import RerankResult


class ProviderConfigurationError(ValueError):
    pass


class ProviderRequestError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _OpenRouterResponse:
    data: dict[str, Any]
    metric: ProviderCallMetric


class OpenRouterClient:
    """Small async client for OpenRouter chat, embedding and rerank endpoints."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 90,
        app_name: str = "AtlasFlow",
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self.base_url = self._normalize_base_url(base_url)
        self.timeout_seconds = timeout_seconds
        self.app_name = app_name
        self.max_retries = max_retries
        self._transport = transport
        self._recent_calls: deque[ProviderCallMetric] = deque(maxlen=100)

    def __repr__(self) -> str:
        return f"OpenRouterClient(base_url={self.base_url!r}, api_key='***')"

    @property
    def recent_calls(self) -> tuple[ProviderCallMetric, ...]:
        """Return bounded, content-free call metadata for diagnostics."""

        return tuple(self._recent_calls)

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int = 2000,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        max_tool_calls: int | None = None,
    ) -> dict[str, Any]:
        if not model.strip():
            raise ProviderConfigurationError("LLM model is required")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # Ask OpenRouter to include its authoritative token/cost accounting.
            # If a provider omits it, AtlasFlow deliberately leaves usage unknown.
            "usage": {"include": True},
        }
        if response_format is not None:
            payload["response_format"] = response_format
            # OpenRouter recommends both of these for reliable structured output:
            # route only to endpoints supporting the requested parameters and repair
            # occasional JSON syntax defects before the response reaches the caller.
            payload["provider"] = {"require_parameters": True}
            payload["plugins"] = [{"id": "response-healing"}]
        if tools is not None:
            payload["tools"] = tools
            payload.setdefault("provider", {"require_parameters": True})
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = parallel_tool_calls
        if max_tool_calls is not None:
            payload["max_tool_calls"] = max_tool_calls
        response = await self._post("/chat/completions", payload)
        data = response.data
        choices = data.get("choices") or []
        first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        if not isinstance(first_choice.get("message"), dict):
            self._record_metric(
                response.metric.model_copy(
                    update={"status": "failed", "error_type": "InvalidChatResponse"}
                )
            )
            raise ProviderRequestError("OpenRouter chat response contains no message")
        self._record_metric(response.metric)
        message = dict(first_choice["message"])
        message["_atlasflow_provider"] = response.metric.model_dump(mode="json", exclude_none=True)
        return message

    async def embed(self, *, model: str, texts: list[str], input_type: str) -> list[list[float]]:
        if not model.strip():
            raise ProviderConfigurationError("Embedding model is required")
        if not texts:
            return []
        response = await self._post(
            "/embeddings",
            {
                "model": model,
                "input": texts,
                "input_type": input_type,
                "encoding_format": "float",
            },
        )
        data = response.data
        raw_rows = data.get("data") or []
        rows = (
            sorted(raw_rows, key=lambda row: row.get("index", 0))
            if isinstance(raw_rows, list) and all(isinstance(row, dict) for row in raw_rows)
            else []
        )
        vectors = [row.get("embedding") for row in rows]
        if len(vectors) != len(texts) or any(not isinstance(vector, list) for vector in vectors):
            self._record_metric(
                response.metric.model_copy(
                    update={"status": "failed", "error_type": "InvalidEmbeddingResponse"}
                )
            )
            raise ProviderRequestError("OpenRouter embedding response has an invalid shape")
        self._record_metric(response.metric)
        return vectors

    async def rerank(
        self, *, model: str, query: str, documents: list[str], top_n: int
    ) -> list[RerankResult]:
        if not model.strip():
            raise ProviderConfigurationError("Rerank model is required")
        if not documents:
            return []
        response = await self._post(
            "/rerank",
            {
                "model": model,
                "query": query,
                "documents": documents,
                "top_n": min(top_n, len(documents)),
            },
        )
        data = response.data
        results: list[RerankResult] = []
        raw_results = data.get("results") or []
        for row in raw_results if isinstance(raw_results, list) else []:
            if not isinstance(row, dict):
                continue
            if "index" not in row or "relevance_score" not in row:
                continue
            results.append(
                RerankResult(index=int(row["index"]), score=float(row["relevance_score"]))
            )
        if not results:
            self._record_metric(
                response.metric.model_copy(
                    update={"status": "failed", "error_type": "InvalidRerankResponse"}
                )
            )
            raise ProviderRequestError("OpenRouter rerank response contains no ranked documents")
        self._record_metric(response.metric)
        return results

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> _OpenRouterResponse:
        if not self._api_key:
            raise ProviderConfigurationError("OpenRouter API key is required")
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Title": self.app_name,
        }
        response: httpx.Response | None = None
        started_at = perf_counter()
        attempt_count = 0
        for attempt in range(self.max_retries + 1):
            attempt_count = attempt + 1
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds, transport=self._transport
                ) as client:
                    response = await client.post(url, headers=headers, json=payload)
            except httpx.RequestError as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                metric = self._provider_metric(
                    endpoint=endpoint,
                    payload=payload,
                    response=None,
                    data=None,
                    status="failed",
                    duration_ms=self._elapsed_ms(started_at),
                    attempt_count=attempt_count,
                    error_type=type(exc).__name__,
                )
                self._record_metric(metric)
                raise ConnectionError(f"OpenRouter request failed: {type(exc).__name__}") from exc

            retryable = response.status_code in {408, 429} or response.status_code >= 500
            if retryable and attempt < self.max_retries:
                retry_after = response.headers.get("retry-after", "")
                try:
                    delay = min(float(retry_after), 5.0) if retry_after else 0.25 * (2**attempt)
                except ValueError:
                    delay = 0.25 * (2**attempt)
                await asyncio.sleep(delay)
                continue
            break

        if response is None:
            metric = self._provider_metric(
                endpoint=endpoint,
                payload=payload,
                response=None,
                data=None,
                status="failed",
                duration_ms=self._elapsed_ms(started_at),
                attempt_count=max(attempt_count, 1),
                error_type="EmptyResponse",
            )
            self._record_metric(metric)
            raise ProviderRequestError(f"OpenRouter {endpoint} returned no response")
        if response.is_error:
            request_id = response.headers.get("x-request-id", "unknown")
            detail = self._safe_error_detail(response)
            metric = self._provider_metric(
                endpoint=endpoint,
                payload=payload,
                response=response,
                data=None,
                status="failed",
                duration_ms=self._elapsed_ms(started_at),
                attempt_count=attempt_count,
                error_type=f"HTTP{response.status_code}",
            )
            self._record_metric(metric)
            raise ProviderRequestError(
                f"OpenRouter {endpoint} returned HTTP {response.status_code} "
                f"(request_id={request_id}){f': {detail}' if detail else ''}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            metric = self._provider_metric(
                endpoint=endpoint,
                payload=payload,
                response=response,
                data=None,
                status="failed",
                duration_ms=self._elapsed_ms(started_at),
                attempt_count=attempt_count,
                error_type="InvalidJSON",
            )
            self._record_metric(metric)
            raise ProviderRequestError(f"OpenRouter {endpoint} returned invalid JSON") from exc
        if not isinstance(data, dict):
            metric = self._provider_metric(
                endpoint=endpoint,
                payload=payload,
                response=response,
                data=None,
                status="failed",
                duration_ms=self._elapsed_ms(started_at),
                attempt_count=attempt_count,
                error_type="InvalidPayload",
            )
            self._record_metric(metric)
            raise ProviderRequestError(f"OpenRouter {endpoint} returned an invalid payload")
        metric = self._provider_metric(
            endpoint=endpoint,
            payload=payload,
            response=response,
            data=data,
            status="succeeded",
            duration_ms=self._elapsed_ms(started_at),
            attempt_count=attempt_count,
        )
        return _OpenRouterResponse(data=data, metric=metric)

    def _record_metric(self, metric: ProviderCallMetric) -> None:
        self._recent_calls.append(metric)
        record_provider_call(metric)

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return max(0, round((perf_counter() - started_at) * 1000))

    @classmethod
    def _provider_metric(
        cls,
        *,
        endpoint: str,
        payload: dict[str, Any],
        response: httpx.Response | None,
        data: dict[str, Any] | None,
        status: ProviderCallStatus,
        duration_ms: int,
        attempt_count: int,
        error_type: str | None = None,
    ) -> ProviderCallMetric:
        request_id: str | None = None
        if response is not None:
            request_id = cls._nonempty_string(
                response.headers.get("x-request-id")
                or response.headers.get("x-openrouter-request-id")
            )
        usage = cls._provider_usage(data.get("usage") if data else None)
        return ProviderCallMetric(
            provider="openrouter",
            operation=cls._operation_name(endpoint),
            endpoint=endpoint,
            status=status,
            requested_model=cls._nonempty_string(payload.get("model")),
            model=cls._nonempty_string(data.get("model") if data else None),
            request_id=request_id,
            generation_id=cls._nonempty_string(data.get("id") if data else None),
            usage=usage,
            duration_ms=duration_ms,
            attempt_count=attempt_count,
            http_status=response.status_code if response is not None else None,
            error_type=error_type,
        )

    @staticmethod
    def _operation_name(endpoint: str) -> str:
        return {
            "/chat/completions": "chat",
            "/embeddings": "embedding",
            "/rerank": "rerank",
        }.get(endpoint, endpoint.strip("/").replace("/", ".") or "request")

    @classmethod
    def _provider_usage(cls, value: Any) -> ProviderUsage | None:
        if not isinstance(value, dict):
            return None
        usage = ProviderUsage(
            prompt_tokens=cls._nonnegative_int(value.get("prompt_tokens")),
            completion_tokens=cls._nonnegative_int(value.get("completion_tokens")),
            total_tokens=cls._nonnegative_int(value.get("total_tokens")),
            cost=cls._nonnegative_number(value.get("cost")),
        )
        if all(
            item is None
            for item in (
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.total_tokens,
                usage.cost,
            )
        ):
            return None
        return usage

    @staticmethod
    def _nonempty_string(value: Any) -> str | None:
        return value if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _nonnegative_int(value: Any) -> int | None:
        return value if type(value) is int and value >= 0 else None

    @staticmethod
    def _nonnegative_number(value: Any) -> float | None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
            or not math.isfinite(value)
        ):
            return None
        return float(value)

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        normalized = base_url.strip().rstrip("/") or "https://openrouter.ai/api/v1"
        for suffix in ("/chat/completions", "/embeddings", "/rerank"):
            if normalized.endswith(suffix):
                return normalized[: -len(suffix)]
        return normalized

    def _safe_error_detail(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        message = error.get("message", "") if isinstance(error, dict) else ""
        if not isinstance(message, str):
            return ""
        sanitized = redact_sensitive_data(message.replace(self._api_key, "***"))
        return str(sanitized)[:300]


class OpenRouterEmbeddingProvider:
    def __init__(self, client: OpenRouterClient, model: str) -> None:
        self.client = client
        self.model = model

    async def embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        return await self.client.embed(model=self.model, texts=texts, input_type=input_type)


class OpenRouterRerankProvider:
    def __init__(self, client: OpenRouterClient, model: str) -> None:
        self.client = client
        self.model = model

    async def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[RerankResult]:
        return await self.client.rerank(
            model=self.model, query=query, documents=documents, top_n=top_n
        )
