"""Method-advisory generation over retrieved corpus evidence.

``build_method_advisory`` turns a ``RetrievalResult`` into either a grounded
``MethodAdvisory`` or an ``InsufficientCorpusAdvisory`` refusal. The default
path is deterministic and extractive: keyword families map evidence spans to
fixed method candidates whose rationale is composed from quoted hit text.
An optional LLM provider may replace composition, but every citation it
returns must resolve to a provided evidence id or the citation (and any
candidate left without citations) is dropped; any failure silently falls back
to the extractive result.

Only pydantic contracts are imported here; no model runtimes.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.corpus_rag.advisory_families import _FAMILIES, _Family
from app.corpus_rag.contracts import (
    RAG_INDEX_VERSION,
    Citation,
    InsufficientCorpusAdvisory,
    MethodAdvisory,
    MethodCandidate,
    RagChunkRecord,
    RetrievedHit,
)

_EXTRACTIVE_BY = 'extractive-fallback'
_MAX_CANDIDATES = 5
_MAX_CITATIONS_PER_CANDIDATE = 3
_QUOTE_LIMIT = 240
_WHY_FRAGMENT_LIMIT = 200

_NEGATIVE_MARKERS = ('limitation', 'failure', 'bias', 'pitfall', 'critical')
_POSITIVE_MARKERS = ('recommend', 'should', 'useful', 'effective')


class AdvisoryError(Exception):
    """Raised when advisory construction is requested in an invalid way."""


class _LlmProvider(Protocol):
    """Narrow duck-typed surface; anything else falls back to extractive."""

    def complete(self, *, system: str, user: str) -> str: ...


def _citation_for_hit(hit: RetrievedHit) -> Citation:
    chunk: RagChunkRecord = hit.chunk
    char_span = None
    if chunk.char_start is not None and chunk.char_end is not None:
        char_span = [chunk.char_start, chunk.char_end]
    return Citation(
        chunk_id=chunk.chunk_id,
        source_id=chunk.source_id,
        evidence_uri=f'knowledge://{chunk.source_id}',
        section_path=chunk.section_path,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        char_span=char_span,
        quote=chunk.text[:_QUOTE_LIMIT],
    )


def _fragment(text: str) -> str:
    return text[:_WHY_FRAGMENT_LIMIT].strip()


def _why_from_hits(hits: list[RetrievedHit]) -> str:
    first = hits[0]
    sentences = [f'Evidence [{first.chunk.source_id}] states: "{_fragment(first.chunk.text)}"']
    if len(hits) > 1:
        second = hits[1]
        sentences.append(
            f'A corroborating span [{second.chunk.source_id}] adds: "{_fragment(second.chunk.text)}"'
        )
    return ' '.join(sentences)


def _matches(text_lower: str, fam: _Family) -> bool:
    return any(k in text_lower for k in fam.any_of) and all(k in text_lower for k in fam.required)


def _group_by_family(hits: list[RetrievedHit]) -> dict[str, list[RetrievedHit]]:
    grouped: dict[str, list[RetrievedHit]] = {}
    seen: set[str] = set()
    for hit in hits:
        if hit.chunk.chunk_id in seen:
            continue
        seen.add(hit.chunk.chunk_id)
        text = hit.chunk.text.lower()
        for fam in _FAMILIES:
            if _matches(text, fam):
                grouped.setdefault(fam.label, []).append(hit)
    return grouped


def _detect_contradictions(grouped: dict[str, list[RetrievedHit]]) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for label, hits in grouped.items():
        for i, first in enumerate(hits):
            neg_first = any(m in first.chunk.text.lower() for m in _NEGATIVE_MARKERS)
            pos_first = any(m in first.chunk.text.lower() for m in _POSITIVE_MARKERS)
            for second in hits[i + 1:]:
                if second.chunk.source_id == first.chunk.source_id:
                    continue
                neg_second = any(m in second.chunk.text.lower() for m in _NEGATIVE_MARKERS)
                pos_second = any(m in second.chunk.text.lower() for m in _POSITIVE_MARKERS)
                opposed = (neg_first and pos_second) or (pos_first and neg_second)
                if not opposed:
                    continue
                key = (first.chunk.source_id, second.chunk.source_id, label)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append({'a': key[0], 'b': key[1], 'topic': label})
    return pairs


def _extractive_candidate(fam: _Family, hits: list[RetrievedHit]) -> MethodCandidate:
    return MethodCandidate(
        method_name=fam.label,
        why=_why_from_hits(hits),
        assumptions=list(fam.assumptions),
        preprocessing=list(fam.preprocessing),
        diagnostics=list(fam.diagnostics),
        metrics=list(fam.metrics),
        failure_modes=list(fam.failure_modes),
        baselines=list(fam.baselines),
        comparisons=list(fam.comparisons),
        citations=[_citation_for_hit(h) for h in hits[:_MAX_CITATIONS_PER_CANDIDATE]],
        confidence='low',
    )


def _insufficient(reason: str, details: str) -> InsufficientCorpusAdvisory:
    return InsufficientCorpusAdvisory(reason=reason, details=details)


def _uncertainty(corpus_slug: str, n_hits: int) -> str:
    return (
        f'The supplied corpus ({corpus_slug}) supports these candidates only '
        f'partially; {n_hits} evidence spans were retrieved. Statements beyond '
        'cited spans are not supported.'
    )


def _dedupe_citations(candidates: list[MethodCandidate]) -> list[Citation]:
    seen: set[str] = set()
    unique: list[Citation] = []
    for candidate in candidates:
        for citation in candidate.citations:
            if citation.chunk_id not in seen:
                seen.add(citation.chunk_id)
                unique.append(citation)
    return unique


def build_method_advisory(
    *,
    objective: str,
    corpus_slug: str,
    retrieval: Any,
    store: Any,
    llm: Any = None,
) -> MethodAdvisory | InsufficientCorpusAdvisory:
    """Build a grounded advisory (or a refusal) from retrieval output."""
    if not objective.strip():
        raise AdvisoryError('objective must be non-empty')
    hits: list[RetrievedHit] = list(retrieval.hits)
    if not hits:
        return _insufficient(
            'no retrievable evidence for this objective',
            f'corpus={corpus_slug}; hits=0 after hybrid retrieval',
        )
    if llm is not None:
        from app.corpus_rag.advisory_llm import llm_advisory_or_none

        llm_result = llm_advisory_or_none(llm, objective, corpus_slug, hits)
        if llm_result is not None:
            return llm_result
    grouped = _group_by_family(hits)
    if not grouped:
        return _insufficient(
            'retrieved evidence does not map to known method families',
            f'corpus={corpus_slug}; hits={len(hits)}; matched_families=0',
        )
    fam_by_label = {f.label: f for f in _FAMILIES}
    candidates = [
        _extractive_candidate(fam_by_label[label], fam_hits)
        for label, fam_hits in grouped.items()
        if label in fam_by_label
    ][:_MAX_CANDIDATES]
    return MethodAdvisory(
        objective=objective,
        corpus_slug=corpus_slug,
        candidates=candidates,
        contradiction_pairs=_detect_contradictions(grouped),
        uncertainty_statement=_uncertainty(corpus_slug, len(hits)),
        citations_all=_dedupe_citations(candidates),
        generated_by=_EXTRACTIVE_BY,
        index_version=RAG_INDEX_VERSION,
    )


def render_markdown(advisory: MethodAdvisory | InsufficientCorpusAdvisory) -> str:
    """Human-readable rendering used by ``ask --advisory`` and QA transcripts."""
    if advisory.kind == 'insufficient_corpus':
        return (
            '# Corpus Advisory: Insufficient Evidence\n\n'
            f'Reason: {advisory.reason}\n\n'
            f'Details: {advisory.details}\n'
        )
    lines: list[str] = [
        '# Methodology Advisory',
        '',
        f'Objective: {advisory.objective}',
        f'Corpus: {advisory.corpus_slug} (generated_by={advisory.generated_by}, '
        f'index_version={advisory.index_version})',
        '',
        f'Uncertainty: {advisory.uncertainty_statement}',
        '',
    ]
    numbered: list[tuple[int, Citation]] = []
    counter = 0
    for position, candidate in enumerate(advisory.candidates, start=1):
        lines += [f'## {position}. {candidate.method_name}', '', f'Why: {candidate.why}', '']
        for heading, items in (
            ('Assumptions', candidate.assumptions),
            ('Preprocessing', candidate.preprocessing),
            ('Diagnostics', candidate.diagnostics),
            ('Metrics', candidate.metrics),
            ('Failure modes', candidate.failure_modes),
            ('Baselines', candidate.baselines),
            ('Comparisons', candidate.comparisons),
        ):
            if items:
                lines.append(f'{heading}:')
                lines += [f'- {item}' for item in items]
        refs = []
        for citation in candidate.citations:
            counter += 1
            numbered.append((counter, citation))
            refs.append(f'[{counter}]')
        if refs:
            lines += ['', 'Citations: ' + ' '.join(refs)]
        lines.append('')
    lines.append('## CITATIONS')
    lines.append('')
    for number, citation in numbered:
        pages = (
            f'pages {citation.page_start}-{citation.page_end}'
            if citation.page_start is not None else 'no page info'
        )
        span = (
            f'chars {citation.char_span[0]}-{citation.char_span[1]}'
            if citation.char_span else 'no char span'
        )
        section = citation.section_path or 'no section path'
        lines.append(f'[{number}] {citation.evidence_uri} — section_path={section}, {pages}, {span}')
        lines.append(f'    "{citation.quote}"')
    return '\n'.join(lines) + '\n'
