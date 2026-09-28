"""``get_rag_document`` round-trip on the SQLite backend.

The corpus UI resolves a source's raw PDF through its stored
``RagDocumentRecord``; the getter reads the ``rag_documents.payload`` column
and raises :class:`RecordNotFound` for an unknown ``source_id`` so the HTTP
layer can map it to a 404 without leaking whether the row exists.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.corpus_rag import RagDocumentRecord
from app.schemas import KnowledgeSource, SourceType
from app.storage import RecordNotFound, SqliteStore


def _source() -> KnowledgeSource:
    return KnowledgeSource(
        source_type=SourceType.PAPER,
        canonical_uri='file:///tmp/corpus/paper.pdf',
        digest=uuid4().hex + uuid4().hex,
    )


def test_get_rag_document_roundtrip_sqlite(tmp_path) -> None:
    store = SqliteStore(str(tmp_path / 'orchestrator.db'))
    source = _source()
    store.save_knowledge_source(source)
    document = RagDocumentRecord(
        source_id=source.source_id,
        doc_type='paper',
        title='A corpus paper',
        authors=['Ada Lovelace'],
        year=2024,
        extraction_version='rag-v1',
    )
    store.upsert_rag_document(document)

    fetched = store.get_rag_document(source.source_id)

    assert fetched == document
    assert fetched.doc_id == document.doc_id
    assert fetched.title == 'A corpus paper'


def test_get_rag_document_unknown_source_raises(tmp_path) -> None:
    store = SqliteStore(str(tmp_path / 'orchestrator.db'))

    with pytest.raises(RecordNotFound):
        store.get_rag_document('no-such-source')
