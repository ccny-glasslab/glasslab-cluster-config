"""Dense embedding + mode-selection tests (offline, deterministic).

The embed step is exercised on a real SQLite store with the hash-seeded
offline provider, so nothing downloads a model. The chat mode tests use a fake
provider whose passage vectors are orthogonal basis vectors and whose query
always embeds to ``e0``, so dense rankings are exact without any model.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from app.config import Settings
from app.corpus_rag.chat import CorpusChatService, build_corpus_chat_service
from app.corpus_rag.contracts import RAG_INDEX_VERSION, RagChunkRecord
from app.corpus_rag.dense import (
    build_rag_dense_index,
    open_rag_vector_index,
    plan_rag_dense_index,
    rag_dense_ready,
)
from app.corpus_rag.embeddings import OfflineDeterministicEmbedding
from app.corpus_rag.vector_index import NumpyVectorIndex
from app.schemas import KnowledgeSource, SourceType
from app.storage import SqliteStore

DIMS = 16
_OFFLINE = 'offline-deterministic'


def _unit(axis: int) -> np.ndarray:
    vec = np.zeros(DIMS, dtype=np.float32)
    vec[axis % DIMS] = 1.0
    return vec


class _FixedEmbedding:
    """Fake provider: passage i maps to e_i; every query maps to e0."""

    model_id = 'fake-dense'
    revision = 'v1'
    dims = DIMS

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return np.stack([_unit(index) for index, _ in enumerate(texts)])

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return np.stack([_unit(0) for _ in texts])


def _seed_store(
    path: Path, texts: list[str]
) -> tuple[SqliteStore, list[tuple[str, str]]]:
    """One knowledge source and one rag chunk per text; returns (store, ids)."""
    store = SqliteStore(str(path))
    ids: list[tuple[str, str]] = []
    for index, text in enumerate(texts):
        uri = f'repo://docs/source-{index}.md'
        source = KnowledgeSource(
            source_type=SourceType.DOCUMENTATION,
            canonical_uri=uri,
            digest=hashlib.sha256(uri.encode()).hexdigest(),
            title=f'Source {index}',
        )
        store.save_knowledge_source(source)
        chunk_id = f'{source.source_id}::c0'
        store.replace_rag_chunks(
            source.source_id,
            [
                RagChunkRecord(
                    chunk_id=chunk_id,
                    source_id=source.source_id,
                    kind='evidence_span',
                    chunk_index=0,
                    text=text,
                    digest=hashlib.sha256(text.encode()).hexdigest(),
                    token_count=max(1, len(text.split())),
                )
            ],
        )
        ids.append((source.source_id, chunk_id))
    return store, ids


# --- embed step --------------------------------------------------------------


def test_plan_and_build_are_idempotent(tmp_path: Path) -> None:
    store, _ = _seed_store(tmp_path / 'dense.db', ['alpha text', 'beta text', 'gamma text'])
    provider = OfflineDeterministicEmbedding(dims=DIMS)

    plan = plan_rag_dense_index(store, provider)
    assert plan['pending'] == 3
    assert plan['existing'] == 0
    assert plan['dims'] == DIMS
    assert plan['index_version'] == RAG_INDEX_VERSION

    summary = build_rag_dense_index(store, provider)
    assert summary['n_vectors'] == 3
    stored = store.list_rag_chunk_vectors(provider.model_id)
    assert len(stored) == 3
    for meta, blob in stored:
        assert meta.dims == DIMS
        assert meta.revision == 'v0'
        assert meta.index_version == RAG_INDEX_VERSION
        assert len(blob) == DIMS * 4

    again = build_rag_dense_index(store, provider)
    assert again['n_vectors'] == 0
    assert again['existing'] == 3
    assert plan_rag_dense_index(store, provider)['pending'] == 0


def test_build_writes_through_vector_index(tmp_path: Path) -> None:
    store, _ = _seed_store(tmp_path / 'index.db', ['first passage', 'second passage'])
    provider = OfflineDeterministicEmbedding(dims=DIMS)
    index = NumpyVectorIndex()

    summary = build_rag_dense_index(store, provider, vector_index=index)

    assert summary['n_vectors'] == 2
    hits = index.search(provider.embed_queries(['first passage'])[0], k=2)
    assert hits, 'vector_index.add write path must reach the index'


def test_force_reembeds_existing_vectors(tmp_path: Path) -> None:
    store, _ = _seed_store(tmp_path / 'force.db', ['one', 'two'])
    provider = OfflineDeterministicEmbedding(dims=DIMS)
    build_rag_dense_index(store, provider)

    summary = build_rag_dense_index(store, provider, force=True)

    assert summary['n_vectors'] == 2
    assert summary['existing'] == 0


def test_open_rag_vector_index_source_scoped(tmp_path: Path) -> None:
    store, ids = _seed_store(tmp_path / 'open.db', ['alpha', 'beta'])
    provider = OfflineDeterministicEmbedding(dims=DIMS)
    build_rag_dense_index(store, provider)

    index = open_rag_vector_index(store, provider)
    everything = index.search(provider.embed_queries(['alpha'])[0], k=5)
    assert len(everything) == 2

    scoped = index.search(
        provider.embed_queries(['alpha'])[0], k=5, source_ids=[ids[1][0]]
    )
    assert {chunk_id for chunk_id, _ in scoped} == {ids[1][1]}


def test_rag_dense_ready_reflects_stored_vectors(tmp_path: Path) -> None:
    store, _ = _seed_store(tmp_path / 'ready.db', ['alpha'])
    provider = OfflineDeterministicEmbedding(dims=DIMS)

    assert rag_dense_ready(store, provider) is False
    build_rag_dense_index(store, provider)
    assert rag_dense_ready(store, provider) is True


# --- chat mode selection -----------------------------------------------------


def test_chat_defaults_to_lexical(tmp_path: Path) -> None:
    store, _ = _seed_store(tmp_path / 'chat.db', ['resampling stability'])

    service = CorpusChatService(store)

    assert service.retrieval_mode == 'lexical'
    assert service.answer('resampling stability').insufficient is False


def test_chat_dense_mode_uses_the_vector_channel(tmp_path: Path) -> None:
    store, ids = _seed_store(tmp_path / 'densechat.db', ['entirely unrelated words'])
    provider = _FixedEmbedding()
    build_rag_dense_index(store, provider)
    index = open_rag_vector_index(store, provider)

    service = CorpusChatService(
        store,
        retrieval_mode='dense',
        vector_index=index,
        embedding_provider=provider,
    )
    result = service.answer('zzz no lexical overlap')

    assert service.retrieval_mode == 'dense'
    assert result.insufficient is False
    assert {citation.source_id for citation in result.citations} == {ids[0][0]}


def test_chat_hybrid_mode_reports_hybrid(tmp_path: Path) -> None:
    store, _ = _seed_store(tmp_path / 'hybrid.db', ['alpha beta gamma'])
    provider = _FixedEmbedding()
    build_rag_dense_index(store, provider)
    index = open_rag_vector_index(store, provider)

    service = CorpusChatService(
        store,
        retrieval_mode='hybrid',
        vector_index=index,
        embedding_provider=provider,
    )

    assert service.retrieval_mode == 'hybrid'
    assert service.answer('alpha beta').insufficient is False


def test_chat_dense_mode_degrades_to_lexical_without_backend(tmp_path: Path) -> None:
    store, _ = _seed_store(tmp_path / 'degrade.db', ['resampling stability'])

    service = CorpusChatService(store, retrieval_mode='dense')

    assert service.retrieval_mode == 'lexical'
    assert service.answer('resampling stability').insufficient is False


def test_chat_unknown_mode_falls_back_to_lexical(tmp_path: Path) -> None:
    store, _ = _seed_store(tmp_path / 'unknown.db', ['resampling stability'])

    service = CorpusChatService(store, retrieval_mode='bogus')  # type: ignore[arg-type]

    assert service.retrieval_mode == 'lexical'


# --- factory (settings -> service) ------------------------------------------


def test_factory_default_is_lexical(tmp_path: Path) -> None:
    store, _ = _seed_store(tmp_path / 'factory.db', ['alpha'])

    service = build_corpus_chat_service(store, Settings())

    assert service.retrieval_mode == 'lexical'


def test_factory_dense_degrades_when_no_vectors(tmp_path: Path) -> None:
    store, _ = _seed_store(tmp_path / 'novec.db', ['alpha'])
    settings = Settings(ui_chat_retrieval_mode='dense')

    service = build_corpus_chat_service(store, settings)

    assert service.retrieval_mode == 'lexical'


def test_factory_dense_active_when_vectors_exist(tmp_path: Path) -> None:
    store, _ = _seed_store(tmp_path / 'active.db', ['alpha beta'])
    provider = OfflineDeterministicEmbedding(dims=768)
    build_rag_dense_index(store, provider)
    settings = Settings(
        ui_chat_retrieval_mode='hybrid',
        knowledge_embedding_model='offline-deterministic',
    )

    service = build_corpus_chat_service(store, settings)

    assert service.retrieval_mode == 'hybrid'


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(pytest.main([__file__, '-q']))
