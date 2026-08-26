"""Batch-ingest staged corpus PDFs into a SQLite store (optionally index).

Reads the eval manifest, ingests every non-skipped entry found under
--raw-dir as <id>.pdf with sha256 verification, registers corpus membership,
and optionally runs offline/real-model vector indexing.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SERVICE_DIR))

from app.corpus_rag.pipeline import build_index, ingest_corpus  # noqa: E402

_MANIFEST_PATH = _SERVICE_DIR / 'eval' / 'corpus_rag' / 'manifest.jsonl'


def default_store_path() -> str:
    return '/home/gr66ss/rag-data/orchestrator-rag.db'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', default=default_store_path())
    parser.add_argument('--raw-dir', default='/home/gr66ss/rag-data/raw')
    parser.add_argument('--corpus', default='statistical-learning-methods')
    parser.add_argument('--manifest', default=str(_MANIFEST_PATH))
    parser.add_argument('--with-index', action='store_true')
    parser.add_argument(
        '--embedding',
        choices=['offline', 'arctic-m', 'arctic-s'],
        default='offline',
    )
    parser.add_argument('--force-index', action='store_true')
    args = parser.parse_args(argv)

    from app.storage import SqliteStore

    store = SqliteStore(str(args.store))
    reports, errors = ingest_corpus(
        store=store,
        corpus_slug=args.corpus,
        raw_dir=Path(args.raw_dir),
        manifest_path=Path(args.manifest),
    )

    index_summary = None
    if args.with_index:
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
                'reports': [dataclasses.asdict(report) for report in reports],
                'errors': errors,
                'index': index_summary,
            }
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
