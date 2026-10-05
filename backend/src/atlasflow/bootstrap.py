from __future__ import annotations

from dataclasses import dataclass

from atlasflow.agents.gateway import ModelGateway, OpenRouterModelGateway
from atlasflow.agents.tool_runtime import ToolRuntime
from atlasflow.agents.workflow import ResearchWorkflow
from atlasflow.config import Settings
from atlasflow.knowledge.application import KnowledgePlatform
from atlasflow.knowledge.infrastructure import (
    InMemoryKnowledgeRepository,
    LocalBlobStore,
    MemoryBlobStore,
)
from atlasflow.knowledge.infrastructure.postgres_repository import (
    PostgresKnowledgeRepository,
)
from atlasflow.knowledge.retrieval import RetrievalService
from atlasflow.observability import configure_langsmith
from atlasflow.providers import EmbeddingProvider, RerankProvider
from atlasflow.providers.openrouter import (
    OpenRouterClient,
    OpenRouterEmbeddingProvider,
    OpenRouterRerankProvider,
    ProviderConfigurationError,
)
from atlasflow.service import RunService
from atlasflow.storage import SQLiteRunStore
from atlasflow.tools import BaseTool, ToolRegistry
from atlasflow.tools.catalog import build_tool_registry
from atlasflow.tools.knowledge_search import KnowledgeSearchTool
from atlasflow.tools.web_search import OpenRouterWebSearchTool


@dataclass(slots=True)
class Container:
    settings: Settings
    knowledge: KnowledgePlatform
    registry: ToolRegistry
    store: SQLiteRunStore
    run_service: RunService


@dataclass(slots=True)
class ProviderBundle:
    model: ModelGateway
    embedding: EmbeddingProvider
    reranker: RerankProvider
    web_search_tool: BaseTool | None = None


def build_container(settings: Settings, providers: ProviderBundle | None = None) -> Container:
    configure_langsmith(settings)
    providers = providers or build_openrouter_providers(settings)
    if settings.knowledge_backend.lower() == "memory":
        knowledge_repository = InMemoryKnowledgeRepository()
        blob_store = MemoryBlobStore()
    elif settings.knowledge_backend.lower() == "postgres":
        knowledge_repository = PostgresKnowledgeRepository(settings.knowledge_database_url)
        blob_store = LocalBlobStore(settings.knowledge_blob_root)
    else:
        raise ProviderConfigurationError(
            f"Knowledge backend '{settings.knowledge_backend}' is unsupported"
        )
    retrieval = RetrievalService(
        repository=knowledge_repository,
        embedding_provider=providers.embedding,
        rerank_provider=providers.reranker,
    )
    knowledge = KnowledgePlatform(
        repository=knowledge_repository,
        blob_store=blob_store,
        embedding_provider=providers.embedding,
        retrieval=retrieval,
        default_space=settings.knowledge_default_space,
        worker_enabled=settings.knowledge_worker_enabled,
        worker_poll_seconds=settings.knowledge_worker_poll_seconds,
    )

    registry = build_tool_registry(settings, web_search=providers.web_search_tool)
    registry.register(KnowledgeSearchTool(knowledge))

    store = SQLiteRunStore(settings.database_path)

    tool_runtime = None
    if isinstance(providers.model, OpenRouterModelGateway):
        tool_runtime = ToolRuntime(
            registry=registry,
            client=providers.model.client,
            model=providers.model.model,
            event_sink=store.append_event,
        )
    workflow = ResearchWorkflow(
        model=providers.model,
        event_sink=store.append_event,
        tool_runtime=tool_runtime,
        max_concurrency=settings.max_research_concurrency,
        max_initial_tasks=settings.max_research_tasks,
        max_research_attempts=settings.max_research_attempts,
        quorum_ratio=settings.research_quorum_ratio,
        max_supplement_rounds=settings.max_supplement_rounds,
        max_supplement_tasks=settings.max_supplement_tasks,
        max_tasks_per_plan=settings.max_tasks_per_plan,
        max_replans=settings.max_replans,
        max_revisions=settings.max_report_revisions,
        quality_threshold=settings.quality_threshold,
        trace_segment_sink=store.upsert_trace_segment,
    )
    return Container(
        settings=settings,
        knowledge=knowledge,
        registry=registry,
        store=store,
        run_service=RunService(store, workflow),
    )


def build_openrouter_providers(settings: Settings) -> ProviderBundle:
    for label, provider in (
        ("LLM", settings.llm_provider),
        ("Embedding", settings.embedding_provider),
        ("Rerank", settings.rerank_provider),
        ("Search", settings.search_provider),
    ):
        if provider.lower() != "openrouter":
            raise ProviderConfigurationError(
                f"{label} provider '{provider}' is unsupported. Offline runtime fallback is disabled; "
                "use 'openrouter'."
            )

    llm_client = _client(
        settings,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        purpose="LLM",
    )
    embedding_client = _client(
        settings,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        purpose="Embedding",
    )
    rerank_client = _client(
        settings,
        api_key=settings.rerank_api_key,
        base_url=settings.rerank_base_url,
        purpose="Rerank",
    )
    search_client = _client(
        settings,
        api_key=settings.search_api_key or settings.llm_api_key,
        base_url=settings.search_base_url or settings.llm_base_url,
        purpose="Search",
    )

    if not settings.llm_model:
        raise ProviderConfigurationError("LLM_MODEL is required")
    if not settings.embedding_model:
        raise ProviderConfigurationError("EMBEDDING_MODEL is required")
    if not settings.rerank_model or settings.rerank_model.startswith("http"):
        raise ProviderConfigurationError("RERANK_MODEL must be an OpenRouter model slug")

    search_model = settings.search_model or settings.llm_model
    return ProviderBundle(
        model=OpenRouterModelGateway(llm_client, settings.llm_model),
        embedding=OpenRouterEmbeddingProvider(embedding_client, settings.embedding_model),
        reranker=OpenRouterRerankProvider(rerank_client, settings.rerank_model),
        web_search_tool=OpenRouterWebSearchTool(search_client, search_model),
    )


def _client(settings: Settings, *, api_key: str, base_url: str, purpose: str) -> OpenRouterClient:
    if not api_key:
        raise ProviderConfigurationError(f"{purpose} API key is required")
    return OpenRouterClient(
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=settings.provider_timeout_seconds,
        app_name=settings.app_name,
        max_retries=settings.max_tool_retries,
    )
