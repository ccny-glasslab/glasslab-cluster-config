"""Runner-level tests: four-mode retrieval benchmark emits a metrics JSON.

Uses a micro in-memory-style SQLite fixture (two tiny synthetic documents)
so the runner's mode matrix, qrels resolution, and output schema are pinned
without any model downloads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pymupdf
import pytest

from app.storage import SqliteStore


def _tiny_pdf(title: str, heading: str, body_words: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), title, fontsize=18)
    page.insert_text((72, 120), heading, fontsize=14)
    page.insert_text((72, 150), ' '.join([body_words] * 8), fontsize=11)
    return doc.tobytes()


@pytest.fixture()
def seeded_store(tmp_path: Path) -> Path:
    store_path = tmp_path / 'bench.db'
    store = SqliteStore(str(store_path))
    from app.corpus_rag.pipeline import ingest_document

    ingest_document(
        store=store,
        data=_tiny_pdf(
            'Gap Statistic', '1 Gap statistic',
            'estimating the number of clusters via the gap statistic method',
        ),
        canonical_uri='file://gap.pdf',
        title='Gap statistic paper',
        doc_type='paper',
        corpus_slug='microbench',
    )
    ingest_document(
        store=store,
        data=_tiny_pdf(
            'Calibration', '2 Calibration',
            'reliability diagrams assess probability calibration of classifiers',
        ),
        canonical_uri='file://calib.pdf',
        title='Calibration paper',
        doc_type='paper',
        corpus_slug='microbench',
    )
    store.close() if hasattr(store, 'close') else None
    return store_path


def test_runner_emits_four_mode_metrics(seeded_store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'corpus_rag'))
    import run_benchmark as cli

    questions = tmp_path / 'questions.jsonl'
    question = {
        'qid': 'q-micro-clusters',
        'text': 'How should we estimate the number of clusters?',
        'expected_source_ids': [],
        'graded_relevance': {},
        'notes': 'micro fixture',
    }
    calibration_question = {
        'qid': 'q-micro-calibration',
        'text': 'How can we check classifier probability calibration?',
        'expected_source_ids': [],
        'graded_relevance': {},
        'notes': 'micro fixture',
    }
    questions.write_text(json.dumps(question) + '\n' + json.dumps(calibration_question) + '\n')

    # Resolve graded relevance against ingested source ids at runtime by slug
    # mapping file: manifest-id -> canonical_uri marker is unavailable here,
    # so the runner accepts a direct qrels TSV keyed by source canonical id.
    store = SqliteStore(str(seeded_store))
    sources = {s.canonical_uri: s.source_id for s in store.list_knowledge_sources()}
    gap_id = [sid for uri, sid in sources.items() if uri.endswith('gap.pdf')][0]
    calib_id = [sid for uri, sid in sources.items() if uri.endswith('calib.pdf')][0]
    qrels = tmp_path / 'qrels.tsv'
    qrels.write_text(
        f'q-micro-clusters\t{gap_id}\t2\n'
        f'q-micro-calibration\t{calib_id}\t2\n'
    )

    out_path = tmp_path / 'metrics.json'
    code = cli.main(
        [
            '--store', str(seeded_store),
            '--questions', str(questions),
            '--qrels', str(qrels),
            '--out', str(out_path),
            '--k', '5',
        ]
    )
    assert code == 0
    payload = json.loads(out_path.read_text())
    assert set(payload['modes']) == {'lexical', 'dense', 'hybrid', 'hybrid+rerank'}
    for mode, metrics in payload['modes'].items():
        for metric in (
            'recall@5', 'mrr@5', 'ndcg@5', 'distinct_sources@5',
            'duplicate_rate@5', 'latency_ms',
        ):
            assert metric in metrics, f'{mode} missing {metric}'
        assert 0.0 <= metrics['recall@5'] <= 1.0
    assert payload['environment']['k'] == 5
    capsys.readouterr()
