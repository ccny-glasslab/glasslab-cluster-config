#!/usr/bin/env python3
"""``ask`` CLI: hybrid retrieval over a corpus-RAG SQLite store.

Emits one JSON document to stdout. Insufficient evidence (empty corpus
membership or zero retrieved hits) prints
``{"kind": "insufficient_evidence", "reason": ...}`` and exits 0 — the S2
contract; T11 upgrades this to ``InsufficientCorpusAdvisory``. The CLI never
emits a citation that fails resolution: every cited chunk is re-verified
against the store before output (S1), otherwise it exits nonzero.

With ``--advisory``, a successful retrieval additionally carries an
extractive ``advisory`` document plus its ``advisory_markdown`` rendering;
the insufficient path is unchanged.

The DSN environment variable is read, never logged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.corpus_rag.contracts import EMBED_DIM
from app.corpus_rag.embeddings import OfflineDeterministicEmbedding
from app.corpus_rag.retrieval import (
    CrossEncoderReranker,
    HybridRetriever,
    OfflineReranker,
    RetrievalOptions,
)
from app.storage import SqliteStore

_DENSE_MODES = ('dense', 'hybrid', 'hybrid+rerank')
_FALLBACK_DIMS = 16


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='ask', description='Hybrid retrieval over the corpus-RAG store.'
    )
    parser.add_argument('--store', required=True, help='SQLite store path')
    parser.add_argument('--question', required=True, help='research question')
    parser.add_argument('--corpus', default=None, help='corpus slug filter')
    parser.add_argument(
        '--mode',
        default='hybrid',
        choices=['lexical', 'dense', 'hybrid', 'hybrid+rerank'],
    )
    parser.add_argument('--k', type=int, default=8)
    parser.add_argument('--candidate-k', type=int, default=40)
    parser.add_argument('--expand', action='store_true')
    parser.add_argument(
        '--vector-backend', default='numpy', choices=['numpy', 'pgvector']
    )
    parser.add_argument(
        '--model-id', default='offline-deterministic',
        help='embedding/rerank lineage id recorded in the output',
    )
    parser.add_argument(
        '--reranker', default='offline', choices=['offline', 'cross-encoder']
    )
    parser.add_argument('--json-out', default=None, help='also write JSON here')
    parser.add_argument(
        '--advisory', action='store_true',
        help='also build an extractive method advisory over the hits',
    )
    return parser


def _emit(payload: dict[str, object], json_out: str | None) -> None:
    text = json.dumps(payload, indent=2)
    print(text)
    if json_out:
        Path(json_out).write_text(text + '\n')


def _emit_insufficient(reason: str) -> None:
    print(json.dumps({'kind': 'insufficient_evidence', 'reason': reason}))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    store = SqliteStore(args.store)

    source_ids: list[str] | None = None
    if args.corpus is not None:
        corpus = store.get_corpus(args.corpus)
        members = (
            store.list_corpus_sources(corpus.corpus_id) if corpus else []
        )
        if not members:
            _emit_insufficient(f'corpus {args.corpus!r} has no member sources')
            return 0
        source_ids = members

    vector_index = None
    embedding_provider = None
    if args.mode in _DENSE_MODES:
        from app.corpus_rag.vector_index import NumpyVectorIndex, open_vector_index

        if args.vector_backend == 'pgvector':
            dsn = os.environ.get('CORPUS_RAG_PG_DSN')
            if not dsn:
                parser.error(
                    '--vector-backend pgvector requires CORPUS_RAG_PG_DSN'
                )
            vector_index = open_vector_index(
                'pgvector', dsn=dsn, model_id=args.model_id
            )
            embedding_provider = OfflineDeterministicEmbedding(dims=EMBED_DIM)
        else:
            entries = store.list_rag_chunk_vectors(args.model_id)
            dims = entries[0][0].dims if entries else _FALLBACK_DIMS
            vector_index = NumpyVectorIndex(entries=entries)
            embedding_provider = OfflineDeterministicEmbedding(dims=dims)

    reranker = CrossEncoderReranker() if args.reranker == 'cross-encoder' else OfflineReranker()

    retriever = HybridRetriever(
        store,
        vector_index=vector_index,
        embedding_provider=embedding_provider,
        reranker=reranker,
        model_id=args.model_id,
    )
    result = retriever.retrieve(
        args.question,
        source_ids=source_ids,
        options=RetrievalOptions(
            mode=args.mode,
            k_final=args.k,
            candidate_k=args.candidate_k,
            expand=args.expand,
        ),
    )

    if not result.hits:
        reason = (
            f'no chunks retrieved for question {args.question!r}'
            + (f' within corpus {args.corpus!r}' if args.corpus else '')
        )
        _emit_insufficient(reason)
        return 0

    # S1 guarantee: every emitted citation must resolve against the store.
    stored_ids = {
        row['chunk_id']
        for row in store.list_rag_chunks(source_ids=source_ids, limit=None)
    }
    unresolved = [
        citation.chunk_id
        for citation in result.citations
        if citation.chunk_id not in stored_ids
    ]
    if unresolved:
        raise SystemExit(
            'citation resolution failed for chunk ids: '
            + ', '.join(sorted(unresolved))
        )

    payload = {
        'question': args.question,
        'corpus': args.corpus,
        'mode': args.mode,
        'model_id': args.model_id,
        'plan': {
            'original_query': result.plan.original_query,
            'subqueries': list(result.plan.subqueries),
            'planner_mode': result.plan.planner_mode,
        },
        'hits': [
            {
                'chunk_id': hit.chunk.chunk_id,
                'source_id': hit.chunk.source_id,
                'score': hit.score,
                'section_path': hit.chunk.section_path,
                'page_start': hit.chunk.page_start,
                'page_end': hit.chunk.page_end,
                'text_preview': hit.chunk.text[:200],
            }
            for hit in result.hits
        ],
        'citations': [citation.model_dump() for citation in result.citations],
        'timings': result.timings,
    }
    if args.advisory:
        from app.corpus_rag.advisory import build_method_advisory, render_markdown

        advisory = build_method_advisory(
            objective=args.question,
            corpus_slug=args.corpus or 'default',
            retrieval=result,
            store=store,
            llm=None,
        )
        payload['advisory'] = advisory.model_dump(mode='json')
        payload['advisory_markdown'] = render_markdown(advisory)
    _emit(payload, args.json_out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
