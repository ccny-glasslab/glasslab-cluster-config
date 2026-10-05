#!/usr/bin/env python3
"""Heal knowledge sources that have no chunks by re-ingesting their raw PDF.

GitHub issue #622: ``ingest_arxiv.py`` skips sources that already exist, so a
live source whose raw PDF vanished from the shared PVC can never regain its
``rag_chunks``. This CLI re-ingests a still-present local ``file://`` PDF, or
re-downloads and re-stages an arXiv PDF whose staged file is missing, then runs
the normal ``ingest_document`` path.

Dry run by default: pass ``--apply`` to write. Idempotent: healed sources have
chunks afterward and drop out of the next scan, so a second ``--apply`` adds
nothing and makes no network calls. Sources it cannot recover (``upload://``,
paths outside the raw root, or a missing non-arXiv filename) are reported
``unrecoverable`` and never fabricated.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SERVICE_DIR))

from app.corpus_rag.reingest import reingest_missing_chunks  # noqa: E402


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
        help='write the re-ingest; without it the run is a dry run',
    )
    parser.add_argument(
        '--raw-root',
        default=None,
        help='staged PDF root; defaults to settings.corpus_rag_raw_root',
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help='heal at most this many sources',
    )
    parser.add_argument(
        '--source-id',
        action='append',
        default=None,
        help='restrict to these knowledge source ids (repeatable)',
    )
    parser.add_argument(
        '--sleep-seconds',
        type=float,
        default=3.0,
        help='delay between arXiv fetches (first fetch is immediate)',
    )
    parser.add_argument(
        '--diagnose',
        action='store_true',
        help='classify each failure (secret-rejected vs other) and print tracebacks',
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
    raw_root = Path(args.raw_root) if args.raw_root else Path(settings.corpus_rag_raw_root)
    report = reingest_missing_chunks(
        store,
        raw_root=raw_root,
        apply=args.apply,
        limit=args.limit,
        source_ids=args.source_id,
        sleep_seconds=args.sleep_seconds,
        diagnose=args.diagnose,
    )
    print(json.dumps(dataclasses.asdict(report)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
