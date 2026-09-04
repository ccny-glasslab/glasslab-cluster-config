"""Fetch recent arXiv preprints and ingest them into the corpus-RAG store.

Sidecar ingestion job (issue #311): query the arXiv API for allowlisted
categories within a date window, download each PDF (size-capped,
digest-verified), feed it through the EXISTING ingest_document path
(extract -> secret-scan -> chunk -> persist), and optionally embed evidence
spans via build_index. Resumable: sources whose canonical URI or content
digest is already in the store are skipped, so re-runs are idempotent.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import json
import sys
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SERVICE_DIR))

from app.corpus_rag.arxiv import (  # noqa: E402
    ALLOWED_CATEGORIES,
    MAX_PDF_BYTES,
    ArxivQuery,
    download_pdf,
    fetch_entries,
    is_oversized,
)
from app.corpus_rag.pipeline import build_index, ingest_document  # noqa: E402


def default_store_path() -> str:
    return '/home/gr66ss/rag-data/orchestrator-rag.db'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', default=default_store_path())
    parser.add_argument('--corpus', default='arxiv-preprints')
    parser.add_argument(
        '--categories',
        nargs='+',
        default=sorted(ALLOWED_CATEGORIES),
        help='arXiv categories; must be within the module allowlist',
    )
    parser.add_argument(
        '--days', type=int, default=1, help='date window: fetch preprints from today-days'
    )
    parser.add_argument('--max-results', type=int, default=50)
    parser.add_argument('--max-pdf-bytes', type=int, default=MAX_PDF_BYTES)
    parser.add_argument('--timeout', type=int, default=90)
    parser.add_argument('--with-index', action='store_true')
    parser.add_argument(
        '--embedding',
        choices=['offline', 'arctic-m', 'arctic-s'],
        default='offline',
    )
    parser.add_argument('--force-index', action='store_true')
    args = parser.parse_args(argv)

    from app.storage import SqliteStore

    date_from = _dt.date.today() - _dt.timedelta(days=args.days)
    query = ArxivQuery(
        categories=tuple(args.categories),
        date_from=date_from,
        max_results=args.max_results,
        max_pdf_bytes=args.max_pdf_bytes,
    )
    entries = fetch_entries(query)

    store = SqliteStore(str(args.store))
    reports = []
    errors: list[str] = []
    skipped_existing: list[str] = []
    skipped_oversized: list[str] = []

    for entry in entries:
        if is_oversized(entry, query.max_pdf_bytes):
            skipped_oversized.append(entry.arxiv_id)
            continue
        if any(source.canonical_uri == entry.pdf_url for source in store.list_knowledge_sources()):
            skipped_existing.append(entry.arxiv_id)
            continue
        try:
            data, digest = download_pdf(
                entry,
                max_pdf_bytes=query.max_pdf_bytes,
                timeout=args.timeout,
            )
            if store.find_knowledge_source(digest=digest, canonical_uri=entry.pdf_url) is not None:
                skipped_existing.append(entry.arxiv_id)
                continue
            reports.append(
                ingest_document(
                    store=store,
                    data=data,
                    canonical_uri=entry.pdf_url,
                    title=entry.title,
                    doc_type='paper',
                    authors=list(entry.authors),
                    year=entry.published.year,
                    doi_isbn_url=entry.arxiv_id,
                    corpus_slug=args.corpus,
                )
            )
        except Exception as exc:  # noqa: BLE001 - per-source isolation
            errors.append(f'{entry.arxiv_id}: {exc}')
            print(f'[ingest-arxiv] failure {entry.arxiv_id}: {exc}', file=sys.stderr)

    index_summary = None
    if args.with_index and reports:
        source_ids = [report.source_id for report in reports]
        if args.embedding == 'offline':
            from app.corpus_rag.embeddings import OfflineDeterministicEmbedding

            provider = OfflineDeterministicEmbedding(dims=16)
        else:
            from app.corpus_rag.embeddings import get_provider

            provider = get_provider(args.embedding)
        try:
            index_summary = build_index(
                store=store,
                source_ids=source_ids,
                provider=provider,
                force=args.force_index,
            )
        finally:
            unload = getattr(provider, 'unload', None)
            if callable(unload):
                unload()

    print(
        json.dumps(
            {
                'query': {
                    'categories': list(query.categories),
                    'date_from': date_from.isoformat(),
                    'max_results': query.max_results,
                    'max_pdf_bytes': query.max_pdf_bytes,
                },
                'entries_found': len(entries),
                'reports': [dataclasses.asdict(report) for report in reports],
                'skipped_existing': skipped_existing,
                'skipped_oversized': skipped_oversized,
                'errors': errors,
                'index': index_summary,
            }
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())