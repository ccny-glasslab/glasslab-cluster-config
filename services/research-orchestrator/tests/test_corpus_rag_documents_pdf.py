"""Born-digital PDF extraction contract for ``app/corpus_rag/pdf_backend``.

These tests pin the offset-exact page/block assembly, the non-PDF and
scanned-PDF rejection surface, and the repeated header/footer stripping of
the swappable :class:`PyMuPdfBackend`. Network-free; PDFs are built in memory.
"""

from __future__ import annotations

from collections.abc import Iterator

import pymupdf
import pytest

from app.corpus_rag.pdf_backend import (
    ExtractedBlock,
    ExtractedDocument,
    PyMuPdfBackend,
    UnsupportedDocumentError,
)


def _finish(doc: 'pymupdf.Document') -> bytes:
    data = doc.write()
    doc.close()
    return data


def _make_pdf() -> bytes:
    """Two-page born-digital PDF: title/intro/body, then a second section."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), 'Clustering Methods', fontsize=20)
    page.insert_text((72, 130), '1 Introduction', fontsize=14)
    page.insert_text(
        (72, 170),
        'We report resampling estimates uncertainty for every configuration.',
        fontsize=11,
    )
    second = doc.new_page()
    second.insert_text((72, 72), '2 Stability', fontsize=14)
    second.insert_text(
        (72, 130),
        'Bootstrap stability separates robust clusters from fragile ones.',
        fontsize=11,
    )
    return _finish(doc)


def _make_blank_pdf() -> bytes:
    """Single image-only page: no extractable text anywhere."""
    doc = pymupdf.open()
    doc.new_page()
    return _finish(doc)


def _make_repeated_header_pdf() -> bytes:
    """Three pages sharing an identical first line; all other lines unique."""
    doc = pymupdf.open()
    sections = (
        ('1 Agglomeration', 'Ward linkage merges compact clusters early.'),
        ('2 Separation', 'Silhouette width penalizes oversized partitions.'),
        ('3 Validation', 'Gap statistic contrasts inertia against null data.'),
    )
    for heading, body in sections:
        page = doc.new_page()
        page.insert_text((72, 50), 'CHAPTER HEADER', fontsize=8)
        page.insert_text((72, 130), heading, fontsize=12)
        page.insert_text((72, 170), body, fontsize=11)
    return _finish(doc)


def _all_blocks(document: ExtractedDocument) -> Iterator[ExtractedBlock]:
    for page in document.pages:
        yield from page.blocks


def test_extract_builds_offset_exact_pages_and_blocks():
    document = PyMuPdfBackend().extract(_make_pdf())

    assert document.n_pages == 2
    assert document.text == '\n\n'.join(page.text for page in document.pages)
    for block in _all_blocks(document):
        assert document.text[block.char_start:block.char_end] == block.text
    assert any(
        'resampling estimates uncertainty' in block.text
        for block in _all_blocks(document)
    )
    sizes = [block.font_size for block in _all_blocks(document)]
    assert sizes
    assert all(isinstance(size, float) for size in sizes)
    assert max(sizes) == pytest.approx(20.0)
    assert min(sizes) == pytest.approx(11.0)


def test_rejects_non_pdf_and_scanned():
    backend = PyMuPdfBackend()

    with pytest.raises(UnsupportedDocumentError):
        backend.extract(b'not a pdf')
    with pytest.raises(UnsupportedDocumentError):
        backend.extract(b'')

    with pytest.raises(UnsupportedDocumentError, match='scanned'):
        backend.extract(_make_blank_pdf())


def test_header_footer_stripped_when_repeated():
    document = PyMuPdfBackend().extract(_make_repeated_header_pdf())

    joined = '\n'.join(block.text for block in _all_blocks(document))
    assert 'CHAPTER HEADER' not in joined
    assert document.metadata['stripped_headers_footers'] >= 1
    assert 'Ward linkage' in joined
    assert document.text == '\n\n'.join(page.text for page in document.pages)
    for block in _all_blocks(document):
        assert document.text[block.char_start:block.char_end] == block.text
