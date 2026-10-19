"""Quality decisions stay concise, actionable, bounded and never auto-accepted."""

import json
from copy import deepcopy

import httpx
import pytest
from atlasflow.agents.contracts import DraftVersion, QualityDecision, QualityRoute, ResearchResult
from atlasflow.agents.gateway import OpenRouterModelGateway, _quality_schema
from atlasflow.providers.openrouter import ProviderRequestError

from backend.tests.test_tool_runtime import _client

DRAFT = DraftVersion(version=3, plan_version=2, content="## 摘要\n\n结论[1]。\n\n## 结论\n\n结论。")
RESULTS = [ResearchResult(task_id="T1", plan_version=2, summary="已核验", findings=["已有依据"],
                          confidence=0.9)]
ISSUE = {"severity": "warning", "code": "claim_support", "task_id": "T1",
         "message": "摘要的结论[1]未说明引用支持的适用范围", "recommendation": "为摘要结论补充适用范围"}


def verdict(route="revise", *, issues=None, instructions=None, score=None):
    return {"verdict": {
        "decision": route, "score": (90 if route == "accept" else 72) if score is None else score,
        "rationale": "依据报告逐项验收", "issues": issues if issues is not None else [],
        "revision_instructions": instructions if instructions is not None else [],
    }}


class Client:
    def __init__(self, outputs):
        self.outputs = outputs
        self.requests = []

    async def chat(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        output = self.outputs[min(len(self.requests) - 1, len(self.outputs) - 1)]
        return {"content": json.dumps(output), "_atlasflow_finish_reason": "stop"}


def test_quality_schema_separates_accept_revise_replan_without_root_union():
    schema = _quality_schema()
    assert schema["type"] == "object" and schema["required"] == ["verdict"]
    assert schema["additionalProperties"] is False and "anyOf" not in schema
    branches = {branch["properties"]["decision"]["enum"][0]: branch
                for branch in schema["properties"]["verdict"]["anyOf"]}
    assert set(branches) == {"accept", "revise", "replan"}
    for name, branch in branches.items():
        fields = branch["properties"]
        assert set(branch["required"]) == set(fields)
        if name != "accept":
            assert fields["issues"]["minItems"] == 1
        if name == "revise":
            assert fields["revision_instructions"]["minItems"] == 1
    assert branches["accept"]["properties"]["score"]["minimum"] == 50
    assert "critical" not in branches["accept"]["properties"]["issues"]["items"]["properties"]["severity"]["enum"]
    assert "critical" in branches["replan"]["properties"]["issues"]["items"]["properties"]["severity"]["enum"]
    assert _quality_schema() == schema  # No shared issue-schema mutation.


@pytest.mark.asyncio
async def test_accept_keeps_public_decision_flat_and_binds_workflow_owned_versions():
    client = Client([verdict("accept")])
    reviewed = await OpenRouterModelGateway(client, "test").evaluate_report("核验", DRAFT, RESULTS)
    assert reviewed.decision is QualityRoute.ACCEPT and not reviewed.issues
    assert reviewed.plan_version == 2 and reviewed.draft_version == 3
    assert "verdict" not in reviewed.model_dump()
    assert client.requests[0]["reasoning_effort"] == "none"
    assert client.requests[0]["max_tokens"] == 1800
    assert len(client.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["revise", "replan"])
async def test_empty_issues_get_actionable_repair_instead_of_a_fabricated_review(route):
    issue = {**ISSUE, "severity": "critical" if route == "replan" else "warning"}
    good = verdict(route, issues=[issue], instructions=["限定摘要结论适用范围"])
    client = Client([verdict(route), good])
    reviewed = await OpenRouterModelGateway(client, "test").evaluate_report("核验", DRAFT, RESULTS)
    assert reviewed.decision.value == route and len(reviewed.issues) == 1
    repair = client.requests[-1]["messages"][-1]["content"]
    assert all(value in repair for value in ["校验错误", "verdict", "revision_instructions", "task_id", "不得为了满足格式虚构缺陷"])
    assert all(DRAFT.content in call["messages"][1]["content"] for call in client.requests)


@pytest.mark.asyncio
async def test_user_failure_sequence_recovers_without_extra_report_writing_or_unbounded_tokens():
    requests = []
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            message = {"content": None, "reasoning": "synthetic internal reasoning"}
            finish = "length"
        else:
            output = verdict() if len(requests) == 2 else verdict(
                issues=[ISSUE], instructions=["为摘要结论补充适用范围"]
            )
            message, finish = {"content": json.dumps(output)}, "stop"
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}]})
    client = _client(respond)
    reviewed = await OpenRouterModelGateway(client, "test").evaluate_report("核验", DRAFT, RESULTS)
    assert reviewed.decision is QualityRoute.REVISE and reviewed.issues
    assert len(requests) == 3 and all(item["max_tokens"] == 1800 for item in requests)
    assert all(item["reasoning_effort"] == "none" for item in requests)
    assert [call.finish_reason for call in client.recent_calls] == ["length", "stop", "stop"]
    assert all("verdict" in item["response_format"]["json_schema"]["schema"]["properties"] for item in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid,match", [
    (verdict(), "concrete issues"),
    (verdict(issues=[ISSUE]), "actionable revision_instructions"),
    (verdict("replan", issues=[ISSUE]), "critical issue"),
    (verdict("accept", score=49), "contract"),
    (verdict("accept", issues=[{**ISSUE, "severity": "critical"}]), "contract"),
    (verdict()["verdict"], "exactly one verdict"),
    ({**verdict(), "extra": "untrusted"}, "exactly one verdict"),
])
async def test_stubborn_invalid_output_fails_closed_with_no_synthetic_score_or_issues(invalid, match):
    original = deepcopy(invalid)
    client = Client([invalid])
    with pytest.raises(ProviderRequestError, match=match):
        await OpenRouterModelGateway(client, "test").evaluate_report("核验", DRAFT, RESULTS)
    assert len(client.requests) == 2 and invalid == original


@pytest.mark.asyncio
async def test_repair_can_legitimately_reassess_accept_without_forcing_invented_issues():
    client = Client([verdict(), verdict("accept")])
    reviewed = await OpenRouterModelGateway(client, "test").evaluate_report("核验", DRAFT, RESULTS)
    assert reviewed.decision is QualityRoute.ACCEPT and reviewed.score == 90
    assert not reviewed.issues and not reviewed.revision_instructions


def test_historical_flat_quality_records_are_still_readable():
    historical = {**verdict()["verdict"], "plan_version": 2, "draft_version": 3}
    loaded = QualityDecision.model_validate(historical)
    assert loaded.decision is QualityRoute.REVISE
    with pytest.raises(ValueError, match="concrete issues"):
        loaded.validate_for_execution()
