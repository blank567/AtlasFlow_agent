"""Deterministic Agent contracts, grounded in structured facts and ordered events.

No natural-language query/report regex is used to infer semantic task success.
Missing event observations remain unknown, rather than becoming a free pass.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def ordered_events(artifact: dict) -> tuple[list[dict], bool]:
    run = artifact.get("content", artifact)
    events = artifact.get("events", run.get("events", []))
    if not isinstance(events, list) or not events:
        return [], False
    if any(not isinstance(event, dict) for event in events):
        return [], False
    if any(event.get("run_id") is not None and event["run_id"] != run.get("id") for event in events):
        return [], False
    sequences = [event.get("sequence") for event in events]
    if any(type(value) is not int or value < 1 for value in sequences):
        return [], False
    if len(set(sequences)) != len(sequences):
        return [], False
    ordered = sorted(events, key=lambda event: event["sequence"])
    complete = (
        ordered[0].get("event_type") == "run_started" and ordered[0]["sequence"] == 1
        and ordered[-1].get("event_type") in {
            "run_completed", "run_degraded", "run_failed", "run_cancelled"
        }
        and sequences and len(ordered) == ordered[-1]["sequence"] - ordered[0]["sequence"] + 1
    )
    # Explicit complete is accepted only in addition to coherent persisted ordering.
    return ordered, bool(complete)


def event_ref(event: dict) -> str:
    return f"event:{event.get('event_id') or event['sequence']}"


def check_result(assertion: dict, value: bool | None, reason: str, events=()) -> dict:
    return {
        "assertion_id": assertion["assertion_id"], "kind": assertion["kind"],
        "dimension": "tools" if assertion["kind"] in {
            "tool_budget", "required_capabilities", "tool_allowlist",
            "nonretryable_failure_not_repeated",
        } else "trajectory",
        "required": assertion.get("required", True), "value": value,
        "status": "evaluated" if value is not None else "not_run",
        "reason": reason, "evidence_refs": [event_ref(event) for event in events],
        "grader_id": "agent-structured-contracts", "grader_version": "2.0.0",
    }


def tool_observations(artifact: dict) -> dict[str, Any]:
    """Count executed and reserved calls separately; cache/rejection is not execution."""
    run = artifact.get("content", artifact)
    events, complete = ordered_events(artifact)
    requested = {
        event.get("data", {}).get("call_id"): event for event in events
        if event.get("event_type") == "tool_requested" and event.get("data", {}).get("call_id")
    }
    terminal = {
        event.get("data", {}).get("call_id"): event for event in events
        if event.get("event_type") in {"tool_succeeded", "tool_failed"}
        and event.get("data", {}).get("call_id")
        and not event.get("data", {}).get("suppressed")
        and event.get("data", {}).get("executed") is not False
    }
    records = {item.get("call_id"): item for item in run.get("tool_calls", [])}
    executed = []
    for call_id, event in terminal.items():
        record = records.get(call_id)
        if call_id not in requested or record is None:
            continue
        if record.get("reused_from_call_id") or event.get("data", {}).get("reused_from_call_id"):
            continue
        executed.append({**record, "event": event, "requested_event": requested[call_id]})
    unobserved = [call_id for call_id in requested if call_id not in terminal]
    # A complete event log and one corresponding completion per request provides
    # coverage. Old ToolCallRecord alone has no reliable execution/caching semantics.
    coverage = complete and not unobserved and all(call_id in records for call_id in terminal)
    return {
        "events": events, "complete": complete, "coverage": coverage,
        "reserved": list(requested.values()), "executed": executed,
        "unknown_call_ids": unobserved,
    }


def _initial(assertion, events, complete):
    plans = [event for event in events if event.get("event_type") == "plan_created"
             and event.get("plan_version", event.get("data", {}).get("plan", {}).get("plan_version")) == 1]
    if not plans:
        return check_result(assertion, False if complete else None,
                            "完整轨迹中缺少首次计划" if complete else "没有可定位的首次计划事件")
    event = plans[0]
    data = event.get("data", {})
    tasks = data.get("tasks", data.get("plan", {}).get("tasks"))
    if not isinstance(tasks, list):
        return check_result(assertion, None, "首次计划事件缺少任务列表", [event])
    count = assertion["parameters"]["count"]
    return check_result(assertion, len(tasks) == count,
                        f"首次计划{len(tasks)}个任务，要求{count}个", [event])


def _supplement(assertion, events, complete):
    parameters = assertion["parameters"]
    initial = _initial({**assertion, "parameters": {"count": parameters["initial_count"]}}, events, complete)
    if initial["value"] is not True:
        return initial
    reviews = [event for event in events if event.get("event_type") == "review_decided"]
    supplement = [event for event in reviews if event.get("data", {}).get("decision") == "supplement"]
    if len(supplement) != 1:
        return check_result(assertion, False if complete or len(supplement) > 1 else None,
                            f"观察到{len(supplement)}轮补充，要求1轮", reviews)
    supplement_event = supplement[0]
    data = supplement_event.get("data", {})
    added = data.get("supplemental_tasks")
    if not isinstance(added, list):
        return check_result(assertion, None, "补充事件未保存补充任务清单", [supplement_event])
    if len(added) != parameters["added_count"]:
        return check_result(assertion, False, "补充增量数量不符合要求", [supplement_event])
    accepts = [event for event in reviews if event.get("data", {}).get("decision") == "accept"
               and event["sequence"] > supplement_event["sequence"]]
    if not accepts:
        return check_result(assertion, False if complete else None, "尚未观察到补充后的Accept", reviews)
    accept = accepts[0]
    expected_count = parameters["initial_count"] + parameters["added_count"]
    active = accept.get("data", {}).get("active_plan", {}).get("tasks")
    if not isinstance(active, list):
        return check_result(assertion, None, "Accept事件未保存审核时的实际计划", [accept])
    if len(active) != expected_count:
        return check_result(assertion, False, "Accept时的任务总数不等于初始数量加补充增量", [accept])
    initial_ids = {
        item["task_id"] for event in events if event.get("event_type") == "plan_created"
        and event.get("plan_version") == 1
        for item in event.get("data", {}).get("tasks", event.get("data", {}).get("plan", {}).get("tasks", []))
    }
    added_ids = {item.get("task_id") for item in added}
    if None in added_ids or len(added_ids) != len(added) or added_ids & initial_ids:
        return check_result(assertion, False, "补充任务ID缺失、重复或覆盖首次任务", [supplement_event])
    completed = {
        event.get("task_id") for event in events
        if event.get("event_type") == "task_completed" and event.get("status") == "succeeded"
        and supplement_event["sequence"] < event["sequence"] < accept["sequence"]
        and event.get("plan_version") == supplement_event.get("plan_version")
    }
    if not added_ids <= completed:
        return check_result(assertion, False if complete else None,
                            "补充任务未在Accept之前完成", [supplement_event, accept])
    if any(event.get("data", {}).get("decision") == "replan" for event in reviews
           if supplement_event["sequence"] < event["sequence"] < accept["sequence"]):
        return check_result(assertion, False, "补充和Accept之间发生了整轮Replan", reviews)
    return check_result(assertion, True, f"已验证{parameters['initial_count']}→{expected_count}→Accept的事件与增量", [supplement_event, accept])


def _dependency(assertion, events, complete):
    executions = [event for event in events if event.get("event_type") == "task_scheduled"
                  or event.get("event_type") == "node_started" and event.get("node") == "researcher"]
    if not executions:
        return check_result(assertion, False if complete else None, "没有实际任务调度轨迹")
    checked = []
    has_dependencies = False
    for event in executions:
        version = event.get("plan_version")
        previous = [item for item in events if item["sequence"] < event["sequence"]
                    and item.get("plan_version") == version]
        declared = {}
        for previous_event in previous:
            data = previous_event.get("data", {})
            tasks = data.get("active_plan", {}).get("tasks")
            if previous_event.get("event_type") == "plan_created":
                tasks = data.get("tasks", data.get("plan", {}).get("tasks"))
            if isinstance(tasks, list):
                declared.update({task.get("task_id"): task.get("dependencies") for task in tasks})
        dependency_declaration = declared.get(event.get("task_id"))
        dependencies = event.get("data", {}).get("dependencies") if event.get("event_type") == "task_scheduled" else dependency_declaration
        if not isinstance(dependencies, list):
            return check_result(assertion, None, "调度事件缺少dependencies，无法核验先后关系", [event])
        if dependency_declaration is not None and set(dependencies) != set(dependency_declaration):
            return check_result(assertion, False, "实际调度依赖与对应计划声明不一致", [event])
        has_dependencies |= bool(dependencies)
        for dependency in dependencies:
            preceding = [item for item in events if item.get("event_type") == "task_completed"
                         and item.get("status") == "succeeded" and item.get("task_id") == dependency
                         and item.get("plan_version") == event.get("plan_version")
                         and item["sequence"] < event["sequence"]]
            if not preceding:
                return check_result(assertion, False if complete else None,
                                    f"依赖{dependency}未在任务{event.get('task_id')}调度前成功完成", [event])
            checked.extend([preceding[-1], event])
    if assertion.get("parameters", {}).get("require_dependencies") and not has_dependencies:
        return check_result(assertion, False if complete else None, "场景要求依赖，但实际调度没有依赖", executions)
    return check_result(assertion, True if complete else None,
                        "全部已声明依赖先完成再调度" if complete else "局部依赖有效，但轨迹不完整", checked or executions)


def _approval(assertion, events, complete):
    research = [event for event in events if event.get("event_type") == "task_scheduled"
                or event.get("event_type") == "node_started" and event.get("node") == "researcher"]
    if not research:
        return check_result(assertion, False if complete else None, "尚未观察到研究执行，无法验证审批门控")
    refs = []
    for event in research:
        version = event.get("plan_version")
        required = [item for item in events if item.get("event_type") == "approval_required"
                    and item.get("plan_version") == version and item["sequence"] < event["sequence"]]
        resolved = [item for item in events if item.get("event_type") == "approval_resolved"
                    and item.get("plan_version") == version and item["sequence"] < event["sequence"]
                    and item.get("data", {}).get("action") in {"approve", "edit"}]
        if not required or not resolved or required[-1]["sequence"] >= resolved[-1]["sequence"]:
            return check_result(assertion, False if complete else None,
                                "研究调度之前未观察到对应计划的等待审批及批准事件", [event])
        refs.extend([required[-1], resolved[-1], event])
    return check_result(assertion, True if complete else None,
                        "实际审批事件早于研究执行" if complete else "审批片段正确，但轨迹不完整", refs)


def _replan(assertion, events, complete):
    reviews = [event for event in events if event.get("event_type") == "review_decided"]
    replans = [event for event in reviews if event.get("data", {}).get("decision") == "replan"]
    allowed = {"invalid_decomposition", "unresolved_critical_gap", "irreconcilable_conflict", "dependency_dead_end"}
    for event in replans:
        data = event.get("data", {})
        reason = data.get("replan_reason")
        issues = data.get("issues")
        if not isinstance(issues, list):
            return check_result(assertion, None, "Replan事件缺少结构化问题清单", [event])
        if reason not in allowed or not any(item.get("severity") == "critical" and item.get("code") == reason for item in issues):
            return check_result(assertion, False, "Replan缺少与原因匹配的critical问题", [event])
    return check_result(assertion, True if complete else None,
                        "未强迫Replan；已发生的Replan均有匹配关键问题" if complete else "轨迹不完整，尚不能排除后续无依据Replan", replans or reviews)


def _recovery(assertion, artifact, events):
    evidence = artifact.get("recovery_evidence", {})
    run = artifact.get("content", artifact)
    if evidence.get("provenance") != "isolated_postgres_process_restart":
        return check_result(assertion, None, "缺少真实进程重启及持久化调用账本证据；recovery_count不能代替验证")
    before, after = evidence.get("before", {}), evidence.get("after", {})
    required = (before.get("process_id"), after.get("process_id"), evidence.get("checkpoint_id"))
    if not all(required) or not isinstance(before.get("completed_invocation_ids"), list) or not isinstance(after.get("replayed_invocation_ids"), list) or not isinstance(after.get("redispatched_invocation_ids"), list):
        return check_result(assertion, None, "重启证明不完整，缺进程、检查点或调用ID清单")
    if before["process_id"] == after["process_id"] or evidence.get("run_id") != run.get("id") or after.get("run_id") != run.get("id"):
        return check_result(assertion, False, "未证明跨进程恢复同一个Run")
    interrupted = [event for event in events if event.get("event_type") == "run_interrupted"]
    resumed = [event for event in events if event.get("event_type") == "run_resumed"]
    if not interrupted or not resumed or interrupted[0]["sequence"] >= resumed[-1]["sequence"]:
        return check_result(assertion, None, "缺少有序的中断与恢复事件")
    completed = set(before["completed_invocation_ids"])
    if not completed:
        return check_result(assertion, None, "中断前没有已完成调用，无法验证避免重复执行")
    valid = completed <= set(after["replayed_invocation_ids"]) and not completed & set(after["redispatched_invocation_ids"])
    return check_result(assertion, valid, "根据跨进程调用ID验证复用与未重复分发", interrupted + resumed)


def evaluate_assertion(assertion: dict, artifact: dict, policy: dict | None = None) -> dict:
    events, complete = ordered_events(artifact)
    kind = assertion["kind"]
    if kind == "initial_task_count":
        return _initial(assertion, events, complete)
    if kind == "supplement_sequence":
        return _supplement(assertion, events, complete)
    if kind == "dependency_order":
        return _dependency(assertion, events, complete)
    if kind == "approval_before_research":
        return _approval(assertion, events, complete)
    if kind == "replan_requires_critical_issue":
        return _replan(assertion, events, complete)
    if kind == "recovery_evidence":
        return _recovery(assertion, artifact, events)
    observed = tool_observations(artifact)
    calls = observed["executed"]
    if kind == "tool_budget":
        limit = assertion.get("parameters", {}).get("max_calls", (policy or {}).get("max_tool_calls_per_run"))
        if limit is None:
            return check_result(assertion, None, "缺少实际生效的Run工具额度")
        count = len(observed["reserved"])
        valid = count <= limit
        return check_result(assertion, valid if valid is False or observed["complete"] else None,
                            f"已记录{count}次预算预留，额度{limit}；缓存复用不计实际执行", observed["reserved"])
    if kind == "required_capabilities":
        required = set(assertion["parameters"]["capabilities"])
        actual = {capability for call in calls if call.get("success") for capability in call.get("capabilities", [])}
        valid = required <= actual
        return check_result(assertion, valid if valid or observed["coverage"] else None,
                            f"所需能力{sorted(required)}；已确认执行成功能力{sorted(actual)}", [call["event"] for call in calls])
    if kind == "tool_allowlist":
        allowed = set(assertion["parameters"]["tools"])
        forbidden = [call for call in calls if call.get("tool_name") not in allowed]
        return check_result(assertion, False if forbidden else True if observed["coverage"] else None,
                            "实际执行工具均在允许清单" if not forbidden else "执行了禁止工具", [call["event"] for call in calls])
    if kind == "nonretryable_failure_not_repeated":
        seen = set()
        duplicates = []
        for call in calls:
            if call.get("success") or call.get("retryable") is not False:
                continue
            scope = call.get("failure_scope")
            if not scope:
                # Never infer retryability from a free-text error string.
                continue
            key = (call.get("tool_name"), scope)
            if key in seen:
                duplicates.append(call)
            seen.add(key)
        missing = any(not call.get("success") and (
            call.get("retryable") is None
            or call.get("retryable") is False and not call.get("failure_scope")
        ) for call in calls)
        value = False if duplicates else True if observed["coverage"] and not missing else None
        return check_result(assertion, value,
                            "仅相同不可重试failure_scope重复执行算违规；合法retryable重试不惩罚", [call["event"] for call in calls])
    return check_result(assertion, None, f"不支持的断言：{kind}")


def arguments_fingerprint(arguments: dict) -> str:
    """Diagnostic identity only; never a semantic correctness test."""
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
