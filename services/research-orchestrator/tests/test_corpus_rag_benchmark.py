"""Golden-value tests for corpus-RAG benchmark metrics.

Every expected number is hand-computed from the formula in the module
docstrings of app/corpus_rag/benchmark.py; these pins catch silent
convention drift (gain encoding, discount base, tie handling).
"""

from __future__ import annotations

import pytest

from app.corpus_rag.benchmark import (
    distinct_sources_at_k,
    duplicate_rate_at_k,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

RANKING = ['a', 'b', 'c', 'd', 'e']
RELEVANCE = {'b': 2, 'd': 1, 'z': 2}


def test_recall_and_precision_golden() -> None:
    # relevant(threshold1) = {b, d, z}; top3 = {a, b, c} -> only b hit.
    assert recall_at_k(RANKING, RELEVANCE, 3) == pytest.approx(1 / 3)
    assert precision_at_k(RANKING, RELEVANCE, 3) == pytest.approx(1 / 3)
    # top5 contains b and d -> 2 of 3 relevant.
    assert recall_at_k(RANKING, RELEVANCE, 5) == pytest.approx(2 / 3)
    assert precision_at_k(RANKING, RELEVANCE, 5) == pytest.approx(2 / 5)


def test_grade_threshold_filtering() -> None:
    # threshold2: relevant = {b, z}; top5 contains only b.
    assert recall_at_k(RANKING, RELEVANCE, 5, grade_threshold=2) == pytest.approx(1 / 2)
    assert precision_at_k(RANKING, RELEVANCE, 5, grade_threshold=2) == pytest.approx(1 / 5)
    # grade-1 evidence must not count as relevant under the default threshold
    # when it never appears: z stays unretrieved regardless.
    assert recall_at_k(['d'], RELEVANCE, 1, grade_threshold=2) == 0.0


def test_mrr_golden() -> None:
    # First relevant hit is b at rank 2.
    assert mrr_at_k(RANKING, RELEVANCE, 3) == pytest.approx(1 / 2)
    assert mrr_at_k(RANKING, RELEVANCE, 1) == 0.0
    assert mrr_at_k(RANKING, {}, 5) == 0.0


def test_ndcg_golden() -> None:
    ranking = ['a', 'b', 'c']
    relevance = {'a': 1, 'b': 2}
    # gains: rank1 a -> 2^1-1 = 1 ; rank2 b -> 2^2-1 = 3 ; rank3 c unjudged -> 0
    # dcg  = 1/log2(2) + 3/log2(3)            = 1 + 1.8927892607 = 2.8927892607
    # idcg = 3/log2(2) + 1/log2(3)            = 3 + 0.6309297536 = 3.6309297536
    # ndcg = 2.8927892607 / 3.6309297536      = 0.796706...
    assert ndcg_at_k(ranking, relevance, 3) == pytest.approx(0.7967, abs=1e-4)
    # k=1 truncation: dcg=1, idcg=3 -> 1/3.
    assert ndcg_at_k(ranking, relevance, 1) == pytest.approx(1 / 3)
    # No judged-relevant ids at all.
    assert ndcg_at_k(RANKING, {}, 5) == 0.0


def test_distinct_sources_and_duplicate_rate_golden() -> None:
    ranking = ['x1', 'x2', 'x1', 'x3']
    source_of = {'x1': 's1', 'x2': 's2', 'x3': 's3'}
    assert distinct_sources_at_k(ranking, 4, source_of) == 3
    # One repeated id (x1 twice) among four positions.
    assert duplicate_rate_at_k(ranking, 4) == pytest.approx(1 / 4)
    assert duplicate_rate_at_k([], 4) == 0.0
    # Unknown ids count as their own source defensively.
    assert distinct_sources_at_k(['u1', 'u2'], 2, {}) == 2


# ------------------ chunk-level qrels tests ------------------ #


def test_chunk_level_ndcg_with_chunk_keys() -> None:
    """Chunk-level nDCG where ranking entries are chunk_ids and qrels use chunk_ids as keys."""
    ranking = ['a', 'b', 'c', 'd', 'e']
    # Qrels keyed by chunk_id (same as ranking entries)
    relevance = {'a': 2, 'b': 1, 'c': 1}

    # rank1 a (grade 2), rank2 b (grade 1), rank3 c (grade 1)
    # DCG@3 = 3/log2(2) + 1/log2(3) + 1/log2(4) = 3 + 0.6309 + 0.5 = 4.1309
    # IDCG@3 = 3/log2(2) + 1/log2(3) + 1/log2(4) = same (perfect order)
    # nDCG@3 = 1.0
    assert ndcg_at_k(ranking, relevance, 3) == pytest.approx(1.0, abs=1e-4)

    # rank1 a (grade 2) -> first relevant at position 1
    assert mrr_at_k(ranking, relevance, 3) == pytest.approx(1.0, abs=1e-4)


def test_chunk_level_ndcg_partial_match() -> None:
    """Chunk-level nDCG with partial match - some chunks in ranking have qrels entries."""
    ranking = ['x', 'a', 'y', 'b', 'z']
    relevance = {'a': 2, 'b': 1}
    # Only a and b have relevance scores
    # rank2 a (grade 2), rank4 b (grade 1)
    # DCG@5 = 3/log2(3) + 1/log2(5) = 1.8928 + 0.4307 = 2.3235
    # IDCG@5 = 3/log2(2) + 1/log2(3) = 3 + 0.6309 = 3.6309
    # nDCG@5 = 2.3235 / 3.6309 = 0.640
    assert ndcg_at_k(ranking, relevance, 5) == pytest.approx(0.640, abs=1e-3)

    # First relevant at rank2 (a)
    assert mrr_at_k(ranking, relevance, 5) == pytest.approx(1 / 2, abs=1e-4)


def test_chunk_level_precision_recall() -> None:
    """Chunk-level precision and recall using chunk_ids as keys."""
    ranking = ['a', 'b', 'c', 'd']
    relevance = {'a': 2, 'c': 1}
    # Top 3 contains a (grade 2) and c (grade 1) = 2 relevant out of 3
    assert precision_at_k(ranking, relevance, 3) == pytest.approx(2 / 3, abs=1e-4)
    # 2 relevant total, 2 found in top 3
    assert recall_at_k(ranking, relevance, 3) == pytest.approx(1.0, abs=1e-4)
