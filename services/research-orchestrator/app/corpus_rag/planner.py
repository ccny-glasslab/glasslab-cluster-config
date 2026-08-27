"""Bounded heuristic-first query planner for the corpus-RAG prototype.

Decomposes a methodology question into at most ``MAX_SUBQUERIES``
retrieval subqueries: the condensed question leads, domain facet
expansions follow, and an optional dataset-profile note is appended.
``mode='auto'`` consults an :class:`LlmProvider` but silently falls back
to the heuristic result on any exception or shape violation — the
planner never fails because the provider did.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from app.corpus_rag.contracts import MAX_SUBQUERIES, QueryPlan
from app.corpus_rag.llm_provider import LlmProvider

STOPWORDS = frozenset({
    'the', 'a', 'an', 'of', 'for', 'to', 'and', 'or', 'in', 'on', 'with',
    'should', 'we', 'what', 'how', 'is', 'are', 'do', 'does', 'this',
    'that', 'our', 'when', 'which', 'be', 'can', 'under', 'than', 'more',
    'rather', 'it', 'its', 'as', 'by', 'from', 'at', 'into',
})

MAX_CONDENSED_TERMS = 12
MAX_FOCUS_TERMS = 4

# Domain keyword stems -> facet expansion templates. Matching is a plain
# substring check on the lowercased question, so 'cluster' also matches
# 'clustering' and hyphenated stems like 'cross-valid' match verbatim.
FACET_TEMPLATES: dict[tuple[str, ...], list[str]] = {
    ('cluster',): [
        'methods for {focus}',
        'stability assessment for {focus}',
        'validation diagnostics for {focus}',
    ],
    ('imbalanc',): [
        'resampling strategies for {focus}',
        'evaluation metrics for {focus}',
    ],
    ('calibrat',): [
        'calibration assessment for {focus}',
        'recalibration methods',
    ],
    ('dimension', 'high-dimensional', 'spars'): [
        'regularization approaches for {focus}',
        'feature-reduction methods for {focus}',
    ],
    ('assumpt', 'linear', 'regress'): [
        'assumption diagnostics for {focus}',
        'robust alternatives to {focus}',
    ],
    ('cross-valid', 'validat', 'generaliz'): [
        'model-selection strategy for {focus}',
        'nested validation necessity for {focus}',
    ],
    ('multiple', 'discover', 'hypothes'): [
        'multiplicity control for {focus}',
    ],
}

_PROFILE_KEYS = ('task_type', 'n_samples', 'n_features', 'class_balance')

_PLANNER_SYSTEM_PROMPT = (
    'You decompose research methodology questions into retrieval '
    'subqueries. Respond with strict JSON only, of the form '
    '{"subqueries": ["...", ...]}. No prose, no markdown fences.'
)

_TOKEN_RE = re.compile(r'[a-z0-9]+')


def _content_terms(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


def _condensed_question(question: str) -> str:
    return ' '.join(_content_terms(question)[:MAX_CONDENSED_TERMS])


def _focus_terms(question: str) -> str:
    """Top content terms (frequency desc, first-seen tiebreak), <=4."""
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for index, term in enumerate(_content_terms(question)):
        counts[term] = counts.get(term, 0) + 1
        first_seen.setdefault(term, index)
    ranked = sorted(counts, key=lambda t: (-counts[t], first_seen[t]))
    return ' '.join(ranked[:MAX_FOCUS_TERMS])


def _facet_expansions(question: str) -> list[str]:
    lowered = question.lower()
    focus = _focus_terms(question)
    expansions: list[str] = []
    for stems, templates in FACET_TEMPLATES.items():
        if not any(stem in lowered for stem in stems):
            continue
        expansions.extend(
            template.format(focus=focus) for template in templates
        )
    return expansions


def _profile_subquery(dataset_profile: dict[str, Any] | None) -> str | None:
    """One defensive summary subquery from known profile keys, or None."""
    if not dataset_profile:
        return None
    parts: list[str] = []
    for key in _PROFILE_KEYS:
        if key not in dataset_profile:
            continue
        try:
            rendered = f'{key}:{dataset_profile[key]}'
        except Exception:  # noqa: BLE001 - profile values are untrusted
            rendered = f'{key}:<unrenderable>'
        parts.append(rendered)
    if not parts:
        return None
    return 'dataset profile considerations: ' + ', '.join(parts)


def _dedupe_cap(candidates: list[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(cleaned)
        if len(kept) >= MAX_SUBQUERIES:
            break
    return kept


def _provider_subqueries(
    provider: LlmProvider, candidates: list[str]
) -> list[str] | None:
    """Ask the provider; return cleaned strings, or None to fall back.

    Any exception or shape violation (non-dict, non-list-of-str, nothing
    usable after stripping) yields None so the caller falls back to the
    heuristic plan.
    """
    user_message = '\n'.join(f'SUBQUERY: {c}' for c in candidates)
    try:
        result = provider.complete_json(
            system=_PLANNER_SYSTEM_PROMPT, user=user_message
        )
    except Exception:  # noqa: BLE001 - any provider failure must fall back
        return None
    raw = result.get('subqueries') if isinstance(result, dict) else None
    if not isinstance(raw, list):
        return None
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None
        text = item.strip()
        if text:
            cleaned.append(text)
    return cleaned or None


def build_query_plan(
    question: str,
    *,
    dataset_profile: dict[str, Any] | None = None,
    provider: LlmProvider | None = None,
    mode: Literal['heuristic', 'auto'] = 'heuristic',
) -> QueryPlan:
    """Build a bounded query plan for ``question``.

    The condensed question always leads. With ``mode='auto'`` and a
    provider, the heuristic candidates are offered as ``SUBQUERY:``
    lines and the provider's answer replaces the heuristic tail when it
    validates; otherwise the heuristic plan is returned unchanged.
    """
    condensed = _condensed_question(question)
    heuristic_candidates = [condensed]
    heuristic_candidates.extend(_facet_expansions(question))
    profile_subquery = _profile_subquery(dataset_profile)
    if profile_subquery is not None:
        heuristic_candidates.append(profile_subquery)

    subqueries = _dedupe_cap(heuristic_candidates)
    planner_mode: Literal['heuristic', 'llm'] = 'heuristic'

    if mode == 'auto' and provider is not None:
        llm_subqueries = _provider_subqueries(provider, heuristic_candidates)
        if llm_subqueries is not None:
            subqueries = _dedupe_cap([condensed, *llm_subqueries])
            planner_mode = 'llm'

    return QueryPlan(
        original_query=question,
        subqueries=subqueries,
        planner_mode=planner_mode,
    )
