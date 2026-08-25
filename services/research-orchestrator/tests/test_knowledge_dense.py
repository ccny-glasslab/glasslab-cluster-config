"""Production dense retrieval over existing knowledge chunks.

Covers app/knowledge_dense.py: idempotent build/rebuild over the canonical
knowledge_chunks namespace, allowlist-respecting cosine search, dimension
mismatch failing cleanly (never silently mixing lineages), and readiness
reporting. PostgreSQL twins run when CORPUS_RAG_PG_DSN is configured
(dev container: scripts/corpus_rag/dev_pg.sh).
"""

from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import numpy as np
import pytest

from app.corpus_rag.contracts import ChunkVectorMeta
from app.corpus_rag.embeddings import OfflineDeterministicEmbedding
from app.knowledge_dense import NumpyChunkIndex, build_dense_index
from app.postgres_store import PostgresStore
from app.schemas import KnowledgeChunk, KnowledgeSource, SourceType
from app.storage import SqliteStore

PG_DSN = os.environ.get('CORPUS_RAG_PG_DSN')
MODEL_ID = 'dense-test-model'


def _source(uri: str) -> KnowledgeSource:
    return KnowledgeSource(
        source_type=SourceType.PAPER,
        canonical_uri=uri,
        digest=uuid4().hex + uuid4().hex,
    )


def _seed_chunks(store, texts: list[str]) -> list[str]:
    source = _source(f'repo://dense/{uuid4().hex[:8]}.md')
    store.save_knowledge_source(source)
    chunks = [
        KnowledgeChunk(
            source_id=source.source_id,
            chunk_index=index,
            text=text,
            digest=hashlib.sha256(text.encode()).hexdigest(),
            token_count=max(1, len(text.split())),
        )
        for index, text in enumerate(texts)
    ]
    store.replace_knowledge_chunks(source.source_id, chunks)
    return [chunk.chunk_id for chunk in chunks]


def _unit(values: list[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float32)
    return arr / np.linalg.norm(arr)


def _truncate_rag_tables(store: PostgresStore) -> None:
    # The dev Postgres database persists across runs; reset state so PG twins
    # get the same blank-slate isolation as tmp_path SQLite stores.
    with store._connect() as conn:
        conn.execute(
            'TRUNCATE orchestrator_rag_chunk_vectors, orchestrator_rag_chunks,'
            ' orchestrator_rag_sections, orchestrator_rag_documents,'
            ' orchestrator_rag_corpus_sources, orchestrator_rag_corpora,'
            ' orchestrator_knowledge_chunk_vectors, orchestrator_knowledge_chunks,'
            ' orchestrator_knowledge_sources RESTART IDENTITY CASCADE'
        )


@pytest.fixture()
def pg_store():
    if not PG_DSN:
        pytest.skip('CORPUS_RAG_PG_DSN is not configured')
    pg_store = PostgresStore(PG_DSN)
    _truncate_rag_tables(pg_store)
    return pg_store


@pytest.fixture(params=['sqlite', 'postgres'])
def store(request: pytest.FixtureRequest, tmp_path):
    if request.param == 'sqlite':
        return SqliteStore(str(tmp_path / 'dense.db'))
    return request.getfixturevalue('pg_store')


def test_build_is_idempotent_and_ready(store) -> None:
    ids = _seed_chunks(store, [
        'bootstrap resampling estimates uncertainty in clustering',
        'cross validation checks predictive stability of models',
        'quantile regression relaxes distributional assumptions',
    ])
    provider = OfflineDeterministicEmbedding(dims=8)

    first = build_dense_index(store, provider, model_id=MODEL_ID)
    assert first['n_vectors'] == 3
    assert first['skipped'] == 0

    second = build_dense_index(store, provider, model_id=MODEL_ID)
    assert second['n_vectors'] == 0

    index = NumpyChunkIndex(store, provider, model_id=MODEL_ID)
    readiness = index.readiness()
    assert readiness.available is True
    assert readiness.indexed_count == 3
    assert readiness.model_id == MODEL_ID

    hits = index.search(
        index.embed_query('bootstrap resampling uncertainty'),
        allowed_chunk_ids=set(ids),
        k=3,
    )
    assert {chunk_id for chunk_id, _ in hits} == set(ids)


def test_search_honors_allowlist_and_cosine_order(store) -> None:
    dim = 8
    provider = OfflineDeterministicEmbedding(dims=dim)
    ids = _seed_chunks(store, ['alpha passage text', 'beta passage text'])
    vec_a = _unit([1, 0, 0, 0, 0, 0, 0, 0])
    vec_b = _unit([0, 1, 0, 0, 0, 0, 0, 0])
    for cid, vec in ((ids[0], vec_a), (ids[1], vec_b)):
        meta = ChunkVectorMeta(
            chunk_id=cid, model_id=MODEL_ID, revision='r1',
            dims=dim, index_version='v1',
        )
        store.upsert_knowledge_chunk_vectors(meta, vec.astype('<f4').tobytes())

    index = NumpyChunkIndex(store, provider, model_id=MODEL_ID)
    hits = index.search(
        _unit([0.9, 0.1, 0, 0, 0, 0, 0, 0]),
        allowed_chunk_ids=set(ids),
        k=2,
    )
    assert [cid for cid, _ in hits] == [ids[0], ids[1]]
    assert hits[0][1] > 0.9


def test_dimension_mismatch_fails_cleanly(store) -> None:
    _seed_chunks(store, ['some cluster stability text'])
    build_dense_index(store, OfflineDeterministicEmbedding(dims=8))

    stale = NumpyChunkIndex(store, OfflineDeterministicEmbedding(dims=4))
    readiness = stale.readiness()
    assert readiness.available is False
    assert 'dimension' in readiness.reason.lower()
    assert stale.search(
        stale.embed_query('anything'), allowed_chunk_ids=None, k=3
    ) == []


def test_rebuild_keeps_single_lineage_per_chunk(store) -> None:
    ids = _seed_chunks(store, ['stability text'])
    provider = OfflineDeterministicEmbedding(dims=8)
    build_dense_index(store, provider, model_id=MODEL_ID)

    build_dense_index(store, provider, model_id=MODEL_ID, force=True)

    rows = store.list_knowledge_chunk_vectors(MODEL_ID)
    assert {meta.chunk_id for meta, _ in rows} == set(ids)
    assert len(rows) == len(ids)


@pytest.mark.skipif(not PG_DSN, reason='CORPUS_RAG_PG_DSN not configured')
def test_pg_backend_roundtrip_and_order(pg_store) -> None:
    from app.knowledge_dense import PgVectorChunkIndex

    ids = _seed_chunks(pg_store, [
        'consensus matrices across bootstrap resamples',
        'unrelated passage about quantile regression',
    ])
    provider = OfflineDeterministicEmbedding(dims=8)
    build_dense_index(pg_store, provider)

    index = PgVectorChunkIndex(pg_store, provider)
    hits = index.search(
        index.embed_query('consensus resampling'),
        allowed_chunk_ids=set(ids),
        k=2,
    )
    assert hits[0][0] == ids[0]
    assert all(cid in ids for cid, _ in hits)
