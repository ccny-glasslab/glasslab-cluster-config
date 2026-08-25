"""Production-surface retrieval benchmark (KnowledgeManager modes).

Evaluates the SAME store Honeydew queries at runtime:

    lexical -> KnowledgeManager.retrieve(retrieval_mode='lexical')
    dense   -> KnowledgeManager.retrieve(retrieval_mode='dense')
    hybrid  -> untuned client-side RRF fusion (k=60) of the two channels

Metrics are computed at the SOURCE level against graded qrels whose keys are
manifest ids resolved through ingested canonical URIs / metadata. This is
deliberately chunking-agnostic, so numbers stay comparable across ingestion
strategies. Cross-encoder reranking is intentionally NOT part of this runner
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


def _load_qrels(path: Path) -> dict[str, dict[str, int]]:
    qrels: dict[str, dict[str, int]] = {}
    for line in path.read_text().splitlines()[1:]:
        if not line.strip():
            continue
        qid, key, grade = line.split('\t')
        qrels.setdefault(qid, {})[key] = int(grade)
    return qrels


def _build_source_resolver(store: SqliteStore) -> dict[str, str]:
    resolver: dict[str, str] = {}
    for source in store.list_knowledge_sources():
        resolver[source.source_id] = source.source_id
        stem = source.canonical_uri.rstrip('/').rsplit('/', 1)[-1]
        if stem.endswith('.pdf'):
            resolver[stem[:-4]] = source.source_id
        metadata_id = (source.metadata or {}).get('manifest_id')
        if metadata_id:
            resolver[metadata_id] = source.source_id
    return resolver


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
    args = parser.parse_args(argv)

    questions = [
        BenchmarkQuestion.model_validate(json.loads(line))
        for line in Path(args.questions).read_text().splitlines()
        if line.strip()
    ]
    raw_qrels = _load_qrels(Path(args.qrels))
    store = SqliteStore(args.store)
    resolver = _build_source_resolver(store)
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
    started = time.perf_counter()

    for question in questions:
        judged: dict[str, int] = {}
        for key, grade in raw_qrels.get(question.qid, {}).items():
            sid = resolver.get(key)
            if sid is not None:
                judged[sid] = grade

        channel_rankings: dict[str, list[str]] = {}
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
            ranked_sources: list[str] = []
            seen: set[str] = set()
            for entry in packet.ranked_sources:
                sid = entry.get('source_id') or ''
                if sid and sid not in seen:
                    seen.add(sid)
                    ranked_sources.append(sid)
            channel_rankings[mode] = ranked_sources

        hybrid = _rrf_fuse(channel_rankings['lexical'], channel_rankings['dense'], args.k)

        for mode, ranking in (
            ('lexical', channel_rankings['lexical']),
            ('dense', channel_rankings['dense']),
            ('hybrid', hybrid),
        ):
            per_mode[mode].append({
                f'recall@{args.k}': recall_at_k(ranking, judged, args.k),
                f'mrr@{args.k}': mrr_at_k(ranking, judged, args.k),
                f'ndcg@{args.k}': ndcg_at_k(ranking, judged, args.k),
                f'precision@{args.k}': precision_at_k(ranking, judged, args.k),
                f'distinct_sources@{args.k}': float(len(set(ranking[: args.k]))),
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

    payload = {
        'modes': results,
        'environment': {
            'k': args.k,
            'n_questions': len(questions),
            'surface': 'KnowledgeManager.retrieve',
            'store': args.store,
        },
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(json.dumps({'written': str(out_path)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
