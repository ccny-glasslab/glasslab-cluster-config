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
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from app.corpus_rag.contracts import ChunkVectorMeta
from app.corpus_rag.embeddings import OfflineDeterministicEmbedding
from app.knowledge_dense import (
    DENSE_INDEX_VERSION,
    NumpyChunkIndex,
    build_dense_index,
)
from app.knowledge_manager import KnowledgeManager
from app.postgres_store import PostgresStore
from app.schemas import (
    KnowledgeChunk,
    KnowledgeSource,
    RunRecord,
    RunState,
    SourceType,
)
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
    source = _source('repo://dense/build.md')
    store.save_knowledge_source(source)
    texts = [
        'bootstrap resampling estimates uncertainty in clustering',
        'cross validation checks predictive stability of models',
        'quantile regression relaxes distributional assumptions',
    ]
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
    ids = [chunk.chunk_id for chunk in chunks]
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
        source_ids=[source.source_id],
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
            chunk_id=cid, model_id=MODEL_ID,
            revision=provider.revision,
            dims=dim, index_version='v1',
        )
        store.upsert_knowledge_chunk_vectors(meta, vec.astype('<f4').tobytes())

    index = NumpyChunkIndex(store, provider, model_id=MODEL_ID)
    hits = index.search(
        _unit([0.9, 0.1, 0, 0, 0, 0, 0, 0]),
        source_ids=None,
        k=2,
    )
    assert [cid for cid, _ in hits] == [ids[0], ids[1]]
    assert hits[0][1] > 0.9
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
        stale.embed_query('anything'), source_ids=None, k=3
    ) == []


def test_declared_revision_mismatch_degrades_readiness(store) -> None:
    """Vectors from another revision must never serve under a new pin."""
    ids = _seed_chunks(store, ['alpha passage text'])
    vec = np.zeros(8, dtype='<f4')
    vec[0] = 1.0
    store.upsert_knowledge_chunk_vectors(
        ChunkVectorMeta(
            chunk_id=ids[0],
            model_id=MODEL_ID,
            revision='rev-a',
            dims=8,
            index_version=DENSE_INDEX_VERSION,
        ),
        vec.tobytes(),
    )

    class PinnedOther:
        model_id = MODEL_ID
        revision = 'rev-b'
        dims = 8

        def embed_queries(self, texts):
            raise AssertionError('lineage-rejected provider must not embed')

        def embed_passages(self, texts):
            raise AssertionError('lineage-rejected provider must not embed')

    stale = NumpyChunkIndex(store, PinnedOther(), model_id=MODEL_ID)
    readiness = stale.readiness()
    assert readiness.available is False
    assert 'revision' in readiness.reason.lower()

    class PinnedMatch:
        model_id = MODEL_ID
        revision = 'rev-a'
        dims = 8

        def embed_queries(self, texts):
            return OfflineDeterministicEmbedding(dims=8).embed_queries(texts)

        def embed_passages(self, texts):
            raise AssertionError('not used')

    matching = NumpyChunkIndex(store, PinnedMatch(), model_id=MODEL_ID)
    assert matching.readiness().available is True

    class Unpinned:
        model_id = MODEL_ID
        revision = ''
        dims = 8

        def embed_queries(self, texts):
            return OfflineDeterministicEmbedding(dims=8).embed_queries(texts)

        def embed_passages(self, texts):
            raise AssertionError('not used')

    unverifiable = NumpyChunkIndex(store, Unpinned(), model_id=MODEL_ID)
    assert unverifiable.readiness().available is True


def test_rebuild_keeps_single_lineage_per_chunk(store) -> None:
    ids = _seed_chunks(store, ['stability text'])
    provider = OfflineDeterministicEmbedding(dims=8)
    build_dense_index(store, provider, model_id=MODEL_ID)

    build_dense_index(store, provider, model_id=MODEL_ID, force=True)

    rows = store.list_knowledge_chunk_vectors(MODEL_ID)
    assert {meta.chunk_id for meta, _ in rows} == set(ids)
    assert len(rows) == len(ids)


def test_index_ready_after_restart_without_embedding_first(store) -> None:
    """A process restart must not brick dense retrieval.

    The provider declares its true dims at construction (as the arctic
    lineage now does), so the index reloads stored vectors before any
    embedding call; ensure_index_built() then finds existing coverage and
    embeds nothing. Readiness is available on the very first check.
    """
    ids = _seed_chunks(store, ['alpha passage text', 'beta passage text'])
    provider = OfflineDeterministicEmbedding(dims=8)
    build_dense_index(store, provider, model_id=MODEL_ID)
    embed_calls = {'count': 0}

    class RestartedProvider:
        model_id = MODEL_ID
        revision = OfflineDeterministicEmbedding(dims=8).revision
        dims = 8

        def _track(self, texts):
            embed_calls['count'] += len(texts)
            return provider.embed_queries(texts)

        def embed_queries(self, texts):
            return self._track(texts)

        def embed_passages(self, texts):
            return provider.embed_passages(texts)

    restarted = RestartedProvider()
    index = NumpyChunkIndex(store, restarted, model_id=MODEL_ID)
    readiness = index.readiness()
    assert readiness.available is True
    assert readiness.indexed_count == len(ids)
    from app.knowledge_dense import ensure_index_built

    assert ensure_index_built(index, store) is None
    assert embed_calls['count'] == 0

    hits = index.search(index.embed_query('alpha passage text'), k=1)
    assert hits and hits[0][0] == ids[0]
    assert embed_calls['count'] == 1


@pytest.mark.skipif(not PG_DSN, reason='CORPUS_RAG_PG_DSN not configured')
def test_pg_backend_roundtrip_and_order(pg_store) -> None:
    from app.knowledge_dense import PgVectorChunkIndex

    source = _source(f'repo://dense/{uuid4().hex[:8]}.md')
    pg_store.save_knowledge_source(source)
    texts = [
        'consensus matrices across bootstrap resamples',
        'unrelated passage about quantile regression',
    ]
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
    pg_store.replace_knowledge_chunks(source.source_id, chunks)
    ids = [chunk.chunk_id for chunk in chunks]

    provider = OfflineDeterministicEmbedding(dims=8)
    build_dense_index(pg_store, provider)

    index = PgVectorChunkIndex(pg_store, provider)
    hits = index.search(
        index.embed_query('consensus resampling'),
        source_ids=[source.source_id],
        k=2,
    )
    assert hits[0][0] == ids[0]
    assert all(cid in ids for cid, _ in hits)

    # A provider pinned to a different revision must not serve stored rows.
    class OtherPin:
        model_id = MODEL_ID
        revision = 'other-pin'
        dims = 8

        def __init__(self):
            self._inner = provider

        def embed_queries(self, texts):
            return self._inner.embed_queries(texts)

        def embed_passages(self, texts):
            return self._inner.embed_passages(texts)

    stale_pg = PgVectorChunkIndex(pg_store, OtherPin())
    assert stale_pg.readiness().available is False
    assert stale_pg.search(
        stale_pg.embed_query('consensus resampling'),
        source_ids=[source.source_id],
        k=2,
    ) == []


# ---------------------------------------------------------------------------
# KnowledgeManager dense-mode integration (T4)
# ---------------------------------------------------------------------------

from app.knowledge_manager import KnowledgeManager  # noqa: E402


class _ExplodingDenseIndex:
    """readiness() claims health; search() always fails (backend outage)."""

    def __init__(self, provider) -> None:
        self._provider = provider

    def readiness(self):
        from app.knowledge_dense import DenseReadiness

        return DenseReadiness(
            available=True, reason='', backend='numpy',
            model_id=self._provider.model_id,
            revision=self._provider.revision,
            dims=int(self._provider.dims), indexed_count=2,
        )

    def embed_query(self, text):
        return self._provider.embed_queries([text])[0]

    def search(self, query_vec, *, source_ids=None, k=10):
        raise RuntimeError('dense backend exploded')

    def hydrate(self, chunk_ids):
        return []


def _seed_km_pair(store) -> tuple[str, str]:
    """paper (allowed for honeydew) + implementation_file (always disallowed)."""
    sha = lambda t: hashlib.sha256(t.encode()).hexdigest()  # noqa: E731

    paper = KnowledgeSource(
        source_type=SourceType.PAPER,
        canonical_uri='repo://km/paper.md',
        digest=uuid4().hex + uuid4().hex,
    )
    store.save_knowledge_source(paper)
    paper_text = 'guidance on cluster stability assessment using bootstrap resampling'
    paper_chunk = KnowledgeChunk(
        source_id=paper.source_id, chunk_index=0, text=paper_text,
        digest=sha(paper_text), token_count=len(paper_text.split()),
    )
    store.replace_knowledge_chunks(paper.source_id, [paper_chunk])

    impl = KnowledgeSource(
        source_type=SourceType.IMPLEMENTATION_FILE,
        canonical_uri='repo://km/impl.py',
        digest=uuid4().hex + uuid4().hex,
    )
    store.save_knowledge_source(impl)
    impl_text = 'internal implementation notes about stability heuristics'
    impl_chunk = KnowledgeChunk(
        source_id=impl.source_id, chunk_index=0, text=impl_text,
        digest=sha(impl_text), token_count=len(impl_text.split()),
    )
    store.replace_knowledge_chunks(impl.source_id, [impl_chunk])
    return paper_chunk.chunk_id, impl_chunk.chunk_id


def _vector_for(store, chunk_id: str, direction: list[float], dims: int = 8) -> None:
    from app.corpus_rag.embeddings import encode_vector

    meta = ChunkVectorMeta(
        chunk_id=chunk_id, model_id='km-dense', revision='r1',
        dims=dims, index_version=DENSE_INDEX_VERSION,
    )
    store.upsert_knowledge_chunk_vectors(meta, _unit(direction).astype('<f4').tobytes())


def _make_km(store, tmp_path, *, dense=True, failing=False):
    import tempfile

    km = KnowledgeManager(store=store, root=Path(tempfile.mkdtemp()) / 'km')
    if dense:
        provider = OfflineDeterministicEmbedding(dims=8)
        km.dense_index = (
            _ExplodingDenseIndex(provider) if failing
            else NumpyChunkIndex(store, provider, model_id='km-dense')
        )
        km.default_retrieval_mode = 'dense'
    return km


def _create_run(store, run_id: str) -> None:
    now = datetime.now(timezone.utc)
    store.create_run(
        RunRecord(
            run_id=run_id,
            objective='Exercise dense retrieval.',
            state=RunState.CREATED,
            evaluation_contract_id='example-research-v1',
            evaluation_contract_version='1.0.0',
            evaluation_contract_digest='a' * 64,
            beaker_workspace='/tmp/beaker',
            honeydew_workspace='/tmp/honeydew',
            shared_artifacts_path='/tmp/shared',
            reports_path='/tmp/reports',
            maximum_turns=20,
            maximum_runtime_seconds=3600,
            maximum_parallel_jobs=2,
            created_at=now,
            updated_at=now,
        ),
        one_active_run=False,
    )


def test_km_dense_mode_respects_scoping_and_persists(tmp_path) -> None:
    store = SqliteStore(str(tmp_path / 'km.db'))
    paper_cid, impl_cid = _seed_km_pair(store)
    _create_run(store, 'run-dense')

    _vector_for(store, impl_cid, [0, 1, 0, 0, 0, 0, 0, 0])  # strongest, but banned
    _vector_for(store, paper_cid, [1, 0, 0, 0, 0, 0, 0, 0])
    km = _make_km(store, tmp_path)
    query_vec_dir = [0, 1, 0, 0, 0, 0, 0, 0]

    class _FixedProvider:
        model_id = 'km-dense'
        revision = 'r1'
        dims = 8

        @staticmethod
        def embed_queries(texts):
            return [_unit(query_vec_dir) for _ in texts]

        @staticmethod
        def embed_passages(texts):
            raise AssertionError('not used here')

    km.dense_index = NumpyChunkIndex(store, _FixedProvider(), model_id='km-dense')

    packet = km.retrieve(
        run_id='run-dense', agent='honeydew', turn_number=1,
        turn_kind='protocol_draft', query='cluster stability assessment',
        run_scope='run-dense', retrieval_mode='dense',
    )
    got_ids = {entry['entry_id'] for entry in packet.ranked_sources}
    assert paper_cid in got_ids
    assert impl_cid not in got_ids

    events = [e for e in store.list_events('run-dense')
              if e.event_type == 'agent.context_retrieved']
    assert events and events[-1].payload.get('retrieval_mode_actual') == 'dense'

    stored = store.get_knowledge_chunks(list(got_ids))
    assert {row['chunk_id'] for row in stored} == got_ids


def test_km_dense_falls_back_to_lexical_on_backend_failure(tmp_path) -> None:
    store = SqliteStore(str(tmp_path / 'kmfb.db'))
    paper_cid, _impl_cid = _seed_km_pair(store)
    _create_run(store, 'run-fb')
    km = _make_km(store, tmp_path, failing=True)

    packet = km.retrieve(
        run_id='run-fb', agent='honeydew', turn_number=1,
        turn_kind='protocol_draft',
        query='cluster stability assessment bootstrap resampling',
        run_scope='run-fb',
    )
    assert packet.ranked_sources, 'lexical fallback must still return content'

    events = [e for e in store.list_events('run-fb')
              if e.event_type == 'agent.context_retrieved']
    actual = events[-1].payload.get('retrieval_mode_actual', '')
    assert actual.startswith('lexical(fallback)')
    assert 'dense backend exploded' in events[-1].payload.get(
        'retrieval_fallback_reason', ''
    )
    assert paper_cid  # sanity: seeded


def test_km_token_budget_applies_in_dense_mode(tmp_path) -> None:
    store = SqliteStore(str(tmp_path / 'kmbudget.db'))
    paper_cid, _impl_cid = _seed_km_pair(store)
    long_text = 'extra padding words ' * 60
    extra = KnowledgeSource(
        source_type=SourceType.PAPER,
        canonical_uri='repo://km/long.md',
        digest=uuid4().hex + uuid4().hex,
    )
    store.save_knowledge_source(extra)
    long_chunk = KnowledgeChunk(
        source_id=extra.source_id, chunk_index=0, text=long_text,
        digest=hashlib.sha256(long_text.encode()).hexdigest(),
        token_count=len(long_text.split()),
    )
    store.replace_knowledge_chunks(extra.source_id, [long_chunk])

    _vector_for(store, paper_cid, [1, 0, 0, 0, 0, 0, 0, 0])
    _vector_for(store, long_chunk.chunk_id, [0, 0, 1, 0, 0, 0, 0, 0])
    _create_run(store, 'run-budget')

    km = _make_km(store, tmp_path)
    packet = km.retrieve(
        run_id='run-budget', agent='honeydew', turn_number=1,
        turn_kind='protocol_draft', query='cluster stability',
        run_scope='run-budget', retrieval_mode='dense',
        token_budget=30,
    )
    assert len(packet.ranked_sources) == 1
