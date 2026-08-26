"""Tesseract OCR backend contract tests.

Logic paths (availability gate, page rasterization loop, fallback selector)
run everywhere via a stubbed ``tesseract`` invocation; the end-to-end
recognition test runs only where the binary exists (the dedicated CI lane
installs it).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.corpus_rag.ocr_backend import (
    OCR_EXTRACTION_VERSION,
    TesseractOcrBackend,
    extract_pdf_with_fallback,
)
from app.corpus_rag.pdf_backend import (
    PyMuPdfBackend,
    UnsupportedDocumentError,
)


def _born_digital_pdf(text: str = 'Born digital body text for extraction.') -> bytes:
    """Two pages with fully DISTINCT lines: the repeated header/footer
    stripper removes any line shared by >=60% of pages, which would be
    every line of a one-page document."""
    import pymupdf

    doc = pymupdf.Document()
    for index in range(2):
        page = doc.new_page()
        page.insert_text((72, 110), f'{text} page {index} part A')
        page.insert_text((72, 140), f'{text} page {index} part B')
        page.insert_text((72, 170), f'{text} page {index} part C')
    data = doc.tobytes()
    doc.close()
    return data


def _scanned_pdf(text: str = 'OCR probe phrase unique zebra quantiles') -> bytes:
    """A two-page PDF whose pages are IMAGES of text — no text layer."""
    import pymupdf

    source = pymupdf.Document()
    for index in range(2):
        page = source.new_page()
        page.insert_text((72, 110), f'{text} page {index} variant one')
        page.insert_text((72, 140), f'{text} page {index} variant two')
        page.insert_text((72, 170), f'{text} page {index} variant three')
    pix_pages = [page.get_pixmap(dpi=150) for page in source]
    source.close()
    scanned = pymupdf.Document()
    for pix in pix_pages:
        image_page = scanned.new_page()
        image_page.insert_image(image_page.rect, pixmap=pix)
    data = scanned.tobytes()
    scanned.close()
    return data


def test_missing_binary_raises_clear_error(monkeypatch) -> None:
    monkeypatch.setattr(
        'app.corpus_rag.ocr_backend.shutil.which', lambda _: None
    )
    with pytest.raises(UnsupportedDocumentError, match='tesseract'):
        TesseractOcrBackend().extract(_born_digital_pdf())


def test_ocr_loop_recognizes_stubbed_pages(monkeypatch) -> None:
    monkeypatch.setattr(
        'app.corpus_rag.ocr_backend.shutil.which',
        lambda _: '/usr/bin/tesseract',
    )

    def fake_run(argv, capture_output, text, check):
        assert argv[0] == 'tesseract'
        assert '--dpi' in argv
        fake_run.calls += 1
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                f'page {fake_run.calls} line one unique\n'
                f'page {fake_run.calls} line two unique\n'
            ),
            stderr='',
        )

    fake_run.calls = 0
    monkeypatch.setattr(
        'app.corpus_rag.ocr_backend.subprocess.run', fake_run
    )
    document = TesseractOcrBackend().extract(_born_digital_pdf())
    assert 'line one' in document.text
    assert len(document.pages) == 2


def test_born_digital_stays_pymupdf_and_versioned() -> None:
    document, version = extract_pdf_with_fallback(_born_digital_pdf())
    assert version == 'pymupdf-v1'
    assert 'Born digital' in document.text


def test_fallback_selects_ocr_for_scans(monkeypatch) -> None:
    monkeypatch.setattr(
        'app.corpus_rag.ocr_backend.shutil.which',
        lambda _: '/usr/bin/tesseract',
    )
    ocr_calls = {'n': 0}

    def fake_run(argv, capture_output, text, check):
        ocr_calls['n'] += 1
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f'recognized scan line {ocr_calls["n"]} unique\n',
            stderr='',
        )

    monkeypatch.setattr(
        'app.corpus_rag.ocr_backend.subprocess.run', fake_run
    )

    def explode(self, data):
        raise UnsupportedDocumentError('scanned or image-only PDF')

    monkeypatch.setattr(PyMuPdfBackend, 'extract', explode)
    document, version = extract_pdf_with_fallback(
        _scanned_pdf(), allow_ocr=True
    )
    assert version == OCR_EXTRACTION_VERSION
    assert 'recognized scan line' in document.text


def test_fallback_off_keeps_rejection() -> None:
    def explode(self, data):
        raise UnsupportedDocumentError('scanned or image-only PDF')

    original = PyMuPdfBackend.extract
    PyMuPdfBackend.extract = explode
    try:
        with pytest.raises(UnsupportedDocumentError):
            extract_pdf_with_fallback(_scanned_pdf(), allow_ocr=False)
    finally:
        PyMuPdfBackend.extract = original


@pytest.mark.skipif(
    not TesseractOcrBackend.available(),
    reason='tesseract binary not installed',
)
def test_real_tesseract_reads_scanned_page(tmp_path: Path) -> None:
    document = TesseractOcrBackend(dpi=150).extract(_scanned_pdf())
    assert 'zebra' in document.text.lower()
