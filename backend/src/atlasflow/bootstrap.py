from __future__ import annotations

from dataclasses import dataclass

from atlasflow.agents import ModelGateway, OpenRouterModelGateway, ResearchWorkflow
from atlasflow.config import Settings
from atlasflow.providers import EmbeddingProvider, RerankProvider
from atlasflow.providers.openrouter import (
    OpenRouterClient,
    OpenRouterEmbeddingProvider,
    OpenRouterRerankProvider,
    ProviderConfigurationError,
)
from atlasflow.rag import HybridRetriever
from atlasflow.service import InMemoryRunStore, RunService
from atlasflow.tools import BaseTool, ToolRegistry
from atlasflow.tools.builtin import CalculatorTool, KnowledgeSearchTool, OpenRouterWebSearchTool


@dataclass(slots=True)
class Container:
    retriever: HybridRetriever
    registry: ToolRegistry
    store: InMemoryRunStore
    run_service: RunService


@dataclass(slots=True)
class ProviderBundle:
    model: ModelGateway
    embedding: EmbeddingProvider
    reranker: RerankProvider
    web_search_tool: BaseTool | None = None


def build_container(settings: Settings, providers: ProviderBundle | None = None) -> Container:
    providers = providers or build_openrouter_providers(settings)
    retriever = HybridRetriever(
        embedding_provider=providers.embedding,
        rerank_provider=providers.reranker,
    )
    _seed_demo_knowledge(retriever)

    registry = ToolRegistry(
        timeout_seconds=settings.tool_timeout_seconds,
        max_retries=settings.max_tool_retries,
    )
    registry.register(KnowledgeSearchTool(retriever))
    if providers.web_search_tool is not None:
        registry.register(providers.web_search_tool)
    registry.register(CalculatorTool())

    store = InMemoryRunStore()
    workflow = ResearchWorkflow(
        registry=registry,
        model=providers.model,
        event_sink=store.append_event,
        top_k=settings.rag_top_k,
        max_iterations=settings.max_agent_iterations,
    )
    return Container(
        retriever=retriever,
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


def _client(
    settings: Settings, *, api_key: str, base_url: str, purpose: str
) -> OpenRouterClient:
    if not api_key:
        raise ProviderConfigurationError(f"{purpose} API key is required")
    return OpenRouterClient(
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=settings.provider_timeout_seconds,
        app_name=settings.app_name,
        max_retries=settings.max_tool_retries,
    )


def _seed_demo_knowledge(retriever: HybridRetriever) -> None:
    retriever.ingest(
        source_id="architecture-overview",
        title="AtlasFlow 架构说明",
        content=(
            "AtlasFlow 使用 Supervisor、Planner、Researcher、Writer、Critic 和 Reporter 协作。"
            "LangGraph 负责状态流转与条件分支。所有外部能力均通过 Tool Registry 调用，"
            "工具执行器负责参数校验、权限、超时、重试和审计。LangSmith 记录 Agent、"
            "模型、检索和工具调用的嵌套 Trace。"
        ),
        metadata={"kind": "architecture", "language": "zh-CN"},
    )
    retriever.ingest(
        source_id="rag-design",
        title="RAG 检索设计",
        content=(
            "RAG 流程包含文档清洗、语义切块、Embedding、关键词检索、向量检索、"
            "Reciprocal Rank Fusion、Reranker 和引用校验。检索结果保留文档编号、"
            "Chunk 编号、各阶段分数和元数据，以支持可解释回答与离线评测。"
        ),
        metadata={"kind": "rag", "language": "zh-CN"},
    )
    retriever.ingest(
        source_id="safety-policy",
        title="工具安全策略",
        content=(
            "工具按 low、medium、high 三个风险等级分类。读取和检索属于低风险；"
            "代码执行和数据库查询属于中风险；写入外部系统和发送消息属于高风险。"
            "中高风险工具必须经过显式授权，高风险工具还应进入人工审批节点。"
        ),
        metadata={"kind": "security", "language": "zh-CN"},
    )
