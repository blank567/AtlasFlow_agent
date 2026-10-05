"""Versioned, domain-neutral knowledge platform used by tools and APIs."""

from atlasflow.knowledge.application.platform import KnowledgePlatform
from atlasflow.knowledge.infrastructure.memory_repository import InMemoryKnowledgeRepository

__all__ = ["InMemoryKnowledgeRepository", "KnowledgePlatform"]
