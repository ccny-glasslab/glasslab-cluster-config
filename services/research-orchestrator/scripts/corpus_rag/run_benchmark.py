"""Run the four-mode retrieval benchmark over eval questions + qrels.

Modes: lexical | dense | hybrid | hybrid+rerank (expansion ablation via
--expand). Qrels keys resolve flexibly: a key matching '<manifest-id>.pdf'
inside an ingested canonical URI maps to that source_id; otherwise the key
is treated literally as a source_id. Metrics are computed at the SOURCE
level (ranked unique sources per query) plus chunk-level diversity stats,
using app.corpus_rag.benchmark utilities.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SERVICE_DIR))

from app.corpus_rag import BenchmarkQuestion, RAG_INDEX_VERSION  # noqa: E402
from app.corpus_rag.benchmark import (  # noqa: E402
    distinct_sources_at_k,
    duplicate_rate_at_k,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from app.corpus_rag.embeddings import OfflineDeterministicEmbedding, get_provider  # noqa: E402
from app.corpus_rag.retrieval import (  # noqa: E402
    HybridRetriever,
    OfflineReranker,
    RetrievalOptions,
)
from app.corpus_rag.vector_index import NumpyVectorIndex  # noqa: E402
from app.storage import SqliteStore  # noqa: E402

MODES = ('lexical', 'dense', 'hybrid', 'hybrid+rerank')


def _load_qrels(path: Path) -> dict[str, dict[str, int]]:
    qrels: dict[str, dict[str, int]] = {}
    lines = path.read_text().splitlines()
    for line in lines[1:]:
        if not line.strip():
            continue
        qid, key, grade = line.split('\t')
        qrels.setdefault(qid, {})[key] = int(grade)
    return qrels


def _build_source_resolver(store: SqliteStore) -> dict[str, str]:
    """Map manifest-style keys ('<id>') and raw source_ids to source_ids."""
    resolver: dict[str, str] = {}
    for source in store.list_knowledge_sources():
        resolver[source.source_id] = source.source_id
        uri = source.canonical_uri.rstrip('/')
        stem = uri.rsplit('/', 1)[-1]
        if stem.endswith('.pdf'):
            resolver[stem[:-4]] = source.source_id
    return resolver


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    parser.add_argument('--questions', required=True)
    parser.add_argument('--qrels', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--corpus', default=None)
    parser.add_argument('--k', type=int, default=10)
    parser.add_argument(
        '--embedding',
        choices=['offline', 'arctic-m', 'arctic-s'],
        default='offline',
    )
    parser.add_argument(
        '--reranker', choices=['offline', 'cross-encoder'], default='offline'
    )
    parser.add_argument('--expand', action='store_true')
    args = parser.parse_args(argv)

    questions = [
        BenchmarkQuestion.model_validate(json.loads(line))
        for line in Path(args.questions).read_text().splitlines()
        if line.strip()
    ]
    raw_qrels = _load_qrels(Path(args.qrels))
    store = SqliteStore(args.store)
    resolver = _build_source_resolver(store)

    member_ids = None
    if args.corpus:
        from app.corpus_rag.corpora import CorpusService

        member_ids = CorpusService(store).member_source_ids(args.corpus) or None

    if args.embedding == 'offline':
        provider = OfflineDeterministicEmbedding(dims=16)
    else:
        provider = get_provider(args.embedding)
    vectors = [
        (meta, blob)
        for meta, blob in store.list_rag_chunk_vectors(provider.model_id)
        if meta.index_version == RAG_INDEX_VERSION
    ]
    chunk_sources = {
        row['chunk_id']: row['source_id']
        for row in store.list_rag_chunks(limit=None)
    }
    vector_index = NumpyVectorIndex(vectors, source_of=chunk_sources)
    reranker = OfflineReranker() if args.reranker == 'offline' else None
    retriever = HybridRetriever(
        store,
        vector_index=vector_index,
        embedding_provider=provider,
        reranker=reranker,
        model_id=provider.model_id,
    )

    results: dict[str, dict[str, float]] = {}
    try:
        for mode in MODES:
            recalls, mrrs, ndcgs, precisions = [], [], [], []
            diversities, dup_rates = [], []
            started = time.perf_counter()
            for question in questions:
                options = RetrievalOptions(
                    mode=mode,  # type: ignore[arg-type]
                    k_final=args.k,
                    candidate_k=max(40, args.k * 4),
                    expand=args.expand,
                )
                outcome = retriever.retrieve(
                    question.text, source_ids=member_ids, options=options
                )
                judged: dict[str, int] = {}
                for key, grade in raw_qrels.get(question.qid, {}).items():
                    source_id = resolver.get(key)
                    if source_id is not None:
                        judged[source_id] = grade
                ranked_sources: list[str] = []
                seen = set()
                for hit in outcome.hits:
                    sid = hit.chunk.source_id
                    if sid not in seen:
                        seen.add(sid)
                        ranked_sources.append(sid)
                recalls.append(recall_at_k(ranked_sources, judged, args.k))
                mrrs.append(mrr_at_k(ranked_sources, judged, args.k))
                ndcgs.append(ndcg_at_k(ranked_sources, judged, args.k))
                precisions.append(precision_at_k(ranked_sources, judged, args.k))
                chunk_ids = [hit.chunk.chunk_id for hit in outcome.hits]
                source_of = {
                    hit.chunk.chunk_id: hit.chunk.source_id for hit in outcome.hits
                }
                diversities.append(
                    float(distinct_sources_at_k(chunk_ids, args.k, source_of))
                )
                dup_rates.append(duplicate_rate_at_k(chunk_ids, args.k))
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            n = max(1, len(questions))
            results[mode] = {
                'recall@k': sum(recalls) / n,
                f'recall@{args.k}': sum(recalls) / n,
                f'mrr@{args.k}': sum(mrrs) / n,
                f'ndcg@{args.k}': sum(ndcgs) / n,
                f'precision@{args.k}': sum(precisions) / n,
                f'distinct_sources@{args.k}': sum(diversities) / n,
                f'duplicate_rate@{args.k}': sum(dup_rates) / n,
                'latency_ms': elapsed_ms,
                'latency_ms_per_question': elapsed_ms / n,
            }
    finally:
        unload = getattr(provider, 'unload', None)
        if callable(unload):
            unload()

    payload = {
        'modes': results,
        'environment': {
            'k': args.k,
            'n_questions': len(questions),
            'embedding_model': provider.model_id,
            'embedding_revision': provider.revision,
            'index_version': RAG_INDEX_VERSION,
            'vectors_loaded': len(vectors),
            'reranker': args.reranker,
            'expand': args.expand,
            'store': args.store,
            'corpus': args.corpus,
        },
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(json.dumps({'written': str(out_path), 'modes': list(results)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
