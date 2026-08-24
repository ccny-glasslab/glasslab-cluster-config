"""Benchmark metrics for corpus-RAG retrieval evaluation.

Pure stdlib functions over ranked result lists. Conventions:

- ``ranking`` is a sequence of ids in score-descending order; duplicates are
  allowed and counted by :func:`duplicate_rate_at_k`.
- ``relevance`` maps id -> graded relevance in {0, 1, 2}; ids below
  ``grade_threshold`` are treated as non-relevant.
- recall/precision/MRR use the default ``grade_threshold=1`` (supporting or
  directly relevant counts); nDCG defaults to ``grade_threshold=0`` so the
  gain encoding 2^grade - 1 can distinguish partial relevance.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def _relevant_ids(relevance: Mapping[str, int], grade_threshold: int) -> set[str]:
    return {id_ for id_, grade in relevance.items() if grade >= grade_threshold}


def recall_at_k(
    ranking: Sequence[str],
    relevance: Mapping[str, int],
    k: int,
    *,
    grade_threshold: int = 1,
) -> float:
    """Fraction of judged-relevant ids retrieved within the top-k ranks."""
    relevant = _relevant_ids(relevance, grade_threshold)
    if not relevant:
        return 0.0
    hits = sum(1 for id_ in ranking[:k] if id_ in relevant)
    return hits / len(relevant)


def precision_at_k(
    ranking: Sequence[str],
    relevance: Mapping[str, int],
    k: int,
    *,
    grade_threshold: int = 1,
) -> float:
    """Fraction of the top-k ranks occupied by judged-relevant ids."""
    if k <= 0:
        return 0.0
    relevant = _relevant_ids(relevance, grade_threshold)
    hits = sum(1 for id_ in ranking[:k] if id_ in relevant)
    return hits / k


def mrr_at_k(
    ranking: Sequence[str],
    relevance: Mapping[str, int],
    k: int,
    *,
    grade_threshold: int = 1,
) -> float:
    """Reciprocal rank of the first relevant id within the top-k ranks."""
    relevant = _relevant_ids(relevance, grade_threshold)
    for position, id_ in enumerate(ranking[:k], start=1):
        if id_ in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(
    ranking: Sequence[str],
    relevance: Mapping[str, int],
    k: int,
    *,
    grade_threshold: int = 0,
) -> float:
    """Binary-threshold nDCG@k with exponential gains ``2^grade - 1``.

    Judged ids below ``grade_threshold`` contribute zero gain. The ideal DCG
    orders ALL judged ids at or above the threshold by descending gain,
    truncated to k positions.
    """
    relevant = {
        id_: relevance[id_] for id_ in _relevant_ids(relevance, grade_threshold)
    }
    if not relevant:
        return 0.0

    def dcg(gains: list[float]) -> float:
        return sum(
            gain / math.log2(position + 1)
            for position, gain in enumerate(gains, start=1)
        )

    run_gains = [
        float(2**relevant[id_] - 1)
        for id_ in ranking[:k]
        if id_ in relevant
    ]
    ideal_gains = sorted(
        (float(2**grade - 1) for grade in relevant.values()), reverse=True
    )[:k]
    ideal = dcg(ideal_gains)
    if ideal <= 0.0:
        return 0.0
    return dcg(run_gains) / ideal


def distinct_sources_at_k(
    ranking: Sequence[str],
    k: int,
    source_of: Mapping[str, str],
) -> int:
    """Count of distinct sources among the top-k ranks.

    Ids missing from ``source_of`` count as their own source defensively.
    """
    return len({source_of.get(id_, id_) for id_ in ranking[:k]})


def duplicate_rate_at_k(ranking: Sequence[str], k: int) -> float:
    """Fraction of top-k positions holding an id seen earlier in the run."""
    prefix = list(ranking[:k])
    if not prefix:
        return 0.0
    seen: set[str] = set()
    duplicates = 0
    for id_ in prefix:
        if id_ in seen:
            duplicates += 1
        else:
            seen.add(id_)
    return duplicates / len(prefix)
