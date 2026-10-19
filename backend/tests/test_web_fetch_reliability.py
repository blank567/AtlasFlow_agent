"""Offline timeout, checked-IP fallback and Run-scoped circuit regressions."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from atlasflow.agents.contracts import RunPolicy
from atlasflow.agents.tool_failures import ToolFailureGuard
from atlasflow.agents.tool_runtime import RequiredToolError
from atlasflow.config import Settings
from atlasflow.evaluation.budget import (
    EvaluationBudget,
    EvaluationBudgetExceeded,
    evaluation_budget_context,
)
from atlasflow.evaluation.contracts import EvaluationBudgetSpec
from atlasflow.schemas import ToolCallRecord
from atlasflow.tools.base import ToolContext
from atlasflow.tools.catalog import build_tool_registry
from atlasflow.tools.registry import ToolRegistry
from atlasflow.tools.web_fetch import WebFetchArguments, WebFetchTool
from pydantic import ValidationError

from backend.tests.fakes import FakeWebSearchTool
from backend.tests.test_tool_runtime import _call, _response, _runtime

CONTEXT = ToolContext(run_id="reliability", agent_name="researcher")
PAGE = "Verified public source text containing sufficient facts for source evidence."


async def dns(host, port):
    return ["93.184.215.14"]


@pytest.mark.parametrize("field", [
    "web_fetch_connect_timeout_seconds", "web_fetch_read_timeout_seconds",
    "web_fetch_total_timeout_seconds",
])
@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf")])
def test_invalid_network_timeouts_are_rejected(field, invalid):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: invalid})


def test_catalog_connects_configuration_to_reader_and_clamps_total_to_outer_limit():
    settings = Settings(
        _env_file=None, tool_timeout_seconds=4,
        web_fetch_connect_timeout_seconds=1, web_fetch_read_timeout_seconds=2,
        web_fetch_total_timeout_seconds=30,
    )
    reader = build_tool_registry(settings, web_search=None).get("web_fetch")
    assert reader.connect_timeout_seconds == 1
    assert reader.read_timeout_seconds == 2
    assert reader.total_timeout_seconds == pytest.approx(3.8)


@pytest.mark.asyncio
async def test_configured_network_timeouts_reach_httpx_transport():
    def handle(request):
        assert request.extensions["timeout"] == {"connect": 2.5, "read": 7, "write": 7, "pool": 7}
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/plain"})
    reader = WebFetchTool(resolver=dns, transport=httpx.MockTransport(handle),
                          connect_timeout_seconds=2.5, read_timeout_seconds=7)
    result = await reader.run(WebFetchArguments(url="https://example.org/page"), CONTEXT)
    assert result.success


@pytest.mark.asyncio
@pytest.mark.parametrize("exception,code", [
    (httpx.ConnectTimeout, "WEB_FETCH_CONNECT_TIMEOUT"),
    (httpx.ReadTimeout, "WEB_FETCH_READ_TIMEOUT"),
    (httpx.WriteTimeout, "WEB_FETCH_WRITE_TIMEOUT"),
    (httpx.PoolTimeout, "WEB_FETCH_POOL_TIMEOUT"),
    (httpx.TimeoutException, "WEB_FETCH_NETWORK_TIMEOUT"),
])
async def test_network_timeout_phases_have_distinct_safe_errors(exception, code):
    def handle(request):
        raise exception("private exception details must not escape", request=request)
    reader = WebFetchTool(resolver=dns, transport=httpx.MockTransport(handle))
    result = await reader.run(WebFetchArguments(url="https://example.org/page"), CONTEXT)
    assert not result.success and result.retryable and not result.evidence
    assert result.error.startswith(code + ":")
    assert "private exception" not in result.error


@pytest.mark.asyncio
async def test_total_timeout_includes_dns_without_starting_an_http_attempt():
    async def stalled_dns(host, port):
        await asyncio.sleep(60)
    reader = WebFetchTool(resolver=stalled_dns, total_timeout_seconds=0.01,
                          transport=httpx.MockTransport(lambda _: pytest.fail("DNS not finished")))
    result = await asyncio.wait_for(
        reader.run(WebFetchArguments(url="https://example.org/page"), CONTEXT), 1
    )
    assert result.error.startswith("WEB_FETCH_TOTAL_TIMEOUT:") and result.retryable


@pytest.mark.asyncio
async def test_fallback_checks_host_and_sni_and_counts_each_business_http_attempt():
    requests = []
    async def two_addresses(host, port):
        return ["93.184.215.14", "93.184.215.15"]
    def handle(request):
        requests.append(request)
        assert request.headers["host"] == "example.org"
        assert request.extensions["sni_hostname"] == "example.org"
        if request.url.host == "93.184.215.14":
            raise httpx.ConnectTimeout("first IP unreachable", request=request)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/plain"})
    reader = WebFetchTool(resolver=two_addresses, transport=httpx.MockTransport(handle))
    cap = EvaluationBudget(EvaluationBudgetSpec(max_http_attempts=2))
    with evaluation_budget_context(cap):
        result = await reader.run(WebFetchArguments(url="https://example.org/page"), CONTEXT)
    assert result.success and len(requests) == 2
    assert result.evidence[0].uri == "https://example.org/page"
    assert cap.ledger()["by_kind"] == {"web_fetch": 2}


@pytest.mark.asyncio
async def test_fallback_cannot_bypass_evaluation_http_budget():
    sent = []
    async def two_addresses(host, port):
        return ["93.184.215.14", "93.184.215.15"]
    def handle(request):
        sent.append(request)
        raise httpx.ConnectTimeout("unreachable", request=request)
    reader = WebFetchTool(resolver=two_addresses, transport=httpx.MockTransport(handle))
    cap = EvaluationBudget(EvaluationBudgetSpec(max_http_attempts=1))
    with evaluation_budget_context(cap), pytest.raises(EvaluationBudgetExceeded):
        await reader.run(WebFetchArguments(url="https://example.org/page"), CONTEXT)
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["read_timeout", "http_503", "private_second_ip", "connect_failures"])
async def test_fallback_is_bounded_and_never_bypasses_response_or_ssrf_checks(mode):
    sent = []
    async def addresses(host, port):
        return ["93.184.215.14", "10.0.0.1"] if mode == "private_second_ip" else [
            "93.184.215.14", "93.184.215.15", "93.184.215.16"
        ]
    def handle(request):
        sent.append(request)
        if mode == "read_timeout":
            raise httpx.ReadTimeout("read stalled", request=request)
        if mode == "connect_failures":
            raise httpx.ConnectTimeout("no address responds", request=request)
        return httpx.Response(503)
    reader = WebFetchTool(resolver=addresses, transport=httpx.MockTransport(handle))
    result = await reader.run(WebFetchArguments(url="https://example.org/page"), CONTEXT)
    expected = {"private_second_ip": 0, "read_timeout": 1, "http_503": 1, "connect_failures": 2}
    assert len(sent) == expected[mode] and not result.success
    if mode == "private_second_ip":
        assert "BLOCKED_ADDRESS" in result.error


@pytest.mark.asyncio
async def test_timeout_during_body_read_closes_stream_and_keeps_read_phase():
    class Body(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b"partial source body"
            raise httpx.ReadTimeout("body stalled")
        async def aclose(self):
            self.closed = True
    body = Body()
    reader = WebFetchTool(resolver=dns, transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=body, headers={"content-type": "text/plain"})
    ))
    result = await reader.run(WebFetchArguments(url="https://example.org/page"), CONTEXT)
    assert result.error.startswith("WEB_FETCH_READ_TIMEOUT:") and body.closed
    assert not result.evidence


@pytest.mark.asyncio
async def test_cancel_is_not_converted_to_retryable_failure():
    entered = asyncio.Event()
    async def resolver(host, port):
        entered.set()
        await asyncio.sleep(60)
    task = asyncio.create_task(WebFetchTool(resolver=resolver).run(
        WebFetchArguments(url="https://example.org/page"), CONTEXT
    ))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def record(index, *, success=False, url="https://example.org/page", reused=None, retryable=True):
    return ToolCallRecord(
        tool_name="web_fetch", call_id=f"call-{index}", arguments={"url": url},
        success=success, error=None if success else "WEB_FETCH_TIMEOUT: legacy timeout",
        retryable=None if success else retryable, duration_ms=1, reused_from_call_id=reused,
        created_at=datetime(2026, 10, 18, tzinfo=UTC) + timedelta(seconds=index),
    )


def test_circuit_counts_real_consecutive_outcomes_not_focus_paths_or_replay():
    guard = ToolFailureGuard()
    reader = WebFetchTool()
    args = {"url": "https://example.org:443/another", "focus": "new question"}
    initial = [record(1), record(2, url="https://example.org/guide")]
    guard.observe("run", initial)
    guard.observe("run", initial)
    assert guard.blocking_record("run", reader, args)
    guard.observe("run", [record(3, success=True, reused="cached-success")])
    assert guard.blocking_record("run", reader, args)
    assert not guard.blocking_record("other-run", reader, args)
    assert not guard.blocking_record("run", reader, {"url": "https://other.example.org/"})
    guard.observe("run", [record(4, success=True), record(5)])
    assert not guard.blocking_record("run", reader, args)
    guard.observe("run", [record(6)])
    assert guard.blocking_record("run", reader, args)
    guard.clear("run")
    assert not guard.blocking_record("run", reader, args)


def test_terminal_or_unknown_failure_does_not_count_as_a_second_transient_failure():
    guard = ToolFailureGuard()
    reader = WebFetchTool()
    guard.observe("run", [record(1), record(2, retryable=False), record(3)])
    assert not guard.blocking_record("run", reader, {"url": "https://example.org/"})
    guard.observe("legacy", [record(1), record(2, retryable=None)])
    assert not guard.blocking_record("legacy", reader, {"url": "https://example.org/"})


@pytest.mark.asyncio
async def test_completed_failures_are_shared_with_branches_holding_stale_prior_calls():
    sent = []
    mode = ["fail"]
    replies = iter([
        _call("web_fetch", {"url": "https://example.org/changed"}),
        _call("web_fetch", {"url": "https://alternative.org/source"}),
        {"content": "done"},
    ])
    def handle(request):
        sent.append(request)
        if request.headers["host"] == "example.org":
            raise httpx.ConnectTimeout("unreachable", request=request)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/plain"})
    def decide(request):
        return _response(_call("web_fetch", {"url": "https://example.org/"})
                         if mode[0] == "fail" else next(replies))
    registry = ToolRegistry()
    registry.register(WebFetchTool(resolver=dns, transport=httpx.MockTransport(handle)))
    runtime, _ = _runtime(registry, decide)
    policy = RunPolicy(max_tool_calls_per_turn=2)
    with pytest.raises(RequiredToolError):
        await runtime.gather(run_id="shared", agent="researcher", task_id="T1", prompt="first",
                             policy=policy, required_capabilities=["web_fetch"])
    mode[0] = "recover"
    stage = await runtime.gather(
        run_id="shared", agent="researcher", task_id="T2", prompt="parallel branch", policy=policy,
        prior_calls=(), required_capabilities=["web_fetch"],
    )
    assert len(sent) == 3 and len(stage.records) == 1 and stage.records[0].success


@pytest.mark.asyncio
async def test_two_timeouts_suppress_same_origin_but_allow_alternative_without_spending_tool_slot():
    sent = []
    def handle(request):
        sent.append(request)
        if request.headers["host"] == "example.org":
            raise httpx.ConnectTimeout("unreachable", request=request)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/plain"})
    registry = ToolRegistry()
    registry.register(WebFetchTool(resolver=dns, transport=httpx.MockTransport(handle)))
    replies = iter([
        _call("web_fetch", {"url": "https://example.org/", "focus": "opening hours"}),
        _call("web_fetch", {"url": "https://example.org/guide", "focus": "tickets"}),
        _call("web_fetch", {"url": "https://example.org/another", "focus": "changed"}),
        _call("web_fetch", {"url": "https://alternative.org/source"}),
        {"content": "done"},
    ])
    runtime, events = _runtime(registry, lambda _: _response(next(replies)))
    result = await runtime.gather(
        run_id="circuit", agent="researcher", prompt="verify facts", policy=RunPolicy(),
        required_capabilities=["web_fetch"],
    )
    assert [item.success for item in result.records] == [False, False, True]
    assert len(sent) == 3
    assert runtime.budget.remaining("circuit", already_used=3, limit=20) == 17
    suppressed = [event for event in events if event.data.get("suppressed")]
    assert len(suppressed) == 1 and suppressed[0].data["budget_counted"] is False
    assert suppressed[0].data["error"].startswith("TOOL_CIRCUIT_OPEN:")


@pytest.mark.asyncio
async def test_legacy_failures_restore_circuit_across_runtime_restart_and_replan():
    sent = []
    registry = ToolRegistry()
    registry.register(WebFetchTool(resolver=dns, transport=httpx.MockTransport(
        lambda req: sent.append(req) or pytest.fail("circuit must prevent network")
    )))
    runtime, events = _runtime(registry, lambda _: _response(
        _call("web_fetch", {"url": "https://example.org/new-path", "focus": "replanned"})
    ))
    with pytest.raises(RequiredToolError) as failure:
        await runtime.gather(
            run_id="recovered", agent="researcher", task_id="T2", plan_version=2,
            prompt="replan", policy=RunPolicy(), prior_calls=[record(1), record(2)],
            required_capabilities=["web_fetch"],
        )
    assert not failure.value.stage.records and not sent
    assert all(event.data.get("budget_counted") is False for event in events)
    assert runtime.budget.remaining("recovered", already_used=2, limit=20) == 18
    runtime.clear_run("recovered")
    assert not runtime.failures.blocking_record(
        "recovered", registry.get("web_fetch"), {"url": "https://example.org/"}
    )


@pytest.mark.asyncio
async def test_circuit_recovery_allows_search_before_returning_to_required_fetch():
    sent = []
    registry = ToolRegistry()
    registry.register(FakeWebSearchTool())
    registry.register(WebFetchTool(resolver=dns, transport=httpx.MockTransport(
        lambda req: sent.append(req) or httpx.Response(
            200, text=PAGE, headers={"content-type": "text/plain"}
        )
    )))
    replies = iter([
        _call("web_fetch", {"url": "https://example.org/blocked"}),
        _call("web_search", {}),
        _call("web_fetch", {"url": "https://alternative.org/source"}),
        {"content": "done"},
    ])
    requests = []
    def decide(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            assert payload["tool_choice"]["function"]["name"] == "web_fetch"
        else:
            assert payload["tool_choice"] == "auto"
        return _response(next(replies))
    runtime, _ = _runtime(registry, decide)
    stage = await runtime.gather(
        run_id="recover-source", agent="researcher", prompt="verify", policy=RunPolicy(),
        prior_calls=[record(1), record(2)], required_capabilities=["web_fetch"],
    )
    assert [item.tool_name for item in stage.records] == ["web_search", "web_fetch"]
    assert len(sent) == 1 and sent[0].headers["host"] == "alternative.org"
