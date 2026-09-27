from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from hashlib import sha256
from threading import RLock
from typing import Any, Literal, TypeVar
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from atlasflow.config import Settings

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])
TraceStatus = Literal[
    "disabled",
    "not_configured",
    "configured",
    "ready",
    "degraded",
]
ConnectionStatus = Literal["not_checked", "reachable", "unreachable"]
SegmentStatus = Literal["running", "completed", "failed"]
SegmentTraceStatus = Literal[
    "disabled",
    "not_configured",
    "active",
    "completed",
    "degraded",
]
ProviderCallStatus = Literal["succeeded", "failed"]

try:
    from langsmith import Client as _LangSmithClient
    from langsmith import get_current_run_tree as _get_current_run_tree
    from langsmith import trace as _langsmith_trace
    from langsmith import traceable as _langsmith_traceable
    from langsmith import tracing_context as _tracing_context
except ImportError:  # Allows core tests to run before optional dependencies are installed.
    _LangSmithClient = None
    _get_current_run_tree = None
    _langsmith_trace = None
    _langsmith_traceable = None
    _tracing_context = None


class ProviderUsage(BaseModel):
    """Usage values returned by a provider. Missing values stay missing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)


class ProviderCallMetric(BaseModel):
    """Safe, content-free metadata for one logical provider request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(default_factory=lambda: str(uuid4()))
    provider: str = Field(min_length=1, max_length=80)
    operation: str = Field(min_length=1, max_length=80)
    endpoint: str = Field(min_length=1, max_length=200)
    status: ProviderCallStatus
    requested_model: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=300)
    request_id: str | None = Field(default=None, max_length=300)
    generation_id: str | None = Field(default=None, max_length=300)
    usage: ProviderUsage | None = None
    duration_ms: int = Field(ge=0)
    attempt_count: int = Field(ge=1)
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_type: str | None = Field(default=None, max_length=120)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TraceSegment(BaseModel):
    """One workflow invocation and its optional LangSmith root trace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_id: str
    trace_id: str | None = None
    atlasflow_run_id: str
    name: str
    kind: str
    status: SegmentStatus
    trace_status: SegmentTraceStatus
    url: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    error: str | None = None
    provider_calls: list[ProviderCallMetric] = Field(default_factory=list)


class ObservabilityMeta(BaseModel):
    """Public, secret-free LangSmith state suitable for API responses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    configured: bool
    project: str
    endpoint: str | None
    trace_content: bool
    status: TraceStatus
    connection_status: ConnectionStatus
    message: str | None = None
    last_checked_at: datetime | None = None
    segments: list[TraceSegment] = Field(default_factory=list)


TraceSegmentSink = Callable[[str, TraceSegment], Awaitable[None] | None]


@dataclass(slots=True)
class _RuntimeState:
    enabled: bool = False
    configured: bool = False
    project: str = "default"
    endpoint: str | None = None
    trace_content: bool = False
    status: TraceStatus = "disabled"
    connection_status: ConnectionStatus = "not_checked"
    message: str | None = None
    last_checked_at: datetime | None = None
    client: Any | None = None
    trace_error_count: int = 0
    configuration_fingerprint: tuple[object, ...] | None = None


_RUNTIME = _RuntimeState()
_RUNTIME_LOCK = RLock()
_PROVIDER_CALLS: ContextVar[list[ProviderCallMetric] | None] = ContextVar(
    "atlasflow_provider_calls", default=None
)
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

_SECRET_FIELD_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "access_token",
    "refresh_token",
    "token",
    "password",
    "passwd",
    "secret",
    "client_secret",
    "cookie",
    "set_cookie",
    "private_key",
}
_SECRET_SUFFIXES = (
    "_api_key",
    "_access_token",
    "_refresh_token",
    "_password",
    "_secret",
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(?:sk-or-v1|sk|or-v1)-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\blsv2_[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*"
        r"([^\s,;]+)"
    ),
)


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
    public_inputs = {
        str(key): value
        for key, value in inputs.items()
        if str(key).lower() not in {"self", "cls"} and not _is_secret_field(str(key))
    }
    if not _trace_content_enabled():
        return {
            "content_redacted": True,
            "input_fields": sorted(public_inputs),
        }
    redacted = redact_sensitive_data(public_inputs)
    return redacted if isinstance(redacted, dict) else {"input": redacted}


def _sanitize_trace_outputs(outputs: Any) -> dict[str, Any]:
    # AMap's response content is intentionally transient even when ordinary
    # LangSmith content tracing is enabled for the rest of the workflow.
    data = outputs.get("data") if isinstance(outputs, dict) else getattr(outputs, "data", None)
    if isinstance(data, dict) and data.get("provider") == "amap":
        return {
            "success": bool(
                outputs.get("success", False)
                if isinstance(outputs, dict)
                else getattr(outputs, "success", False)
            ),
            "provider": "amap",
            "navigation_url": data.get("navigation_url") if _trace_content_enabled() else None,
            "route_content_redacted": True,
        }
    if not _trace_content_enabled():
        return {
            "content_redacted": True,
            "output_type": type(outputs).__name__,
        }
    redacted = redact_sensitive_data(outputs)
    if isinstance(redacted, dict):
        return redacted
    return {"output": redacted}


def redact_sensitive_data(value: Any) -> Any:
    """Recursively convert trace data to safe JSON-like values and redact secrets."""

    return _redact(value, seen=set())


def _redact(value: Any, *, seen: set[int]) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<bytes:{len(value)}>"

    value_id = id(value)
    if value_id in seen:
        return "<cycle>"

    if isinstance(value, BaseModel):
        seen.add(value_id)
        try:
            return _redact(value.model_dump(mode="json"), seen=seen)
        finally:
            seen.remove(value_id)
    if is_dataclass(value) and not isinstance(value, type):
        seen.add(value_id)
        try:
            return _redact(
                {field.name: getattr(value, field.name) for field in fields(value)},
                seen=seen,
            )
        finally:
            seen.remove(value_id)
    if isinstance(value, Mapping):
        seen.add(value_id)
        try:
            sanitized: dict[str, Any] = {}
            for raw_key, item in value.items():
                key = str(raw_key)
                sanitized[key] = "[REDACTED]" if _is_secret_field(key) else _redact(item, seen=seen)
            return sanitized
        finally:
            seen.remove(value_id)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        seen.add(value_id)
        try:
            return [_redact(item, seen=seen) for item in value]
        finally:
            seen.remove(value_id)
    if isinstance(value, set | frozenset):
        seen.add(value_id)
        try:
            return sorted((_redact(item, seen=seen) for item in value), key=str)
        finally:
            seen.remove(value_id)
    return f"<{type(value).__name__}>"


def _redact_string(value: str) -> str:
    redacted = value
    redacted = _SECRET_PATTERNS[0].sub(r"\1[REDACTED]", redacted)
    redacted = _SECRET_PATTERNS[1].sub("[REDACTED]", redacted)
    redacted = _SECRET_PATTERNS[2].sub("[REDACTED]", redacted)
    redacted = _SECRET_PATTERNS[3].sub(r"\1=[REDACTED]", redacted)
    return redacted


def _is_secret_field(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return normalized in _SECRET_FIELD_NAMES or normalized.endswith(_SECRET_SUFFIXES)


def _trace_content_enabled() -> bool:
    return os.getenv("LANGSMITH_TRACE_CONTENT", "false").lower() in {
        "1",
        "true",
        "yes",
    }


def configure_langsmith(settings: Settings) -> ObservabilityMeta:
    """Configure the SDK authoritatively and remove stale tracing credentials."""

    tracing_requested = bool(settings.langsmith_tracing)
    api_key = settings.langsmith_api_key.strip()
    project = settings.langsmith_project.strip() or "default"
    raw_endpoint = settings.langsmith_endpoint.strip()
    endpoint = _safe_endpoint(raw_endpoint)
    trace_content = bool(settings.langsmith_trace_content)
    fingerprint: tuple[object, ...] = (
        tracing_requested,
        trace_content,
        project,
        endpoint,
        sha256(api_key.encode("utf-8")).hexdigest(),
        id(_LangSmithClient),
    )

    sdk_tracing_enabled = bool(
        tracing_requested
        and api_key
        and _LangSmithClient is not None
        and (endpoint is not None or not raw_endpoint)
    )
    # Decorated child calls also inspect the environment. Keep them disabled when
    # tracing was requested but cannot be configured, otherwise the SDK may try to
    # create a global client with missing or invalid credentials.
    os.environ["LANGSMITH_TRACING"] = str(sdk_tracing_enabled).lower()
    os.environ["LANGSMITH_TRACE_CONTENT"] = str(trace_content).lower()
    os.environ["LANGSMITH_HIDE_INPUTS"] = str(not trace_content).lower()
    os.environ["LANGSMITH_HIDE_OUTPUTS"] = str(not trace_content).lower()
    os.environ["LANGSMITH_PROJECT"] = project
    _set_or_clear_env("LANGSMITH_ENDPOINT", endpoint or "")
    _set_or_clear_env("LANGSMITH_API_KEY", api_key)

    # Prevent legacy aliases from silently re-enabling a stale configuration.
    for legacy_name in (
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
        "LANGCHAIN_ENDPOINT",
        "LANGCHAIN_PROJECT",
    ):
        os.environ.pop(legacy_name, None)

    with _RUNTIME_LOCK:
        if _RUNTIME.configuration_fingerprint == fingerprint:
            return get_observability_meta()

    configured = bool(api_key) and _LangSmithClient is not None
    status: TraceStatus
    message: str | None = None
    client: Any | None = None
    initialization_error: Exception | None = None
    if configured and (endpoint is not None or not raw_endpoint):
        try:
            client = _LangSmithClient(
                api_url=endpoint,
                api_key=api_key,
                hide_inputs=(redact_sensitive_data if trace_content else True),
                hide_outputs=(redact_sensitive_data if trace_content else True),
                hide_metadata=redact_sensitive_data,
                tracing_error_callback=_record_tracing_error,
            )
        except Exception as exc:  # noqa: BLE001 - observability must never block runtime.
            initialization_error = exc
    elif configured:
        initialization_error = ValueError("Invalid LangSmith endpoint")

    if not tracing_requested:
        status = "disabled"
        if initialization_error is not None:
            message = (
                f"LangSmith client initialization failed ({type(initialization_error).__name__})."
            )
    elif not api_key:
        status = "not_configured"
        message = "LangSmith tracing is enabled but LANGSMITH_API_KEY is missing."
    elif _LangSmithClient is None:
        status = "degraded"
        message = "LangSmith SDK is unavailable; the application will continue without tracing."
    elif initialization_error is not None:
        status = "degraded"
        message = f"LangSmith client initialization failed ({type(initialization_error).__name__})."
    else:
        status = "configured"

    os.environ["LANGSMITH_TRACING"] = str(tracing_requested and client is not None).lower()

    with _RUNTIME_LOCK:
        _RUNTIME.enabled = tracing_requested
        _RUNTIME.configured = configured and client is not None
        _RUNTIME.project = project
        _RUNTIME.endpoint = endpoint
        _RUNTIME.trace_content = trace_content
        _RUNTIME.status = status
        _RUNTIME.connection_status = "not_checked"
        _RUNTIME.message = message
        _RUNTIME.last_checked_at = None
        _RUNTIME.client = client
        _RUNTIME.trace_error_count = 0
        _RUNTIME.configuration_fingerprint = fingerprint if initialization_error is None else None
    return get_observability_meta()


def get_observability_meta(*, segments: Sequence[TraceSegment] = ()) -> ObservabilityMeta:
    with _RUNTIME_LOCK:
        return ObservabilityMeta(
            enabled=_RUNTIME.enabled,
            configured=_RUNTIME.configured,
            project=_RUNTIME.project,
            endpoint=_RUNTIME.endpoint,
            trace_content=_RUNTIME.trace_content,
            status=_RUNTIME.status,
            connection_status=_RUNTIME.connection_status,
            message=_RUNTIME.message,
            last_checked_at=_RUNTIME.last_checked_at,
            segments=list(segments),
        )


async def check_langsmith_connection() -> ObservabilityMeta:
    """Verify project access without exposing credentials or blocking the event loop."""

    with _RUNTIME_LOCK:
        client = _RUNTIME.client
        project = _RUNTIME.project
        enabled = _RUNTIME.enabled
        configured = _RUNTIME.configured

    checked_at = datetime.now(UTC)
    if not configured or client is None:
        with _RUNTIME_LOCK:
            _RUNTIME.last_checked_at = checked_at
        return get_observability_meta()

    try:
        await asyncio.to_thread(client.read_project, project_name=project)
    except Exception as exc:  # noqa: BLE001 - connectivity is reported, not raised.
        with _RUNTIME_LOCK:
            _RUNTIME.connection_status = "unreachable"
            _RUNTIME.status = "degraded" if enabled else "disabled"
            _RUNTIME.message = f"LangSmith connection check failed ({type(exc).__name__})."
            _RUNTIME.last_checked_at = checked_at
    else:
        with _RUNTIME_LOCK:
            _RUNTIME.connection_status = "reachable"
            _RUNTIME.status = "ready" if enabled else "disabled"
            _RUNTIME.message = None
            _RUNTIME.last_checked_at = checked_at
    return get_observability_meta()


def record_provider_call(metric: ProviderCallMetric) -> None:
    """Attach a provider metric to the active segment and current LangSmith span."""

    collector = _PROVIDER_CALLS.get()
    if collector is not None:
        collector.append(metric)

    if _get_current_run_tree is None:
        return
    try:
        run_tree = _get_current_run_tree()
        if run_tree is None:
            return
        metadata: dict[str, Any] = {
            "provider": metric.provider,
            "provider_operation": metric.operation,
            "provider_status": metric.status,
        }
        for key, value in (
            ("provider_request_id", metric.request_id),
            ("provider_generation_id", metric.generation_id),
            ("model", metric.model),
            ("requested_model", metric.requested_model),
            ("prompt_tokens", metric.usage.prompt_tokens if metric.usage else None),
            (
                "completion_tokens",
                metric.usage.completion_tokens if metric.usage else None,
            ),
            ("total_tokens", metric.usage.total_tokens if metric.usage else None),
            ("cost", metric.usage.cost if metric.usage else None),
        ):
            if value is not None:
                metadata[key] = value
        run_tree.add_metadata(metadata)
    except Exception as exc:  # noqa: BLE001 - metrics must not affect model calls.
        _record_tracing_error(exc)


@dataclass(slots=True)
class TraceSegmentHandle:
    segment: TraceSegment
    outputs: dict[str, Any] | None = None

    def set_outputs(self, outputs: Mapping[str, Any]) -> None:
        safe = redact_sensitive_data(dict(outputs))
        self.outputs = safe if isinstance(safe, dict) else {"output": safe}


@asynccontextmanager
async def trace_segment(
    *,
    atlasflow_run_id: str,
    name: str,
    kind: str,
    metadata: Mapping[str, Any] | None = None,
    sink: TraceSegmentSink | None = None,
):
    """Create a root trace for one initial/resume workflow invocation.

    Tracing and sink failures are intentionally isolated from the workflow exception.
    """

    segment_uuid = uuid4()
    started_at = datetime.now(UTC)
    provider_calls: list[ProviderCallMetric] = []
    calls_token = _PROVIDER_CALLS.set(provider_calls)
    manager: Any | None = None
    tracing_scope: Any | None = None
    run_tree: Any | None = None
    trace_setup_error: BaseException | None = None

    with _RUNTIME_LOCK:
        enabled = _RUNTIME.enabled
        configured = _RUNTIME.configured
        client = _RUNTIME.client
        project = _RUNTIME.project
        trace_errors_at_start = _RUNTIME.trace_error_count

    if not enabled:
        initial_trace_status: SegmentTraceStatus = "disabled"
    elif not configured or client is None:
        initial_trace_status = "not_configured"
    else:
        initial_trace_status = "active"
        try:
            if _tracing_context is None or _langsmith_trace is None:
                raise RuntimeError("LangSmith tracing API is unavailable")
            safe_metadata = redact_sensitive_data(
                {
                    "atlasflow_run_id": atlasflow_run_id,
                    "atlasflow_segment_id": str(segment_uuid),
                    "atlasflow_segment_kind": kind,
                    **dict(metadata or {}),
                }
            )
            tracing_scope = _tracing_context(
                enabled=True,
                client=client,
                project_name=project,
                tags=[
                    "atlasflow",
                    f"atlasflow_run:{atlasflow_run_id}",
                    f"segment:{kind}",
                ],
                metadata=safe_metadata,
            )
            tracing_scope.__enter__()
            manager = _langsmith_trace(
                name=name,
                run_type="chain",
                run_id=segment_uuid,
                project_name=project,
                client=client,
                inputs={
                    "atlasflow_run_id": atlasflow_run_id,
                    "segment_kind": kind,
                },
            )
            run_tree = await manager.__aenter__()
        except Exception as exc:  # noqa: BLE001 - trace setup is best effort.
            trace_setup_error = exc
            initial_trace_status = "degraded"
            _record_tracing_error(exc)
            if tracing_scope is not None:
                try:
                    tracing_scope.__exit__(type(exc), exc, exc.__traceback__)
                except Exception as cleanup_exc:  # noqa: BLE001
                    _record_tracing_error(cleanup_exc)
                tracing_scope = None
            manager = None
            run_tree = None

    segment = TraceSegment(
        segment_id=str(segment_uuid),
        trace_id=(str(run_tree.trace_id) if run_tree is not None else None),
        atlasflow_run_id=atlasflow_run_id,
        name=name,
        kind=kind,
        status="running",
        trace_status=initial_trace_status,
        started_at=started_at,
        error=(
            f"Tracing setup failed ({type(trace_setup_error).__name__})."
            if trace_setup_error is not None
            else None
        ),
    )
    handle = TraceSegmentHandle(segment=segment)
    await _notify_segment(sink, atlasflow_run_id, segment)

    application_error: BaseException | None = None
    try:
        yield handle
    except BaseException as exc:
        application_error = exc
        raise
    finally:
        if run_tree is not None and application_error is None:
            try:
                outputs = handle.outputs or {"status": "completed"}
                run_tree.end(outputs=_sanitize_trace_outputs(outputs))
            except Exception as exc:  # noqa: BLE001
                _record_tracing_error(exc)
        if manager is not None:
            try:
                await manager.__aexit__(
                    type(application_error) if application_error is not None else None,
                    application_error,
                    application_error.__traceback__ if application_error is not None else None,
                )
            except Exception as exc:  # noqa: BLE001 - preserve application result/error.
                _record_tracing_error(exc)
        if tracing_scope is not None:
            try:
                tracing_scope.__exit__(
                    type(application_error) if application_error is not None else None,
                    application_error,
                    application_error.__traceback__ if application_error is not None else None,
                )
            except Exception as exc:  # noqa: BLE001
                _record_tracing_error(exc)

        with _RUNTIME_LOCK:
            trace_error_count = _RUNTIME.trace_error_count
        trace_status = initial_trace_status
        if initial_trace_status == "active":
            trace_status = "degraded" if trace_error_count > trace_errors_at_start else "completed"
        final_error: str | None = None
        if application_error is not None:
            safe_error = _redact_string(str(application_error).strip())[:500]
            final_error = safe_error or type(application_error).__name__
        elif trace_setup_error is not None:
            final_error = f"Tracing setup failed ({type(trace_setup_error).__name__})."
        final_segment = segment.model_copy(
            update={
                "status": "failed" if application_error is not None else "completed",
                "trace_status": trace_status,
                "ended_at": datetime.now(UTC),
                "error": final_error,
                "provider_calls": list(provider_calls),
            }
        )
        handle.segment = final_segment
        await _notify_segment(sink, atlasflow_run_id, final_segment)
        if sink is not None and final_segment.trace_status == "completed":
            _schedule_url_resolution(sink, atlasflow_run_id, final_segment)
        _PROVIDER_CALLS.reset(calls_token)


async def resolve_trace_segment_url(segment: TraceSegment) -> TraceSegment:
    """Resolve a URL only after LangSmith confirms that the run exists."""

    if segment.trace_id is None or segment.trace_status not in {"active", "completed"}:
        return segment
    with _RUNTIME_LOCK:
        client = _RUNTIME.client
    if client is None:
        return segment
    try:
        await asyncio.to_thread(client.flush, 2.0)
        remote_run = await asyncio.to_thread(client.read_run, segment.segment_id)
        url = remote_run.url
        if not url:
            url = await asyncio.to_thread(client.get_run_url, run=remote_run)
    except Exception as exc:  # noqa: BLE001 - link resolution is optional.
        _record_tracing_error(exc)
        return segment.model_copy(update={"trace_status": "degraded"})
    return segment.model_copy(update={"url": str(url)})


def _schedule_url_resolution(sink: TraceSegmentSink, run_id: str, segment: TraceSegment) -> None:
    async def resolve_and_notify() -> None:
        resolved = await resolve_trace_segment_url(segment)
        if resolved != segment:
            await _notify_segment(sink, run_id, resolved)

    task = asyncio.create_task(
        resolve_and_notify(),
        name=f"atlasflow-trace-url-{segment.segment_id}",
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _notify_segment(
    sink: TraceSegmentSink | None, run_id: str, segment: TraceSegment
) -> None:
    if sink is None:
        return
    try:
        result = sink(run_id, segment)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 - telemetry persistence is best effort here.
        logger.warning("Trace segment sink failed: %s", type(exc).__name__)


def _record_tracing_error(exc: Exception) -> None:
    with _RUNTIME_LOCK:
        _RUNTIME.trace_error_count += 1
        if _RUNTIME.enabled:
            _RUNTIME.status = "degraded"
            _RUNTIME.message = f"LangSmith tracing degraded ({type(exc).__name__})."


def _set_or_clear_env(name: str, value: str) -> None:
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)


def _safe_endpoint(endpoint: str) -> str | None:
    if not endpoint:
        return None
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
