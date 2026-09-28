"""Store-aware ingestion with raw-PDF staging (file:// canonical URIs).

Production keeps the source PDF bytes on the shared artifacts PVC under
``settings.corpus_rag_raw_root`` and records an authoritative ``file://``
canonical URI so the chat/PDF viewer can resolve the exact bytes the corpus
was built from. These tests pin the staging helper, the batch ingest path,
and the arXiv sidecar (which must no longer record the remote https URL as
the canonical URI).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pymupdf
import pytest

from app.corpus_rag import pipeline
from app.corpus_rag.pipeline import ingest_corpus
from app.storage import SqliteStore

SERVICE_DIR = Path(__file__).resolve().parents[1]
CORPUS_SCRIPT_DIR = SERVICE_DIR / 'scripts' / 'corpus_rag'


def _make_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), 'Resampling Methods for Evaluation', fontsize=20)
    page.insert_text((72, 130), '1 Resampling', fontsize=14)
    body = 'bootstrap resampling estimates uncertainty in model evaluation studies reliably'
    page.insert_text((72, 160), ' '.join([body] * 6), fontsize=11)
    return doc.tobytes()


def _path_of_file_uri(uri: str) -> Path:
    return Path(url2pathname(urlsplit(uri).path))


def test_ingest_stages_pdf_under_raw_root_with_file_uri(tmp_path: Path) -> None:
    raw_root = tmp_path / 'raw'
    store = SqliteStore(str(tmp_path / 'ingest.db'))
    pdf = _make_pdf()

    staged = pipeline.stage_raw_pdf(pdf, raw_root, 'gap-statistic')
    assert staged.parent == raw_root

    manifest = tmp_path / 'manifest.jsonl'
    manifest.write_text(
        json.dumps(
            {
                'id': 'gap-statistic',
                'title': 'Gap statistic fixture',
                'url': 'https://example.invalid/gap.pdf',
                'sha256': None,
            }
        )
        + '\n'
    )

    reports, errors = ingest_corpus(
        store=store,
        corpus_slug='bench',
        raw_dir=raw_root,
        manifest_path=manifest,
    )
    assert errors == []
    assert len(reports) == 1

    source = store.get_knowledge_source(reports[0].source_id)
    assert source.canonical_uri.startswith('file://')
    resolved = _path_of_file_uri(source.canonical_uri)
    assert resolved == staged.resolve()
    assert raw_root in resolved.parents
    assert resolved.read_bytes().startswith(b'%PDF')


def test_stage_raw_pdf_rejects_path_separators(tmp_path: Path) -> None:
    data = b'%PDF-1.4\n%%EOF\n'
    for bad_name in ('../evil', 'a/b', '..', 'dir/../x', 'dir\\x', ''):
        with pytest.raises(ValueError):
            pipeline.stage_raw_pdf(data, tmp_path, bad_name)
    assert list(tmp_path.iterdir()) == []


def test_ingest_arxiv_uses_file_uri_not_https(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(CORPUS_SCRIPT_DIR))
    import ingest_arxiv

    from app.corpus_rag.arxiv import ArxivEntry

    raw_root = tmp_path / 'raw'
    store_path = tmp_path / 'arxiv.db'
    monkeypatch.setenv('GLASSLAB_ORCHESTRATOR_STORE_BACKEND', 'sqlite')
    monkeypatch.setenv(
        'GLASSLAB_ORCHESTRATOR_CORPUS_RAG_STORE_PATH', str(store_path)
    )
    monkeypatch.setenv('GLASSLAB_ORCHESTRATOR_CORPUS_RAG_RAW_ROOT', str(raw_root))

    pdf = _make_pdf()
    entry = ArxivEntry(
        arxiv_id='http://arxiv.org/abs/2401.00001v1',
        title='Resampling Methods for Evaluation',
        authors=('Ada Author',),
        published=_dt.date(2024, 1, 1),
        pdf_url='https://arxiv.org/pdf/2401.00001v1',
    )
    monkeypatch.setattr(ingest_arxiv, 'fetch_entries', lambda query: [entry])
    monkeypatch.setattr(
        ingest_arxiv,
        'download_pdf',
        lambda e, **kwargs: (pdf, hashlib.sha256(pdf).hexdigest()),
    )

    assert ingest_arxiv.main(['--days', '1']) == 0
    capsys.readouterr()

    sources = SqliteStore(str(store_path)).list_knowledge_sources()
    assert len(sources) == 1
    source = sources[0]
    assert source.canonical_uri.startswith('file://')
    resolved = _path_of_file_uri(source.canonical_uri)
    assert raw_root in resolved.parents
    assert resolved.name == '2401.00001v1.pdf'
    assert resolved.read_bytes().startswith(b'%PDF')
    assert source.metadata['source_url'] == 'https://arxiv.org/pdf/2401.00001v1'


def test_stage_raw_pdf_overwrites_atomically(tmp_path: Path) -> None:
    raw_root = tmp_path / 'raw'
    first = pipeline.stage_raw_pdf(b'%PDF-1.4\nfirst\n', raw_root, 'paper')
    second = pipeline.stage_raw_pdf(b'%PDF-1.4\nsecond\n', raw_root, 'paper')

    assert first == second
    assert second.read_bytes().startswith(b'%PDF-1.4\nsecond')
    leftovers = [p.name for p in raw_root.iterdir() if p.name != 'paper.pdf']
    assert leftovers == []


@pytest.mark.parametrize('name', ['arxiv-preprint', '2401.00001v1'])
def test_stage_raw_pdf_accepts_safe_names(tmp_path: Path, name: str) -> None:
    staged = pipeline.stage_raw_pdf(b'%PDF-1.4\n%%EOF\n', tmp_path, name)
    assert staged.name == f'{name}.pdf'
    assert staged.read_bytes().startswith(b'%PDF')
