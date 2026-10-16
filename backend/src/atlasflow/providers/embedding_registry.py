"""Resolve historical models only inside explicitly configured trusted services.

Database records never select an endpoint or a credential. Model identifiers may
vary on an already trusted OpenRouter service, using its currently configured key.
"""

from __future__ import annotations

import math
import re
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

from atlasflow.knowledge.domain.index_specs import EmbeddingSpec


class EmbeddingCompatibilityError(ValueError):
    pass


def validate_vectors(vectors: list[list[float]], count: int, dimensions: int | None = None) -> int:
    if len(vectors) != count or not vectors:
        raise EmbeddingCompatibilityError("EMBEDDING_INVALID: unexpected vector count")
    sizes = set()
    for vector in vectors:
        if not isinstance(vector, list) or not vector or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) for value in vector
        ):
            raise EmbeddingCompatibilityError("EMBEDDING_INVALID: vectors must be finite numeric lists")
        sizes.add(len(vector))
    if len(sizes) != 1 or (dimensions is not None and sizes != {dimensions}):
        raise EmbeddingCompatibilityError("EMBEDDING_DIMENSION_MISMATCH: index and returned dimensions differ")
    return sizes.pop()


class EmbeddingRegistry:
    def __init__(self, provider: Any) -> None:
        self.current_provider = provider
        self._services: dict[str, Any] = {}
        self.register(provider)

    def describe(self, provider: Any | None = None) -> EmbeddingSpec:
        provider = provider or self.current_provider
        client = getattr(provider, "client", None)
        endpoint = getattr(client, "base_url", "")
        adapter = f"{type(provider).__module__}.{type(provider).__qualname__}"
        if endpoint:
            parsed = urlsplit(endpoint)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname:
                raise EmbeddingCompatibilityError("EMBEDDING_SERVICE_INVALID: invalid configured service")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise EmbeddingCompatibilityError("EMBEDDING_SERVICE_INVALID: credential-bearing service URL")
            service = "configured-" + sha256(endpoint.rstrip("/").encode()).hexdigest()[:24]
        else:
            service = "adapter-" + sha256(adapter.encode()).hexdigest()[:24]
        model = str(getattr(provider, "model", None) or type(provider).__name__)
        return EmbeddingSpec(provider=adapter, service_id=service, model=model)

    def register(self, provider: Any) -> EmbeddingSpec:
        spec = self.describe(provider)
        self._services[spec.service_id] = provider
        return spec

    @staticmethod
    def observed_model(provider: Any) -> str | None:
        client = getattr(provider, "client", None)
        for metric in reversed(getattr(client, "recent_calls", ())):
            if (getattr(metric, "endpoint", None) == "/embeddings"
                    and getattr(metric, "requested_model", None) == getattr(provider, "model", None)):
                actual = getattr(metric, "model", None)
                if isinstance(actual, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", actual) and "://" not in actual:
                    return actual
                return None
        return None

    def validate_observed_model(self, provider: Any, spec: EmbeddingSpec) -> None:
        actual = self.observed_model(provider)
        if spec.actual_model and actual and spec.actual_model != actual:
            raise EmbeddingCompatibilityError("EMBEDDING_ACTUAL_MODEL_MISMATCH: provider returned another resolved model")

    def resolve(self, spec: EmbeddingSpec) -> Any:
        configured = self._services.get(spec.service_id)
        if configured is None:
            raise EmbeddingCompatibilityError("EMBEDDING_SERVICE_UNAVAILABLE: historical service is not configured")
        current = self.describe(configured)
        if current.provider != spec.provider:
            raise EmbeddingCompatibilityError("EMBEDDING_SPEC_MISMATCH: historical adapter differs")
        candidate = configured
        if current.model != spec.model:
            # Only this known adapter has the contract allowing another model on
            # the same trusted client. Arbitrary DB-selected class constructors
            # or client targets are deliberately forbidden.
            from atlasflow.providers.openrouter import OpenRouterEmbeddingProvider

            if not isinstance(configured, OpenRouterEmbeddingProvider):
                raise EmbeddingCompatibilityError("EMBEDDING_MODEL_UNAVAILABLE: historical model is not registered")
            candidate = OpenRouterEmbeddingProvider(configured.client, spec.model)
            current = self.describe(candidate)
        if current.request_hash != spec.request_hash:
            raise EmbeddingCompatibilityError("EMBEDDING_SPEC_MISMATCH: historical request settings are incompatible")
        return candidate
