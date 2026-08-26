"""Hybrid retrieval + rerank + expansion tests (network-free, fake-backed).

The dense channel is crafted deterministically: chunk vectors are seeded
manually as orthogonal basis vectors and the fake query embedding returns a
fixed unit vector, so cosine rankings are exact regardless of any model
download. No sentence-transformers/torch model is ever loaded here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from app.corpus_rag.contracts import (
    MAX_CHUNKS_PER_SOURCE,
    RAG_INDEX_VERSION,
    ChunkVectorMeta,
    RagChunkRecord,
)
from app.corpus_rag.embeddings import encode_vector
from app.corpus_rag.retrieval import (
    HybridRetriever,
    OfflineReranker,
    RetrievalOptions,
    Reranker,
)
from app.schemas import KnowledgeSource, SourceType
from app.storage import SqliteStore

_ASK_PATH = (
    Path(__file__).resolve().parents[1] / 'scripts' / 'corpus_rag' / 'ask.py'
)
_spec = importlib.util.spec_from_file_location('corpus_rag_ask', _ASK_PATH)
ask = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ask)

MODEL_ID = 'offline-deterministic'
DIMS = 16


def _unit(dims: int, axis: int) -> np.ndarray:
    vec = np.zeros(dims, dtype=np.float32)
    vec[axis % dims] = 1.0
    return vec


class _FixedQueryEmbedding:
    """Fake provider: every query maps to e_0; passages roll the axis."""

    model_id = MODEL_ID
    revision = 'v0'
    dims = DIMS

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return np.stack(
            [_unit(self.dims, i + 1) for i in range(len(texts))]
        ) if texts else np.zeros((0, self.dims), dtype=np.float32)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return np.stack([_unit(self.dims, 0) for _ in texts]) if texts else np.zeros(
            (0, self.dims), dtype=np.float32
        )


class _ScriptedReranker:
    """Deterministic reranker returning hand-set scores per text."""

    def __init__(self, scores_by_text: dict[str, float]) -> None:
        self._scores = dict(scores_by_text)

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        return [self._scores[text] for text in texts]


def _source(uri: str) -> KnowledgeSource:
    return KnowledgeSource(
        source_type=SourceType.DOCUMENTATION,
        canonical_uri=uri,
        digest=hashlib.sha256(uri.encode()).hexdigest(),
    )


def _chunk(
    source_id: str,
    index: int,
    text: str,
    *,
    kind: str = 'evidence_span',
    section_path: str | None = None,
) -> RagChunkRecord:
    return RagChunkRecord(
        chunk_id=f'{source_id}::c{index}',
        source_id=source_id,
        kind=kind,  # type: ignore[arg-type]
        chunk_index=index,
        text=text,
        digest=hashlib.sha256(text.encode()).hexdigest(),
        token_count=max(1, len(text.split())),
        section_path=section_path,
    )


def _seed_vector(store: SqliteStore, chunk_id: str, vec: np.ndarray) -> None:
    meta = ChunkVectorMeta(
        chunk_id=chunk_id,
        model_id=MODEL_ID,
        revision='v0',
        dims=DIMS,
        index_version=RAG_INDEX_VERSION,
    )
    store.upsert_rag_chunk_vectors(meta, encode_vector(vec))


def _vector_index(store: SqliteStore):
    from app.corpus_rag.vector_index import NumpyVectorIndex

    return NumpyVectorIndex(entries=store.list_rag_chunk_vectors(MODEL_ID))


@pytest.fixture()
def mismatch_store(tmp_path: Path) -> SimpleNamespace:
    """Sources A/B/C where B1 is dense-top but lexically invisible.

    Query 'cohesive groups unknown geometry' matches A lexically; B1 shares
    no token with it, so only the dense channel can surface B1.
    """
    store = SqliteStore(str(tmp_path / 'mismatch.db'))
    ids: dict[str, str] = {}
    texts = {
        'A1': 'cohesive subgroup geometry inflates uncertainty estimates',
        'A2': 'unknown number of groups biases cluster validity',
        # Vocab-mismatch hit: zero token overlap with the query.
        'B1': 'clusterability diagnostics reveal tightly bonded community structure',
        'B2': 'modularity optimization pitfalls in sparse graphs',
        'C1': 'spectral embedding dimensionality tradeoffs',
    }
    for key in ('A', 'B', 'C'):
        source = _source(f'repo://docs/{key.lower()}.md')
        store.save_knowledge_source(source)
        ids[key] = source.source_id
    rows = {
        name: _chunk(ids[name[0]], int(name[1]) - 1, text)
        for name, text in texts.items()
    }
    for key in ('A', 'B', 'C'):
        chunks = [rows[n] for n in rows if n.startswith(key)]
        assert store.replace_rag_chunks(ids[key], chunks) == len(chunks)
    # Dense geometry: B1 sits exactly on the query direction (e_0); every
    # other chunk is orthogonal, so B1 is cosine-top deterministically.
    _seed_vector(store, rows['B1'].chunk_id, _unit(DIMS, 0))
    for axis, name in enumerate(('A1', 'A2', 'B2', 'C1'), start=1):
        _seed_vector(store, rows[name].chunk_id, _unit(DIMS, axis))
    return SimpleNamespace(store=store, ids=ids, rows=rows)


# --- S1 core: hybrid fusion recovers the vocabulary-mismatch hit ------------


def test_hybrid_rrf_recovers_vocabulary_mismatch_hit(mismatch_store) -> None:
    ns = mismatch_store
    question = 'cohesive groups unknown geometry'
    retriever = HybridRetriever(
        ns.store,
        vector_index=_vector_index(ns.store),
        embedding_provider=_FixedQueryEmbedding(),
        model_id=MODEL_ID,
    )

    hybrid = retriever.retrieve(
        question, source_ids=None, options=RetrievalOptions(mode='hybrid')
    )
    lexical = retriever.retrieve(
        question, source_ids=None, options=RetrievalOptions(mode='lexical')
    )

    b1_id = ns.rows['B1'].chunk_id
    lexical_ids = [hit.chunk.chunk_id for hit in lexical.hits]
    hybrid_top3 = [hit.chunk.chunk_id for hit in hybrid.hits[:3]]
    assert b1_id not in lexical_ids, 'B1 must be invisible to lexical-only'
    assert b1_id in hybrid_top3, f'B1 not recovered in top-3: {hybrid_top3}'
    assert hybrid.plan.subqueries, 'plan must carry subqueries'
    assert hybrid.plan.original_query == question
    expected_stages = {'lexical', 'dense', 'fuse', 'rerank', 'expand', 'total'}
    assert expected_stages <= set(hybrid.timings)
    assert all(isinstance(v, float) for v in hybrid.timings.values())
    # The dense channel actually ranked B1 first.
    b1_hit = next(h for h in hybrid.hits if h.chunk.chunk_id == b1_id)
    assert b1_hit.dense_rank == 1
    assert b1_hit.lexical_rank is None


def test_source_diversity_cap_three(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / 'diversity.db'))
    dominant = _source('repo://docs/dominant.md')
    other_e = _source('repo://docs/other-e.md')
    other_f = _source('repo://docs/other-f.md')
    chunks_by_source: dict[str, list[RagChunkRecord]] = {}
    for source in (dominant, other_e, other_f):
        store.save_knowledge_source(source)
    chunks_by_source[dominant.source_id] = [
        _chunk(dominant.source_id, i, f'resampling uncertainty estimate variant {i}')
        for i in range(6)
    ]
    chunks_by_source[other_e.source_id] = [
        _chunk(other_e.source_id, 0, 'resampling workflow notes')
    ]
    chunks_by_source[other_f.source_id] = [
        _chunk(other_f.source_id, 0, 'uncertainty propagation notes')
    ]
    for source_id, chunks in chunks_by_source.items():
        store.replace_rag_chunks(source_id, chunks)

    retriever = HybridRetriever(store)
    result = retriever.retrieve(
        'resampling uncertainty',
        source_ids=None,
        options=RetrievalOptions(mode='lexical', k_final=8, candidate_k=40),
    )

    per_source: dict[str, int] = {}
    for hit in result.hits:
        per_source[hit.chunk.source_id] = per_source.get(hit.chunk.source_id, 0) + 1
    assert per_source[dominant.source_id] == MAX_CHUNKS_PER_SOURCE
    capped_available = MAX_CHUNKS_PER_SOURCE + 1 + 1
    assert len(result.hits) == min(8, capped_available)
    assert other_e.source_id in per_source
    assert other_f.source_id in per_source


def test_offline_reranker_flips_order(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / 'rerank.db'))
    source = _source('repo://docs/rerank.md')
    store.save_knowledge_source(source)
    x_chunk = _chunk(source.source_id, 0, 'gradient descent convergence')
    y_chunk = _chunk(source.source_id, 1, 'gradient descent convergence rates')
    z_chunk = _chunk(source.source_id, 2, 'unrelated filler content entirely')
    store.replace_rag_chunks(
        source.source_id, [x_chunk, y_chunk, z_chunk]
    )
    # Dense ranks X first (query direction), Z second (cosine 0.5), Y last.
    q_dir = _unit(DIMS, 0)
    perp = _unit(DIMS, 3)
    z_vec = q_dir * 0.5 + perp * (0.75**0.5)
    _seed_vector(store, x_chunk.chunk_id, q_dir)
    _seed_vector(store, z_chunk.chunk_id, z_vec)
    _seed_vector(store, y_chunk.chunk_id, _unit(DIMS, 7))

    question = 'gradient descent convergence rates'
    retriever = HybridRetriever(
        store,
        vector_index=_vector_index(store),
        embedding_provider=_FixedQueryEmbedding(),
        model_id=MODEL_ID,
    )
    plain = retriever.retrieve(
        question, source_ids=[source.source_id],
        options=RetrievalOptions(mode='hybrid'),
    )
    plain_ids = [h.chunk.chunk_id for h in plain.hits]
    assert plain_ids.index(x_chunk.chunk_id) < plain_ids.index(y_chunk.chunk_id), (
        f'RRF must rank X above Y before reranking: {plain_ids}'
    )

    flipper = HybridRetriever(
        store,
        vector_index=_vector_index(store),
        embedding_provider=_FixedQueryEmbedding(),
        reranker=OfflineReranker(),
        model_id=MODEL_ID,
    )
    flipped = flipper.retrieve(
        question,
        source_ids=[source.source_id],
        options=RetrievalOptions(mode='hybrid+rerank'),
    )
    flipped_ids = [h.chunk.chunk_id for h in flipped.hits]
    assert flipped_ids.index(y_chunk.chunk_id) < flipped_ids.index(x_chunk.chunk_id), (
        f'rerank must put Y above X: {flipped_ids}'
    )
    by_id = {h.chunk.chunk_id: h for h in flipped.hits}
    y_score = by_id[y_chunk.chunk_id].rerank_score
    x_score = by_id[x_chunk.chunk_id].rerank_score
    assert y_score is not None and x_score is not None
    assert y_score > x_score


def test_neighbor_expansion_appends_section_sibling(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / 'expand.db'))
    source = _source('repo://docs/expand.md')
    store.save_knowledge_source(source)
    ev0 = _chunk(
        source.source_id, 0, 'first span text alpha',
        section_path='2.1',
    )
    ev1 = _chunk(
        source.source_id, 1, 'second span text beta',
        section_path='2.1',
    )
    unit = _chunk(
        source.source_id, 2, 'section two overview',
        kind='section_unit', section_path='2.1',
    )
    store.replace_rag_chunks(source.source_id, [ev0, ev1, unit])

    retriever = HybridRetriever(store)
    options = RetrievalOptions(mode='lexical', k_final=1, expand=True)
    result = retriever.retrieve('beta specifics', source_ids=None, options=options)

    assert [h.chunk.chunk_id for h in result.hits] == [ev1.chunk_id, ev0.chunk_id]
    assert 'expanded' not in result.hits[0].stage_scores
    assert result.hits[1].stage_scores.get('expanded') == 1.0

    plain = retriever.retrieve(
        'beta specifics',
        source_ids=None,
        options=RetrievalOptions(mode='lexical', k_final=1),
    )
    assert [h.chunk.chunk_id for h in plain.hits] == [ev1.chunk_id]


def test_token_budget_drops_whole_entries(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / 'budget.db'))
    source = _source('repo://docs/budget.md')
    store.save_knowledge_source(source)
    ten_words = 'resampling alpha bravo charlie delta echo foxtrot golf hotel india'
    five_words = 'resampling methods gamma epsilon zeta'
    two_words = 'resampling theta'
    t1 = _chunk(source.source_id, 0, ten_words)
    t2 = _chunk(source.source_id, 1, five_words)
    t3 = _chunk(source.source_id, 2, two_words)
    store.replace_rag_chunks(source.source_id, [t1, t2, t3])

    scripted = _ScriptedReranker({ten_words: 30.0, five_words: 20.0, two_words: 10.0})
    retriever = HybridRetriever(store, reranker=scripted, model_id=MODEL_ID)
    result = retriever.retrieve(
        'resampling methods',
        source_ids=[source.source_id],
        options=RetrievalOptions(mode='hybrid+rerank', token_budget=12),
    )

    assert len(result.hits) == 2
    emitted = {h.chunk.text for h in result.hits}
    assert emitted == {ten_words, two_words}, 'middle entry dropped whole'
    assert five_words not in emitted
    total_words = sum(len(h.chunk.text.split()) for h in result.hits)
    assert total_words <= 12
    for hit in result.hits:
        assert hit.chunk.text in (ten_words, two_words)  # intact, never truncated


# --- S1/S2 CLI surface -------------------------------------------------------


def test_ask_cli_json_citations_resolve(mismatch_store, tmp_path: Path, capsys) -> None:
    ns = mismatch_store
    store_path = tmp_path / 'mismatch.db'
    out_path = tmp_path / 'answer.json'
    rc = ask.main(
        [
            '--store', str(store_path),
            '--question', 'cohesive groups unknown geometry',
            '--mode', 'hybrid',
            '--json-out', str(out_path),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['question'] == 'cohesive groups unknown geometry'
    assert payload['mode'] == 'hybrid'
    assert payload['hits'], 'expected nonempty hits'
    assert payload['plan']['original_query'] == 'cohesive groups unknown geometry'
    assert payload['plan']['subqueries']
    stored_ids = {row['chunk_id'] for row in ns.store.list_rag_chunks()}
    assert payload['citations'], 'expected citations for final hits'
    for citation in payload['citations']:
        assert citation['chunk_id'] in stored_ids
        assert citation['evidence_uri'].startswith('knowledge://')
    written = json.loads(out_path.read_text())
    assert written['hits'] == payload['hits']


def test_ask_cli_insufficient_corpus_exit_zero(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / 'empty.db'
    SqliteStore(str(store_path))  # fresh empty store
    json_out = tmp_path / 'insufficient.json'
    rc = ask.main(
        [
            '--store', str(store_path),
            '--question', 'cohesive groups unknown geometry',
            '--mode', 'hybrid',
            '--corpus', 'missing',
            '--json-out', str(json_out),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['kind'] == 'insufficient_evidence'
    assert payload['reason']
    # The --json-out contract holds on EVERY exit path, including insufficiency.
    assert json_out.exists()
    assert json.loads(json_out.read_text())['kind'] == 'insufficient_evidence'
