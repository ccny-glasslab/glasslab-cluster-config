"""Tests for corpus-RAG document ingestion and section detection.

These tests are deliberately independent of ``app.corpus_rag.pdf_backend``:
section detection is exercised through a local duck-typed stand-in matching
the documented ExtractedDocument shape, so this suite passes whether or not
the parallel-owned PDF backend module exists yet.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field

import pytest

from app.corpus_rag.documents import (
    SectionNode,
    detect_sections,
    ingest_document_bytes,
)
from app.corpus_rag.corpora import CorpusService
from app.knowledge_manager import SECRET_PATTERNS, KnowledgeError
from app.storage import SqliteStore


@dataclass
class _FakeBlock:
    text: str
    char_start: int
    char_end: int
    font_size: float | None = None
    bold: bool = False


@dataclass
class _FakePage:
    page_index: int
    label: str
    text: str
    blocks: list[_FakeBlock] = field(default_factory=list)


@dataclass
class _FakeDocument:
    """Duck-typed stand-in for pdf_backend.ExtractedDocument."""

    n_pages: int
    pages: list[_FakePage]
    text: str
    metadata: dict


@pytest.fixture()
def store(tmp_path):
    return SqliteStore(str(tmp_path / 'i.db'))


def _count_rag_document_rows(db_path) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute('SELECT COUNT(*) FROM rag_documents').fetchone()[0]
    finally:
        connection.close()


def test_secret_bearing_document_rejected(store):
    secret_text = (
        'Reliable learning under drift.\n'
        'api_key = "sk-live-9f2c1ab77e"\n'
        'We study stability of SGD.\n'
    )
    # Verify the fixture actually trips the shared filter before ingesting.
    assert any(p.search(secret_text) for p in SECRET_PATTERNS)

    before = [s.source_id for s in store.list_knowledge_sources()]
    with pytest.raises(KnowledgeError, match='secret pattern'):
        ingest_document_bytes(
            store=store,
            data=secret_text.encode('utf-8'),
            canonical_uri='file://docs/stability-notes.txt',
            title='Stability Notes',
        )
    after = [s.source_id for s in store.list_knowledge_sources()]
    assert after == before


def test_non_utf8_bytes_rejected(store):
    with pytest.raises(KnowledgeError, match='UTF-8'):
        ingest_document_bytes(
            store=store,
            data=b'\xff\xfe\x00garbage',
            canonical_uri='file://docs/binary-blob.bin',
        )
    assert store.list_knowledge_sources() == []


def test_dedup_preserves_identity_and_updates_metadata(store, tmp_path):
    data = b'%PDF-1.4\n%%EOF\n'

    first_source, first_record = ingest_document_bytes(
        store=store,
        data=data,
        canonical_uri='file://docs/stability-paper.pdf',
        title='Original Title',
        authors=['A. Author'],
        year=2024,
        doi_isbn_url='doi:10.1000/stab.1',
    )
    second_source, second_record = ingest_document_bytes(
        store=store,
        data=data,
        canonical_uri='file://docs/stability-paper.pdf',
        title='Updated Title',
    )

    assert second_source.source_id == first_source.source_id
    assert second_source.digest == hashlib.sha256(data).hexdigest()
    assert second_source.ingested_at == first_source.ingested_at
    assert second_source.title == 'Updated Title'
    assert len(store.list_knowledge_sources()) == 1

    # The rag document row must be reused (one row), not duplicated.
    assert second_record.source_id == first_record.source_id
    assert _count_rag_document_rows(tmp_path / 'i.db') == 1

    # Ingestion does not touch chunking state.
    assert store.list_rag_chunks() == []


def test_detect_sections_on_synthetic_document():
    front = 'Preface remarks about scope.'
    h1 = '1 Introduction'
    b1 = 'We study gradient descent.'
    h11 = '1.1 Details'
    b2 = 'Details follow here.'
    h2 = '2 Stability'
    b3 = 'Stability requires strong convexity.'

    pieces = [front, h1, b1, h11, b2, h2, b3]
    text = '\n\n'.join(pieces)

    blocks: list[_FakeBlock] = []
    cursor = 0
    sizes = {
        front: 11.0,
        h1: 14.0,
        b1: 11.0,
        h11: 14.0,
        b2: 11.0,
        h2: 14.0,
        b3: 11.0,
    }
    for piece in pieces:
        blocks.append(
            _FakeBlock(
                text=piece,
                char_start=cursor,
                char_end=cursor + len(piece),
                font_size=sizes[piece],
            )
        )
        cursor += len(piece) + 2  # '\n\n' separator

    # Two fake pages split at the start of '1.1 Details'.
    split_at = text.index(h11)
    page_one_blocks = [b for b in blocks if b.char_end <= split_at]
    page_two_blocks = [b for b in blocks if b.char_start >= split_at]
    page_one = _FakePage(
        page_index=0,
        label='1',
        text=text[:split_at].rstrip('\n '),
        blocks=page_one_blocks,
    )
    page_two = _FakePage(
        page_index=1,
        label='2',
        text=text[split_at:],
        blocks=page_two_blocks,
    )
    document = _FakeDocument(
        n_pages=2,
        pages=[page_one, page_two],
        text=text,
        metadata={},
    )

    sections = detect_sections(document)

    assert [s.path for s in sections] == ['0', '1', '1.1', '2']
    assert [s.title for s in sections] == [
        'Front matter',
        'Introduction',
        'Details',
        'Stability',
    ]
    assert [s.level for s in sections] == [1, 1, 2, 1]

    # Contiguity: each section starts where the previous ended.
    assert sections[0].start_char == 0
    for previous, current in zip(sections, sections[1:]):
        assert current.start_char == previous.end_char
    assert sections[-1].end_char == len(text)

    # Page spans resolved against the two fake pages.
    assert sections[0].page_start == 0
    assert sections[0].page_end == 0
    assert sections[1].page_start == 0
    assert sections[1].page_end == 0
    assert sections[2].page_start == 1
    assert sections[2].page_end == 1
    assert sections[3].page_start == 1
    assert sections[3].page_end == 1

    assert all(isinstance(s, SectionNode) for s in sections)


def test_corpus_registration_via_ingest(store):
    slug = 'statistical-learning-methods'
    data_one = b'%PDF-1.4\n%%EOF\n one\n'
    data_two = b'%PDF-1.4\n%%EOF\n two\n'

    source_one, _ = ingest_document_bytes(
        store=store,
        data=data_one,
        canonical_uri='file://docs/learning-one.pdf',
        title='Learning One',
        corpus_slug=slug,
    )
    source_two, _ = ingest_document_bytes(
        store=store,
        data=data_two,
        canonical_uri='file://docs/learning-two.pdf',
        title='Learning Two',
        corpus_slug=slug,
    )

    service = CorpusService(store)
    members = service.member_source_ids(slug)
    assert sorted(members) == sorted([source_one.source_id, source_two.source_id])

    described = service.describe(slug)
    assert described['slug'] == slug
    assert described['n_sources'] == 2
    assert described['corpus_id'] is not None

    # Unknown slugs are empty, not errors.
    assert service.member_source_ids('no-such-corpus') == []

    # Re-ingesting into the same corpus must not duplicate membership.
    ingest_document_bytes(
        store=store,
        data=data_one,
        canonical_uri='file://docs/learning-one.pdf',
        title='Learning One',
        corpus_slug=slug,
    )
    assert len(service.member_source_ids(slug)) == 2
