import os
from collections.abc import Callable
from typing import Any, TypeVar

from atlasflow.config import Settings

F = TypeVar("F", bound=Callable[..., Any])

try:
    from langsmith import traceable as _langsmith_traceable
except ImportError:  # Allows core unit tests to run before optional dependencies are installed.
    _langsmith_traceable = None


def traced(*, name: str, run_type: str = "chain") -> Callable[[F], F]:
    """Return a LangSmith decorator, or a no-op when the SDK is unavailable."""

    def decorator(func: F) -> F:
        if _langsmith_traceable is None:
            return func
        return _langsmith_traceable(
            name=name,
            run_type=run_type,
            # Bound instances may own API clients. Never serialize `self` or secret-like inputs.
            process_inputs=_sanitize_trace_inputs,
            process_outputs=_sanitize_trace_outputs,
        )(func)  # type: ignore[return-value]

    return decorator


def _sanitize_trace_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    blocked = {"self", "cls", "api_key", "authorization"}
    sanitized = {
        key: value
        for key, value in inputs.items()
        if key.lower() not in blocked and not key.lower().endswith("_api_key")
    }
    if not _trace_content_enabled():
        return {
            "content_redacted": True,
            "input_fields": sorted(sanitized),
        }
    return sanitized


def _sanitize_trace_outputs(outputs: Any) -> dict[str, Any]:
    if not _trace_content_enabled():
        return {
            "content_redacted": True,
            "output_type": type(outputs).__name__,
        }
    if isinstance(outputs, dict):
        return outputs
    return {"output": outputs}


def _trace_content_enabled() -> bool:
    return os.getenv("LANGSMITH_TRACE_CONTENT", "false").lower() in {"1", "true", "yes"}


def configure_langsmith(settings: Settings) -> None:
    """Expose validated settings to the LangSmith SDK without logging secrets."""

    os.environ["LANGSMITH_TRACING"] = str(settings.langsmith_tracing).lower()
    os.environ["LANGSMITH_TRACE_CONTENT"] = str(settings.langsmith_trace_content).lower()
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    if settings.langsmith_api_key:
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
