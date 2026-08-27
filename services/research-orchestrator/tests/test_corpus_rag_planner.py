"""Tests for the corpus-RAG bounded query planner and LLM provider.

Everything here is networkless: the offline provider is scripted, and the
remote provider is only ever constructed (never called) against a fake
base URL.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.corpus_rag.contracts import MAX_SUBQUERIES
from app.corpus_rag.llm_provider import (
    OfflineDeterministicLlm,
    OpenAiCompatibleProvider,
    ProviderNotConfiguredError,
    get_llm,
)
from app.corpus_rag.planner import STOPWORDS, build_query_plan

STABILITY_QUESTION = (
    'What methods should we use to assess the stability of clustering '
    'results on this dataset and which validation diagnostics are most '
    'reliable?'
)


def test_heuristic_plan_caps_and_leads_with_condensed() -> None:
    plan = build_query_plan(STABILITY_QUESTION)

    assert plan.planner_mode == 'heuristic'
    assert plan.original_query == STABILITY_QUESTION
    assert 1 <= len(plan.subqueries) <= MAX_SUBQUERIES

    condensed = plan.subqueries[0]
    terms = condensed.split()
    # The lead subquery is the condensed question: content terms only,
    # bounded length, no stopword leakage.
    assert len(terms) <= 12
    assert all(term not in STOPWORDS for term in terms)
    # No duplicate subqueries, case-insensitively.
    lowered = [s.casefold() for s in plan.subqueries]
    assert len(lowered) == len(set(lowered))


def test_facet_matching_covers_expected_domains() -> None:
    cases = [
        (
            'How should we handle class imbalance when training the '
            'classifier on this dataset?',
            ['resampling strategies', 'evaluation metrics'],
        ),
        (
            'Do we need calibration for the probability scores before '
            'comparing models?',
            ['calibration assessment'],
        ),
        (
            'Which regularization works best for high-dimensional sparse '
            'feature data?',
            ['regularization approaches', 'feature-reduction methods'],
        ),
        (
            'We ran many hypothesis tests during discovery; how do we keep '
            'the error rate controlled?',
            ['multiplicity control'],
        ),
    ]
    for question, expected_fragments in cases:
        plan = build_query_plan(question)
        joined = ' | '.join(plan.subqueries).casefold()
        for fragment in expected_fragments:
            assert fragment in joined, (
                f'{fragment!r} missing for question {question!r}; '
                f'got {plan.subqueries!r}'
            )


def test_offline_provider_roundtrip() -> None:
    provider = OfflineDeterministicLlm()

    result = provider.complete_json(
        system='irrelevant',
        user='context line\nSUBQUERY: alpha\nnoise\nSUBQUERY: beta beta',
    )
    assert result == {'subqueries': ['alpha', 'beta beta']}

    many = '\n'.join(f'SUBQUERY: q{i}' for i in range(9))
    capped = provider.complete_json(system='', user=many)
    assert capped['subqueries'] == [f'q{i}' for i in range(MAX_SUBQUERIES)]

    empty = provider.complete_json(system='', user='no markers at all')
    assert empty == {'subqueries': []}


class BrokenProvider:
    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        raise RuntimeError('provider exploded')


class BadShapeProvider:
    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        return {'subqueries': 'not-a-list'}


def test_auto_mode_uses_provider_and_falls_back() -> None:
    heuristic_plan = build_query_plan(STABILITY_QUESTION)

    llm_plan = build_query_plan(
        STABILITY_QUESTION,
        provider=OfflineDeterministicLlm(),
        mode='auto',
    )
    assert llm_plan.planner_mode == 'llm'
    # The offline provider echoes the heuristic candidates back, so the
    # decomposition survives the roundtrip while the mode flips.
    assert llm_plan.subqueries[0] == heuristic_plan.subqueries[0]
    assert set(llm_plan.subqueries[1:]) == set(heuristic_plan.subqueries[1:])
    assert len(llm_plan.subqueries) <= MAX_SUBQUERIES

    for broken in (BrokenProvider(), BadShapeProvider()):
        fallback_plan = build_query_plan(
            STABILITY_QUESTION, provider=broken, mode='auto'
        )
        assert fallback_plan.planner_mode == 'heuristic'
        assert fallback_plan.subqueries == heuristic_plan.subqueries


def test_remote_provider_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('GLASSLAB_RAG_LLM_BASE_URL', raising=False)
    with pytest.raises(
        ProviderNotConfiguredError, match='GLASSLAB_RAG_LLM_BASE_URL'
    ):
        OpenAiCompatibleProvider()
    with pytest.raises(
        ProviderNotConfiguredError, match='GLASSLAB_RAG_LLM_BASE_URL'
    ):
        get_llm('remote')

    # A fake base URL must be enough to construct the provider; no network
    # I/O happens until complete_json is called.
    monkeypatch.setenv('GLASSLAB_RAG_LLM_BASE_URL', 'http://127.0.0.1:9')
    provider = OpenAiCompatibleProvider()
    assert provider.base_url == 'http://127.0.0.1:9'

    offline_default = get_llm()
    assert isinstance(offline_default, OfflineDeterministicLlm)
