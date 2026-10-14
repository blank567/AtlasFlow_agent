from __future__ import annotations

import math
from collections import Counter

from atlasflow.knowledge.domain import KnowledgeChunk
from atlasflow.knowledge.ingestion.chunker import lexicalize


def cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def bm25(query: str, chunks: list[KnowledgeChunk]) -> dict[str, float]:
    if not chunks:
        return {}
    query_tokens = lexicalize(query).split()
    token_rows = {item.id: item.lexical_text.split() for item in chunks}
    average_length = sum(len(row) for row in token_rows.values()) / len(chunks)
    document_frequency: Counter[str] = Counter()
    for row in token_rows.values():
        document_frequency.update(set(row))
    scores: dict[str, float] = {}
    for chunk in chunks:
        frequencies = Counter(token_rows[chunk.id])
        score = 0.0
        for token in query_tokens:
            frequency = frequencies[token]
            if not frequency:
                continue
            df = document_frequency[token]
            inverse = math.log(1 + (len(chunks) - df + 0.5) / (df + 0.5))
            denominator = frequency + 1.5 * (
                1 - 0.75 + 0.75 * len(token_rows[chunk.id]) / max(average_length, 1)
            )
            score += inverse * frequency * 2.5 / denominator
        scores[chunk.id] = score
    return scores


def ranks(scores: dict[str, float], *, limit: int) -> dict[str, int]:
    """Rank only positive matches from one retrieval lane, capped before fusion."""
    return {
        item_id: index
        for index, item_id in enumerate(
            sorted(
                (item_id for item_id, score in scores.items() if score > 0 and math.isfinite(score)),
                key=lambda key: (-scores[key], key),
            )[:limit],
            start=1,
        )
    }


def pool_query_scores(
    rows: list[dict[str, float]], *, limit: int, rrf_k: int
) -> tuple[dict[str, float], dict[str, int]]:
    """Pool query variants by rank, keeping the original query as the anchor.

    The original has weight 1; expansions share a total weight of 0.5. More
    aliases therefore cannot amplify an expansion's total influence. Raw score
    maxima are returned only for inspection, not used to compare query lengths.
    With a single query this is exactly the existing single-lane ranking.
    """
    if not rows:
        return {}, {}
    if len(rows) == 1:
        return rows[0], ranks(rows[0], limit=limit)
    item_ids = set().union(*(row.keys() for row in rows))
    scores = {
        item_id: max(
            (row.get(item_id, 0.0) for row in rows if math.isfinite(row.get(item_id, 0.0))),
            default=0.0,
        )
        for item_id in item_ids
    }
    pooled: dict[str, float] = {}
    expansion_weight = 0.5 / (len(rows) - 1)
    for index, row in enumerate(rows):
        weight = 1.0 if index == 0 else expansion_weight
        for item_id, rank in ranks(row, limit=limit).items():
            pooled[item_id] = pooled.get(item_id, 0.0) + weight / (rrf_k + rank)
    return scores, ranks(pooled, limit=limit)
