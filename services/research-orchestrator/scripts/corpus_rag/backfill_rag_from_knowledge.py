#!/usr/bin/env python3
"""Project the live knowledge store into the configured corpus-RAG store.

The operator ``/ui`` chat reads ``orchestrator_rag_chunks``; the live corpus
ingested through the operator upload path lives only in
``orchestrator_knowledge_*`` and its raw bytes were discarded. This CLI bridges
that gap by projecting the retained knowledge text into the rag tables so the
``/ui`` chat can cite it.

Dry run by default: pass ``--apply`` to write. Idempotent: a source that
already has a ``rag_document`` is skipped, so repeated runs add nothing. It
never invents bytes or page numbers; see ``app/corpus_rag/backfill.py``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SERVICE_DIR))

from app.corpus_rag.backfill import (  # noqa: E402
    DEFAULT_CORPUS_SLUG,
    DEFAULT_CORPUS_TITLE,
    backfill_knowledge_into_rag,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--store',
        default=None,
        help=(
            'SQLite override for local runs; omit to use the configured store '
            '(GLASSLAB_ORCHESTRATOR_STORE_BACKEND)'
        ),
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='write the projection; without it the run is a dry run',
    )
    parser.add_argument('--corpus', default=DEFAULT_CORPUS_SLUG)
    parser.add_argument('--corpus-title', default=DEFAULT_CORPUS_TITLE)
    parser.add_argument(
        '--source-id',
        action='append',
        default=None,
        help='restrict to these knowledge source ids (repeatable)',
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help='stop after adding at most this many sources',
    )
    args = parser.parse_args(argv)

    from app.config import Settings
    from app.store_factory import build_store

    settings = (
        Settings(store_backend='sqlite', corpus_rag_store_path=args.store)
        if args.store
        else Settings()
    )
    store = build_store(settings)
    report = backfill_knowledge_into_rag(
        store,
        apply=args.apply,
        corpus_slug=args.corpus,
        corpus_title=args.corpus_title,
        source_ids=args.source_id,
        limit=args.limit,
    )
    print(json.dumps(dataclasses.asdict(report)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
