"""Research recovery keeps real findings and never manufactures missing support."""

import json
from copy import deepcopy

import pytest
from atlasflow.agents.contracts import ResearchResult, ResearchTask
from atlasflow.agents.gateway import OpenRouterModelGateway
from atlasflow.agents.output_validation import (
    StructuredOutputError,
    normalize_research_output,
)
from atlasflow.agents.task_quality import validate_task_result

FACTS = [
    "合成试验共记录12个样本。",
    "第一组的均值为3.5。",
    "第二组的均值为4.0。",
    "两组的差值为0.5。",
    "尚未收集独立复测结果，不能据此断言普遍有效。",
]


def payload():
    return {
        "summary": "合成试验记录了两组结果，尚待独立复测。",
        "findings": list(FACTS),
        "limitations": ["缺少独立复测，结论只适用于合成样本。"],
        "confidence": 0.43,
        "assessments": [
            {
                "criterion_index": 1,
                "status": "partial",
                "finding_indices": [1, 2, 3, 4, 5],
                "source_ids": ["synthetic-source-1"],
                "reason": "已有合成样本的计算结果，但独立复测尚未完成。",
            }
        ],
    }


def current_task():
    return ResearchTask(
        task_id="synthetic-task",
        title="核对合成试验",
        objective="核对合成结果并如实说明复测缺口",
        success_criteria=["给出合成样本的计算结果及尚未完成的核验"],
        priority=1,
        plan_version=7,
    )


class Client:
    """Return local JSON fixtures without any provider or network access."""

    def __init__(self, outputs):
        self.outputs = outputs
        self.requests = []

    async def chat(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        output = self.outputs[min(len(self.requests) - 1, len(self.outputs) - 1)]
        return {"content": json.dumps(output), "_atlasflow_finish_reason": "stop"}


def test_exact_mixed_shape_keeps_five_facts_and_remaps_only_existing_support():
    raw = payload()
    raw["findings"].insert(0, ":")
    original = deepcopy(raw)

    normalized, removed = normalize_research_output(raw)

    expected = deepcopy(original)
    expected["findings"] = list(FACTS)
    # Original references 1..5 never supported the sixth (last real) finding.
    expected["assessments"][0]["finding_indices"] = [1, 2, 3, 4]
    assert normalized == expected and removed == [1]
    assert raw == original


@pytest.mark.parametrize(
    "position,expected_refs", [(0, [2, 1]), (1, [2, 1]), (2, [1, 2])]
)
def test_noise_at_start_middle_or_end_preserves_reference_order(position, expected_refs):
    raw = payload()
    raw["findings"] = FACTS[:2]
    raw["findings"].insert(position, "，")
    raw["assessments"][0]["finding_indices"] = [3, 1, 2]

    normalized, removed = normalize_research_output(raw)

    assert normalized["findings"] == FACTS[:2]
    assert normalized["assessments"][0]["finding_indices"] == expected_refs
    assert removed == [position + 1]


def test_multiple_empty_and_punctuation_entries_return_original_one_based_indices():
    raw = payload()
    raw["findings"] = ["", FACTS[0], "\n\t", FACTS[1], "...", "，。", "—"]
    raw["assessments"][0]["finding_indices"] = [7, 4, 1, 2, 3, 5, 6]

    normalized, removed = normalize_research_output(raw)

    assert normalized["findings"] == FACTS[:2]
    assert normalized["assessments"][0]["finding_indices"] == [2, 1]
    assert removed == [1, 3, 5, 6, 7]


def test_valid_numeric_and_punctuated_facts_are_exact_deep_copies():
    raw = payload()
    raw["findings"] = ["  合成计数：12。 \n", "0", "3.5%", "1+1=2", "α=0.05", "A/B"]
    raw["assessments"][0]["finding_indices"] = [1, 2, 3, 4, 5, 6]
    original = deepcopy(raw)

    normalized, removed = normalize_research_output(raw)

    assert normalized == original and removed == []
    assert normalized is not raw
    assert normalized["findings"] is not raw["findings"]
    assert normalized["assessments"][0] is not raw["assessments"][0]
    normalized["findings"][0] = "仅修改返回副本"
    normalized["assessments"][0]["source_ids"].append("another-synthetic-source")
    normalized["limitations"].append("仅修改返回副本的局限")
    assert raw == original


def test_normalization_is_idempotent_and_does_not_share_cleaned_nested_lists():
    raw = payload()
    raw["findings"].insert(0, ":")
    original = deepcopy(raw)
    normalized, removed = normalize_research_output(raw)

    again, removed_again = normalize_research_output(normalized)

    assert removed == [1] and removed_again == []
    assert again == normalized and again is not normalized
    assert again["assessments"][0]["finding_indices"] is not normalized["assessments"][0][
        "finding_indices"
    ]
    assert raw == original


@pytest.mark.parametrize("findings", [[":"], [",", "...", "，。"], ["", "\n\t"]])
def test_all_noise_fails_even_when_summary_contains_real_text(findings):
    raw = payload()
    raw["findings"] = findings
    raw["assessments"][0]["finding_indices"] = [1]
    original = deepcopy(raw)

    with pytest.raises(ValueError, match="findings") as error:
        normalize_research_output(raw)

    assert "substantive" in str(error.value)
    assert raw == original  # The summary must never be promoted into a finding.


@pytest.mark.parametrize(
    "references", [[True], ["2"], [0], [-1], [3], [2.0], [2, 2], [1, 1]]
)
def test_bad_original_references_are_rejected_before_noise_is_removed(references):
    raw = payload()
    raw["findings"] = [":", FACTS[0]]
    raw["assessments"][0].update(status="unmet", finding_indices=references)
    original = deepcopy(raw)

    with pytest.raises(ValueError, match="finding_indices"):
        normalize_research_output(raw)

    assert raw == original


@pytest.mark.parametrize("status", ["met", "partial"])
def test_claim_losing_its_only_support_gets_actionable_field_error(status):
    raw = payload()
    raw["findings"] = [":", FACTS[0]]
    raw["assessments"][0].update(status=status, finding_indices=[1])
    original = deepcopy(raw)

    with pytest.raises(ValueError, match="finding_indices"):
        normalize_research_output(raw)

    assert raw == original


def test_unmet_criterion_can_lose_a_placeholder_reference_without_changing_its_status():
    raw = payload()
    raw["findings"] = [":", FACTS[0]]
    raw["assessments"][0].update(status="unmet", finding_indices=[1])

    normalized, removed = normalize_research_output(raw)

    expected = deepcopy(raw)
    expected["findings"] = [FACTS[0]]
    expected["assessments"][0]["finding_indices"] = []
    assert normalized == expected and removed == [1]


@pytest.mark.parametrize("malformation", ["findings_type", "finding_type", "assessment", "refs"])
def test_malformed_types_are_rejected_or_remain_invalid_for_downstream_validation(malformation):
    raw = payload()
    if malformation == "findings_type":
        raw["findings"] = None
    elif malformation == "finding_type":
        raw["findings"] = [":", 12]
    elif malformation == "assessment":
        raw["findings"].insert(0, ":")
        raw["assessments"] = [None]
    else:
        raw["findings"].insert(0, ":")
        raw["assessments"][0]["finding_indices"] = None
    original = deepcopy(raw)

    try:
        normalized, _ = normalize_research_output(raw)
    except (TypeError, ValueError):
        pass
    else:
        task = current_task()
        with pytest.raises((TypeError, ValueError)):
            result = ResearchResult.model_validate(
                {**normalized, "task_id": task.task_id, "plan_version": task.plan_version}
            )
            validate_task_result(task, result, require_assessments=True)
    assert raw == original


def test_historical_punctuation_record_still_loads_but_is_not_valid_new_execution():
    raw = payload()
    raw["findings"] = [":"]
    raw["assessments"][0]["finding_indices"] = [1]
    task = current_task()

    historical = ResearchResult.model_validate(
        {**raw, "task_id": task.task_id, "plan_version": task.plan_version}
    )

    assert historical.findings == [":"]
    with pytest.raises(ValueError, match="substantive"):
        validate_task_result(task, historical, require_assessments=True)


@pytest.mark.asyncio
async def test_gateway_salvages_mixed_first_output_without_retry_or_confidence_inflation(monkeypatch):
    raw = payload()
    raw["findings"].insert(0, ":")
    original = deepcopy(raw)
    client = Client([raw])
    gateway = OpenRouterModelGateway(client, "test/model")
    audit = []
    monkeypatch.setattr(
        gateway, "_record_research_normalization", lambda **kwargs: audit.append(deepcopy(kwargs))
    )
    task = current_task()
    original_task = task.model_dump()

    result = await gateway.analyze_task(
        "核对离线合成试验", task, tool_context="合成证据ID：synthetic-source-1"
    )

    assert len(client.requests) == 1 and raw == original
    assert result.findings == FACTS
    assert result.assessments[0].finding_indices == [1, 2, 3, 4]
    assert result.assessments[0].status == "partial"
    assert result.assessments[0].reason == raw["assessments"][0]["reason"]
    assert result.assessments[0].source_ids == ["synthetic-source-1"]
    assert result.summary == raw["summary"] and result.limitations == raw["limitations"]
    assert result.confidence == 0.43 and result.outcome != "complete"
    assert result.task_id == task.task_id and result.plan_version == 7
    assert task.model_dump() == original_task
    assert audit == [{
        "task_id": task.task_id,
        "removed_finding_indices": [1],
        "retained_finding_count": 5,
    }]


@pytest.mark.asyncio
async def test_cleanup_of_otherwise_invalid_candidate_does_not_emit_success_audit(monkeypatch):
    invalid = payload()
    invalid["findings"].insert(0, ":")
    invalid["assessments"][0]["criterion_index"] = 2
    corrected = payload()
    original = deepcopy([invalid, corrected])
    client = Client([invalid, corrected])
    gateway = OpenRouterModelGateway(client, "test/model")
    audit = []
    monkeypatch.setattr(
        gateway, "_record_research_normalization", lambda **kwargs: audit.append(deepcopy(kwargs))
    )

    result = await gateway.analyze_task("核对离线合成试验", current_task())

    assert len(client.requests) == 2 and result.findings == FACTS
    assert audit == []  # Cleanup alone cannot mark an invalid assessment successful.
    assert [invalid, corrected] == original


@pytest.mark.asyncio
async def test_gateway_repairs_all_placeholder_with_concrete_feedback_in_two_calls():
    invalid = payload()
    invalid["findings"] = [","]
    invalid["assessments"][0]["finding_indices"] = [1]
    corrected = payload()
    original = deepcopy([invalid, corrected])
    client = Client([invalid, corrected])
    task = current_task()
    original_task = task.model_dump()

    result = await OpenRouterModelGateway(client, "test/model").analyze_task(
        "核对离线合成试验", task, tool_context="合成证据ID：synthetic-source-1"
    )

    assert len(client.requests) == 2 and [invalid, corrected] == original
    assert result.findings == FACTS and result.confidence == corrected["confidence"]
    assert result.assessments[0].model_dump() == corrected["assessments"][0]
    assert result.summary == corrected["summary"]
    assert result.limitations == corrected["limitations"]
    feedback = client.requests[1]["messages"][-1]["content"]
    assert "findings" in feedback and "substantive" in feedback
    assert "finding_indices" in feedback
    assert client.requests[0]["messages"] == client.requests[1]["messages"][:2]
    assert all(request["reasoning_effort"] == "none" for request in client.requests)
    assert client.requests[0]["max_tokens"] == client.requests[1]["max_tokens"]
    assert task.model_dump() == original_task
    assert result.task_id == task.task_id and result.plan_version == 7


@pytest.mark.asyncio
async def test_two_invalid_candidates_fail_bounded_without_fabricating_research():
    first = payload()
    first["findings"] = [":"]
    first["assessments"][0]["finding_indices"] = [1]
    second = payload()
    second["findings"] = [","]
    second["assessments"][0]["finding_indices"] = [1, 2, 3, 4, 5]
    original = deepcopy([first, second])
    client = Client([first, second])
    task = current_task()
    original_task = task.model_dump()

    with pytest.raises(StructuredOutputError, match="findings") as error:
        await OpenRouterModelGateway(client, "test/model").analyze_task(
            "核对离线合成试验", task, tool_context="合成证据ID：synthetic-source-1"
        )

    assert len(client.requests) == 2
    assert error.value.operation == "research task synthetic-task"
    assert error.value.outcomes == ("CONTRACT_INVALID", "CONTRACT_INVALID")
    assert not error.value.review_fallback_safe
    assert [first, second] == original and task.model_dump() == original_task
    assert client.requests[0]["messages"] == client.requests[1]["messages"][:2]
