"""Tests for the knowledge -> corpus-RAG backfill projection.

The projection makes live-corpus sources (which have no raw bytes) retrievable
by the lexical ``/ui`` chat by copying their retained knowledge text into the
rag tables. It must be additive, idempotent, and must never fabricate bytes or
page geometry.
"""

from __future__ import annotations

import hashlib

import pytest

from app.corpus_rag.backfill import (
    BACKFILL_INDEX_VERSION,
    backfill_knowledge_into_rag,
)
from app.schemas import KnowledgeChunk, KnowledgeSource, SourceType
from app.storage import RecordNotFound, SqliteStore


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _add_source(
    store: SqliteStore,
    source_id: str,
    *,
    source_type: SourceType = SourceType.DOCUMENTATION,
    uri: str | None = None,
    texts: tuple[str, ...] = ('Alpha beta gamma.', 'Delta epsilon.'),
) -> KnowledgeSource:
    source = KnowledgeSource(
        source_id=source_id,
        source_type=source_type,
        canonical_uri=uri or f'upload://{source_id}.md',
        digest=_digest(source_id),
        title=f'Title {source_id}',
    )
    store.save_knowledge_source(source)
    store.replace_knowledge_chunks(
        source_id,
        [
            KnowledgeChunk(
                chunk_id=f'{source_id}-c{i}',
                source_id=source_id,
                chunk_index=i,
                text=text,
                digest=_digest(text),
                token_count=len(text.split()),
            )
            for i, text in enumerate(texts)
        ],
    )
    return source


@pytest.fixture()
def store(tmp_path):
    return SqliteStore(str(tmp_path / 'backfill.db'))


def test_dry_run_reports_without_writing(store):
    _add_source(store, 'src-a')

    report = backfill_knowledge_into_rag(store, apply=False)

    assert report.apply is False
    assert report.added_sources == 1
    assert report.added_chunks == 2
    assert report.sources[0].status == 'would-add'
    # Nothing was written.
    assert store.list_rag_chunks() == []
    with pytest.raises(RecordNotFound):
        store.get_rag_document('src-a')
    assert store.get_corpus('live-knowledge') is None


def test_apply_projects_text_and_corpus_membership(store):
    _add_source(store, 'src-a')

    report = backfill_knowledge_into_rag(store, apply=True)

    assert report.added_sources == 1
    assert report.added_chunks == 2

    document = store.get_rag_document('src-a')
    assert document.source_id == 'src-a'
    assert document.doc_type == 'reference'
    assert document.title == 'Title src-a'

    chunks = store.list_rag_chunks(source_ids=['src-a'])
    assert [c['chunk_id'] for c in chunks] == ['src-a-c0', 'src-a-c1']
    assert [c['text'] for c in chunks] == ['Alpha beta gamma.', 'Delta epsilon.']
    assert all(c['kind'] == 'evidence_span' for c in chunks)
    # No fabricated page geometry: the viewer must not offer a link.
    assert all(c['page_start'] is None for c in chunks)

    corpus = store.get_corpus('live-knowledge')
    assert corpus is not None
    assert store.list_corpus_sources(corpus.corpus_id) == ['src-a']


def test_apply_is_idempotent(store):
    _add_source(store, 'src-a')

    first = backfill_knowledge_into_rag(store, apply=True)
    second = backfill_knowledge_into_rag(store, apply=True)

    assert first.added_sources == 1
    assert second.added_sources == 0
    assert second.skipped_existing == 1
    # No duplicate chunk rows.
    assert len(store.list_rag_chunks(source_ids=['src-a'])) == 2
    corpus = store.get_corpus('live-knowledge')
    assert store.list_corpus_sources(corpus.corpus_id) == ['src-a']


def test_paper_source_maps_to_paper_doc_type(store):
    _add_source(store, 'paper-1', source_type=SourceType.PAPER)

    backfill_knowledge_into_rag(store, apply=True)

    assert store.get_rag_document('paper-1').doc_type == 'paper'


def test_source_without_chunks_is_skipped(store):
    _add_source(store, 'empty', texts=())

    report = backfill_knowledge_into_rag(store, apply=True)

    assert report.added_sources == 0
    assert report.skipped_empty == 1
    assert store.list_rag_chunks(source_ids=['empty']) == []


def test_limit_bounds_added_sources(store):
    _add_source(store, 'src-a')
    _add_source(store, 'src-b')

    report = backfill_knowledge_into_rag(store, apply=True, limit=1)

    assert report.added_sources == 1


def test_source_ids_filter(store):
    _add_source(store, 'src-a')
    _add_source(store, 'src-b')

    report = backfill_knowledge_into_rag(store, apply=True, source_ids=['src-b'])

    assert report.added_sources == 1
    assert report.sources[0].source_id == 'src-b'
    with pytest.raises(RecordNotFound):
        store.get_rag_document('src-a')


def test_backfill_marker_recorded(store):
    _add_source(store, 'src-a')

    backfill_knowledge_into_rag(store, apply=True)

    document = store.get_rag_document('src-a')
    assert document.extraction_version == BACKFILL_INDEX_VERSION
    assert document.metadata['backfill']['canonical_uri'] == 'upload://src-a.md'
