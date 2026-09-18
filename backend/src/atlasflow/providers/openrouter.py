from __future__ import annotations

import asyncio
from typing import Any

import httpx

from atlasflow.providers.base import RerankResult


class ProviderConfigurationError(ValueError):
    pass


class ProviderRequestError(RuntimeError):
    pass


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

    def __repr__(self) -> str:
        return f"OpenRouterClient(base_url={self.base_url!r}, api_key='***')"

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 2000,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not model.strip():
            raise ProviderConfigurationError("LLM model is required")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
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
        data = await self._post("/chat/completions", payload)
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0].get("message"), dict):
            raise ProviderRequestError("OpenRouter chat response contains no message")
        return choices[0]["message"]

    async def embed(
        self, *, model: str, texts: list[str], input_type: str
    ) -> list[list[float]]:
        if not model.strip():
            raise ProviderConfigurationError("Embedding model is required")
        if not texts:
            return []
        data = await self._post(
            "/embeddings",
            {
                "model": model,
                "input": texts,
                "input_type": input_type,
                "encoding_format": "float",
            },
        )
        rows = sorted(data.get("data") or [], key=lambda row: row.get("index", 0))
        vectors = [row.get("embedding") for row in rows]
        if len(vectors) != len(texts) or any(not isinstance(vector, list) for vector in vectors):
            raise ProviderRequestError("OpenRouter embedding response has an invalid shape")
        return vectors

    async def rerank(
        self, *, model: str, query: str, documents: list[str], top_n: int
    ) -> list[RerankResult]:
        if not model.strip():
            raise ProviderConfigurationError("Rerank model is required")
        if not documents:
            return []
        data = await self._post(
            "/rerank",
            {
                "model": model,
                "query": query,
                "documents": documents,
                "top_n": min(top_n, len(documents)),
            },
        )
        results: list[RerankResult] = []
        for row in data.get("results") or []:
            if "index" not in row or "relevance_score" not in row:
                continue
            results.append(
                RerankResult(index=int(row["index"]), score=float(row["relevance_score"]))
            )
        if not results:
            raise ProviderRequestError("OpenRouter rerank response contains no ranked documents")
        return results

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            raise ProviderConfigurationError("OpenRouter API key is required")
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Title": self.app_name,
        }
        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds, transport=self._transport
                ) as client:
                    response = await client.post(url, headers=headers, json=payload)
            except httpx.RequestError as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                raise ConnectionError(
                    f"OpenRouter request failed: {type(exc).__name__}"
                ) from exc

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
            raise ProviderRequestError(f"OpenRouter {endpoint} returned no response")
        if response.is_error:
            request_id = response.headers.get("x-request-id", "unknown")
            detail = self._safe_error_detail(response)
            raise ProviderRequestError(
                f"OpenRouter {endpoint} returned HTTP {response.status_code} "
                f"(request_id={request_id}){f': {detail}' if detail else ''}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderRequestError(f"OpenRouter {endpoint} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ProviderRequestError(f"OpenRouter {endpoint} returned an invalid payload")
        return data

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
        return message.replace(self._api_key, "***")[:300]


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

    async def rerank(
        self, query: str, documents: list[str], *, top_n: int
    ) -> list[RerankResult]:
        return await self.client.rerank(
            model=self.model, query=query, documents=documents, top_n=top_n
        )
