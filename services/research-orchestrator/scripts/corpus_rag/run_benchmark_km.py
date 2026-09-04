"""Production-surface retrieval benchmark (KnowledgeManager modes).

Evaluates the SAME store Honeydew queries at runtime:

    lexical -> KnowledgeManager.retrieve(retrieval_mode='lexical')
    dense   -> KnowledgeManager.retrieve(retrieval_mode='dense')
    hybrid  -> untuned client-side RRF fusion (k=60) of the two channels

Metrics are computed at the CHUNK level against graded qrels whose keys are
`manifest_id::chunk_index` pairs resolved through ingested canonical URIs /
metadata. This provides granular evaluation of chunk ranking quality per
channel. Cross-encoder reranking is intentionally NOT part of this runner
(no recall@10 improvement in PR #220; interface stays extensible).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SERVICE_DIR))

from app.corpus_rag import BenchmarkQuestion  # noqa: E402
from app.corpus_rag.benchmark import (  # noqa: E402
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from app.knowledge_manager import KnowledgeManager  # noqa: E402
from app.storage import SqliteStore  # noqa: E402

MODES = ('lexical', 'dense', 'hybrid')
_RRF_K = 60


def _load_qrels(path: Path) -> tuple[dict[str, dict[str, int]], str | None]:
    """Load qrels from TSV (legacy) or JSON (chunk-level).

    Returns (qrels_dict, key_scheme_or_none).
    TSV format: qid\tkey\tgrade (legacy, source-level)
    JSON format: {"questions": {qid: {chunk_key: grade, ...}}, "key_scheme": "..."}
    """
    content = path.read_text().strip()
    if not content:
        return {}, None

    # Try JSON first
    if content.startswith('{'):
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                questions = data.get('questions', {})
                key_scheme = data.get('key_scheme')
                if isinstance(questions, dict):
                    return questions, key_scheme
        except json.JSONDecodeError:
            pass

    # Fall back to TSV format
    qrels: dict[str, dict[str, int]] = {}
    for line in content.splitlines()[1:]:
        if not line.strip():
            continue
        qid, key, grade = line.split('\t')
        qrels.setdefault(qid, {})[key] = int(grade)
    return qrels, None


def _build_resolver(store: SqliteStore) -> dict[str, str]:
    """Build mapping from entry_id (chunk_id or manifest_id::chunk_index) to chunk_id.

    For chunk-level qrels, we need to map manifest_id::chunk_index -> chunk_id.
    For legacy source-level qrels, we map source_id -> source_id.
    """
    resolver: dict[str, str] = {}
    for source in store.list_knowledge_sources():
        source_id = source.source_id
        resolver[source_id] = source_id
        metadata_id = (source.metadata or {}).get('manifest_id')
        if metadata_id:
            resolver[metadata_id] = source_id
            # Also add manifest_id::chunk_index -> chunk_id mappings
            chunks = store.list_knowledge_chunks(source_id)
            for chunk in chunks:
                chunk_key = f"{metadata_id}::{chunk.chunk_index}"
                resolver[chunk_key] = chunk.chunk_id
    return resolver


def _build_chunk_relevance(
    raw_qrels: dict[str, dict[str, int]],
    resolver: dict[str, str],
) -> dict[str, dict[str, int]]:
    """Convert qrels from key_scheme to chunk_id keys.

    For legacy qrels (no key_scheme), keys are source_id and we use them as-is.
    For chunk-level qrels, keys are manifest_id::chunk_index and we resolve to chunk_id.
    """
    converted: dict[str, dict[str, int]] = {}
    for qid, key_grade_map in raw_qrels.items():
        converted[qid] = {}
        for key, grade in key_grade_map.items():
            chunk_id = resolver.get(key)
            if chunk_id is not None:
                converted[qid][chunk_id] = grade
    return converted


def _rrf_fuse(rank_a: list[str], rank_b: list[str], k: int) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in (rank_a, rank_b):
        for position, cid in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + position)
    return sorted(scores, key=lambda cid: (-scores[cid], cid))[:k]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True, help='KM store database')
    parser.add_argument('--questions', required=True)
    parser.add_argument('--qrels', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--k', type=int, default=10)
    parser.add_argument('--granularity', default='chunk', help='Metric granularity: chunk or source')
    parser.add_argument('--qrels_key_scheme', default=None, help='Qrels key scheme (e.g., manifest_id::chunk_index)')
    args = parser.parse_args(argv)

    questions = [
        BenchmarkQuestion.model_validate(json.loads(line))
        for line in Path(args.questions).read_text().splitlines()
        if line.strip()
    ]
    raw_qrels, qrels_key_scheme = _load_qrels(Path(args.qrels))
    store = SqliteStore(args.store)
    resolver = _build_resolver(store)
    km = KnowledgeManager(store=store, root=str(Path(args.store).parent / 'knowledge'))

    # Packets reference runs(run_id); a synthetic benchmark run keeps the
    # provenance chain intact without touching any real research run.
    from datetime import datetime, timezone

    from app.schemas import RunRecord, RunState

    now = datetime.now(timezone.utc)
    try:
        store.create_run(
            RunRecord(
                run_id='benchmark',
                objective='Corpus-RAG production retrieval benchmark.',
                state=RunState.CREATED,
                evaluation_contract_id='example-research-v1',
                evaluation_contract_version='1.0.0',
                evaluation_contract_digest='a' * 64,
                beaker_workspace='/tmp/beaker',
                honeydew_workspace='/tmp/honeydew',
                shared_artifacts_path='/tmp/shared',
                reports_path='/tmp/reports',
                maximum_turns=20,
                maximum_runtime_seconds=3600,
                maximum_parallel_jobs=2,
                created_at=now,
                updated_at=now,
            ),
            one_active_run=False,
        )
    except Exception:
        pass  # already exists from an earlier invocation

    per_mode: dict[str, list[dict[str, float]]] = {mode: [] for mode in MODES}
    per_mode_per_question: dict[str, list[dict[str, Any]]] = {mode: [] for mode in MODES}
    started = time.perf_counter()

    for question in questions:
        # Build chunk-level relevance
        judged: dict[str, int] = {}
        for key, grade in raw_qrels.get(question.qid, {}).items():
            chunk_id = resolver.get(key)
            if chunk_id is not None:
                judged[chunk_id] = grade

        channel_rankings: dict[str, list[str]] = {}
        channel_chunks: dict[str, list[dict[str, Any]]] = {}

        for mode in ('lexical', 'dense'):
            packet = km.retrieve(
                run_id='benchmark',
                agent='honeydew',
                turn_number=len(per_mode[mode]) + 1,
                turn_kind='methodology_review',
                query=question.text,
                max_results=args.k * 2,
                retrieval_mode=mode,
            )
            # Keep chunks as-is (no source dedup)
            ranked_chunks: list[str] = []
            for entry in packet.ranked_sources:
                entry_id = entry.get('entry_id')
                if entry_id:
                    ranked_chunks.append(entry_id)
            channel_rankings[mode] = ranked_chunks
            channel_chunks[mode] = packet.ranked_sources[: args.k]

        hybrid = _rrf_fuse(channel_rankings['lexical'], channel_rankings['dense'], args.k)
        hybrid_chunks = [
            entry
            for entry in packet.ranked_sources
            if entry.get('entry_id') in hybrid
        ][: args.k]

        for mode, ranking, chunks in (
            ('lexical', channel_rankings['lexical'], channel_chunks['lexical']),
            ('dense', channel_rankings['dense'], channel_chunks['dense']),
            ('hybrid', hybrid, hybrid_chunks),
        ):
            chunk_relevance = judged
            source_relevance: dict[str, int] = {}
            for chunk_id, grade in chunk_relevance.items():
                for entry in chunks:
                    if entry.get('entry_id') == chunk_id:
                        sid = entry.get('source_id')
                        if sid:
                            source_relevance.setdefault(sid, grade)
                            break

            chunk_ndcg = ndcg_at_k(ranking, chunk_relevance, args.k)
            chunk_mrr = mrr_at_k(ranking, chunk_relevance, args.k)
            chunk_precision = precision_at_k(ranking, chunk_relevance, args.k)
            chunk_recall = recall_at_k(ranking, chunk_relevance, args.k)

            # Count distinct sources in chunk ranking
            distinct_sources = len({entry.get('source_id', '') for entry in chunks})

            per_mode[mode].append({
                f'ndcg@{args.k}': chunk_ndcg,
                f'mrr@{args.k}': chunk_mrr,
                f'precision@{args.k}': chunk_precision,
                f'recall@{args.k}': chunk_recall,
                f'distinct_sources@{args.k}': float(distinct_sources),
                f'chunk_count@{args.k}': float(len(ranking)),
            })

            per_mode_per_question[mode].append({
                'question_id': question.qid,
                'ranked_chunks': [
                    {
                        'chunk_id': entry.get('entry_id'),
                        'source_id': entry.get('source_id'),
                        'score': entry.get('score', 0),
                        'grade': chunk_relevance.get(entry.get('entry_id'), 0),
                    }
                    for entry in chunks
                ],
                'distinct_sources': distinct_sources,
            })

    elapsed_ms = (time.perf_counter() - started) * 1000.0

    def _mean(values: list[float]) -> float:
        values = list(values)
        return sum(values) / len(values) if values else 0.0

    results = {}
    for mode in MODES:
        rows = per_mode[mode]
        results[mode] = {
            key: round(_mean(row[key] for row in rows), 4)
            for key in rows[0]
        } if rows else {}
        results[mode]['latency_ms_per_question'] = round(
            elapsed_ms / max(1, len(questions)), 1
        )
        results[mode]['per_question'] = per_mode_per_question[mode]

    payload = {
        'modes': results,
        'environment': {
            'k': args.k,
            'n_questions': len(questions),
            'surface': 'KnowledgeManager.retrieve',
            'store': args.store,
            'granularity': args.granularity,
            'qrels_key_scheme': qrels_key_scheme or args.qrels_key_scheme or 'legacy_source_id',
        },
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(json.dumps({'written': str(out_path)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
