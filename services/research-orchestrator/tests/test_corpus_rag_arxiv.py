"""Tests for the allowlisted arXiv fetcher and its pipeline composition.

Covers the boundary contract from issue #311: deterministic Atom parsing,
fail-closed category allowlisting, size caps enforced before and during
download, and end-to-end composition with the EXISTING ingest_document path
(extract -> secret-scan -> chunk -> persist) using a stubbed HTTP layer.
"""

from __future__ import annotations

import datetime as _dt
import hashlib

import pymupdf
import pytest

from app.corpus_rag.arxiv import (
    ALLOWED_CATEGORIES,
    MAX_PDF_BYTES,
    ArxivEntry,
    ArxivQuery,
    PdfTooLargeError,
    download_pdf,
    fetch_entries,
    is_oversized,
)
from app.corpus_rag.pipeline import ingest_document
from app.storage import SqliteStore

_ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>ArXiv Query: cat:cs.LG</title>
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <updated>2024-01-01T00:00:00Z</updated>
    <published>2024-01-01T00:00:00Z</published>
    <title>Resampling Methods for Evaluation</title>
    <author><name>Ada Author</name></author>
    <author><name>Ben Researcher</name></author>
    <link rel="related" type="application/pdf"
          href="http://arxiv.org/pdf/2401.00001v1"/>
    <arxiv:primary_category term="cs.LG"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00002v2</id>
    <updated>2024-01-02T00:00:00Z</updated>
    <published>2024-01-02T00:00:00Z</published>
    <title>Stability of Stochastic Gradient Descent</title>
    <author><name>Casey Chen</name></author>
    <link rel="related" type="application/pdf"
          href="http://arxiv.org/pdf/2401.00002v2"/>
    <arxiv:primary_category term="stat.ML"/>
  </entry>
</feed>
"""


class _FakeResponse:
    """Minimal context-managed urllib response with a headers mapping."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}
        self.read_called = False

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        self.read_called = True
        if size == -1 or size >= len(self._body):
            data, self._body = self._body, b''
            return data
        data, self._body = self._body[:size], self._body[size:]
        return data


def _stub_urlopen(routes: dict[str, _FakeResponse]):
    def urlopen(request, timeout: int | None = None):
        url = request.full_url if hasattr(request, 'full_url') else str(request)
        for prefix, response in routes.items():
            if url.startswith(prefix):
                return response
        raise AssertionError(f'unexpected url: {url}')
    return urlopen


def _make_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), 'Resampling Methods for Evaluation', fontsize=20)
    page.insert_text((72, 130), '1 Resampling', fontsize=14)
    body = 'bootstrap resampling estimates uncertainty in model evaluation studies reliably'
    page.insert_text((72, 160), ' '.join([body] * 6), fontsize=11)
    return doc.tobytes()


@pytest.fixture()
def store(tmp_path) -> SqliteStore:
    return SqliteStore(str(tmp_path / 'arxiv.db'))


# ------------------------- (a) Atom parsing ------------------------- #


def test_fetch_entries_parses_atom_feed_deterministically() -> None:
    query = ArxivQuery(categories=('cs.LG', 'stat.ML'), max_results=10)
    stub = _stub_urlopen({'https://export.arxiv.org/api/query': _FakeResponse(_ATOM_FEED.encode('utf-8'))})

    entries = fetch_entries(query, urlopen=stub)

    assert [entry.arxiv_id for entry in entries] == [
        'http://arxiv.org/abs/2401.00001v1',
        'http://arxiv.org/abs/2401.00002v2',
    ]
    first = entries[0]
    assert first.title == 'Resampling Methods for Evaluation'
    assert first.authors == ('Ada Author', 'Ben Researcher')
    assert first.published == _dt.date(2024, 1, 1)
    assert first.pdf_url == 'http://arxiv.org/pdf/2401.00001v1'
    second = entries[1]
    assert second.authors == ('Casey Chen',)
    assert second.published == _dt.date(2024, 1, 2)


def test_fetch_entries_filters_by_date_from() -> None:
    query = ArxivQuery(
        categories=('cs.LG',), date_from=_dt.date(2024, 1, 2), max_results=10
    )
    stub = _stub_urlopen({'https://export.arxiv.org/api/query': _FakeResponse(_ATOM_FEED.encode('utf-8'))})

    entries = fetch_entries(query, urlopen=stub)

    assert [entry.arxiv_id for entry in entries] == [
        'http://arxiv.org/abs/2401.00002v2'
    ]


# --------------------- (b) allowlist enforcement --------------------- #


def test_query_rejects_non_allowlisted_category_before_fetch() -> None:
    with pytest.raises(ValueError, match='not allowlisted'):
        ArxivQuery(categories=('cs.LG', 'cs.Crypto'))


def test_query_accepts_only_allowlisted_categories() -> None:
    query = ArxivQuery(categories=tuple(sorted(ALLOWED_CATEGORIES)))
    assert set(query.categories) == ALLOWED_CATEGORIES


# ------------------------- (c) size caps ------------------------- #


def test_oversized_entry_skipped_before_download() -> None:
    entry = ArxivEntry(
        arxiv_id='http://arxiv.org/abs/2401.00003v1',
        title='Huge Paper',
        authors=(),
        published=_dt.date(2024, 1, 3),
        pdf_url='http://arxiv.org/pdf/2401.00003v1',
        pdf_bytes=MAX_PDF_BYTES + 1,
    )
    assert is_oversized(entry, MAX_PDF_BYTES)
    assert not is_oversized(entry, MAX_PDF_BYTES * 2)


def test_download_rejects_declared_content_length_over_cap() -> None:
    entry = ArxivEntry(
        arxiv_id='http://arxiv.org/abs/2401.00004v1',
        title='Declared Huge',
        authors=(),
        published=_dt.date(2024, 1, 4),
        pdf_url='http://arxiv.org/pdf/2401.00004v1',
    )
    response = _FakeResponse(
        b'%PDF-1.4\n%%EOF\n', headers={'Content-Length': str(MAX_PDF_BYTES + 1)}
    )
    stub = _stub_urlopen({'http://arxiv.org/pdf/': response})

    with pytest.raises(PdfTooLargeError):
        download_pdf(entry, max_pdf_bytes=MAX_PDF_BYTES, urlopen=stub)
    assert not response.read_called


def test_download_rejects_streamed_body_over_cap() -> None:
    entry = ArxivEntry(
        arxiv_id='http://arxiv.org/abs/2401.00005v1',
        title='Streamed Huge',
        authors=(),
        published=_dt.date(2024, 1, 5),
        pdf_url='http://arxiv.org/pdf/2401.00005v1',
    )
    stub = _stub_urlopen(
        {'http://arxiv.org/pdf/': _FakeResponse(b'%PDF-' + b'x' * (MAX_PDF_BYTES + 1))}
    )

    with pytest.raises(PdfTooLargeError):
        download_pdf(entry, max_pdf_bytes=MAX_PDF_BYTES, urlopen=stub)


# --------------- (d) end-to-end pipeline composition --------------- #


def test_fetched_pdf_flows_through_ingest_document(store: SqliteStore) -> None:
    pdf = _make_pdf()
    routes = {
        'https://export.arxiv.org/api/query': _FakeResponse(_ATOM_FEED.encode('utf-8')),
        'http://arxiv.org/pdf/2401.00001v1': _FakeResponse(pdf),
    }
    stub = _stub_urlopen(routes)
    query = ArxivQuery(
        categories=('cs.LG',), date_from=_dt.date(2024, 1, 1), max_results=10
    )

    entries = fetch_entries(query, urlopen=stub)
    assert len(entries) == 2
    entry = entries[0]
    data, digest = download_pdf(entry, max_pdf_bytes=MAX_PDF_BYTES, urlopen=stub)
    assert data == pdf
    assert digest == hashlib.sha256(pdf).hexdigest()

    report = ingest_document(
        store=store,
        data=data,
        canonical_uri=entry.pdf_url,
        title=entry.title,
        doc_type='paper',
        authors=list(entry.authors),
        year=entry.published.year,
        doi_isbn_url=entry.arxiv_id,
        corpus_slug='arxiv-preprints',
    )

    sources = store.list_knowledge_sources()
    assert len(sources) == 1
    assert sources[0].canonical_uri == entry.pdf_url
    assert sources[0].digest == digest
    assert sources[0].metadata['authors'] == ['Ada Author', 'Ben Researcher']
    assert sources[0].metadata['year'] == 2024

    chunks = store.list_rag_chunks(source_ids=[report.source_id], limit=None)
    assert chunks
    assert {chunk['kind'] for chunk in chunks} == {'evidence_span', 'section_unit'}
    assert report.n_evidence_spans > 0