from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from atlasflow.agents.gateway import ModelGateway, OpenRouterModelGateway
from atlasflow.agents.tool_runtime import ToolRuntime
from atlasflow.agents.workflow import ResearchWorkflow
from atlasflow.config import Settings
from atlasflow.conversations.service import ConversationService
from atlasflow.conversations.storage import SQLiteConversationStore
from atlasflow.evaluation.common.service import V2EvaluationService
from atlasflow.evaluation.runner import EvaluationService
from atlasflow.evaluation_bindings import build_evaluation
from atlasflow.evaluation_bindings_v2 import build_evaluation_v2
from atlasflow.execution import ExecutionRuntime, create_postgres_execution
from atlasflow.execution.contracts import digest
from atlasflow.knowledge.application import KnowledgePlatform
from atlasflow.knowledge.domain import RetrievalProfile
from atlasflow.knowledge.infrastructure import (
    InMemoryKnowledgeRepository,
    LocalBlobStore,
    MemoryBlobStore,
)
from atlasflow.knowledge.infrastructure.postgres_repository import (
    PostgresKnowledgeRepository,
)
from atlasflow.knowledge.retrieval import RetrievalService
from atlasflow.knowledge.retrieval.bilingual_query_expander import BilingualQueryExpander
from atlasflow.knowledge.retrieval.query_pipeline import QueryPipeline
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
    conversations: ConversationService | None = None
    execution: ExecutionRuntime | None = None
    evaluation: EvaluationService | None = None
    evaluation_v2: V2EvaluationService | None = None


@dataclass(slots=True)
class ProviderBundle:
    model: ModelGateway
    embedding: EmbeddingProvider
    reranker: RerankProvider
    web_search_tool: BaseTool | None = None


def build_container(settings: Settings, providers: ProviderBundle | None = None) -> Container:
    configure_langsmith(settings)
    injected = providers is not None
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
        profile=RetrievalProfile(
            query_expansion_enabled=settings.knowledge_query_expansion_enabled,
            query_expansion_limit=settings.knowledge_query_expansion_limit,
        ),
        query_pipeline=QueryPipeline(
            expander=(
                BilingualQueryExpander.from_file(settings.knowledge_query_aliases_path)
                if settings.knowledge_query_aliases_path.strip()
                else None
            )
        ),
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

    workflow_options = _workflow_options(settings)
    execution = None if injected and settings.knowledge_backend == "memory" else (
        create_postgres_execution(settings.execution_database_url or settings.knowledge_database_url,
            profile_hash=_execution_profile(settings, knowledge, workflow_options))
    )
    store = SQLiteRunStore(settings.database_path, recover_interrupted=execution is None,
                           defer_open=execution is not None)

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
        execution=execution,
        **workflow_options,
        trace_segment_sink=store.upsert_trace_segment,
    )
    async def capture_run_snapshot(policy):
        space = policy.knowledge_space if policy.knowledge_mode == "selected" else None
        return (await knowledge.capture_search_snapshot(space)).model_dump(mode="json")

    run_service = RunService(store, workflow, execution=execution,
                            snapshot_factory=capture_run_snapshot if not injected else None)
    evaluation = build_evaluation(settings, knowledge, registry, providers, workflow_options, store.get)
    return Container(
        settings=settings,
        knowledge=knowledge,
        registry=registry,
        store=store,
        run_service=run_service,
        conversations=ConversationService(SQLiteConversationStore(store), run_service, providers.model),
        execution=execution,
        evaluation=evaluation,
        evaluation_v2=build_evaluation_v2(settings, knowledge, registry, providers, workflow_options, evaluation),
    )


def _workflow_options(settings: Settings) -> dict[str, Any]:
    return {
        "max_concurrency": settings.max_research_concurrency,
        "max_initial_tasks": settings.max_research_tasks,
        "max_research_attempts": settings.max_research_attempts,
        "quorum_ratio": settings.research_quorum_ratio,
        "accept_partial_results": settings.research_accept_partial_results,
        "max_supplement_rounds": settings.max_supplement_rounds,
        "max_supplement_tasks": settings.max_supplement_tasks,
        "max_tasks_per_plan": settings.max_tasks_per_plan,
        "max_replans": settings.max_replans,
        "max_revisions": settings.max_report_revisions,
        "quality_threshold": settings.quality_threshold,
        "quality_relaxed_review": settings.quality_relaxed_review,
    }


def _execution_profile(settings, knowledge, workflow_options) -> str:
    package = Path(__file__).parent
    code = {str(path.relative_to(package)): sha256(path.read_bytes()).hexdigest()
            for path in sorted(package.rglob("*.py"))}
    models = {name: getattr(settings, name) for name in (
        "llm_provider", "llm_model", "rerank_provider", "rerank_model", "search_provider", "search_model")}
    services = {name: sha256(getattr(settings, name).encode()).hexdigest()
                for name in ("llm_base_url", "embedding_base_url", "rerank_base_url", "search_base_url")}
    return digest({"code": code, "models": models, "services": services,
                   "request_limits": {name: getattr(settings, name) for name in
                       ("max_tool_retries", "tool_timeout_seconds", "provider_timeout_seconds",
                        "web_fetch_connect_timeout_seconds", "web_fetch_read_timeout_seconds",
                        "web_fetch_total_timeout_seconds")},
                   "workflow": workflow_options, "query_pipeline": knowledge.describe_query_pipeline(),
                   "retrieval": knowledge.retrieval.profile.model_dump(mode="json")})


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
        model=OpenRouterModelGateway(
            llm_client, settings.llm_model,
            quality_threshold=settings.quality_threshold,
            accept_partial_results=settings.research_accept_partial_results,
            relaxed_review=settings.quality_relaxed_review,
        ),
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
