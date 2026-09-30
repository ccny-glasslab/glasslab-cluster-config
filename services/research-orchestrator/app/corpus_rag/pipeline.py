"""Ingestion/index pipeline composing the corpus_rag modules.

extract (pdf_backend) -> sections (documents) -> chunks (chunking) ->
persist (store, transactional FTS) -> vectors (embeddings). Vector indexing
is deliberately a separate pass so the text pipeline works without torch
loaded; callers must unload embedding providers before constructing
cross-encoder rerankers (RAM discipline on CPU-only hosts).
"""

from __future__ import annotations

import bisect
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.corpus_rag import CorpusManifestEntry, RAG_INDEX_VERSION, ChunkVectorMeta
from app.corpus_rag.retrieval import _is_index_like
from app.storage import SqliteStore

_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / 'eval' / 'corpus_rag' / 'manifest.jsonl'
)
_BOOK_IDS = frozenset({'islr2', 'esl'})
_EMBED_BATCH = 32

# Detected heading titles whose body is a listing rather than prose; matched
# case-insensitively against SectionNode.title and dropped at ingest.
_NON_PROSE_SECTION_TITLES = frozenset(
    {'index', 'contents', 'table of contents', 'bibliography', 'references'}
)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def stage_raw_pdf(data: bytes, raw_root: Path, name: str) -> Path:
    """Atomically persist ``data`` as ``<raw_root>/<name>.pdf``.

    ``name`` must be a single, safe filename component: a manifest id or
    source label that carries a path separator or a ``..`` component would
    otherwise escape the configured raw root. The bytes are written to a
    temporary sibling and ``os.replace``d into place so a crash mid-write
    never leaves a truncated PDF the ingester would later trust.
    """
    if (
        not name
        or name in {'.', '..'}
        or Path(name).name != name
        or '/' in name
        or '\\' in name
        or '\x00' in name
    ):
        raise ValueError(f'unsafe raw-pdf name: {name!r}')
    root = Path(raw_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f'{name}.pdf'
    handle_fd, temporary = tempfile.mkstemp(
        prefix=f'.{name}.', suffix='.pdf.tmp', dir=root
    )
    try:
        with os.fdopen(handle_fd, 'wb') as handle:
            handle.write(data)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return target


@dataclass
class IngestReport:
    source_id: str
    doc_id: str | None
    n_sections: int
    n_section_units: int
    n_evidence_spans: int
    n_vectors: int
    extraction_chars: int


def _page_resolver(document: Any):
    offsets: list[int] = []
    position = 0
    for page in document.pages:
        offsets.append(position)
        position += len(page.text) + 2
    total = max(position - 2, 0)

    def page_for_char(char: int) -> int | None:
        if char < 0 or char >= total:
            return None
        index = bisect.bisect_right(offsets, char) - 1
        return document.pages[index].page_index

    return page_for_char


def _drop_non_prose(
    sections: list[Any], chunks: list[Any]
) -> tuple[list[Any], list[Any]]:
    """Drop index-like chunks plus index/front-matter sections at ingest.

    A denylisted heading title removes its section and every chunk referencing
    it; other index-like chunks are dropped too. Retained chunks never point
    at a removed section.
    """
    kept_sections = [
        section
        for section in sections
        if (section.title or '').strip().lower() not in _NON_PROSE_SECTION_TITLES
    ]
    kept_ids = {section.section_id for section in kept_sections}
    kept_chunks = [
        chunk
        for chunk in chunks
        if (chunk.section_id is None or chunk.section_id in kept_ids)
        and not _is_index_like(chunk.text)
    ]
    return kept_sections, kept_chunks


def ingest_document(
    *,
    store: Any,
    data: bytes,
    canonical_uri: str,
    title: str | None = None,
    doc_type: str = 'paper',
    authors: list[str] | None = None,
    year: int | None = None,
    doi_isbn_url: str | None = None,
    corpus_slug: str | None = None,
    extractor: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> IngestReport:
    """Extract, chunk, and persist one document; vectors are added separately."""
    from app.corpus_rag.chunking import build_chunks
    from app.corpus_rag.documents import (
        assert_no_secrets,
        detect_sections,
        ingest_document_bytes,
    )
    from app.corpus_rag.normalize import normalize_chunks
    from app.corpus_rag.pdf_backend import PyMuPdfBackend

    source, record = ingest_document_bytes(
        store=store,
        data=data,
        canonical_uri=canonical_uri,
        title=title,
        doc_type=doc_type,
        authors=authors,
        year=year,
        doi_isbn_url=doi_isbn_url,
        corpus_slug=corpus_slug,
    )
    if metadata:
        store.save_knowledge_source(
            source.model_copy(
                update={'metadata': {**source.metadata, **metadata}}
            )
        )
    backend = extractor if extractor is not None else PyMuPdfBackend()
    document = backend.extract(data)
    # Fail-closed: scan EXTRACTED text before any derived content persists.
    assert_no_secrets(document.text)
    section_nodes = detect_sections(document)
    sections, chunks = build_chunks(
        document.text,
        section_nodes,
        source_id=source.source_id,
        doc_id=record.doc_id,
        page_for_char=_page_resolver(document),
    )
    chunks = normalize_chunks(chunks)
    sections, chunks = _drop_non_prose(sections, chunks)
    store.replace_rag_sections(record.doc_id, sections)
    store.replace_rag_chunks(source.source_id, chunks)
    return IngestReport(
        source_id=source.source_id,
        doc_id=record.doc_id,
        n_sections=len(sections),
        n_section_units=sum(1 for c in chunks if c.kind == 'section_unit'),
        n_evidence_spans=sum(1 for c in chunks if c.kind == 'evidence_span'),
        n_vectors=0,
        extraction_chars=len(document.text),
    )


def build_index(
    *,
    store: Any,
    source_ids: list[str],
    provider: Any | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Embed evidence spans and persist vectors with per-model lineage.

    Already-indexed chunk_ids (for this model_id) are skipped unless
    ``force`` re-upserts everything.
    """
    from app.corpus_rag.embeddings import encode_vector, get_provider

    if provider is None:
        provider = get_provider('offline')

    existing = {
        meta.chunk_id for meta, _ in store.list_rag_chunk_vectors(provider.model_id)
    }
    rows = store.list_rag_chunks(source_ids=source_ids, kinds=['evidence_span'], limit=None)
    todo = [row for row in rows if force or row['chunk_id'] not in existing]

    indexed = 0
    skipped = 0
    for start in range(0, len(todo), _EMBED_BATCH):
        batch = todo[start : start + _EMBED_BATCH]
        vectors = provider.embed_passages([row['text'] for row in batch])
        for row, vector in zip(batch, vectors):
            meta = ChunkVectorMeta(
                chunk_id=row['chunk_id'],
                model_id=provider.model_id,
                revision=provider.revision,
                dims=int(provider.dims),
                index_version=RAG_INDEX_VERSION,
            )
            try:
                store.upsert_rag_chunk_vectors(meta, encode_vector(vector))
            except sqlite3.IntegrityError:
                # A row whose parent chunk vanished between listing and write
                # must not kill a multi-hour unattended indexing run; skipping
                # is safe because citation resolution independently verifies
                # every chunk against the store before anything is emitted.
                skipped += 1
                print(
                    f'[build_index] skipping unresolvable chunk {meta.chunk_id}',
                    file=sys.stderr,
                )
                continue
            indexed += 1
    return {
        'model_id': provider.model_id,
        'revision': provider.revision,
        'dims': int(provider.dims),
        'n_vectors': indexed,
        'skipped': skipped,
    }


def ingest_corpus(
    *,
    store: SqliteStore | Any,
    corpus_slug: str,
    raw_dir: Path,
    manifest_path: Path | None = None,
) -> tuple[list[IngestReport], list[str]]:
    """Batch-ingest manifest entries staged under ``raw_dir`` as <id>.pdf."""
    path = manifest_path if manifest_path is not None else _MANIFEST_PATH
    entries = [
        CorpusManifestEntry.model_validate(json.loads(line))
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    reports: list[IngestReport] = []
    errors: list[str] = []
    for entry in entries:
        if entry.skip:
            continue
        raw_path = raw_dir / f'{entry.id}.pdf'
        if not raw_path.exists():
            errors.append(f'{entry.id}: missing file {raw_path}')
            continue
        if entry.sha256 is not None and _sha256_of(raw_path) != entry.sha256:
            errors.append(f'{entry.id}: sha256 mismatch vs manifest')
            continue
        try:
            reports.append(
                ingest_document(
                    store=store,
                    data=raw_path.read_bytes(),
                    canonical_uri=raw_path.resolve().as_uri(),
                    title=entry.title,
                    doc_type='book' if entry.id in _BOOK_IDS else 'paper',
                    corpus_slug=corpus_slug,
                )
            )
        except Exception as exc:  # noqa: BLE001 - isolate per-source failures
            errors.append(f'{entry.id}: {exc}')
    return reports, errors
