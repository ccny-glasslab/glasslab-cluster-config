"""Tesseract OCR fallback for scanned PDFs.

Implements the :class:`PdfExtractor` protocol by rasterizing each page with
pymupdf and recognizing the image with the ``tesseract`` CLI. Born-digital
PDFs should always use :class:`PyMuPdfBackend` (faster, exact); this backend
exists so scanned textbooks can still enter the corpus, at roughly 1-3
seconds per page on CPU. Recognition quality bounds retrieval quality — a
garbage scan stays garbage.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .pdf_backend import (
    ExtractedDocument,
    UnsupportedDocumentError,
    _RawBlock,
    _build_document,
    _page_label,
    _strip_repeated_headers_footers,
)

OCR_EXTRACTION_VERSION = 'tesseract-ocr-v1'


class TesseractOcrBackend:
    """Recognize page images with the tesseract CLI (lazily verified)."""

    def __init__(self, dpi: int = 200) -> None:
        # 200 DPI is the quality/speed compromise for retrieval-grade text;
        # raise to 300 only when scans are known to be small type.
        self.dpi = dpi

    @staticmethod
    def available() -> bool:
        return shutil.which('tesseract') is not None

    def extract(self, data: bytes) -> ExtractedDocument:
        if not self.available():
            raise UnsupportedDocumentError(
                'tesseract binary not found; install tesseract-ocr to ingest '
                'scanned PDFs'
            )
        import pymupdf

        try:
            doc = pymupdf.Document(stream=data, filetype='pdf')
        except Exception as exc:  # noqa: BLE001 - boundary conversion
            raise UnsupportedDocumentError(
                f'pymupdf failed to open payload: {exc}'
            ) from exc
        try:
            labels: list[str | None] = []
            pages_blocks: list[list[_RawBlock]] = []
            with tempfile.TemporaryDirectory() as workdir:
                png_path = Path(workdir) / 'page.png'
                for page in doc:
                    pix = page.get_pixmap(dpi=self.dpi)
                    pix.save(png_path)
                    recognized = subprocess.run(  # noqa: S603 - fixed argv
                        [
                            'tesseract',
                            str(png_path),
                            'stdout',
                            '--dpi',
                            str(self.dpi),
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    lines = [
                        line.strip()
                        for line in recognized.stdout.splitlines()
                        if line.strip()
                    ]
                    pages_blocks.append(
                        [_RawBlock(text=line, font_size=None, bold=False) for line in lines]
                    )
                    labels.append(_page_label(page))
        finally:
            doc.close()
        if not pages_blocks:
            raise UnsupportedDocumentError('PDF contains no pages')
        total_lines = sum(len(blocks) for blocks in pages_blocks)
        if total_lines == 0:
            raise UnsupportedDocumentError(
                'OCR recognized no text on any page; scan may be blank or '
                'unreadable'
            )
        stripped = _strip_repeated_headers_footers(pages_blocks)
        return _build_document(labels, pages_blocks, stripped)


def extract_pdf_with_fallback(
    data: bytes,
    *,
    allow_ocr: bool = False,
    dpi: int = 200,
) -> tuple[ExtractedDocument, str]:
    """Born-digital first; optional OCR fallback for scans.

    Returns ``(document, extraction_version)`` so callers can stamp
    provenance: ``pymupdf-v1`` for the text layer, ``tesseract-ocr-v1``
    when recognition produced it.
    """
    from .pdf_backend import PyMuPdfBackend

    try:
        return PyMuPdfBackend().extract(data), 'pymupdf-v1'
    except UnsupportedDocumentError:
        if not allow_ocr:
            raise
    return TesseractOcrBackend(dpi=dpi).extract(data), OCR_EXTRACTION_VERSION
