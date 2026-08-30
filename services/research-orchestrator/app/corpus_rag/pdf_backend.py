"""Born-digital PDF text extraction behind a swappable backend protocol.

OCR is explicitly out of scope: scanned or image-only PDFs raise
:class:`UnsupportedDocumentError` instead of attempting recognition.

The default :class:`PyMuPdfBackend` depends on ``pymupdf``, which is
distributed under the GNU AGPL. Extraction goes through the
:class:`PdfExtractor` protocol, so deployments that cannot accept AGPL terms
can swap in a differently-licensed parser without touching callers.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    'ExtractedBlock',
    'ExtractedDocument',
    'ExtractedPage',
    'PdfExtractor',
    'PyMuPdfBackend',
    'UnsupportedDocumentError',
]

_PDF_MAGIC = b'%PDF'
_PAGE_SEPARATOR = '\n\n'
_BLOCK_SEPARATOR = '\n'
_BOLD_FLAG = 16  # pymupdf span flag bit 2**4 marks bold text.
_SCANNED_PAGE_SHARE = 0.8
_HEADER_FOOTER_MAX_CHARS = 120
_HEADER_FOOTER_MIN_SHARE = 0.6


class UnsupportedDocumentError(Exception):
    """Raised when a payload cannot be extracted as born-digital text."""


class ExtractedBlock(BaseModel):
    """One contiguous text run inside :attr:`ExtractedDocument.text`."""

    model_config = ConfigDict(extra='forbid')

    text: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    font_size: float | None = None
    bold: bool = False

    @model_validator(mode='after')
    def _char_end_after_start(self) -> 'ExtractedBlock':
        if self.char_end <= self.char_start:
            raise ValueError('char_end must be greater than char_start')
        return self


class ExtractedPage(BaseModel):
    model_config = ConfigDict(extra='forbid')

    page_index: int = Field(ge=0)
    label: str | None = None
    text: str
    blocks: list[ExtractedBlock] = Field(default_factory=list)


class ExtractedDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')

    n_pages: int = Field(ge=1)
    pages: list[ExtractedPage]
    text: str
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode='after')
    def _text_matches_pages_and_block_offsets(self) -> 'ExtractedDocument':
        if len(self.pages) != self.n_pages:
            raise ValueError('n_pages must match the number of pages')
        assembled = _PAGE_SEPARATOR.join(page.text for page in self.pages)
        if self.text != assembled:
            raise ValueError(
                "document.text must equal '\\n\\n'.join(page.text for page in pages)"
            )
        cursor = 0
        for page in self.pages:
            for block in page.blocks:
                if self.text[block.char_start:block.char_end] != block.text:
                    raise ValueError(
                        f'block {block.char_start}:{block.char_end} does not index document.text'
                    )
            cursor += len(page.text) + len(_PAGE_SEPARATOR)
        return self


@runtime_checkable
class PdfExtractor(Protocol):
    def extract(self, data: bytes) -> ExtractedDocument: ...


@dataclass(frozen=True, slots=True)
class _RawBlock:
    text: str
    font_size: float | None
    bold: bool


def _page_label(page: object) -> str | None:
    try:
        return page.get_label()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - third-party API varies across versions
        return None


def _page_blocks(page: object) -> list[_RawBlock]:
    try:
        raw = page.get_text('dict', sort=True)  # type: ignore[attr-defined]
    except TypeError:  # older pymupdf without the sort kwarg
        raw = page.get_text('dict')  # type: ignore[attr-defined]
    lines: list[tuple[float, float, list[dict]]] = []
    for block in raw.get('blocks', []):
        if block.get('type') != 0:
            continue
        for line in block.get('lines', []):
            spans = [span for span in line.get('spans', []) if span.get('text')]
            if spans:
                lines.append((round(line['bbox'][1], 1), line['bbox'][0], spans))
    lines.sort(key=lambda item: (item[0], item[1]))
    raw_blocks: list[_RawBlock] = []
    for _, _, spans in lines:
        text = ''.join(span['text'] for span in spans).strip()
        if not text:
            continue
        sizes = [float(span['size']) for span in spans]
        raw_blocks.append(
            _RawBlock(
                text=text,
                font_size=statistics.median(sizes),
                bold=any(bool(span['flags'] & _BOLD_FLAG) for span in spans),
            )
        )
    return raw_blocks


def _strip_repeated_headers_footers(pages: list[list[_RawBlock]]) -> int:
    counts: dict[str, int] = {}
    for blocks in pages:
        if not blocks:
            continue
        edge_texts = {blocks[0].text.strip()}
        if len(blocks) > 1:
            edge_texts.add(blocks[-1].text.strip())
        for candidate in edge_texts:
            if candidate:
                counts[candidate] = counts.get(candidate, 0) + 1
    repeated = {
        text
        for text, count in counts.items()
        if len(text) <= _HEADER_FOOTER_MAX_CHARS
        and count >= _HEADER_FOOTER_MIN_SHARE * len(pages)
    }
    if not repeated:
        return 0
    removed = 0
    for blocks in pages:
        survivors = [
            block
            for index, block in enumerate(blocks)
            if not (
                index in (0, len(blocks) - 1) and block.text.strip() in repeated
            )
        ]
        removed += len(blocks) - len(survivors)
        blocks[:] = survivors
    return removed


def _build_document(
    labels: list[str | None], pages_blocks: list[list[_RawBlock]], stripped: int
) -> ExtractedDocument:
    pages: list[ExtractedPage] = []
    cursor = 0
    n_blocks = 0
    for page_index, (label, blocks) in enumerate(zip(labels, pages_blocks)):
        page_start = cursor
        page_blocks: list[ExtractedBlock] = []
        offset = page_start
        for raw in blocks:
            page_blocks.append(
                ExtractedBlock(
                    text=raw.text,
                    char_start=offset,
                    char_end=offset + len(raw.text),
                    font_size=raw.font_size,
                    bold=raw.bold,
                )
            )
            offset += len(raw.text) + len(_BLOCK_SEPARATOR)
        page_text = _BLOCK_SEPARATOR.join(block.text for block in blocks)
        pages.append(
            ExtractedPage(
                page_index=page_index, label=label, text=page_text, blocks=page_blocks
            )
        )
        n_blocks += len(page_blocks)
        cursor = page_start + len(page_text) + len(_PAGE_SEPARATOR)
    return ExtractedDocument(
        n_pages=len(pages),
        pages=pages,
        text=_PAGE_SEPARATOR.join(page.text for page in pages),
        metadata={
            'extraction_version': 'pymupdf-v1',
            'n_blocks': n_blocks,
            'stripped_headers_footers': stripped,
        },
    )


class PyMuPdfBackend:
    """Extract born-digital text with pymupdf, imported lazily per call."""

    def extract(self, data: bytes) -> ExtractedDocument:
        pymupdf = self._import_pymupdf()
        if not data:
            raise UnsupportedDocumentError('empty PDF payload')
        if not data.startswith(_PDF_MAGIC):
            raise UnsupportedDocumentError(
                "payload does not start with '%PDF'; not a PDF"
            )
        try:
            doc = pymupdf.Document(stream=data, filetype='pdf')
        except Exception as exc:  # noqa: BLE001 - boundary conversion of parser errors
            raise UnsupportedDocumentError(f'pymupdf failed to open payload: {exc}') from exc
        try:
            labels = [_page_label(page) for page in doc]
            pages_blocks = [_page_blocks(page) for page in doc]
        finally:
            doc.close()
        if not pages_blocks:
            raise UnsupportedDocumentError('PDF contains no pages')
        empty_pages = sum(
            1 for blocks in pages_blocks if not any(b.text for b in blocks)
        )
        if empty_pages / len(pages_blocks) > _SCANNED_PAGE_SHARE:
            raise UnsupportedDocumentError(
                'scanned or image-only PDF; OCR out of scope'
            )
        stripped = _strip_repeated_headers_footers(pages_blocks)
        return _build_document(labels, pages_blocks, stripped)

    @staticmethod
    def _import_pymupdf():
        try:
            import pymupdf
        except ImportError as exc:
            raise UnsupportedDocumentError(
                'pymupdf backend dependency is not installed'
            ) from exc
        return pymupdf
