"""Document ingestion and section detection for the corpus-RAG prototype.

``ingest_document_bytes`` is fail-closed: it mirrors ``knowledge_manager``'s
secret filtering (content plus canonical-URI patterns) before any store write,
deduplicates re-ingested sources by (digest, canonical_uri), and optionally
registers the source in a corpus. ``detect_sections`` duck-types against
``pdf_backend.ExtractedDocument`` and never imports the PDF backend.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from app.corpus_rag.contracts import CorpusRecord, RagDocumentRecord, utc_now
from app.knowledge_manager import (
    SECRET_PATTERNS,
    SECRET_PATH_PATTERNS,
    KnowledgeError,
)
from app.schemas import KnowledgeSource, SourceType

EXTRACTION_VERSION = 'pymupdf-v1'

DocType = Literal['book', 'paper', 'reference', 'other']

# '1', '1.1', '2.3.4' followed by optional punctuation then a title word.
_NUMBERED_HEADING = re.compile(r'^(\d+(?:\.\d+)*)[.)]?\s+(\S.*)$')

# A heading candidate must be clearly larger than body text.
_HEADING_SIZE_RATIO = 1.12
_HEADING_MAX_CHARS = 120

_FRONT_MATTER_PATH = '0'
_FRONT_MATTER_TITLE = 'Front matter'

# Page texts are joined with '\n\n' in ExtractedDocument.text.
_PAGE_SEPARATOR_WIDTH = 2


@dataclass(frozen=True, slots=True)
class SectionNode:
    """One node of a document's heading tree; spans index into document.text."""

    path: str
    title: str | None
    level: int
    start_char: int
    end_char: int
    page_start: int | None = None
    page_end: int | None = None


def _modal_body_font_size(blocks: list[Any]) -> float | None:
    """Most common rounded font size across all blocks (ties: larger wins)."""
    sizes = [
        round(block.font_size)
        for block in blocks
        if block.font_size is not None
    ]
    if not sizes:
        return None
    counts = Counter(sizes)
    top_count = max(counts.values())
    body_size = max(
        size for size, count in counts.items() if count == top_count
    )
    return float(body_size)


def _numbered_heading(text: str) -> tuple[str, str] | None:
    match = _NUMBERED_HEADING.match(text.strip())
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


@dataclass(slots=True)
class _DraftSection:
    path: str
    title: str | None
    level: int
    start_char: int
    heading_size: float | None = None  # set only for unnumbered headings


def detect_sections(document: Any) -> list[SectionNode]:
    """Build contiguous sections from an extracted document.

    Numbered headings ('1 Introduction', '1.1 Details') define their own dotted
    path and level. Unnumbered large-font headings get the next free top-level
    number and a level equal to their size's rank among distinct heading sizes
    (largest = 1). Body blocks attach to the most recent heading; content
    before any heading becomes the '0 Front matter' section.
    """
    blocks = sorted(
        (block for page in document.pages for block in page.blocks),
        key=lambda block: block.char_start,
    )
    body_font_size = _modal_body_font_size(blocks)

    raw_drafts: list[tuple[str, _DraftSection]] = []
    for block in blocks:
        numbered = _numbered_heading(block.text)
        if numbered is not None:
            number, title = numbered
            raw_drafts.append(
                (
                    'numbered',
                    _DraftSection(
                        path=number,
                        title=title,
                        level=number.count('.') + 1,
                        start_char=block.char_start,
                    ),
                )
            )
            continue
        if (
            block.font_size is not None
            and body_font_size is not None
            and len(block.text) < _HEADING_MAX_CHARS
            and block.font_size >= _HEADING_SIZE_RATIO * body_font_size
        ):
            raw_drafts.append(
                (
                    'size',
                    _DraftSection(
                        path='',
                        title=block.text.strip(),
                        level=0,  # resolved from size rank below
                        start_char=block.char_start,
                        heading_size=float(block.font_size),
                    ),
                )
            )

    # Unnumbered headings take free top-level numbers AFTER the full scan so
    # they can never collide with numbered paths appearing later in the doc.
    used_top_level = {
        int(kind_path[1].path.split('.')[0])
        for kind_path in raw_drafts
        if kind_path[0] == 'numbered'
    }
    next_free = 1
    drafts: list[_DraftSection] = []
    for kind, draft in raw_drafts:
        if kind == 'numbered':
            drafts.append(draft)
            continue
        while next_free in used_top_level:
            next_free += 1
        draft.path = str(next_free)
        drafts.append(draft)
        next_free += 1

    size_rank = {
        size: rank + 1
        for rank, size in enumerate(
            sorted(
                {d.heading_size for d in drafts if d.heading_size},
                reverse=True,
            )
        )
    }
    for draft in drafts:
        if draft.heading_size is not None:
            draft.level = size_rank[draft.heading_size]

    if not drafts or drafts[0].start_char != 0:
        drafts.insert(
            0,
            _DraftSection(_FRONT_MATTER_PATH, _FRONT_MATTER_TITLE, 1, 0),
        )

    text_length = len(document.text)
    page_bounds = _page_bounds(document)
    bounds: list[SectionNode] = []
    for index, draft in enumerate(drafts):
        end_char = (
            drafts[index + 1].start_char
            if index + 1 < len(drafts)
            else text_length
        )
        page_start = _page_for_char(page_bounds, draft.start_char)
        page_end = _page_for_char(page_bounds, end_char - 1)
        if page_start is not None and page_end is not None:
            page_end = max(page_start, page_end)
        bounds.append(
            SectionNode(
                path=draft.path,
                title=draft.title,
                level=draft.level,
                start_char=draft.start_char,
                end_char=end_char,
                page_start=page_start,
                page_end=page_end,
            )
        )
    return bounds


def _page_bounds(document: Any) -> list[tuple[int, int]]:
    bounds: list[tuple[int, int]] = []
    offset = 0
    for page in document.pages:
        bounds.append((offset, offset + len(page.text)))
        offset += len(page.text) + _PAGE_SEPARATOR_WIDTH
    return bounds


def _page_for_char(page_bounds: list[tuple[int, int]], char_index: int) -> int | None:
    page_index: int | None = None
    for index, (start, _) in enumerate(page_bounds):
        if start <= char_index:
            page_index = index
        else:
            break
    return page_index


def document_id_for_source(source_id: str) -> str:
    """Deterministic rag-document id so re-ingest upserts one row."""
    return hashlib.sha256(f'rag-document:{source_id}'.encode('utf-8')).hexdigest()


def assert_no_secrets(text: str) -> None:
    """Fail-closed secret scan over extracted document text (reject-only)."""
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise KnowledgeError(
                f'document matches secret pattern {pattern.pattern!r}'
            )


def ingest_document_bytes(
    *,
    store: Any,
    data: bytes,
    canonical_uri: str,
    title: str | None = None,
    doc_type: DocType = 'paper',
    authors: list[str] | None = None,
    year: int | None = None,
    doi_isbn_url: str | None = None,
    corpus_slug: str | None = None,
) -> tuple[KnowledgeSource, RagDocumentRecord]:
    """Fail-closed ingestion of one document into the knowledge store.

    Raises ``KnowledgeError`` on non-UTF-8 text input or secret-pattern
    matches in either the decoded text or the canonical URI, before any
    store write. Binary PDF payloads (``%PDF`` magic) defer content scanning
    to :func:`assert_no_secrets` over the EXTRACTED text — raw PDF bytes are
    binary and must never be strict-decoded. Re-ingesting identical bytes
    under the same URI preserves the original source identity (source_id,
    ingested_at) while refreshing metadata.
    """
    for pattern in SECRET_PATH_PATTERNS:
        if pattern.search(canonical_uri):
            raise KnowledgeError(
                f'document matches secret pattern {pattern.pattern!r}'
            )

    if not data.startswith(b'%PDF'):
        try:
            text = data.decode('utf-8', errors='strict')
        except UnicodeDecodeError as error:
            raise KnowledgeError(
                'document bytes fail UTF-8 decode; treating as secret-suspect'
            ) from error
        assert_no_secrets(text)

    digest = hashlib.sha256(data).hexdigest()
    incoming_metadata: dict[str, Any] = {
        'authors': list(authors or []),
        'year': year,
        'doi_isbn_url': doi_isbn_url,
        'extraction_version': EXTRACTION_VERSION,
    }

    existing = store.find_knowledge_source(digest=digest, canonical_uri=canonical_uri)
    if existing is not None:
        source = existing.model_copy(
            update={
                'title': title if title is not None else existing.title,
                'metadata': {**existing.metadata, **incoming_metadata},
            }
        )
    else:
        source = KnowledgeSource(
            source_type=(
                SourceType.PAPER if doc_type == 'paper' else SourceType.DOCUMENTATION
            ),
            canonical_uri=canonical_uri,
            digest=digest,
            ingested_at=utc_now(),
            title=title,
            metadata=dict(incoming_metadata),
        )
    store.save_knowledge_source(source)

    record = RagDocumentRecord(
        doc_id=document_id_for_source(source.source_id),
        source_id=source.source_id,
        doc_type=doc_type,
        title=title,
        authors=list(authors or []),
        year=year,
        doi_isbn_url=doi_isbn_url,
        extraction_version=EXTRACTION_VERSION,
    )
    store.upsert_rag_document(record)

    if corpus_slug is not None:
        corpus = store.get_corpus(corpus_slug) or store.create_corpus(
            CorpusRecord(slug=corpus_slug)
        )
        store.add_corpus_source(corpus.corpus_id, source.source_id)

    return source, record
