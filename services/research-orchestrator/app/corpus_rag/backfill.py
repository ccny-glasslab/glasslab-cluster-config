"""Project the live knowledge store into the corpus-RAG store.

The operator ``/ui`` chat and PDF viewer read ``orchestrator_rag_documents`` /
``orchestrator_rag_sections`` / ``orchestrator_rag_chunks``. Sources ingested
through the operator upload path (:meth:`app.knowledge_manager.KnowledgeManager.
ingest_bytes`) live only in ``orchestrator_knowledge_*`` and never reach the
rag tables. Their raw bytes are discarded at upload, so the PDF pipeline cannot
re-ingest them -- but the extracted text and its chunking were retained.

This module bridges that gap by projecting the already-extracted knowledge text
into the rag tables. It is deliberately narrow and honest:

* It is additive and idempotent: a source that already has a ``rag_document``
  is skipped, and re-running never duplicates rows.
* It never fabricates bytes or page geometry. Projected chunks carry no
  ``page_start`` / ``page_end``, so the ``/ui`` PDF viewer will not offer a
  viewer link for them (there is no file to serve). It makes the live corpus
  retrievable by the lexical ``/ui`` chat, which reads only ``rag_chunks``.
* Every projected chunk keeps the knowledge ``chunk_id`` and ``digest`` so the
  provenance chain back to ``orchestrator_knowledge_*`` is intact, and every
  document/chunk is stamped with :data:`BACKFILL_INDEX_VERSION` so the backfill
  can be identified (and reversed) without touching a real pipeline ingest.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.corpus_rag.contracts import (
    CorpusRecord,
    RagChunkRecord,
    RagDocumentRecord,
)
from app.schemas import KnowledgeChunk, KnowledgeSource, SourceType
from app.storage import RecordNotFound

BACKFILL_INDEX_VERSION = 'knowledge-backfill-v1'
BACKFILL_EXTRACTION_VERSION = 'knowledge-backfill-v1'
DEFAULT_CORPUS_SLUG = 'live-knowledge'
DEFAULT_CORPUS_TITLE = 'Live knowledge corpus (backfilled)'

_DOC_TYPE_BY_SOURCE = {
    SourceType.PAPER.value: 'paper',
    SourceType.DOCUMENTATION.value: 'reference',
}


@dataclass
class BackfillSourceReport:
    """Per-source outcome of one backfill pass."""

    source_id: str
    canonical_uri: str
    status: str  # 'added' (applied) | 'would-add' (dry-run) | 'skipped-empty'
    chunk_count: int


@dataclass
class BackfillReport:
    """Aggregate outcome of one backfill pass."""

    corpus_slug: str
    apply: bool
    considered: int = 0
    skipped_existing: int = 0
    skipped_empty: int = 0
    added_sources: int = 0
    added_chunks: int = 0
    sources: list[BackfillSourceReport] = field(default_factory=list)


def _stable_doc_id(source_id: str) -> str:
    """Deterministic doc id so a re-run targets the same row (idempotency)."""
    digest = hashlib.sha256(f'knowledge-backfill:{source_id}'.encode()).hexdigest()
    return f'kb-{digest[:32]}'


def _document_for(source: KnowledgeSource) -> RagDocumentRecord:
    doc_type = _DOC_TYPE_BY_SOURCE.get(source.source_type.value, 'other')
    return RagDocumentRecord(
        doc_id=_stable_doc_id(source.source_id),
        source_id=source.source_id,
        doc_type=doc_type,
        title=source.title,
        extraction_version=BACKFILL_EXTRACTION_VERSION,
        metadata={
            'backfill': {
                'canonical_uri': source.canonical_uri,
                'digest': source.digest,
                'source_type': source.source_type.value,
            }
        },
    )


def _chunk_record(chunk: KnowledgeChunk, doc_id: str) -> RagChunkRecord:
    return RagChunkRecord(
        chunk_id=chunk.chunk_id,
        source_id=chunk.source_id,
        doc_id=doc_id,
        kind='evidence_span',
        chunk_index=chunk.chunk_index,
        text=chunk.text,
        digest=chunk.digest,
        token_count=chunk.token_count,
        index_version=BACKFILL_INDEX_VERSION,
    )


def _has_rag_document(store, source_id: str) -> bool:
    try:
        store.get_rag_document(source_id)
    except RecordNotFound:
        return False
    return True


def backfill_knowledge_into_rag(
    store,
    *,
    apply: bool = False,
    corpus_slug: str = DEFAULT_CORPUS_SLUG,
    corpus_title: str = DEFAULT_CORPUS_TITLE,
    source_ids: list[str] | None = None,
    limit: int | None = None,
) -> BackfillReport:
    """Project knowledge sources lacking a rag document into the rag store.

    ``apply=False`` (the default) is a dry run: it reports what would change
    without writing. ``source_ids`` and ``limit`` narrow the pass for a staged
    rollout. Returns a :class:`BackfillReport`.
    """
    report = BackfillReport(corpus_slug=corpus_slug, apply=apply)
    wanted = set(source_ids) if source_ids else None

    corpus_id: str | None = None
    if apply:
        corpus = store.get_corpus(corpus_slug)
        if corpus is None:
            corpus = store.create_corpus(
                CorpusRecord(
                    slug=corpus_slug,
                    title=corpus_title,
                    metadata={'backfill': BACKFILL_INDEX_VERSION},
                )
            )
        corpus_id = corpus.corpus_id

    remaining = limit
    for source in store.list_knowledge_sources():
        if wanted is not None and source.source_id not in wanted:
            continue
        if remaining is not None and remaining <= 0:
            break
        if _has_rag_document(store, source.source_id):
            report.skipped_existing += 1
            continue
        chunks = store.list_knowledge_chunks(source.source_id)
        if not chunks:
            report.skipped_empty += 1
            continue

        document = _document_for(source)
        records = [_chunk_record(chunk, document.doc_id) for chunk in chunks]
        report.considered += 1
        report.added_sources += 1
        report.added_chunks += len(records)
        report.sources.append(
            BackfillSourceReport(
                source_id=source.source_id,
                canonical_uri=source.canonical_uri,
                status='added' if apply else 'would-add',
                chunk_count=len(records),
            )
        )
        if remaining is not None:
            remaining -= 1

        if apply:
            store.upsert_rag_document(document)
            store.replace_rag_chunks(source.source_id, records)
            if corpus_id is not None:
                store.add_corpus_source(corpus_id, source.source_id)

    return report
