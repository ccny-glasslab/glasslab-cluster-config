"""End-to-end pipeline tests: extract -> chunk -> persist -> index.

The pipeline composes existing corpus_rag modules; these tests pin the
composition contract (row counts, deterministic re-ingest, vector skip
logic, manifest-driven batch with per-source error isolation) using the
offline embedding provider only — no model downloads, network, or /tmp.
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest

from app.corpus_rag.embeddings import OfflineDeterministicEmbedding
from app.corpus_rag.pipeline import build_index, ingest_corpus, ingest_document
from app.storage import SqliteStore


def _make_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), 'Resampling Methods for Evaluation', fontsize=20)
    page.insert_text((72, 130), '1 Resampling', fontsize=14)
    body_words = 'bootstrap resampling estimates uncertainty in model evaluation studies reliably'
    page.insert_text((72, 160), ' '.join([body_words] * 6), fontsize=11)
    second = doc.new_page()
    second.insert_text((72, 72), '3 Validation', fontsize=14)
    second.insert_text(
        (72, 102),
        'cross validation checks predictive stability under distribution shift',
        fontsize=11,
    )
    return doc.tobytes()


@pytest.fixture()
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(str(tmp_path / 'pipeline.db'))


def test_ingest_pdf_end_to_end_sqlite(store: SqliteStore) -> None:
    report = ingest_document(
        store=store,
        data=_make_pdf(),
        canonical_uri='file://fixture.pdf',
        title='Resampling fixture',
        doc_type='book',
        corpus_slug='fixture-corpus',
    )
    assert report.n_sections >= 2
    assert report.n_evidence_spans > 0
    assert report.n_section_units >= report.n_sections - 1 or report.n_section_units > 0

    chunks = store.list_rag_chunks(source_ids=[report.source_id], limit=None)
    kinds = {chunk['kind'] for chunk in chunks}
    assert kinds == {'evidence_span', 'section_unit'}
    assert all(chunk['text'] for chunk in chunks)

    corpus = store.get_corpus('fixture-corpus')
    assert corpus is not None
    assert store.list_corpus_sources(corpus.corpus_id) == [report.source_id]


def test_reingest_is_idempotent(store: SqliteStore) -> None:
    data = _make_pdf()

    def run() -> list[str]:
        ingest_document(
            store=store,
            data=data,
            canonical_uri='file://fixture.pdf',
            title='Resampling fixture',
            doc_type='book',
        )
        rows = store.list_rag_chunks(limit=None)
        return sorted(row['chunk_id'] for row in rows)

    first = run()
    second = run()
    assert first and first == second


def test_build_index_offline_skips_then_forces(store: SqliteStore) -> None:
    report = ingest_document(
        store=store,
        data=_make_pdf(),
        canonical_uri='file://fixture.pdf',
        doc_type='paper',
    )
    provider = OfflineDeterministicEmbedding(dims=16)

    summary = build_index(store=store, source_ids=[report.source_id], provider=provider)
    assert summary['model_id'] == 'offline-deterministic'
    assert summary['n_vectors'] == report.n_evidence_spans

    rerun = build_index(store=store, source_ids=[report.source_id], provider=provider)
    assert rerun['n_vectors'] == 0

    forced = build_index(
        store=store,
        source_ids=[report.source_id],
        provider=provider,
        force=True,
    )
    assert forced['n_vectors'] == report.n_evidence_spans

    vectors = store.list_rag_chunk_vectors('offline-deterministic')
    assert len(vectors) == report.n_evidence_spans
    meta, blob = vectors[0]
    assert meta.dims == 16
    assert len(blob) == 16 * 4


def test_ingest_corpus_manifest_driven_with_errors(
    store: SqliteStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_dir = tmp_path / 'raw'
    raw_dir.mkdir()
    (raw_dir / 'gap-statistic.pdf').write_bytes(_make_pdf())

    entries = [
        {
            'id': 'gap-statistic',
            'title': 'Gap statistic fixture',
            'url': 'https://example.invalid/gap.pdf',
            'sha256': None,
            'license_note': 'fixture',
        },
        {
            'id': 'missing-one',
            'title': 'Missing file entry',
            'url': 'https://example.invalid/missing.pdf',
            'sha256': None,
            'license_note': 'fixture',
        },
        {
            'id': 'skipped-one',
            'title': 'Skipped entry',
            'url': 'https://example.invalid/skip.pdf',
            'sha256': None,
            'license_note': 'paywalled',
            'skip': True,
            'skip_reason': 'paywalled',
        },
    ]
    manifest = tmp_path / 'manifest.jsonl'
    manifest.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
    monkeypatch.setattr('app.corpus_rag.pipeline._MANIFEST_PATH', manifest)

    reports, errors = ingest_corpus(
        store=store, corpus_slug='bench', raw_dir=raw_dir
    )
    assert [report.source_id for report in reports]
    assert len(reports) == 1
    assert any('missing-one' in error for error in errors)
    assert not any('skipped-one' in error for error in errors)


def test_ingest_corpus_cli_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'corpus_rag'))
    import ingest_corpus as cli

    raw_dir = tmp_path / 'raw'
    raw_dir.mkdir()
    (raw_dir / 'gap-statistic.pdf').write_bytes(_make_pdf())
    entries = [
        {
            'id': 'gap-statistic',
            'title': 'Gap statistic fixture',
            'url': 'https://example.invalid/gap.pdf',
            'sha256': None,
            'license_note': 'fixture',
        }
    ]
    manifest = tmp_path / 'manifest.jsonl'
    manifest.write_text(json.dumps(entries[0]) + '\n')
    monkeypatch.setattr(cli, '_MANIFEST_PATH', manifest)
    monkeypatch.setattr(cli, 'default_store_path', lambda: str(tmp_path / 'cli.db'))

    code = cli.main(
        [
            '--store',
            str(tmp_path / 'cli.db'),
            '--raw-dir',
            str(raw_dir),
            '--corpus',
            'bench-cli',
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload['reports']) == 1
    assert payload['errors'] == []
