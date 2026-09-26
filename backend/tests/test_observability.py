from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from atlasflow.config import Settings
from atlasflow.observability import (
    ProviderCallMetric,
    ProviderUsage,
    _sanitize_trace_inputs,
    _sanitize_trace_outputs,
    configure_langsmith,
    get_observability_meta,
    record_provider_call,
    redact_sensitive_data,
    trace_segment,
)


@pytest.fixture(autouse=True)
def _isolate_langsmith_runtime():
    configure_langsmith(
        Settings(_env_file=None, langsmith_tracing=False, langsmith_api_key="")
    )
    yield
    # Do not leave tracing enabled for tests whose decorators consult process env.
    configure_langsmith(
        Settings(_env_file=None, langsmith_tracing=False, langsmith_api_key="")
    )


def test_trace_content_is_redacted_by_default(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACE_CONTENT", "false")

    inputs = _sanitize_trace_inputs(
        {"self": object(), "query": "confidential", "api_key": "secret"}
    )
    outputs = _sanitize_trace_outputs("confidential result")

    assert inputs == {"content_redacted": True, "input_fields": ["query"]}
    assert outputs == {"content_redacted": True, "output_type": "str"}
    assert "confidential" not in repr((inputs, outputs))


def test_trace_content_can_be_enabled_explicitly(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACE_CONTENT", "true")

    assert _sanitize_trace_inputs({"query": "public demo"}) == {"query": "public demo"}
    assert _sanitize_trace_outputs(["public output"]) == {"output": ["public output"]}


def test_recursive_redaction_never_serializes_nested_secrets() -> None:
    cyclic: dict[str, Any] = {
        "query": "Bearer public-looking-but-secret-token",
        "nested": {
            "apiKey": "top-secret",
            "headers": {"Authorization": "Bearer another-secret-token"},
        },
        "items": [{"password": "hidden"}],
        "openrouter": "sk-or-v1-abcdefghijklmnop",
        "langsmith": "lsv2_pt_abcdefghijklmnop",
    }
    cyclic["cycle"] = cyclic

    sanitized = redact_sensitive_data(cyclic)
    representation = repr(sanitized)

    assert sanitized["nested"]["apiKey"] == "[REDACTED]"
    assert sanitized["nested"]["headers"]["Authorization"] == "[REDACTED]"
    assert sanitized["cycle"] == "<cycle>"
    for secret in (
        "public-looking-but-secret-token",
        "another-secret-token",
        "top-secret",
        "hidden",
        "abcdefghijklmnop",
    ):
        assert secret not in representation


def test_configure_is_authoritative_and_enables_sdk_level_hiding(monkeypatch) -> None:
    created: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            created.update(kwargs)

    monkeypatch.setattr("atlasflow.observability._LangSmithClient", FakeClient)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "stale-legacy-key")
    monkeypatch.setenv("LANGSMITH_API_KEY", "stale-modern-key")

    meta = configure_langsmith(
        Settings(
            _env_file=None,
            langsmith_tracing=True,
            langsmith_trace_content=False,
            langsmith_api_key="new-key",
            langsmith_endpoint="https://user:pass@smith.example/api?token=hidden",
            langsmith_project="atlasflow-test",
        )
    )

    assert meta.status == "configured"
    assert meta.configured is True
    assert meta.endpoint == "https://smith.example/api"
    assert created["api_key"] == "new-key"
    assert created["hide_inputs"] is True
    assert created["hide_outputs"] is True
    assert created["hide_metadata"] is redact_sensitive_data
    assert os.environ["LANGSMITH_HIDE_INPUTS"] == "true"
    assert os.environ["LANGSMITH_HIDE_OUTPUTS"] == "true"
    assert "LANGCHAIN_TRACING_V2" not in os.environ
    assert "LANGCHAIN_API_KEY" not in os.environ


def test_full_trace_content_keeps_inputs_outputs_and_redacts_secrets(monkeypatch) -> None:
    created: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            created.update(kwargs)

    monkeypatch.setattr("atlasflow.observability._LangSmithClient", FakeClient)
    configure_langsmith(
        Settings(
            _env_file=None,
            langsmith_tracing=True,
            langsmith_trace_content=True,
            langsmith_api_key="configured-key",
        )
    )
    assert os.environ["LANGSMITH_HIDE_INPUTS"] == "false"
    assert os.environ["LANGSMITH_HIDE_OUTPUTS"] == "false"
    assert callable(created["hide_inputs"])
    assert callable(created["hide_outputs"])
    assert created["hide_inputs"]({"query": "visible", "api_key": "private"}) == {
        "query": "visible",
        "api_key": "[REDACTED]",
    }


def test_configure_clears_a_stale_api_key_and_reports_missing(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "stale-key")

    meta = configure_langsmith(
        Settings(
            _env_file=None,
            langsmith_tracing=True,
            langsmith_api_key="",
        )
    )

    assert meta.status == "not_configured"
    assert meta.configured is False
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert "LANGSMITH_API_KEY" not in os.environ


def test_configure_is_idempotent_for_the_same_secret_fingerprint(monkeypatch) -> None:
    clients = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            clients.append(kwargs)

    monkeypatch.setattr("atlasflow.observability._LangSmithClient", FakeClient)
    settings = Settings(
        _env_file=None,
        langsmith_tracing=True,
        langsmith_api_key="never-exposed-in-meta",
        langsmith_project="atlasflow-idempotent",
    )

    first = configure_langsmith(settings)
    second = configure_langsmith(settings)

    assert first == second
    assert len(clients) == 1
    assert "never-exposed-in-meta" not in repr(second)


@pytest.mark.asyncio
async def test_disabled_segment_collects_real_provider_metrics() -> None:
    configure_langsmith(
        Settings(_env_file=None, langsmith_tracing=False, langsmith_api_key="")
    )
    updates = []

    async def sink(run_id, segment) -> None:
        assert run_id == "run-42"
        updates.append(segment)

    async with trace_segment(
        atlasflow_run_id="run-42",
        name="atlasflow.workflow.initial",
        kind="initial",
        sink=sink,
    ) as handle:
        record_provider_call(
            ProviderCallMetric(
                provider="openrouter",
                operation="chat",
                endpoint="/chat/completions",
                status="succeeded",
                requested_model="requested/model",
                model="actual/model",
                request_id="request-1",
                generation_id="generation-1",
                usage=ProviderUsage(
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                    cost=0.002,
                ),
                duration_ms=20,
                attempt_count=1,
                http_status=200,
            )
        )
        handle.set_outputs({"status": "waiting_approval"})

    assert [item.status for item in updates] == ["running", "completed"]
    assert updates[-1].trace_status == "disabled"
    assert updates[-1].trace_id is None
    assert updates[-1].url is None
    assert updates[-1].provider_calls[0].model == "actual/model"


@pytest.mark.asyncio
async def test_trace_setup_failure_is_degraded_without_blocking_work(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

    class FakeTracingContext:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            del args

    class BrokenTrace:
        async def __aenter__(self):
            raise ConnectionError("secret=must-not-leak")

        async def __aexit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr("atlasflow.observability._LangSmithClient", FakeClient)
    monkeypatch.setattr(
        "atlasflow.observability._tracing_context",
        lambda **kwargs: FakeTracingContext(),
    )
    monkeypatch.setattr(
        "atlasflow.observability._langsmith_trace", lambda **kwargs: BrokenTrace()
    )
    configure_langsmith(
        Settings(
            _env_file=None,
            langsmith_tracing=True,
            langsmith_api_key="configured-key",
        )
    )
    updates = []

    async with trace_segment(
        atlasflow_run_id="run-degraded",
        name="atlasflow.workflow.initial",
        kind="initial",
        sink=lambda run_id, segment: updates.append(segment),
    ):
        result = "workflow-still-ran"

    assert result == "workflow-still-ran"
    assert updates[-1].status == "completed"
    assert updates[-1].trace_status == "degraded"
    assert "must-not-leak" not in repr(updates[-1])
    assert get_observability_meta().status == "degraded"


@pytest.mark.asyncio
async def test_trace_segment_ends_once_then_exits_async_manager(monkeypatch) -> None:
    from uuid import uuid4

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

    class FakeTracingContext:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            del args

    class FakeRunTree:
        trace_id = uuid4()

        def __init__(self) -> None:
            self.end_calls = []

        def end(self, **kwargs: Any) -> None:
            self.end_calls.append(kwargs)

    run_tree = FakeRunTree()

    class FakeTraceManager:
        def __init__(self) -> None:
            self.exit_calls = 0

        async def __aenter__(self):
            return run_tree

        async def __aexit__(self, *args: object) -> None:
            del args
            self.exit_calls += 1

    manager = FakeTraceManager()
    url_resolved = asyncio.Event()
    updates = []

    async def fake_resolver(segment):
        return segment.model_copy(update={"url": "https://smith.test/runs/confirmed"})

    async def sink(run_id, segment) -> None:
        del run_id
        updates.append(segment)
        if segment.url is not None:
            url_resolved.set()

    monkeypatch.setattr("atlasflow.observability._LangSmithClient", FakeClient)
    monkeypatch.setattr(
        "atlasflow.observability._tracing_context",
        lambda **kwargs: FakeTracingContext(),
    )
    monkeypatch.setattr(
        "atlasflow.observability._langsmith_trace", lambda **kwargs: manager
    )
    monkeypatch.setattr(
        "atlasflow.observability.resolve_trace_segment_url", fake_resolver
    )
    configure_langsmith(
        Settings(
            _env_file=None,
            langsmith_tracing=True,
            langsmith_api_key="configured-key",
        )
    )

    async with trace_segment(
        atlasflow_run_id="run-success",
        name="atlasflow.workflow.initial",
        kind="initial",
        sink=sink,
    ) as handle:
        handle.set_outputs({"status": "completed"})

    await asyncio.wait_for(url_resolved.wait(), timeout=1)

    assert len(run_tree.end_calls) == 1
    assert manager.exit_calls == 1
    assert handle.segment.status == "completed"
    assert handle.segment.trace_status == "completed"
    assert updates[-1].url == "https://smith.test/runs/confirmed"
