"""LangGraph Studio entrypoint for the existing AtlasFlow workflow.

Studio owns its threads and checkpoints. AtlasFlow's FastAPI Run/Event store is
deliberately not used here, so exploratory Studio runs cannot mutate app runs.
"""

from __future__ import annotations

from atlasflow.agents.gateway import ModelGateway, OpenRouterModelGateway
from atlasflow.agents.tool_runtime import ToolRuntime
from atlasflow.agents.workflow import ResearchWorkflow
from atlasflow.config import Settings
from atlasflow.observability import configure_langsmith
from atlasflow.providers.openrouter import OpenRouterClient, ProviderConfigurationError
from atlasflow.schemas import RunEvent
from atlasflow.tools.catalog import build_tool_registry
from atlasflow.tools.web_search import OpenRouterWebSearchTool


async def _ignore_studio_event(_run_id: str, _event: RunEvent) -> None:
    """Studio visualizes graph state and LangSmith spans, not FastAPI SSE events."""


def build_studio_graph(*, settings: Settings, model: ModelGateway):
    """Reuse the production graph nodes with a Studio-only input adapter."""

    tool_runtime = None
    if isinstance(model, OpenRouterModelGateway):
        registry = build_tool_registry(
            settings,
            web_search=OpenRouterWebSearchTool(
                OpenRouterClient(
                    api_key=settings.search_api_key or settings.llm_api_key,
                    base_url=settings.search_base_url or settings.llm_base_url,
                    timeout_seconds=settings.provider_timeout_seconds,
                    app_name=settings.app_name,
                    max_retries=settings.max_tool_retries,
                ),
                settings.search_model or model.model,
            )
        )
        tool_runtime = ToolRuntime(
            registry=registry,
            client=model.client,
            model=model.model,
            event_sink=_ignore_studio_event,
        )

    workflow = ResearchWorkflow(
        model=model,
        event_sink=_ignore_studio_event,
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
        studio_mode=True,
    )
    return workflow.graph


def create_studio_graph():
    """Factory referenced by langgraph.json; real Provider calls happen on Run."""

    settings = Settings()
    configure_langsmith(settings)
    if settings.llm_provider.lower() != "openrouter":
        raise ProviderConfigurationError("Studio requires LLM_PROVIDER=openrouter")
    if not settings.llm_api_key or not settings.llm_model:
        raise ProviderConfigurationError("Studio requires LLM_API_KEY and LLM_MODEL")
    client = OpenRouterClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout_seconds=settings.provider_timeout_seconds,
        app_name=settings.app_name,
        max_retries=settings.max_tool_retries,
    )
    model = OpenRouterModelGateway(client, settings.llm_model)
    return build_studio_graph(settings=settings, model=model)
