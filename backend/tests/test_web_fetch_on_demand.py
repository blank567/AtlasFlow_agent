"""Search-first planning and optional page reading, entirely offline."""

import json

import httpx
import pytest
from atlasflow.agents.contracts import (
    CritiqueRoute,
    ResearchPlan,
    ResearchResult,
    ResearchTask,
    RunPolicy,
)
from atlasflow.agents.gateway import OpenRouterModelGateway
from atlasflow.agents.report_skill import build_citation_catalog, build_report_context
from atlasflow.agents.tool_runtime import RequiredToolError
from atlasflow.agents.web_evidence import (
    WEB_EVIDENCE_RULES,
    WEB_SOURCE_EXECUTION_RULES,
    WEB_SOURCE_PLANNING_RULES,
    evidence_access_label,
)
from atlasflow.schemas import Evidence, RunEventType, RunStatus
from atlasflow.tools.base import BaseTool, ToolResult
from atlasflow.tools.registry import ToolRegistry
from atlasflow.tools.web_fetch import WebFetchTool
from atlasflow.tools.web_search import OpenRouterWebSearchTool, WebSearchArguments

from backend.tests.fakes import GatewayScenario, make_test_container
from backend.tests.test_capability_planning import raw_task
from backend.tests.test_critic_reliability import CATALOG, review_inputs
from backend.tests.test_quality_output_recovery import Client, DRAFT, RESULTS, verdict
from backend.tests.test_tool_runtime import _call, _response, _runtime


PAGE_URL = "https://example.test/huangshan"


def source(
    provider="openrouter",
    kind="search_response_citation",
    content="搜索摘要中的黄山景点介绍",
    key="search",
):
    return Evidence(
        id=key,
        source_id=key,
        title="黄山资料",
        uri=PAGE_URL,
        content=content,
        metadata={"provider": provider, "content_kind": kind},
    )


class Search(BaseTool):
    name = "web_search"
    description = OpenRouterWebSearchTool.description
    arguments_model = WebSearchArguments
    capabilities = OpenRouterWebSearchTool.capabilities

    async def run(self, arguments, context):
        return ToolResult(
            success=True, evidence=[source()], data={"summary": "搜索证据足以介绍黄山主要景观"}
        )


def tool_registry(status=403, *, include_reader=True):
    registry = ToolRegistry(max_retries=0)
    registry.register(Search())
    fetched = []

    async def dns(host, port):
        return ["93.184.215.14"]

    def http(request):
        fetched.append(request)
        return httpx.Response(
            status,
            text="<main>直接读取的黄山介绍原文，包含足以核对的具体景点特色及相关历史材料。</main>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    if include_reader:
        registry.register(WebFetchTool(resolver=dns, transport=httpx.MockTransport(http)))
    return registry, fetched


@pytest.mark.asyncio
async def test_ordinary_plan_gets_search_first_instructions_without_a_fetch_requirement():
    tasks = [raw_task("T1", ["web_search"]), raw_task("T2", ["web_search"])]
    tasks[0]["title"], tasks[1]["title"] = "黄山景点介绍", "黄山历史介绍"
    client = Client([{"rationale": "仅检索所需介绍资料，必要时再读网页", "tasks": tasks}])
    registry, _ = tool_registry()
    plan = await OpenRouterModelGateway(client, "test").create_plan(
        "介绍黄山景点和历史", capability_catalog=registry.capability_catalog()
    )
    prompt = client.requests[0]["messages"][0]["content"]
    assert WEB_SOURCE_PLANNING_RULES in prompt and WEB_EVIDENCE_RULES in prompt
    assert "营业/开放/预约核验必须使用官方网页能力" not in prompt
    assert all(item.required_capabilities == ["web_search"] for item in plan.tasks)
    assert client.requests[0]["messages"][1]["content"].count("介绍黄山景点和历史") == 1


@pytest.mark.asyncio
async def test_explicit_page_reading_is_not_silently_removed_from_a_plan():
    tasks = [raw_task("T1", ["web_fetch"]), raw_task("T2")]
    tasks[0].update(
        objective="读取指定网页并核对原句", success_criteria=["直接读取网页中的指定原句"]
    )
    client = Client([{"rationale": "用户明确要求核对原文", "tasks": tasks}])
    registry, _ = tool_registry()
    plan = await OpenRouterModelGateway(client, "test").create_plan(
        f"读取{PAGE_URL}并逐字核对原句", capability_catalog=registry.capability_catalog()
    )
    assert plan.tasks[0].required_capabilities == ["web_fetch"]


@pytest.mark.asyncio
async def test_critic_and_quality_use_the_same_optional_reading_and_evidence_policy():
    plan, results, context = review_inputs()
    review = Client(
        [
            {
                "decision": "accept",
                "rationale": "搜索材料足够",
                "issues": [],
                "supplemental_tasks": [],
                "replan_reason": None,
            }
        ]
    )
    output = await OpenRouterModelGateway(review, "test").review_research(
        "一般资料介绍", plan, results, context=context, capability_catalog=CATALOG
    )
    assert output.decision is CritiqueRoute.ACCEPT
    assert WEB_SOURCE_PLANNING_RULES in review.requests[0]["messages"][0]["content"]
    assert WEB_EVIDENCE_RULES in review.requests[0]["messages"][0]["content"]
    quality = Client([verdict("accept")])
    await OpenRouterModelGateway(quality, "test").evaluate_report("一般资料介绍", DRAFT, RESULTS)
    assert WEB_EVIDENCE_RULES in quality.requests[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_researcher_is_told_not_to_claim_it_read_search_citations_as_original_text():
    client = Client(
        [
            {
                "summary": "依据搜索资料介绍黄山",
                "findings": ["搜索资料给出的景点特点"],
                "limitations": [],
                "confidence": 0.85,
                "assessments": [
                    {
                        "criterion_index": 1,
                        "status": "met",
                        "finding_indices": [1],
                        "source_ids": ["search"],
                        "reason": "有对应的搜索资料片段",
                    }
                ],
            }
        ]
    )
    current = ResearchTask(**raw_task(capabilities=["web_search"]), plan_version=1)
    await OpenRouterModelGateway(client, "test").analyze_task(
        "介绍黄山", current, tool_context=build_report_context([source()], [])
    )
    assert WEB_EVIDENCE_RULES in client.requests[0]["messages"][0]["content"]
    assert "搜索摘要/回答中的引用片段" in client.requests[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_writer_receives_explicit_search_provenance_and_same_evidence_rules():
    class TextClient:
        async def chat(self, **kwargs):
            self.request = kwargs
            return {"content": "## 摘要\n\n依据搜索材料整理景点介绍[1]。"}

    client = TextClient()
    plan = ResearchPlan(
        plan_version=1,
        rationale="资料整理",
        tasks=[
            ResearchTask(**raw_task(key, ["web_search"]), plan_version=1) for key in ("T1", "T2")
        ],
    )
    results = [
        ResearchResult(
            task_id=key,
            plan_version=1,
            summary="已有搜索材料",
            findings=["相关介绍"],
            confidence=0.85,
        )
        for key in ("T1", "T2")
    ]
    await OpenRouterModelGateway(client, "test").synthesize_report(
        "介绍黄山", plan, results, tool_context=build_report_context([source()], [])
    )
    assert WEB_EVIDENCE_RULES in client.request["messages"][0]["content"]
    assert "搜索摘要/回答中的引用片段" in client.request["messages"][1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("include_reader", [True, False])
async def test_search_is_sufficient_and_never_forces_a_page_request(include_reader):
    registry, fetched = tool_registry(include_reader=include_reader)
    requests = []

    def choose(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert WEB_SOURCE_EXECUTION_RULES in payload["messages"][0]["content"]
        if len(requests) == 1:
            assert payload["tool_choice"]["function"]["name"] == "web_search"
            return _response(_call("web_search", {"query": "黄山景点与历史"}))
        assert payload["tool_choice"] == "auto"
        assert "搜索摘要/回答中的引用片段" in payload["messages"][-1]["content"]
        return _response({"content": ""})

    runtime, _ = _runtime(registry, choose)
    stage = await runtime.gather(
        run_id="search-only",
        agent="researcher",
        prompt="介绍黄山",
        policy=RunPolicy(),
        required_capabilities=["web_search"],
    )
    assert [item.tool_name for item in stage.records] == ["web_search"]
    assert stage.evidence and "搜索摘要/回答中的引用片段" in stage.context
    assert not fetched


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403])
async def test_researcher_can_read_on_demand_but_optional_failure_keeps_search_evidence(status):
    registry, fetched = tool_registry(status)
    responses = iter(
        [
            _call("web_search", {"query": "黄山具体景点"}),
            _call("web_fetch", {"url": PAGE_URL, "focus": "核对搜索片段未说明的具体特色"}),
            {"content": ""},
        ]
    )
    requests = []

    def choose(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) > 1:
            assert payload["tool_choice"] == "auto"  # Reading is NOT forced.
        return _response(next(responses))

    runtime, events = _runtime(registry, choose)
    stage = await runtime.gather(
        run_id="optional-read",
        agent="researcher",
        prompt="核对具体特色",
        policy=RunPolicy(),
        required_capabilities=["web_search"],
    )
    assert [item.tool_name for item in stage.records] == ["web_search", "web_fetch"]
    assert stage.records[1].success is (status == 200) and len(fetched) == 1
    assert any(item.metadata["provider"] == "openrouter" for item in stage.evidence)
    if status == 403:
        assert stage.records[1].error.startswith("WEB_FETCH_HTTP_403")
        assert any(e.event_type is RunEventType.TOOL_FAILED for e in events)
        assert "搜索摘要/回答中的引用片段" in stage.context
    else:
        assert "直接读取网页的原文片段" in stage.context


@pytest.mark.asyncio
async def test_mandatory_reading_still_fails_when_the_requested_page_is_unavailable():
    registry, fetched = tool_registry(403)
    responses = iter([_call("web_fetch", {"url": PAGE_URL}), _call("web_fetch", {"url": PAGE_URL})])
    runtime, _ = _runtime(registry, lambda request: _response(next(responses)))
    with pytest.raises(RequiredToolError, match="web_fetch") as caught:
        await runtime.gather(
            run_id="required-read",
            agent="researcher",
            prompt="用户明确要求读取指定原文",
            policy=RunPolicy(),
            required_capabilities=["web_fetch"],
        )
    assert len(fetched) == 1 and caught.value.stage.records[0].success is False


@pytest.mark.asyncio
async def test_explicit_search_and_read_contract_is_not_downgraded_by_runtime():
    registry, fetched = tool_registry(403)
    responses = iter(
        [
            _call("web_search", {"query": "指定内容"}),
            _call("web_fetch", {"url": PAGE_URL}),
            _call("web_fetch", {"url": PAGE_URL}),
        ]
    )
    runtime, _ = _runtime(registry, lambda request: _response(next(responses)))
    with pytest.raises(RequiredToolError, match="web_fetch") as caught:
        await runtime.gather(
            run_id="explicit-both",
            agent="researcher",
            prompt="搜索并直接核验指定网页",
            policy=RunPolicy(),
            required_capabilities=["web_search", "web_fetch"],
        )
    assert caught.value.stage.records[0].success and len(fetched) == 1


@pytest.mark.parametrize(
    "provider,kind,expected",
    [
        ("http", "source_excerpt", "直接读取网页的原文片段"),
        ("openrouter", "source_excerpt", "搜索服务返回的来源片段"),
        ("openrouter", "search_response_citation", "搜索摘要/回答中的引用片段"),
        ("unknown", "unknown", "获取方式未标注"),
    ],
)
def test_access_labels_describe_acquisition_without_claiming_full_text_reading(
    provider, kind, expected
):
    item = source(provider, kind)
    assert expected in evidence_access_label(item.metadata)
    assert expected in build_report_context([item], [])


@pytest.mark.parametrize("reverse", [False, True])
def test_on_demand_original_excerpt_upgrades_search_excerpt_without_changing_number(reverse):
    summary = source(content="搜索回答中的概述")
    original = source("http", "source_excerpt", "网页中实际读取的原句", "read")
    other = source(key="other").model_copy(update={"uri": "https://example.test/other"})
    evidence = [original, other, summary] if reverse else [summary, other, original]
    catalog = build_citation_catalog(evidence, [], source_order=[PAGE_URL, other.uri])
    assert [(item.number, item.url) for item in catalog] == [(1, PAGE_URL), (2, other.uri)]
    assert catalog[0].excerpt == "网页中实际读取的原句"
    assert "直接读取网页的原文片段" in catalog[0].access
    assert "搜索回答中的概述" not in build_report_context(evidence, [])


@pytest.mark.asyncio
async def test_optional_read_failure_does_not_fail_or_replan_a_search_backed_run():
    container = make_test_container(
        scenario=GatewayScenario(initial_task_count=2, task_capabilities={"p1-t1": ["web_search"]})
    )
    registry, fetched = tool_registry(403)

    def choose(request):
        payload = json.loads(request.content)
        if isinstance(payload["tool_choice"], dict):
            return _response(_call("web_search", {"query": "黄山资料"}))
        phase = payload["messages"][0]["content"]
        previous_reads = [
            message
            for message in payload["messages"]
            if message.get("role") == "assistant"
            and any(
                call.get("function", {}).get("name") == "web_fetch"
                for call in message.get("tool_calls", [])
            )
        ]
        if (
            "你是 researcher " in phase
            and "web_search" in phase
            and not previous_reads
            and any(message.get("role") == "tool" for message in payload["messages"])
        ):
            return _response(_call("web_fetch", {"url": PAGE_URL, "focus": "按需补充细节"}))
        return _response({"content": ""})

    runtime, _ = _runtime(registry, choose)
    container.run_service.workflow.tool_runtime = runtime
    run = await container.run_service.execute_and_wait("介绍黄山景点和历史")
    assert run.status is RunStatus.COMPLETED and run.report and run.research_results
    assert run.metrics.replan_count == 0 and run.metrics.failed_tasks == 0
    assert any(item.tool_name == "web_fetch" and not item.success for item in run.tool_calls)
    assert len(fetched) == 1
