"""Vector-index backends for corpus-RAG dense retrieval (numpy + pgvector).

``NumpyVectorIndex`` is the dependency-free reference backend;
``PgVectorIndex`` exercises the real HNSW index over
``orchestrator_rag_chunk_vectors.embedding``. The Postgres tests are gated on
``CORPUS_RAG_PG_DSN`` and reuse the truncate-isolation fixture pattern from
``test_corpus_rag_storage.py``.
"""

from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import numpy as np
import pytest

from app.corpus_rag import ChunkVectorMeta, RagChunkRecord
from app.corpus_rag.embeddings import decode_vector, encode_vector
from app.corpus_rag.vector_index import (
    NumpyVectorIndex,
    PgVectorIndex,
    VectorIndex,
    open_vector_index,
)
from app.postgres_store import PostgresStore
from app.schemas import KnowledgeSource, SourceType


PG_DSN = os.environ.get('CORPUS_RAG_PG_DSN')
MODEL_ID = 'vector-index-test-model'
REVISION = 'r1'


def _meta(chunk_id: str, dims: int) -> ChunkVectorMeta:
    return ChunkVectorMeta(
        chunk_id=chunk_id,
        model_id=MODEL_ID,
        revision=REVISION,
        dims=dims,
    )


def _reference_topk(
    ids: list[str], vectors: list[np.ndarray], query: np.ndarray, k: int
) -> list[tuple[str, float]]:
    """Brute-force cosine reference sorted by (-score, chunk_id)."""
    q = np.asarray(query, dtype=np.float32)
    q = q / np.linalg.norm(q)
    scored = []
    for cid, vec in zip(ids, vectors):
        v = np.asarray(vec, dtype=np.float32)
        v = v / np.linalg.norm(v)
        scored.append((cid, float(np.dot(q, v))))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:k]


# --- numpy backend -----------------------------------------------------------


def test_numpy_topk_matches_reference() -> None:
    rng = np.random.default_rng(42)
    dim, n_chunks, n_queries = 16, 200, 50
    vectors = rng.standard_normal((n_chunks, dim)).astype(np.float32)
    ids = [f'chunk-{i:04d}' for i in range(n_chunks)]

    index = NumpyVectorIndex()
    assert isinstance(index, VectorIndex)  # runtime_checkable protocol
    for cid, vec in zip(ids, vectors):
        index.add(_meta(cid, dim), vec)

    queries = rng.standard_normal((n_queries, dim)).astype(np.float32)
    for qi in range(n_queries):
        expected = _reference_topk(ids, vectors, queries[qi], 5)
        got = index.search(queries[qi], k=5)
        assert [cid for cid, _ in got] == [cid for cid, _ in expected]
        scores = [score for _, score in got]
        assert scores == sorted(scores, reverse=True)

    # Constructor-from-bytes path must agree with the add() path.
    from_bytes = NumpyVectorIndex(
        (_meta(cid, dim), encode_vector(vec)) for cid, vec in zip(ids, vectors)
    )
    assert from_bytes.search(queries[0], k=5) == index.search(queries[0], k=5)

    # Duplicate chunk_id add replaces the stored row (not a second entry).
    replacement = rng.standard_normal(dim).astype(np.float32)
    index.add(_meta(ids[0], dim), replacement)
    replaced_vectors = list(vectors)
    replaced_vectors[0] = replacement
    expected = _reference_topk(ids, replaced_vectors, queries[0], 5)
    got = index.search(queries[0], k=5)
    assert [cid for cid, _ in got] == [cid for cid, _ in expected]

    self_hits = index.search(replacement, k=3)
    assert self_hits[0][0] == ids[0]
    assert self_hits[0][1] == pytest.approx(1.0, abs=1e-5)


def test_numpy_source_map_for_plain_chunk_ids() -> None:
    """Production chunk ids are bare hex without any delimiter, so filtered
    search must honor an explicit chunk_id -> source_id mapping."""
    rng = np.random.default_rng(11)
    dim = 8
    source_of: dict[str, str] = {}
    index = NumpyVectorIndex()
    for i in range(6):
        cid = uuid4().hex
        source_of[cid] = 'src-a' if i < 3 else 'src-b'
        index.add(_meta(cid, dim), rng.standard_normal(dim).astype(np.float32))
    query = rng.standard_normal(dim).astype(np.float32)

    mapped = NumpyVectorIndex(source_of=source_of)
    for cid in source_of:
        mapped.add(_meta(cid, dim), index._rows[cid])

    filtered = mapped.search(query, k=10, source_ids=['src-b'])
    assert {cid for cid, _ in filtered} == {
        cid for cid, src in source_of.items() if src == 'src-b'
    }
    # Unfiltered search still sees everything.
    assert len(mapped.search(query, k=10)) == 6


def test_numpy_source_filter() -> None:
    rng = np.random.default_rng(7)
    dim, per_source = 8, 6
    sources = ['src-alpha', 'src-beta', 'src-gamma']

    index = NumpyVectorIndex()
    vecs_by_source: dict[str, list[np.ndarray]] = {}
    ids_by_source: dict[str, list[str]] = {}
    for src in sources:
        vecs = rng.standard_normal((per_source, dim)).astype(np.float32)
        vecs_by_source[src] = list(vecs)
        ids_by_source[src] = [
            f'{src}::{src}-c{i}' for i in range(per_source)
        ]
        for cid, vec in zip(ids_by_source[src], vecs):
            index.add(_meta(cid, dim), vec)

    query = rng.standard_normal(dim).astype(np.float32)

    unfiltered = index.search(query, k=100)
    assert {cid.split('::')[0] for cid, _ in unfiltered} == set(sources)

    only_alpha = index.search(query, k=100, source_ids=['src-alpha'])
    assert {cid.split('::')[0] for cid, _ in only_alpha} == {'src-alpha'}
    expected = _reference_topk(
        ids_by_source['src-alpha'], vecs_by_source['src-alpha'], query, 100
    )
    assert [cid for cid, _ in only_alpha] == [cid for cid, _ in expected]

    two = index.search(query, k=100, source_ids=['src-beta', 'src-gamma'])
    assert {cid.split('::')[0] for cid, _ in two} == {'src-beta', 'src-gamma'}
    assert len(two) == 2 * per_source


def test_open_vector_index_factory() -> None:
    assert isinstance(open_vector_index('numpy'), NumpyVectorIndex)
    with pytest.raises(ValueError):
        open_vector_index('faiss')


# --- pgvector HNSW backend (skip unless CORPUS_RAG_PG_DSN is configured) -----


def _source(uri: str) -> KnowledgeSource:
    return KnowledgeSource(
        source_type=SourceType.DOCUMENTATION,
        canonical_uri=uri,
        digest=uuid4().hex + uuid4().hex,
    )


def _chunk(source_id: str, index: int, text: str) -> RagChunkRecord:
    return RagChunkRecord(
        source_id=source_id,
        kind='evidence_span',
        chunk_index=index,
        text=text,
        digest=hashlib.sha256(text.encode()).hexdigest(),
        token_count=max(1, len(text.split())),
    )


def _truncate_rag_tables(store: PostgresStore) -> None:
    # The dev Postgres database persists across test runs; reset prototype
    # state so PG twins get blank-slate isolation (pattern copied from
    # test_corpus_rag_storage.py).
    with store._connect() as conn:
        conn.execute(
            'TRUNCATE orchestrator_rag_chunk_vectors, orchestrator_rag_chunks, '
            'orchestrator_rag_sections, orchestrator_rag_documents, '
            'orchestrator_rag_corpus_sources, orchestrator_rag_corpora, '
            'orchestrator_knowledge_chunks, orchestrator_knowledge_sources '
            'RESTART IDENTITY CASCADE'
        )


@pytest.fixture()
def pg_store():
    if not PG_DSN:
        pytest.skip('CORPUS_RAG_PG_DSN is not configured')
    store = PostgresStore(PG_DSN)
    _truncate_rag_tables(store)
    return store


def _seed_pg_corpus(
    store: PostgresStore, n_sources: int, chunks_per_source: int
) -> tuple[list[str], list[KnowledgeSource]]:
    """Seed knowledge sources + chunks; return (chunk_ids, sources) in order."""
    source_ids: list[str] = []
    seeded_sources: list[KnowledgeSource] = []
    for si in range(n_sources):
        source = _source(f'repo://vec/src-{si}.md')
        store.save_knowledge_source(source)
        chunks = [
            _chunk(source.source_id, j, f'vector corpus chunk {si}-{j}')
            for j in range(chunks_per_source)
        ]
        store.replace_rag_chunks(source.source_id, chunks)
        source_ids.extend(chunk.chunk_id for chunk in chunks)
        seeded_sources.append(source)
    return source_ids, seeded_sources


def test_pg_hnsw_matches_brute_force(pg_store) -> None:
    rng = np.random.default_rng(1234)
    dim, n_sources, chunks_per_source, n_queries = 8, 4, 16, 10
    n_chunks = n_sources * chunks_per_source

    ids = _seed_pg_corpus(pg_store, n_sources, chunks_per_source)[0]
    vectors = rng.standard_normal((n_chunks, dim)).astype(np.float32)

    index = PgVectorIndex(PG_DSN, MODEL_ID)
    for cid, vec in zip(ids, vectors):
        index.add(_meta(cid, dim), vec)

    numpy_index = NumpyVectorIndex(
        (_meta(cid, dim), encode_vector(vec)) for cid, vec in zip(ids, vectors)
    )

    queries = rng.standard_normal((n_queries, dim)).astype(np.float32)
    recalls = []
    for qi in range(n_queries):
        truth = {cid for cid, _ in numpy_index.search(queries[qi], k=5)}
        got = {cid for cid, _ in index.search(queries[qi], k=5)}
        recalls.append(len(truth & got) / len(truth))
    assert min(recalls) >= 0.8
    assert sum(recalls) / len(recalls) >= 0.9


def test_pg_hnsw_source_filter(pg_store) -> None:
    rng = np.random.default_rng(99)
    dim, chunks_per_source = 8, 8

    first_ids, first_sources = _seed_pg_corpus(pg_store, 1, chunks_per_source)
    second_ids, _ = _seed_pg_corpus(pg_store, 1, chunks_per_source)
    first_source_id = first_sources[0].source_id

    vectors = rng.standard_normal((2 * chunks_per_source, dim)).astype(np.float32)
    index = PgVectorIndex(PG_DSN, MODEL_ID)
    for cid, vec in zip([*first_ids, *second_ids], vectors):
        index.add(_meta(cid, dim), vec)

    query = rng.standard_normal(dim).astype(np.float32)
    hits = index.search(query, k=50, source_ids=[first_source_id])
    returned = {cid for cid, _ in hits}
    assert returned == set(first_ids)
    scores = [score for _, score in hits]
    assert scores == sorted(scores, reverse=True)

    unfiltered = index.search(query, k=50)
    assert {cid for cid, _ in unfiltered} == {*first_ids, *second_ids}
