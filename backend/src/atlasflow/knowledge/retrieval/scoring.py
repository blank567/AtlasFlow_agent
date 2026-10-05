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


def ranks(scores: dict[str, float]) -> dict[str, int]:
    return {
        item_id: index
        for index, item_id in enumerate(
            sorted(scores, key=lambda key: (-scores[key], key)), start=1
        )
    }
