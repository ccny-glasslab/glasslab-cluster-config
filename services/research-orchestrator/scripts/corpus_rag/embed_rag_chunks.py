#!/usr/bin/env python3
"""Embed the corpus-RAG chunks that dense retrieval serves.

The ``/ui`` corpus chat reads ``orchestrator_rag_chunks``; lexical retrieval
needs no vectors, but ``dense``/``hybrid`` modes need the
``orchestrator_rag_chunk_vectors`` rows that ``app/corpus_rag/vector_index.py``
serves. This CLI embeds every evidence-span chunk lacking a vector for the
active model lineage and persists it.

Dry run by default: pass ``--apply`` to write. Idempotent: a chunk whose stored
vector already matches the active model/revision/dims is skipped, so re-running
adds nothing. See ``app/corpus_rag/dense.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SERVICE_DIR))

_DEFAULT_PG_DSN_ENV = 'CORPUS_RAG_PG_DSN'


def _build_provider(choice: str, settings):
    from app.corpus_rag.embeddings import (
        OfflineDeterministicEmbedding,
        get_provider,
    )
    from app.knowledge_dense import create_embedding_provider

    if choice == 'offline':
        return OfflineDeterministicEmbedding(dims=16)
    if choice == 'settings':
        return create_embedding_provider(
            settings.knowledge_embedding_model,
            revision=settings.knowledge_embedding_revision,
        )
    return get_provider(choice)


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
        help='write the embeddings; without it the run is a dry run',
    )
    parser.add_argument(
        '--embedding',
        choices=['settings', 'offline', 'arctic-m', 'arctic-s'],
        default='settings',
        help=(
            'embedding provider; "settings" uses the configured model and '
            'pinned revision (the deployment lineage)'
        ),
    )
    parser.add_argument(
        '--vector-backend',
        choices=['auto', 'store', 'pgvector'],
        default='auto',
        help=(
            'auto: pgvector when a DSN is present, else store bytes; store '
            'writes canonical bytes only; pgvector also fills the halfvec '
            'column HNSW reads'
        ),
    )
    parser.add_argument(
        '--pg-dsn-env',
        default=_DEFAULT_PG_DSN_ENV,
        help='environment variable holding the pgvector DSN (never logged)',
    )
    parser.add_argument('--batch-size', type=int, default=64)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)

    from app.config import Settings
    from app.corpus_rag.dense import build_rag_dense_index, plan_rag_dense_index
    from app.store_factory import build_store

    settings = (
        Settings(store_backend='sqlite', corpus_rag_store_path=args.store)
        if args.store
        else Settings()
    )
    store = build_store(settings)
    provider = _build_provider(args.embedding, settings)

    backend = args.vector_backend
    dsn = os.environ.get(args.pg_dsn_env, '')
    if backend == 'auto':
        backend = 'pgvector' if dsn else 'store'
    if backend == 'pgvector' and not dsn:
        print(json.dumps({'error': f'{args.pg_dsn_env} is not set'}))
        return 2

    try:
        if not args.apply:
            print(json.dumps(plan_rag_dense_index(
                store, provider, force=args.force
            )))
            return 0

        vector_index = None
        if backend == 'pgvector':
            from app.corpus_rag.vector_index import PgVectorIndex

            vector_index = PgVectorIndex(dsn, provider.model_id)
        summary = build_rag_dense_index(
            store,
            provider,
            vector_index=vector_index,
            batch_size=args.batch_size,
            force=args.force,
        )
        summary['backend'] = backend
        print(json.dumps(summary))
        return 0
    finally:
        unload = getattr(provider, 'unload', None)
        if callable(unload):
            unload()


if __name__ == '__main__':
    raise SystemExit(main())
