"""Runner-level tests: the chat-service eval harness emits a metrics JSON.

Uses a micro synthetic SQLite fixture (two tiny documents, one answerable and
one unanswerable gold question) so the runner's metric sections, gold-set
resolution, and output schema are pinned without any model downloads.
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
    store_path = tmp_path / 'eval.db'
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
        corpus_slug='microeval',
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
        corpus_slug='microeval',
    )
    store.close() if hasattr(store, 'close') else None
    return store_path


def _write_gold(path: Path) -> None:
    rows = [
        {
            'qid': 'q-eval-clusters',
            'text': 'How should we estimate the number of clusters?',
            'answerable': True,
            'expected_source_ids': ['gap'],
            'expected_citation_ids': ['gap'],
            'graded_relevance': {'gap': 2},
            'expected_abstention': False,
            'notes': 'micro fixture',
        },
        {
            'qid': 'q-eval-unanswerable',
            'text': 'What is the capital of the planet Vulcan?',
            'answerable': False,
            'expected_source_ids': [],
            'expected_citation_ids': [],
            'graded_relevance': {},
            'expected_abstention': True,
            'notes': 'micro fixture',
        },
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')


def test_run_eval_emits_metric_sections(
    seeded_store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'corpus_rag'))
    import run_eval as cli

    gold = tmp_path / 'gold_qa.jsonl'
    _write_gold(gold)
    out_path = tmp_path / 'eval-metrics.json'

    code = cli.main(
        [
            '--store', str(seeded_store),
            '--gold', str(gold),
            '--out', str(out_path),
            '--k', '5',
        ]
    )
    assert code == 0
    payload = json.loads(out_path.read_text())
    assert {'retrieval', 'citation', 'faithfulness', 'abstention', 'environment'} <= set(
        payload
    )
    for metric in ('recall@5', 'precision@5', 'mrr@5', 'ndcg@5', 'duplicate_rate@5'):
        assert 0.0 <= payload['retrieval'][metric] <= 1.0
    for section in ('citation', 'faithfulness', 'abstention'):
        for name, value in payload[section].items():
            assert 0.0 <= value <= 1.0, f'{section}.{name}={value}'
    assert payload['environment']['k'] == 5
    assert payload['environment']['n_questions'] == 2
    capsys.readouterr()
