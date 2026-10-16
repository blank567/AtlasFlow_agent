"""Explicit evidence diagnostics, not a proxy for semantic answer correctness."""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from atlasflow.knowledge.domain import SearchResult

VERSION = "rag-evidence-v2"


def metric(value=None, *, status="evaluated", unit="ratio", direction="higher",
           scoring_unit="child_evidence", numerator=None, denominator=None, reason=None):
    return {"value": value, "status": status, "unit": unit, "direction": direction,
            "scoring_unit": scoring_unit, "numerator": numerator, "denominator": denominator,
            "grader_id": "deterministic-rag", "grader_version": VERSION,
            "reason": reason, "evidence_refs": []}


def normalized(text: str) -> str:
    return " ".join(re.sub(r"[*`]+", "", unicodedata.normalize("NFKC", text)).casefold().split())


def _source(hit, artifact):
    chunk = hit.chunk
    source = artifact.get("source_id_map", {}).get(chunk.document_id) or chunk.metadata.get("source_id")
    content_hash = artifact.get("source_hash_map", {}).get(chunk.version_id) or chunk.metadata.get("content_hash")
    return source, content_hash


def _anchor_matches(gold, hit, identity, *, context=False):
    if identity != (gold.get("source_id"), gold.get("content_hash")):
        return False
    # Parent text can span another page/heading; do not apply the child's locator
    # to parent context. Both are still bound to the same verified source bytes.
    if not context:
        if gold.get("page_number") is not None and gold["page_number"] != hit.chunk.page_number:
            return False
        if gold.get("heading") and gold["heading"] not in hit.chunk.heading_path:
            return False
    text = hit.chunk.content + ("\n" + (hit.context_excerpt or "") if context else "")
    return bool(gold.get("anchor")) and normalized(gold["anchor"]) in normalized(text)


def evaluate_rag(protocol: str, case: dict, artifact: dict, plan: dict) -> dict[str, Any]:
    content = artifact.get("content", artifact)
    result = SearchResult.model_validate(content["retrieval"] if protocol == "rag-answer-v2" else content)
    gold = case.get("expected_sources", [])
    identities = [_source(hit, artifact) for hit in result.hits]
    degraded = list(result.trace.degraded)
    values = {
        "evidence_count": metric(len(result.hits), unit="count", direction="neutral"),
        "retrieval_duration_ms": metric(result.trace.duration_ms, unit="ms", direction="lower"),
    }
    checks = []
    complete_identities = all(source and digest for source, digest in identities)
    if case.get("gold_status") != "verified":
        missing = "missing_gold"
    elif not complete_identities:
        missing = "source_identity_missing"
    elif not case.get("answerable", True):
        missing = "not_applicable"
    elif not gold:
        missing = "missing_gold"
    else:
        missing = None
    captured_depth = artifact.get("captured_retrieval_limit")
    if captured_depth is None:
        captured_depth = result.trace.applied_parameters.get("result_limit")
    cutoffs = plan.get("cutoffs", [1, 3, 5])
    qrels = case.get("qrels", {})
    for k in cutoffs:
        depth_error = captured_depth is None or captured_depth < k
        status = "insufficient_capture_depth" if depth_error else missing
        prefix = result.hits[:k]
        child_sets = [{i for i, g in enumerate(gold) if _anchor_matches(g, hit, identities[rank])}
                      for rank, hit in enumerate(prefix)]
        contexts = {i for rank, hit in enumerate(prefix) for i, g in enumerate(gold)
                    if _anchor_matches(g, hit, identities[rank], context=True)}
        children = set().union(*child_sets) if child_sets else set()
        relevant_sources = {(g["source_id"], g["content_hash"]) for g in gold}
        sources = set(identities[:k]) & relevant_sources
        row_status = status or ("degraded" if degraded else "evaluated")
        diagnostic = "前K条child；仅针对已标注正例，不代表全库全部相关内容召回"
        def scored(value, numerator=None, denominator=None, unit="ratio", *,
                   _status=status, _row_status=row_status, _diagnostic=diagnostic):
            return metric(None if _status else value, status=_row_status, numerator=numerator,
                          denominator=denominator, unit=unit, reason=_status or _diagnostic)
        values[f"source_coverage_at_{k}"] = scored(
            len(sources) / len(relevant_sources) if relevant_sources else None,
            len(sources), len(relevant_sources))
        values[f"anchor_coverage_at_{k}"] = scored(
            len(children) / len(gold) if gold else None, len(children), len(gold))
        values[f"context_anchor_coverage_at_{k}"] = scored(
            len(contexts) / len(gold) if gold else None, len(contexts), len(gold))
        values[f"anchor_hit_at_{k}"] = scored(bool(children), unit="boolean")
        values[f"anchor_mrr_at_{k}"] = scored(
            next((1 / (rank + 1) for rank, labels in enumerate(child_sets) if labels), 0.0))
        if qrels and case.get("qrels_status") == "verified" and not status:
            gains = [float(qrels.get(hit.chunk.id, 0)) for hit in prefix]
            ideal = sorted((float(v) for v in qrels.values()), reverse=True)[:k]
            dcg = sum((2 ** value - 1) / math.log2(rank + 2) for rank, value in enumerate(gains))
            idcg = sum((2 ** value - 1) / math.log2(rank + 2) for rank, value in enumerate(ideal))
            values[f"ndcg_at_{k}"] = metric(dcg / idcg if idcg else None,
                status=row_status if idcg else "not_applicable", reason="显式qrels；未评判项按0处理，覆盖率另列")
            values[f"unjudged_fraction_at_{k}"] = metric(
                sum(hit.chunk.id not in qrels for hit in prefix) / len(prefix) if prefix else 0,
                direction="lower")
        else:
            values[f"ndcg_at_{k}"] = metric(status=status or "missing_gold",
                reason="缺少经核验的qrels，不能把原文锚点当作完整相关性标注")
    noanswer_status = ("evaluated" if not case.get("answerable", True)
                       and case.get("gold_status") == "verified" else "not_applicable")
    values["false_sufficiency"] = metric(
        result.decision.status == "sufficient" if noanswer_status == "evaluated" else None,
        status=noanswer_status, unit="boolean", direction="lower")
    values["abstention"] = metric(
        result.decision.status == "insufficient" if noanswer_status == "evaluated" else None,
        status=noanswer_status, unit="boolean")
    if degraded:
        checks.append({"check_id": "effective_retrieval_pipeline", "status": "degraded",
                       "passed": None, "reason": "; ".join(degraded), "evidence_refs": []})
    if protocol == "rag-answer-v2":
        answer = content.get("answer", "")
        generated = bool(answer.strip()) and content.get("generation_status", "completed") == "completed"
        evidence = content.get("context_evidence", [])
        known = {item["evidence_id"] for item in evidence}
        cited = set(re.findall(r"\[(E\d+)\]", answer))
        values["answer_present"] = metric(bool(answer.strip()) if generated else None,
            status="evaluated" if generated else "not_run", unit="boolean", scoring_unit="answer")
        values["citation_validity"] = metric(
            len(cited & known) / len(cited) if cited else None,
            status="evaluated" if cited else "not_applicable", numerator=len(cited & known),
            denominator=len(cited), scoring_unit="citation_id",
            reason="只检查引用ID是否合法，不证明证据支持结论")
        for name in ("answer_correctness", "reference_fact_coverage", "faithfulness", "citation_support"):
            values[name] = metric(status="not_evaluated", scoring_unit="answer_claim",
                                  reason="需要独立Judge或人工逐项评审，不能以非空答案代替正确性")
    return {"metrics": values, "checks": checks,
            "stage_status": {"retrieval": "degraded" if degraded else "completed",
                             **({"generation": "completed" if content.get("answer") else "not_completed"}
                                if protocol == "rag-answer-v2" else {})},
            "degraded": degraded,
            "diagnostics": {"label_policy": "positive_evidence_only", "rank_unit": "child_evidence",
                            "captured_retrieval_limit": captured_depth,
                            "applied_parameters": result.trace.applied_parameters}}
