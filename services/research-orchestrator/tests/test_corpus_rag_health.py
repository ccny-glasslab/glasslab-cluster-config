"""Corpus-RAG dense readiness on ``/health`` (issue #683).

Covers the store aggregate (both backends via the shared ``store`` fixture
shape), the readiness block for a healthy index, an empty index, and chunk-less
growth over the alert threshold, plus the liveness-safe caching and
transition-only warning in ``CorpusRagHealth``.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.corpus_rag import RAG_INDEX_VERSION, ChunkVectorMeta, RagChunkRecord
from app.corpus_rag.dense import rag_dense_readiness
from app.main import CorpusRagHealth, create_app
from app.postgres_store import PostgresStore
from app.schemas import KnowledgeSource, SourceType
from app.storage import SqliteStore

PG_DSN = os.environ.get('CORPUS_RAG_PG_DSN')
MODEL = 'model-a'
REVISION = 'r1'
DIMS = 4


@pytest.fixture(params=['sqlite', 'postgres'])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == 'sqlite':
        return SqliteStore(str(tmp_path / 'rag.db'))
    if not PG_DSN:
        pytest.skip('CORPUS_RAG_PG_DSN is not configured')
    pg = PostgresStore(PG_DSN)
    with pg._connect() as conn:
        conn.execute(
            'TRUNCATE orchestrator_rag_chunk_vectors, orchestrator_rag_chunks, '
            'orchestrator_knowledge_sources CASCADE'
        )
        conn.commit()
    return pg


def _add_source(store, *, chunks: int, kind: str = 'evidence_span') -> list[str]:
    source = KnowledgeSource(
        source_type=SourceType.DOCUMENTATION,
        canonical_uri=f'repo://docs/{uuid4().hex}.md',
        digest=uuid4().hex + uuid4().hex,
    )
    store.save_knowledge_source(source)
    records = [
        RagChunkRecord(
            source_id=source.source_id,
            kind=kind,
            chunk_index=index,
            text=f'chunk {index} of {source.source_id}',
            digest=hashlib.sha256(f'{source.source_id}{index}'.encode()).hexdigest(),
            token_count=4,
        )
        for index in range(chunks)
    ]
    if records:
        store.replace_rag_chunks(source.source_id, records)
    return [record.chunk_id for record in records]


def _add_vector(store, chunk_id: str, *, dims: int = DIMS,
                revision: str = REVISION, model_id: str = MODEL) -> None:
    store.upsert_rag_chunk_vectors(
        ChunkVectorMeta(
            chunk_id=chunk_id,
            model_id=model_id,
            revision=revision,
            dims=dims,
            index_version=RAG_INDEX_VERSION,
        ),
        b'\x00' * (dims * 4),
    )


def _readiness(store, *, threshold: int = 5, dims: int = DIMS,
               revision: str = REVISION):
    return rag_dense_readiness(
        store,
        model_id=MODEL,
        dims=dims,
        revision=revision,
        chunkless_threshold=threshold,
    )


def test_healthy_rag_index(store) -> None:
    chunk_ids = _add_source(store, chunks=3)
    _add_source(store, chunks=1, kind='section_unit')
    for chunk_id in chunk_ids:
        _add_vector(store, chunk_id)
    # A stale-lineage row and another model's row must not count as usable.
    stale = _add_source(store, chunks=1)
    _add_vector(store, stale[0], revision='r0')
    other = _add_source(store, chunks=1)
    _add_vector(store, other[0], model_id='model-b')

    block = _readiness(store)

    assert block['available'] is True
    assert block['indexed_vectors'] == 3
    assert block['evidence_spans'] == 5
    assert block['unindexed_evidence_spans'] == 2
    assert block['sources'] == 4
    assert block['chunkless_sources'] == 0
    assert block['chunkless_over_threshold'] is False
    assert '1 row(s) ignored' in block['reason']
    assert (block['model_id'], block['revision'], block['dims']) == (
        MODEL, REVISION, DIMS
    )


def test_unresolved_lineage_is_not_filtered(store) -> None:
    chunk_ids = _add_source(store, chunks=2)
    _add_vector(store, chunk_ids[0], dims=8, revision='r9')
    _add_vector(store, chunk_ids[1])

    block = _readiness(store, dims=0, revision='')

    assert block['indexed_vectors'] == 2
    assert block['reason'] == ''


def test_empty_rag_index(store) -> None:
    _add_source(store, chunks=2)

    block = _readiness(store)

    assert block['available'] is False
    assert block['indexed_vectors'] == 0
    assert block['evidence_spans'] == 2
    assert block['unindexed_evidence_spans'] == 2
    assert "no usable rag vectors for model 'model-a'" == block['reason']


def test_chunkless_growth_over_threshold(store) -> None:
    _add_source(store, chunks=1)
    for _ in range(2):
        _add_source(store, chunks=0)
    assert _readiness(store, threshold=2)['chunkless_over_threshold'] is False

    _add_source(store, chunks=0)
    block = _readiness(store, threshold=2)

    assert block['chunkless_sources'] == 3
    assert block['chunkless_threshold'] == 2
    assert block['chunkless_over_threshold'] is True


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _health(store, tmp_path: Path, clock: _Clock, **overrides) -> CorpusRagHealth:
    settings = Settings(
        database_path=str(tmp_path / 'settings.db'),
        knowledge_embedding_model='offline-deterministic',
        knowledge_embedding_revision='',
        **overrides,
    )
    return CorpusRagHealth(store, settings, clock=clock)


def test_health_block_is_cached_until_ttl(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / 'rag.db'))
    clock = _Clock()
    health = _health(store, tmp_path, clock, corpus_rag_health_cache_seconds=60)

    assert health.snapshot()['sources'] == 0
    _add_source(store, chunks=1)
    clock.now += 59
    assert health.snapshot()['sources'] == 0
    clock.now += 1
    assert health.snapshot()['sources'] == 1


def test_health_block_warns_once_per_transition(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = SqliteStore(str(tmp_path / 'rag.db'))
    clock = _Clock()
    health = _health(
        store,
        tmp_path,
        clock,
        corpus_rag_health_cache_seconds=0,
        corpus_rag_chunkless_alert_threshold=1,
    )
    _add_source(store, chunks=0)
    _add_source(store, chunks=0)

    with caplog.at_level(logging.WARNING, logger='app.main'):
        assert health.snapshot()['chunkless_over_threshold'] is True
        health.snapshot()
    warnings = [r for r in caplog.records if 'chunk-less' in r.getMessage()]
    assert len(warnings) == 1
    assert health.snapshot()['dims'] == 768


def test_health_block_degrades_when_store_fails(tmp_path: Path) -> None:
    class BrokenStore:
        def count_rag_index(self, *args, **kwargs):
            raise RuntimeError('database unavailable')

    health = _health(BrokenStore(), tmp_path, _Clock())

    assert health.snapshot() == {
        'available': False,
        'reason': 'database unavailable',
    }


def test_health_endpoint_exposes_corpus_rag(orchestrator_bundle) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    _add_source(store, chunks=0)
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        body = client.get('/health').json()

    assert 'knowledge_dense' in body
    corpus_rag = body['corpus_rag']
    assert corpus_rag['model_id'] == settings.knowledge_embedding_model
    assert corpus_rag['chunkless_sources'] == 1
    assert corpus_rag['available'] is False
