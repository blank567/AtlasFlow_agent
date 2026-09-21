from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    langsmith_tracing: bool = False
    langsmith_trace_content: bool = False
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_api_key: str = Field(default="", repr=False)
    langsmith_project: str = "atlasflow-dev"

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/atlasflow"
    redis_url: str = "redis://localhost:6379/0"

    max_research_tasks: int = Field(default=5, ge=2, le=5)
    max_research_concurrency: int = Field(default=3, ge=1, le=3)
    max_research_attempts: int = Field(default=2, ge=1, le=2)
    research_quorum_ratio: float = Field(default=0.6, gt=0.0, le=1.0)
    max_supplement_rounds: int = Field(default=1, ge=0, le=1)
    max_supplement_tasks: int = Field(default=2, ge=1, le=2)
    max_tasks_per_plan: int = Field(default=7, ge=5, le=7)
    max_replans: int = Field(default=1, ge=0, le=1)
    max_report_revisions: int = Field(default=2, ge=0, le=2)
    quality_threshold: int = Field(default=80, ge=80, le=100)
    max_tool_retries: int = 2
    tool_timeout_seconds: float = 120.0
    provider_timeout_seconds: float = 90.0
    rag_top_k: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
