"""Conservative output cleanup and safe structured-error diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from atlasflow.agents.task_quality import has_substantive_text
from atlasflow.observability import redact_sensitive_data
from atlasflow.providers.openrouter import ProviderRequestError


class StructuredOutputError(ProviderRequestError):
    """A bounded output-validation failure, not a network/persistence failure.

    A review may be degraded only after its candidate issue envelopes were
    validated and contained no critical issue/replan on ANY attempt.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        outcomes: Sequence[str] = (),
        review_fallback_safe: bool = False,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.outcomes = tuple(outcomes)
        self.review_fallback_safe = review_fallback_safe


def normalize_task_capabilities(
    raw: Mapping[str, Any], catalog: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Drop a redundant OR group already guaranteed by a mandatory capability.

    A AND (A OR B) is exactly A. Removing A from the OR group would instead
    require B and change the intent. Invalid/unknown declarations are left
    untouched so normal validation still rejects them. Never alter caller data.
    """
    task = deepcopy(dict(raw))
    mandatory = task.get("required_capabilities", [])
    groups = task.get("capability_alternatives", [])
    known = {item["id"] for item in catalog}
    if (
        not isinstance(mandatory, list)
        or len(mandatory) > 16
        or not all(isinstance(item, str) for item in mandatory)
        or len(mandatory) != len(set(mandatory))
        or not isinstance(groups, list)
        or len(groups) > 8
    ):
        return task
    if any(
        not isinstance(group, list)
        or not 2 <= len(group) <= 16
        or not all(isinstance(item, str) for item in group)
        or len(group) != len(set(group))
        for group in groups
    ):
        return task
    declared = set(mandatory) | {item for group in groups for item in group}
    if not declared <= known:
        return task
    if groups:
        task["capability_alternatives"] = [
            group for group in groups if not set(group) & set(mandatory)
        ]
    return task


def normalize_research_output(raw: Mapping[str, Any]) -> tuple[dict[str, Any], list[int]]:
    """Remove symbol-only findings without inventing content or changing assessments.

    Only fresh gateway responses pass here; historical stored results stay intact.
    References use the OLD 1-based positions until all have been validated, then
    are remapped together. Never hide dangling/duplicate refs or transfer support
    from a removed placeholder to a different finding. Other contract validation
    remains the caller's responsibility.
    """
    payload = deepcopy(dict(raw))
    findings = payload.get("findings")
    if (
        not isinstance(findings, list)
        or not findings
        or any(not isinstance(item, str) for item in findings)
    ):
        return payload, []  # Let schema validation report invalid types/empty lists.
    removed = [
        index for index, finding in enumerate(findings, 1)
        if not has_substantive_text(finding)
    ]
    if not removed:
        return payload, []
    if len(removed) == len(findings):
        raise ValueError(
            "findings: all entries lack substantive text (empty/punctuation placeholders); "
            "regenerate actual findings from available evidence, never substitute the summary"
        )

    assessments = payload.get("assessments")
    if not isinstance(assessments, list) or any(
        not isinstance(item, dict) for item in assessments
    ):
        return payload, []  # Do not mask another malformed field with cleanup.
    removed_set = set(removed)
    retained_positions = [index for index in range(1, len(findings) + 1) if index not in removed_set]
    remapping = {old: new for new, old in enumerate(retained_positions, 1)}
    for position, assessment in enumerate(assessments):
        field = f"assessments[{position}].finding_indices"
        indices = assessment.get("finding_indices")
        if (
            not isinstance(indices, list)
            or any(type(index) is not int or not 1 <= index <= len(findings) for index in indices)
            or len(indices) != len(set(indices))
        ):
            raise ValueError(
                f"{field}: must reference unique existing findings using strict 1-based integers "
                "before placeholder cleanup; rebuild references from the actual findings array"
            )
        new_indices = [remapping[index] for index in indices if index in remapping]
        if assessment.get("status") in {"met", "partial"} and not new_indices:
            raise ValueError(
                f"{field}: met/partial assessment has no substantive supporting finding after "
                "placeholder cleanup; supply real support or mark the criterion unmet"
            )
        assessment["finding_indices"] = new_indices
    payload["findings"] = [findings[index - 1] for index in retained_positions]
    return payload, removed


def validation_error_detail(error: ValidationError) -> str:
    """Keep actionable validator messages, never raw inputs or exception context."""
    details = []
    for item in error.errors(include_input=False, include_url=False, include_context=False)[:8]:
        location = ".".join(map(str, item["loc"])) or "output"
        message = str(redact_sensitive_data(item.get("msg", "invalid value")))[:240]
        details.append(f"{location}: {item['type']}: {message}")
    return str(redact_sensitive_data("; ".join(details)))[:1800]
