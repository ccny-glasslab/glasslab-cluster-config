"""Optional LLM path for advisory generation.

``llm_advisory_or_none`` asks a duck-typed provider for strict JSON matching
the candidate schema, validates every returned citation id against the real
evidence blocks ([E1..En]), drops unresolvable citations (and candidates left
without any), and returns ``None`` on ANY failure so the caller can fall back
to the extractive result silently.
"""

from __future__ import annotations

import json
from typing import Any

from app.corpus_rag.advisory import (
    _QUOTE_LIMIT,
    _citation_for_hit,
    _dedupe_citations,
    _detect_contradictions,
    _group_by_family,
    _uncertainty,
)
from app.corpus_rag.contracts import (
    RAG_INDEX_VERSION,
    MethodAdvisory,
    MethodCandidate,
    RetrievedHit,
)


_LLM_SYSTEM_PROMPT = (
    'You are a methodology advisor. Using ONLY the numbered evidence blocks '
    '[E1..En], return STRICT JSON: {"candidates": [{"method_name": str, '
    '"why": str, "assumptions": [str], "preprocessing": [str], '
    '"diagnostics": [str], "metrics": [str], "failure_modes": [str], '
    '"baselines": [str], "comparisons": [str], "citations": '
    '[{"evidence_id": "E<i>"}], "confidence": "high"|"medium"|"low"}]}. '
    'Every citation MUST reference a provided evidence id. No prose outside JSON.'
)


def llm_advisory_or_none(
    llm: Any, objective: str, corpus_slug: str, hits: list[RetrievedHit]
) -> MethodAdvisory | None:
    try:
        blocks = '\n\n'.join(
            f'[E{i}] source_id={h.chunk.source_id}\nquote={h.chunk.text[:_QUOTE_LIMIT]}'
            for i, h in enumerate(hits, start=1)
        )
        raw = llm.complete(
            system=_LLM_SYSTEM_PROMPT,
            user=f'Objective: {objective}\n\nEvidence blocks:\n{blocks}',
        )
        payload = json.loads(raw.strip().removeprefix('```json').removesuffix('```').strip())
        by_evidence_id = {f'E{i}': h for i, h in enumerate(hits, start=1)}
        candidates: list[MethodCandidate] = []
        for entry in payload.get('candidates', []):
            citations = []
            for ref in entry.get('citations', []):
                evidence_id = ref.get('evidence_id') if isinstance(ref, dict) else ref
                hit = by_evidence_id.get(str(evidence_id))
                if hit is not None:
                    citations.append(_citation_for_hit(hit))
            if not citations:
                continue
            confidence = entry.get('confidence')
            candidates.append(MethodCandidate(
                method_name=str(entry['method_name']),
                why=str(entry.get('why', '')),
                assumptions=[str(a) for a in entry.get('assumptions', [])],
                preprocessing=[str(a) for a in entry.get('preprocessing', [])],
                diagnostics=[str(a) for a in entry.get('diagnostics', [])],
                metrics=[str(a) for a in entry.get('metrics', [])],
                failure_modes=[str(a) for a in entry.get('failure_modes', [])],
                baselines=[str(a) for a in entry.get('baselines', [])],
                comparisons=[str(a) for a in entry.get('comparisons', [])],
                citations=citations,
                confidence=confidence if confidence in ('high', 'medium', 'low') else 'low',
            ))
        if not candidates:
            return None
        return MethodAdvisory(
            objective=objective,
            corpus_slug=corpus_slug,
            candidates=candidates,
            contradiction_pairs=_detect_contradictions(_group_by_family(hits)),
            uncertainty_statement=_uncertainty(corpus_slug, len(hits)),
            citations_all=_dedupe_citations(candidates),
            generated_by=getattr(llm, 'model_id', '') or 'remote',
            index_version=RAG_INDEX_VERSION,
        )
    except Exception:
        return None


