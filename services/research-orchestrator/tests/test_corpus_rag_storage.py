"""Corpus-RAG prototype storage contract: additive tables on both backends.

The frozen schema shapes live in ``app/corpus_rag/contracts.py``; these tests
pin the durable-store surface (tables, transactional FTS maintenance, corpus
membership, and vector metadata) for SQLite and, when CORPUS_RAG_PG_DSN is
configured, PostgreSQL. Behavioral parity between backends is asserted by
running the same scenario helpers against each store.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from app.corpus_rag import (
    RAG_INDEX_VERSION,
    ChunkVectorMeta,
    CorpusRecord,
    RagChunkRecord,
    RagDocumentRecord,
    RagSectionRecord,
)
from app.postgres_store import PostgresStore
from app.schemas import KnowledgeSource, SourceType
from app.storage import SqliteStore


PG_DSN = os.environ.get('CORPUS_RAG_PG_DSN')

RAG_TABLES = (
    'rag_corpora',
    'rag_corpus_sources',
    'rag_documents',
    'rag_sections',
    'rag_chunks',
    'rag_chunks_fts',
    'rag_chunk_vectors',
)

# Postgres keeps lexical search as a GIN index over the chunks table rather
# than a physical FTS table, mirroring orchestrator_knowledge_chunks.
PG_RAG_TABLES = tuple(
    f'orchestrator_{name}' for name in RAG_TABLES if name != 'rag_chunks_fts'
)


def _id(prefix: str) -> str:
    return f'{prefix}-{uuid4().hex}'


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
    # state so PG twins get the same blank-slate isolation as tmp_path SQLite.
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


@pytest.fixture(params=['sqlite', 'postgres'])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == 'sqlite':
        return SqliteStore(str(tmp_path / 'rag.db'))
    return request.getfixturevalue('pg_store')


# --- shared scenario bodies (run against both backends) ---------------------


def _scenario_fts_roundtrip(store) -> None:
    source = _source('repo://docs/bootstrap.md')
    store.save_knowledge_source(source)
    chunk_a = _chunk(source.source_id, 0, 'bootstrap resampling estimates uncertainty')
    chunk_b = _chunk(source.source_id, 1, 'gradient descent converges slowly')
    assert store.replace_rag_chunks(source.source_id, [chunk_a, chunk_b]) == 2
    hits = store.search_rag_chunks_fts(
        'resampling', source_ids=[source.source_id], limit=5
    )
    assert [hit['chunk_id'] for hit in hits] == [chunk_a.chunk_id]
    assert hits[0]['text'] == chunk_a.text
    assert hits[0]['kind'] == 'evidence_span'
    assert hits[0]['index_version'] == RAG_INDEX_VERSION
    assert isinstance(hits[0]['rank'], float)

    # Replacing without the term must remove it from the index in the same
    # transaction as the row replacement.
    replacement = _chunk(source.source_id, 0, 'cross validation variance')
    assert store.replace_rag_chunks(source.source_id, [replacement]) == 1
    assert (
        store.search_rag_chunks_fts(
            'resampling', source_ids=[source.source_id], limit=5
        )
        == []
    )

    # Multi-term OR behavior: term A in one chunk, term B in another.
    other = _source('repo://docs/or-terms.md')
    store.save_knowledge_source(other)
    term_a = _chunk(other.source_id, 0, 'alpha kernel smoothing')
    term_b = _chunk(other.source_id, 1, 'beta matrix factorization')
    store.replace_rag_chunks(other.source_id, [term_a, term_b])
    both = store.search_rag_chunks_fts(
        'alpha beta', source_ids=[other.source_id], limit=5
    )
    assert {hit['chunk_id'] for hit in both} == {term_a.chunk_id, term_b.chunk_id}


def _scenario_corpus_membership_and_documents(store) -> None:
    corpus = store.create_corpus(
        CorpusRecord(slug=f'corpus-{uuid4().hex[:8]}', title='Prototype corpus')
    )
    fetched = store.get_corpus(corpus.slug)
    assert fetched is not None
    assert fetched.corpus_id == corpus.corpus_id
    assert fetched.title == 'Prototype corpus'
    assert store.get_corpus(_id('missing-slug')) is None
    assert any(c.corpus_id == corpus.corpus_id for c in store.list_corpora())
    assert store.list_corpus_sources(corpus.corpus_id) == []

    source = _source('repo://docs/member.md')
    store.save_knowledge_source(source)
    assert store.add_corpus_source(corpus.corpus_id, source.source_id) is True
    assert store.add_corpus_source(corpus.corpus_id, source.source_id) is False
    assert store.list_corpus_sources(corpus.corpus_id) == [source.source_id]

    document = RagDocumentRecord(
        source_id=source.source_id,
        doc_type='reference',
        title='Member doc',
        extraction_version='v1',
    )
    stored = store.upsert_rag_document(document)
    again = store.upsert_rag_document(stored)
    assert again.doc_id == stored.doc_id

    sections = [
        RagSectionRecord(doc_id=document.doc_id, path='1', level=1, title='Top'),
        RagSectionRecord(
            doc_id=document.doc_id, path='1.1', level=2, title='Nested'
        ),
    ]
    assert store.replace_rag_sections(document.doc_id, sections) == 2

    chunks = [
        _chunk(source.source_id, 0, 'membership evidence alpha'),
        _chunk(source.source_id, 1, 'second unit'),
    ]
    assert store.replace_rag_chunks(source.source_id, chunks) == 2

    meta = ChunkVectorMeta(
        chunk_id=chunks[0].chunk_id,
        model_id='test-model',
        revision='r1',
        dims=4,
        index_version=RAG_INDEX_VERSION,
    )
    store.upsert_rag_chunk_vectors(meta, b'\x00\x01\x02\x03')
    store.upsert_rag_chunk_vectors(meta, b'\x09\x08\x07\x06')
    vectors = store.list_rag_chunk_vectors('test-model')
    assert len(vectors) == 1
    vector_meta, blob = vectors[0]
    assert vector_meta.chunk_id == chunks[0].chunk_id
    assert vector_meta.model_id == 'test-model'
    assert vector_meta.dims == 4
    assert blob == b'\x09\x08\x07\x06'
    assert store.list_rag_chunk_vectors('other-model') == []


def _scenario_source_filter(store) -> None:
    first = _source('repo://docs/filter-a.md')
    second = _source('repo://docs/filter-b.md')
    for source in (first, second):
        store.save_knowledge_source(source)
        store.replace_rag_chunks(
            source.source_id,
            [_chunk(source.source_id, 0, 'quantile normalization workflow')],
        )
    filtered = store.search_rag_chunks_fts(
        'normalization', source_ids=[first.source_id], limit=10
    )
    assert {hit['source_id'] for hit in filtered} == {first.source_id}
    unfiltered = store.search_rag_chunks_fts(
        'normalization', source_ids=None, limit=10
    )
    assert {hit['source_id'] for hit in unfiltered} == {
        first.source_id,
        second.source_id,
    }


# --- backend-neutral tests ---------------------------------------------------


def test_replace_rag_chunks_fts_roundtrip(store) -> None:
    _scenario_fts_roundtrip(store)


def test_corpus_membership_and_documents_roundtrip(store) -> None:
    _scenario_corpus_membership_and_documents(store)


def test_search_rag_chunks_fts_respects_source_filter(store) -> None:
    _scenario_source_filter(store)


def test_list_rag_chunks_filters_by_kind_and_limit(store) -> None:
    source = _source('repo://docs/kinds.md')
    store.save_knowledge_source(source)
    evidence = _chunk(source.source_id, 0, 'evidence span text')
    unit = RagChunkRecord(
        source_id=source.source_id,
        kind='section_unit',
        chunk_index=1,
        text='section unit text',
        digest='a' * 64,
        token_count=3,
    )
    store.replace_rag_chunks(source.source_id, [evidence, unit])
    everything = store.list_rag_chunks(source_ids=[source.source_id], kinds=None, limit=None)
    assert [row['chunk_id'] for row in everything] == [evidence.chunk_id, unit.chunk_id]
    units = store.list_rag_chunks(
        source_ids=[source.source_id], kinds=['section_unit'], limit=None
    )
    assert [row['chunk_id'] for row in units] == [unit.chunk_id]
    limited = store.list_rag_chunks(source_ids=None, kinds=None, limit=1)
    assert len(limited) == 1


# --- SQLite-only schema assertions -------------------------------------------


def test_rag_tables_exist_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / 'schema.db'
    SqliteStore(str(database_path))
    with sqlite3.connect(str(database_path)) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    missing = [name for name in RAG_TABLES if name not in names]
    assert missing == []


# --- PostgreSQL twins (skip unless CORPUS_RAG_PG_DSN is configured) ----------


def test_pg_rag_tables_exist(pg_store) -> None:
    with pg_store._connect() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        ).fetchall()
        indexes = conn.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'orchestrator_rag_chunks'"
        ).fetchall()
    names = {row['table_name'] for row in rows}
    missing = [name for name in PG_RAG_TABLES if name not in names]
    assert missing == []
    assert any('fts_idx' in row['indexname'] for row in indexes)


def test_pg_replace_rag_chunks_fts_roundtrip(pg_store) -> None:
    _scenario_fts_roundtrip(pg_store)


def test_pg_corpus_membership_and_documents_roundtrip(pg_store) -> None:
    _scenario_corpus_membership_and_documents(pg_store)


def test_pg_search_rag_chunks_fts_respects_source_filter(pg_store) -> None:
    _scenario_source_filter(pg_store)

def test_pg_list_rag_chunks_filters_by_kind_and_limit(pg_store) -> None:
    source = _source('repo://docs/kinds.md')
    pg_store.save_knowledge_source(source)
    evidence = _chunk(source.source_id, 0, 'evidence span text')
    unit = RagChunkRecord(
        source_id=source.source_id,
        kind='section_unit',
        chunk_index=1,
        text='section unit text',
        digest='a' * 64,
        token_count=3,
    )
    pg_store.replace_rag_chunks(source.source_id, [evidence, unit])
    units = pg_store.list_rag_chunks(
        source_ids=[source.source_id], kinds=['section_unit'], limit=None
    )
    assert [row['chunk_id'] for row in units] == [unit.chunk_id]
    limited = pg_store.list_rag_chunks(source_ids=[source.source_id], kinds=None, limit=1)
    assert len(limited) == 1
