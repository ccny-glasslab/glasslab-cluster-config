"""Tests for the missing-chunk corpus re-ingest healer.

Issue #622: dozens of live knowledge sources have zero ``rag_chunks``. The
regular arXiv sidecar skips existing sources, so it can never heal them. This
module re-ingests the chunk-less sources from local raw bytes when the staged
PDF still exists, and re-fetches it from arXiv when the staged ``file://`` path
is missing. It must never fabricate chunks for a source it cannot recover, and
a second applied run must add nothing and touch the network not at all.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest

from app.corpus_rag import reingest
from app.corpus_rag.contracts import RagChunkRecord
from app.corpus_rag.reingest import (
    arxiv_pdf_url,
    parse_arxiv_id,
    reingest_missing_chunks,
    scan_missing_chunk_sources,
)
from app.schemas import KnowledgeSource, SourceType
from app.storage import SqliteStore


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_pdf(marker: str = 'Resampling Methods for Evaluation') -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), marker, fontsize=20)
    page.insert_text((72, 130), '1 Resampling', fontsize=14)
    body = 'bootstrap resampling estimates uncertainty in model evaluation studies reliably'
    page.insert_text((72, 160), ' '.join([body] * 6), fontsize=11)
    return doc.tobytes()


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _add_source(
    store: SqliteStore, *, source_id: str, uri: str, digest: str
) -> KnowledgeSource:
    source = KnowledgeSource(
        source_id=source_id,
        source_type=SourceType.PAPER,
        canonical_uri=uri,
        digest=digest,
        title=f'Paper {source_id}',
    )
    store.save_knowledge_source(source)
    return source


def _add_existing_chunk(store: SqliteStore, source_id: str) -> None:
    text = 'already indexed'
    store.replace_rag_chunks(
        source_id,
        [
            RagChunkRecord(
                chunk_id=f'{source_id}-c0',
                source_id=source_id,
                kind='evidence_span',
                chunk_index=0,
                text=text,
                digest=_sha(text.encode()),
                token_count=2,
            )
        ],
    )


@pytest.fixture()
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(str(tmp_path / 'reingest.db'))


def test_scan_selects_only_chunk_less_sources(store: SqliteStore) -> None:
    _add_source(store, source_id='full', uri='upload://full.md', digest=_sha(b'x'))
    _add_existing_chunk(store, 'full')
    _add_source(store, source_id='bare', uri='upload://bare.md', digest=_sha(b'y'))

    found = {source.source_id for source in scan_missing_chunk_sources(store)}

    assert found == {'bare'}


def test_dry_run_writes_nothing_and_never_fetches(
    store: SqliteStore, tmp_path: Path
) -> None:
    raw_root = tmp_path / 'raw'
    name = '2401.00001v1'
    _add_source(
        store,
        source_id='a',
        uri=_file_uri(raw_root / f'{name}.pdf'),
        digest=_sha(b'incoming'),
    )
    calls: list[str] = []

    def downloader(url: str) -> bytes:
        calls.append(url)
        raise AssertionError('dry run must not download')

    report = reingest_missing_chunks(
        store, raw_root=raw_root, apply=False, downloader=downloader
    )

    assert report.sources[0].status == 'would-heal-fetch'
    assert report.added == 0
    assert report.network_fetches == 0
    assert calls == []
    assert not (raw_root / f'{name}.pdf').exists()
    assert store.list_rag_chunks(source_ids=['a']) == []


def test_apply_heals_missing_file_via_injected_downloader(
    store: SqliteStore, tmp_path: Path
) -> None:
    raw_root = tmp_path / 'raw'
    name = '2401.00002v1'
    pdf = _make_pdf()
    _add_source(
        store, source_id='a', uri=_file_uri(raw_root / f'{name}.pdf'), digest=_sha(pdf)
    )
    urls: list[str] = []

    def downloader(url: str) -> bytes:
        urls.append(url)
        return pdf

    report = reingest_missing_chunks(
        store, raw_root=raw_root, apply=True, downloader=downloader
    )

    assert report.sources[0].status == 'healed-fetch'
    assert report.added == 1
    assert report.added_chunks >= 1
    assert urls == ['https://arxiv.org/pdf/2401.00002v1']
    assert (raw_root / f'{name}.pdf').read_bytes() == pdf
    assert store.list_rag_chunks(source_ids=['a'])


def test_second_apply_run_is_a_noop_with_no_network(
    store: SqliteStore, tmp_path: Path
) -> None:
    raw_root = tmp_path / 'raw'
    pdf = _make_pdf()
    _add_source(
        store,
        source_id='a',
        uri=_file_uri(raw_root / '2401.00011v1.pdf'),
        digest=_sha(pdf),
    )
    urls: list[str] = []

    def downloader(url: str) -> bytes:
        urls.append(url)
        return pdf

    reingest_missing_chunks(store, raw_root=raw_root, apply=True, downloader=downloader)
    second = reingest_missing_chunks(
        store, raw_root=raw_root, apply=True, downloader=downloader
    )

    assert second.added == 0
    assert second.network_fetches == 0
    assert second.sources[0].status == 'skipped-has-chunks'
    assert urls == ['https://arxiv.org/pdf/2401.00011v1']


def test_local_file_heal_performs_no_network(
    store: SqliteStore, tmp_path: Path
) -> None:
    raw_root = tmp_path / 'raw'
    raw_root.mkdir()
    pdf = _make_pdf()
    staged = raw_root / '2401.00003v1.pdf'
    staged.write_bytes(pdf)
    _add_source(store, source_id='a', uri=_file_uri(staged), digest=_sha(pdf))

    def downloader(url: str) -> bytes:
        raise AssertionError('local heal must not download')

    report = reingest_missing_chunks(
        store, raw_root=raw_root, apply=True, downloader=downloader
    )

    assert report.sources[0].status == 'healed-local'
    assert report.network_fetches == 0
    assert store.list_rag_chunks(source_ids=['a'])


def test_unrecoverable_sources_never_fabricate(
    store: SqliteStore, tmp_path: Path
) -> None:
    raw_root = tmp_path / 'raw'
    _add_source(store, source_id='upload', uri='upload://paper.pdf', digest=_sha(b'a'))
    _add_source(
        store,
        source_id='outside',
        uri='file:///etc/papers/2401.00004v1.pdf',
        digest=_sha(b'b'),
    )
    _add_source(
        store,
        source_id='nonarxiv',
        uri=_file_uri(raw_root / 'mystery.pdf'),
        digest=_sha(b'c'),
    )

    def downloader(url: str) -> bytes:
        raise AssertionError('unrecoverable sources must not be fetched')

    report = reingest_missing_chunks(
        store, raw_root=raw_root, apply=True, downloader=downloader
    )

    assert report.unrecoverable == 3
    assert report.added == 0
    assert {source.status for source in report.sources} == {'unrecoverable'}
    assert store.list_rag_chunks() == []


def test_downloader_error_is_isolated_and_reported(
    store: SqliteStore, tmp_path: Path
) -> None:
    raw_root = tmp_path / 'raw'
    raw_root.mkdir()
    pdf = _make_pdf()
    staged = raw_root / '2401.00005v1.pdf'
    staged.write_bytes(pdf)
    _add_source(store, source_id='good', uri=_file_uri(staged), digest=_sha(pdf))
    _add_source(
        store,
        source_id='bad',
        uri=_file_uri(raw_root / '2401.00006v1.pdf'),
        digest=_sha(b'z'),
    )

    def downloader(url: str) -> bytes:
        raise RuntimeError('boom network')

    report = reingest_missing_chunks(
        store, raw_root=raw_root, apply=True, downloader=downloader
    )

    statuses = {source.source_id: source.status for source in report.sources}
    assert statuses == {'good': 'healed-local', 'bad': 'error'}
    assert report.errors == 1
    assert report.added == 1
    bad = next(source for source in report.sources if source.source_id == 'bad')
    assert bad.detail is not None and 'boom network' in bad.detail


def test_diagnose_prints_traceback(
    store: SqliteStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw_root = tmp_path / 'raw'
    _add_source(
        store,
        source_id='bad',
        uri=_file_uri(raw_root / '2401.00012v1.pdf'),
        digest=_sha(b'z'),
    )

    def downloader(url: str) -> bytes:
        raise RuntimeError('trace me')

    reingest_missing_chunks(
        store, raw_root=raw_root, apply=True, downloader=downloader, diagnose=True
    )

    assert 'RuntimeError' in capsys.readouterr().err


def test_parse_arxiv_id_modern_and_old() -> None:
    assert parse_arxiv_id('2401.00007v2') == '2401.00007v2'
    assert parse_arxiv_id('2401.00007') == '2401.00007'
    assert parse_arxiv_id('2401.00007v2.pdf') == '2401.00007v2'
    assert parse_arxiv_id('hep-th_9901001') == 'hep-th/9901001'
    assert parse_arxiv_id('math.GT_0309136v1') == 'math.GT/0309136v1'
    assert parse_arxiv_id('mystery') is None
    assert parse_arxiv_id('notes.pdf') is None


def test_arxiv_pdf_url() -> None:
    assert arxiv_pdf_url('2401.00008v1') == 'https://arxiv.org/pdf/2401.00008v1'
    assert arxiv_pdf_url('hep-th/9901001') == 'https://arxiv.org/pdf/hep-th/9901001'


def test_sleep_is_invoked_between_network_fetches(
    store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_root = tmp_path / 'raw'
    pdf_a = _make_pdf('Paper A')
    pdf_b = _make_pdf('Paper B')
    _add_source(
        store,
        source_id='a',
        uri=_file_uri(raw_root / '2401.00009v1.pdf'),
        digest=_sha(pdf_a),
    )
    _add_source(
        store,
        source_id='b',
        uri=_file_uri(raw_root / '2401.00010v1.pdf'),
        digest=_sha(pdf_b),
    )
    pdfs = {
        'https://arxiv.org/pdf/2401.00009v1': pdf_a,
        'https://arxiv.org/pdf/2401.00010v1': pdf_b,
    }
    sleeps: list[float] = []
    monkeypatch.setattr(reingest.time, 'sleep', lambda seconds: sleeps.append(seconds))

    report = reingest_missing_chunks(
        store,
        raw_root=raw_root,
        apply=True,
        sleep_seconds=3.0,
        downloader=lambda url: pdfs[url],
    )

    assert report.added == 2
    assert sleeps == [3.0]
