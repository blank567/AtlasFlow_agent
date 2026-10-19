import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "atlasflow.sqlite3"
DEFAULT_KNOWLEDGE_BLOB_ROOT = PROJECT_ROOT / "data" / "knowledge" / "blobs"
DEFAULT_KNOWLEDGE_WORK_ROOT = Path("E:/codex/tmp/atlasflow-rag")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "AtlasFlow"
    app_env: str = "development"
    log_level: str = "INFO"

    llm_provider: str = "openrouter"
    llm_model: str = ""
    # Additional trusted choices served to the UI; the primary model is always included.
    llm_model_options: list[str] = Field(default_factory=list, max_length=30)
    llm_api_key: str = Field(default="", repr=False)
    llm_base_url: str = ""

    embedding_provider: str = "openrouter"
    embedding_model: str = ""
    embedding_api_key: str = Field(default="", repr=False)
    embedding_base_url: str = ""

    rerank_provider: str = "openrouter"
    rerank_model: str = ""
    rerank_api_key: str = Field(default="", repr=False)
    rerank_base_url: str = ""

    search_provider: str = "openrouter"
    search_model: str = ""
    search_api_key: str = Field(default="", repr=False)
    search_base_url: str = ""
    amap_api_key: str = Field(default="", repr=False)
    open_meteo_api_key: str = Field(default="", repr=False)

    langsmith_tracing: bool = False
    langsmith_trace_content: bool = False
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_api_key: str = Field(default="", repr=False)
    langsmith_project: str = "atlasflow-dev"

    database_path: str = str(DEFAULT_DATABASE_PATH)
    execution_database_url: str = Field(default="", repr=False)
    evaluation_storage_root: str = str(PROJECT_ROOT / "data" / "evaluations")
    evaluation_max_http_attempts: int = Field(default=60, ge=1, le=5000)
    evaluation_max_seconds: int = Field(default=1200, ge=1, le=7200)
    evaluation_case_timeout_seconds: int = Field(default=90, ge=1, le=1200)
    evaluation_concurrency: int = Field(default=2, ge=1, le=4)
    evaluation_judge_enabled: bool = False
    evaluation_judge_model: str = ""
    evaluation_judge_api_key: str = Field(default="", repr=False)
    evaluation_judge_base_url: str = ""
    evaluation_judge_max_tokens: int = Field(default=4096, ge=256, le=16384)
    evaluation_judge_max_reason_chars: int = Field(default=160, ge=40, le=600)
    evaluation_langsmith_sync: bool = True
    redis_url: str = "redis://localhost:6379/0"

    knowledge_backend: str = "postgres"
    knowledge_database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/atlasflow"
    knowledge_blob_root: str = str(DEFAULT_KNOWLEDGE_BLOB_ROOT)
    knowledge_work_root: str = str(DEFAULT_KNOWLEDGE_WORK_ROOT)
    knowledge_default_space: str = "user-default"
    knowledge_worker_enabled: bool = True
    knowledge_worker_poll_seconds: float = Field(default=0.5, ge=0.1, le=30.0)
    knowledge_query_expansion_enabled: bool = True
    knowledge_query_expansion_limit: int = Field(default=2, ge=1, le=4)
    knowledge_query_aliases_path: str = ""

    sse_heartbeat_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    sse_poll_seconds: float = Field(default=0.25, ge=0.05, le=2.0)

    max_research_tasks: int = Field(default=5, ge=2, le=5)
    max_research_concurrency: int = Field(default=3, ge=1, le=3)
    max_research_attempts: int = Field(default=2, ge=1, le=2)
    research_quorum_ratio: float = Field(default=0.5, gt=0.0, le=1.0)
    # Presentation: reliability is distinct from coverage; no artificial score floor.
    research_accept_partial_results: bool = True
    max_supplement_rounds: int = Field(default=1, ge=0, le=1)
    max_supplement_tasks: int = Field(default=2, ge=1, le=2)
    max_tasks_per_plan: int = Field(default=7, ge=5, le=7)
    max_replans: int = Field(default=1, ge=0, le=1)
    max_report_revisions: int = Field(default=1, ge=0, le=2)
    quality_threshold: int = Field(default=50, ge=50, le=100)
    quality_relaxed_review: bool = True
    max_tool_retries: int = 2
    tool_timeout_seconds: float = 120.0
    # Independent network phases, bounded again by the outer tool execution limit.
    web_fetch_connect_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    web_fetch_read_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    web_fetch_total_timeout_seconds: float = Field(default=35.0, gt=0, le=300)
    provider_timeout_seconds: float = 90.0
    rag_top_k: int = 5

    @field_validator("llm_model_options")
    @classmethod
    def trusted_chat_model_options(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not re.fullmatch(r"~?[A-Za-z0-9][A-Za-z0-9_.:/-]{0,198}", value)
               or "://" in value for value in cleaned):
            raise ValueError("LLM_MODEL_OPTIONS must contain model identifiers, not URLs")
        return list(dict.fromkeys(cleaned))


def resolve_evaluation_judge_model(settings: Settings) -> str:
    """An explicit Judge override wins; a blank override follows the primary LLM."""
    return settings.evaluation_judge_model.strip() or settings.llm_model.strip()


def configured_chat_models(settings: Settings, primary_model: str | None = None) -> list[str]:
    """Expose configured chat identifiers only, never credentials or provider URLs."""
    candidates = [primary_model if primary_model is not None else settings.llm_model,
                  *settings.llm_model_options]
    return list(dict.fromkeys(value.strip() for value in candidates if value.strip()))


@lru_cache
def get_settings() -> Settings:
    return Settings()
