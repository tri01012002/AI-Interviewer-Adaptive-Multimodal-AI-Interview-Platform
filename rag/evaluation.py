"""Implementation-independent retrieval metrics."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    return len(set(retrieved[:k]) & relevant_set) / len(relevant_set)


def precision_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    if k <= 0:
        return 0.0
    relevant_set = set(relevant)
    return len(set(retrieved[:k]) & relevant_set) / min(k, len(retrieved)) if retrieved else 0.0


def reciprocal_rank(retrieved: Sequence[str], relevant: Iterable[str]) -> float:
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    for index, chunk_id in enumerate(retrieved, start=1):
        if chunk_id in relevant_set:
            return 1.0 / index
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevance: Mapping[str, float], k: int) -> float:
    if not relevance or k <= 0:
        return 0.0
    dcg = sum(
        (2 ** float(relevance.get(chunk_id, 0.0)) - 1) / math.log2(index + 2)
        for index, chunk_id in enumerate(retrieved[:k])
    )
    ideal = sorted((float(value) for value in relevance.values()), reverse=True)[:k]
    idcg = sum((2 ** value - 1) / math.log2(index + 2) for index, value in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def evaluate_ranking(retrieved: Sequence[str], relevance: Mapping[str, float], k: int) -> dict[str, float]:
    return {
        "recall": recall_at_k(retrieved, relevance, k),
        "precision": precision_at_k(retrieved, relevance, k),
        "mrr": reciprocal_rank(retrieved, relevance),
        "ndcg": ndcg_at_k(retrieved, relevance, k),
    }
