"""Build the dense vector index for ingested corpus sources.

Persists vectors through the ResearchStore (canonical bytes + lineage) and,
with --vector-backend pgvector, additionally writes each embedding into the
halfvec column so HNSW-backed queries work against the dev container.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SERVICE_DIR))

_PG_DSN_ENV = 'CORPUS_RAG_PG_DSN'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    parser.add_argument('--source-id', action='append', dest='source_ids')
    parser.add_argument('--corpus', default=None)
    parser.add_argument(
        '--embedding',
        choices=['offline', 'arctic-m', 'arctic-s'],
        default='offline',
    )
    parser.add_argument(
        '--vector-backend', choices=['numpy', 'pgvector'], default='numpy'
    )
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)

    from app.corpus_rag.pipeline import build_index
    from app.storage import SqliteStore

    store = SqliteStore(args.store)
    source_ids = list(args.source_ids or [])
    if not source_ids and args.corpus:
        from app.corpus_rag.corpora import CorpusService

        source_ids = CorpusService(store).member_source_ids(args.corpus)
    if not source_ids:
        print(json.dumps({'error': 'no source ids selected'}))
        return 2

    if args.embedding == 'offline':
        from app.corpus_rag.embeddings import OfflineDeterministicEmbedding

        provider = OfflineDeterministicEmbedding(dims=16)
    else:
        from app.corpus_rag.embeddings import get_provider

        provider = get_provider(args.embedding)

    summary = None
    try:
        if args.vector_backend == 'pgvector':
            dsn = os.environ.get(_PG_DSN_ENV)
            if not dsn:
                print(json.dumps({'error': f'{_PG_DSN_ENV} is not set'}))
                return 2
            from app.corpus_rag.vector_index import PgVectorIndex

            pg_index = PgVectorIndex(dsn, provider.model_id)
            rows = store.list_rag_chunks(
                source_ids=source_ids, kinds=['evidence_span'], limit=None
            )
            existing = {
                meta.chunk_id
                for meta, _ in store.list_rag_chunk_vectors(provider.model_id)
            }
            for row in rows:
                if row['chunk_id'] in existing and not args.force:
                    continue
            summary = build_index(
                store=store,
                source_ids=source_ids,
                provider=provider,
                force=args.force,
            )
            for meta, blob in store.list_rag_chunk_vectors(provider.model_id):
                from numpy import frombuffer

                pg_index.add(meta, frombuffer(blob, dtype='<f4'))
        else:
            summary = build_index(
                store=store,
                source_ids=source_ids,
                provider=provider,
                force=args.force,
            )
    finally:
        unload = getattr(provider, 'unload', None)
        if callable(unload):
            unload()

    print(json.dumps({'index': summary, 'backend': args.vector_backend}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
