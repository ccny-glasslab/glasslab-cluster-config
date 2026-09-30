"""Run the chat-service eval harness over the gold QA set.

Answers every gold question through :class:`CorpusChatService` and reports
four metric groups: retrieval (source-level ranking against graded
relevance), citation (precision and resolution rate), faithfulness (exact
quote rate via ``app.citation_locator.classify_citation``), and abstention
accuracy against the gold answerable/unanswerable labels. Emits one JSON
document to ``--out``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_SERVICE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SERVICE_DIR))

from app.corpus_rag import RAG_INDEX_VERSION, ChatAnswer  # noqa: E402
from app.corpus_rag.benchmark import (  # noqa: E402
    abstention_accuracy,
    citation_precision,
    citation_resolution_rate,
    distinct_sources_at_k,
    duplicate_rate_at_k,
    faithfulness,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from app.corpus_rag.chat import CorpusChatService  # noqa: E402
from app.corpus_rag.embeddings import (  # noqa: E402
    OfflineDeterministicEmbedding,
    get_provider,
)
from app.corpus_rag.retrieval import (  # noqa: E402
    HybridRetriever,
    RetrievalOptions,
    RetrievalResult,
)
from app.corpus_rag.vector_index import NumpyVectorIndex  # noqa: E402
from app.storage import SqliteStore  # noqa: E402

MODES = ('lexical', 'dense', 'hybrid', 'hybrid+rerank')


def _load_gold(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def _build_source_resolver(store: SqliteStore) -> dict[str, str]:
    """Map manifest-style keys ('<id>') and raw source_ids to source_ids."""
    resolver: dict[str, str] = {}
    for source in store.list_knowledge_sources():
        resolver[source.source_id] = source.source_id
        stem = source.canonical_uri.rstrip('/').rsplit('/', 1)[-1]
        if stem.endswith('.pdf'):
            resolver[stem[:-4]] = source.source_id
    return resolver


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _faithfulness_pairs(
    answer: ChatAnswer, outcome: RetrievalResult
) -> list[tuple[str, str]]:
    text_by_source: dict[str, str] = {}
    for hit in outcome.hits:
        text_by_source.setdefault(hit.chunk.source_id, hit.chunk.text)
    return [
        (citation.excerpt, text_by_source.get(citation.source_id, ''))
        for citation in answer.citations
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    parser.add_argument('--gold', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--corpus', default=None)
    parser.add_argument('--k', type=int, default=5)
    parser.add_argument('--mode', choices=MODES, default='lexical')
    parser.add_argument(
        '--embedding',
        choices=['offline', 'arctic-m', 'arctic-s'],
        default='offline',
    )
    args = parser.parse_args(argv)

    gold = _load_gold(Path(args.gold))
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
    retriever = HybridRetriever(
        store,
        vector_index=vector_index,
        embedding_provider=provider,
        model_id=provider.model_id,
    )
    chat = CorpusChatService(
        store,
        top_k=args.k,
        retrieval_mode=args.mode,  # type: ignore[arg-type]
        vector_index=vector_index,
        embedding_provider=provider,
    )
    known_source_ids = set(chunk_sources.values())

    recalls: list[float] = []
    precisions: list[float] = []
    mrrs: list[float] = []
    ndcgs: list[float] = []
    diversities: list[float] = []
    dup_rates: list[float] = []
    citation_precisions: list[float] = []
    resolution_rates: list[float] = []
    citation_pairs: list[tuple[str, str]] = []
    expected_abstentions: list[bool] = []
    actual_abstentions: list[bool] = []

    started = time.perf_counter()
    try:
        for row in gold:
            relevant = {
                resolver[key]: grade
                for key, grade in row['graded_relevance'].items()
                if key in resolver
            }
            expected_sources = {
                resolver[key]
                for key in row['expected_source_ids']
                if key in resolver
            }
            options = RetrievalOptions(
                mode=args.mode,  # type: ignore[arg-type]
                k_final=args.k,
                candidate_k=max(40, args.k * 4),
            )
            outcome = retriever.retrieve(
                row['text'], source_ids=member_ids, options=options
            )
            ranked_sources: list[str] = []
            seen: set[str] = set()
            for hit in outcome.hits:
                if hit.chunk.source_id not in seen:
                    seen.add(hit.chunk.source_id)
                    ranked_sources.append(hit.chunk.source_id)
            if relevant:
                recalls.append(recall_at_k(ranked_sources, relevant, args.k))
                precisions.append(precision_at_k(ranked_sources, relevant, args.k))
                mrrs.append(mrr_at_k(ranked_sources, relevant, args.k))
                ndcgs.append(ndcg_at_k(ranked_sources, relevant, args.k))
                chunk_ids = [hit.chunk.chunk_id for hit in outcome.hits]
                source_of = {
                    hit.chunk.chunk_id: hit.chunk.source_id
                    for hit in outcome.hits
                }
                diversities.append(
                    float(distinct_sources_at_k(chunk_ids, args.k, source_of))
                )
                dup_rates.append(duplicate_rate_at_k(chunk_ids, args.k))

            answer = chat.answer(row['text'])
            cited_sources = [
                citation.source_id for citation in answer.citations
            ]
            if expected_sources:
                citation_precisions.append(
                    citation_precision(cited_sources, expected_sources)
                )
                resolution_rates.append(
                    citation_resolution_rate(cited_sources, known_source_ids)
                )
            citation_pairs.extend(_faithfulness_pairs(answer, outcome))
            expected_abstentions.append(bool(row['expected_abstention']))
            actual_abstentions.append(bool(answer.insufficient))
    finally:
        unload = getattr(provider, 'unload', None)
        if callable(unload):
            unload()

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    payload = {
        'retrieval': {
            f'recall@{args.k}': _mean(recalls),
            f'precision@{args.k}': _mean(precisions),
            f'mrr@{args.k}': _mean(mrrs),
            f'ndcg@{args.k}': _mean(ndcgs),
            f'distinct_sources@{args.k}': _mean(diversities),
            f'duplicate_rate@{args.k}': _mean(dup_rates),
        },
        'citation': {
            'precision': _mean(citation_precisions),
            'resolution_rate': _mean(resolution_rates),
        },
        'faithfulness': {'exact_rate': faithfulness(citation_pairs)},
        'abstention': {
            'accuracy': abstention_accuracy(
                expected_abstentions, actual_abstentions
            )
        },
        'environment': {
            'k': args.k,
            'mode': args.mode,
            'n_questions': len(gold),
            'n_answerable': sum(1 for row in gold if row['answerable']),
            'n_citations': len(citation_pairs),
            'embedding_model': provider.model_id,
            'index_version': RAG_INDEX_VERSION,
            'vectors_loaded': len(vectors),
            'store': args.store,
            'gold': args.gold,
            'corpus': args.corpus,
            'latency_ms': elapsed_ms,
        },
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(json.dumps({'written': str(out_path), 'n_questions': len(gold)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
