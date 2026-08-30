"""Hybrid retrieval for the corpus-RAG prototype.

Pipeline per :meth:`HybridRetriever.retrieve` call: plan -> lexical and/or
dense candidate channels per subquery -> RRF fusion -> optional rerank ->
per-source diversification -> optional section-sibling expansion ->
whole-entry token-budget admission. Citations are built only for the final
admitted hits so every emitted citation resolves to a stored chunk.

Heavy runtimes stay out of import time: :class:`CrossEncoderReranker`
imports sentence-transformers/torch lazily inside its first predict call.

# allow: SIZE_OK — task-mandated single-module deliverable: the retrieval
# engine plus both reranker implementations ship together by contract.
"""

from __future__ import annotations

import gc
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

import numpy as np

from app.corpus_rag.contracts import (
    MAX_CHUNKS_PER_SOURCE,
    RETRIEVAL_TOKEN_BUDGET,
    RRF_K,
    Citation,
    QueryPlan,
    RagChunkRecord,
    RetrievedHit,
)
from app.corpus_rag.planner import build_query_plan

if TYPE_CHECKING:
    from app.corpus_rag.vector_index import VectorIndex

__all__ = [
    'CrossEncoderReranker',
    'HybridRetriever',
    'OfflineReranker',
    'RetrievalOptions',
    'RetrievalResult',
    'Reranker',
]

Mode = Literal['lexical', 'dense', 'hybrid', 'hybrid+rerank']
_RERANK_TOP_N = 24
_TIMING_STAGES = ('lexical', 'dense', 'fuse', 'rerank', 'expand', 'total')


def _estimate_tokens(text: str) -> int:
    """Mirror knowledge_manager's word-count floor locally."""
    return max(1, len(text.split()))


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


@dataclass
class RetrievalOptions:
    """Knobs for one retrieve call; ``rerank`` derives from ``mode``."""

    mode: Mode = 'hybrid'
    k_final: int = 8
    candidate_k: int = 40
    expand: bool = False
    token_budget: int = RETRIEVAL_TOKEN_BUDGET

    @property
    def rerank(self) -> bool:
        return self.mode == 'hybrid+rerank'

    @property
    def wants_dense(self) -> bool:
        return self.mode in ('dense', 'hybrid', 'hybrid+rerank')


@dataclass
class RetrievalResult:
    """Final hits plus the plan, resolvable citations, and stage timings."""

    hits: list[RetrievedHit]
    plan: QueryPlan
    citations: list[Citation]
    timings: dict[str, float]


@runtime_checkable
class Reranker(Protocol):
    """Structural interface: scores aligned to the input text order."""

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        ...


class OfflineReranker:
    """Deterministic pseudo cross-encoder: normalized query-term overlap.

    Score = (distinct query tokens of length > 2 present in the lowercased
    text) / (distinct query tokens of length > 2). Stable and monotone;
    good enough to flip candidate orders in tests without any model.
    """

    _TOKEN_RE = re.compile(r'[a-z0-9]+')

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        terms = sorted({
            token
            for token in self._TOKEN_RE.findall(query.lower())
            if len(token) > 2
        })
        if not terms:
            return [0.0 for _ in texts]
        return [
            sum(1 for term in terms if term in text.lower()) / len(terms)
            for text in texts
        ]


class CrossEncoderReranker:
    """Real cross-encoder over (query, passage) pairs, loaded lazily.

    sentence-transformers is imported inside the first predict call, never
    at module import. Passages are truncated to ``passage_char_budget``
    chars (the query stays full). Instances sharing a ``model_name`` reuse
    one loaded model via a class-level cache; :meth:`unload` drops them.
    """

    DEFAULT_MODEL_NAME = 'BAAI/bge-reranker-base'
    BATCH_SIZE = 8

    _cache: ClassVar[dict[str, Any]] = {}

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        passage_char_budget: int = 320 * 4,
    ) -> None:
        self.model_name = model_name
        self.passage_char_budget = passage_char_budget
        self.model_id = model_name

    def _load(self) -> Any:
        model = type(self)._cache.get(self.model_name)
        if model is None:
            import torch
            from sentence_transformers import CrossEncoder

            torch.set_num_threads(min(8, os.cpu_count() or 1))
            model = CrossEncoder(self.model_name)
            type(self)._cache[self.model_name] = model
        return model

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        model = self._load()
        pairs = [[query, text[: self.passage_char_budget]] for text in texts]
        scores = model.predict(pairs, batch_size=self.BATCH_SIZE)
        return [float(score) for score in scores]

    @classmethod
    def unload(cls) -> None:
        """Drop every cached model and reclaim RAM."""
        cls._cache.clear()
        gc.collect()


def _channel_ranks(channels: list[list[str]]) -> dict[str, int]:
    """Best (minimum) 1-based position per chunk across subquery channels."""
    ranks: dict[str, int] = {}
    for channel in channels:
        for position, chunk_id in enumerate(channel, start=1):
            known = ranks.get(chunk_id)
            if known is None or position < known:
                ranks[chunk_id] = position
    return ranks


def _citation_for(record: RagChunkRecord) -> Citation:
    char_span: list[int] | None = None
    if record.char_start is not None and record.char_end is not None:
        char_span = [record.char_start, record.char_end]
    return Citation(
        chunk_id=record.chunk_id,
        source_id=record.source_id,
        evidence_uri=f'knowledge://{record.source_id}',
        section_path=record.section_path,
        page_start=record.page_start,
        page_end=record.page_end,
        char_span=char_span,
        quote=record.text[:240],
    )


class HybridRetriever:
    """Lexical+dense hybrid retrieval with RRF fusion over subqueries.

    The embedding provider and vector index are only consulted when the
    mode asks for the dense channel; a lexical-only retriever needs neither.
    Chunk rows referenced by a channel but missing from the store are
    skipped — candidates are never invented.
    """

    def __init__(
        self,
        store: Any,
        *,
        vector_index: VectorIndex | None = None,
        embedding_provider: Any | None = None,
        reranker: Reranker | None = None,
        model_id: str = 'offline-deterministic',
    ) -> None:
        self._store = store
        self._vector_index = vector_index
        self._embedding_provider = embedding_provider
        self._reranker = reranker
        self.model_id = model_id

    def retrieve(
        self,
        question: str,
        *,
        source_ids: list[str] | None = None,
        options: RetrievalOptions | None = None,
    ) -> RetrievalResult:
        options = options or RetrievalOptions()
        timings = {stage: 0.0 for stage in _TIMING_STAGES}
        started = time.perf_counter()
        scope = list(source_ids) if source_ids else None

        plan = build_query_plan(question)
        subqueries = plan.subqueries or [question]

        # (b) LEXICAL: FTS per subquery, rank position -> lexical_rank.
        stage = time.perf_counter()
        lexical_channels = [
            [
                row['chunk_id']
                for row in self._store.search_rag_chunks_fts(
                    subquery, source_ids=scope, limit=options.candidate_k
                )
            ]
            for subquery in subqueries
        ]
        timings['lexical'] = _elapsed_ms(stage)

        # (c) DENSE: embed each subquery, search the index per subquery.
        dense_channels: list[list[str]] = []
        if (
            options.wants_dense
            and self._vector_index is not None
            and self._embedding_provider is not None
        ):
            stage = time.perf_counter()
            query_matrix = self._embedding_provider.embed_queries(list(subqueries))
            for row in range(query_matrix.shape[0]):
                query_vec = np.asarray(query_matrix[row], dtype=np.float32)
                found = self._vector_index.search(
                    query_vec, options.candidate_k, source_ids=scope
                )
                dense_channels.append([chunk_id for chunk_id, _ in found])
            timings['dense'] = _elapsed_ms(stage)

        # Hydrate every referenced chunk through ONE table pass; missing
        # rows are skipped rather than invented.
        referenced = {
            chunk_id
            for channel in (*lexical_channels, *dense_channels)
            for chunk_id in channel
        }
        table: dict[str, RagChunkRecord] = {}
        if referenced:
            for row in self._store.list_rag_chunks(
                source_ids=scope, kinds=None, limit=None
            ):
                table[row['chunk_id']] = RagChunkRecord.model_validate(row)

        # (d) FUSE: RRF over the channels the mode selects.
        stage = time.perf_counter()
        lexical_rank = _channel_ranks(lexical_channels)
        dense_rank = _channel_ranks(dense_channels)
        relevant: set[str] = set()
        if options.mode != 'dense':
            relevant.update(lexical_rank)
        if options.mode != 'lexical':
            relevant.update(dense_rank)
        candidates = sorted(relevant & table.keys())
        fused: dict[str, float] = {}
        for chunk_id in candidates:
            score = 0.0
            if chunk_id in lexical_rank:
                score += 1.0 / (RRF_K + lexical_rank[chunk_id])
            if chunk_id in dense_rank:
                score += 1.0 / (RRF_K + dense_rank[chunk_id])
            fused[chunk_id] = score
        ranked = sorted(candidates, key=lambda cid: (-fused[cid], cid))
        timings['fuse'] = _elapsed_ms(stage)

        # (e) RERANK: reorder the head by reranker score; fall back to the
        # RRF order untouched when no reranker is configured.
        rerank_scores: dict[str, float] = {}
        if options.rerank and self._reranker is not None and ranked:
            stage = time.perf_counter()
            head = ranked[:_RERANK_TOP_N]
            tail = ranked[_RERANK_TOP_N:]
            scores = self._reranker.rerank(
                question, [table[cid].text for cid in head]
            )
            order = sorted(
                range(len(head)), key=lambda i: (-float(scores[i]), head[i])
            )
            ranked = [head[i] for i in order] + tail
            for position, chunk_id in enumerate(head):
                rerank_scores[chunk_id] = float(scores[position])
            timings['rerank'] = _elapsed_ms(stage)

        # (f) DIVERSIFY: cap chunks per source while filling k_final.
        selected: list[str] = []
        per_source: Counter[str] = Counter()
        for chunk_id in ranked:
            owner = table[chunk_id].source_id
            if per_source[owner] >= MAX_CHUNKS_PER_SOURCE:
                continue
            selected.append(chunk_id)
            per_source[owner] += 1
            if len(selected) >= options.k_final:
                break

        # (g) EXPAND: append up to one adjacent section sibling per selected
        # evidence span, after the diversified selection.
        extras: list[RagChunkRecord] = []
        if options.expand:
            stage = time.perf_counter()
            chosen = set(selected)
            for chunk_id in selected:
                anchor = table[chunk_id]
                if anchor.kind != 'evidence_span' or anchor.section_path is None:
                    continue
                siblings = [
                    record
                    for sibling_id, record in table.items()
                    if sibling_id not in chosen
                    and record.source_id == anchor.source_id
                    and record.kind == 'evidence_span'
                    and record.section_path == anchor.section_path
                ]
                if not siblings:
                    continue
                siblings.sort(
                    key=lambda record: (
                        abs(record.chunk_index - anchor.chunk_index),
                        record.chunk_index,
                    )
                )
                neighbor = siblings[0]
                chosen.add(neighbor.chunk_id)
                extras.append(neighbor)
            timings['expand'] = _elapsed_ms(stage)

        # (h) BUDGET: whole-entry admission in final order, no truncation.
        extra_ids = {record.chunk_id for record in extras}
        hits: list[RetrievedHit] = []
        citations: list[Citation] = []
        cumulative = 0
        for chunk_id in [*selected, *(record.chunk_id for record in extras)]:
            record = table[chunk_id]
            cost = _estimate_tokens(record.text)
            if cumulative + cost > options.token_budget:
                continue
            cumulative += cost
            if chunk_id in extra_ids:
                hits.append(RetrievedHit(
                    chunk=record, score=0.0, stage_scores={'expanded': 1.0}
                ))
            else:
                hits.append(RetrievedHit(
                    chunk=record,
                    score=fused[chunk_id],
                    stage_scores={'rrf': fused[chunk_id]},
                    lexical_rank=lexical_rank.get(chunk_id),
                    dense_rank=dense_rank.get(chunk_id),
                    rerank_score=rerank_scores.get(chunk_id),
                ))
            citations.append(_citation_for(record))

        timings['total'] = _elapsed_ms(started)
        return RetrievalResult(
            hits=hits, plan=plan, citations=citations, timings=timings
        )
