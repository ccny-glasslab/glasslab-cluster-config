"""Network-free tests for scripts/corpus_rag/fetch_corpus.py (file:// URLs).

Covers resumable acquisition, digest verification, skip handling, and
optional registration of fetched sources into a SQLite store + corpus.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'corpus_rag'))

from fetch_corpus import main as fetch_main  # noqa: E402

from app.storage import SqliteStore  # noqa: E402


def _write_pdf(path: Path, marker: str) -> str:
    data = f'%PDF-1.4\n{marker}\n%%EOF\n'.encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def local_manifest(tmp_path: Path) -> Path:
    raw_a = tmp_path / 'src-a.pdf'
    raw_b = tmp_path / 'src-b.pdf'
    digest_a = _write_pdf(raw_a, 'fixture alpha')
    digest_b = _write_pdf(raw_b, 'fixture beta')
    entries = [
        {
            'id': 'alpha',
            'title': 'Alpha paper',
            'url': raw_a.as_uri(),
            'sha256': digest_a,
            'license_note': 'fixture',
        },
        {
            'id': 'beta',
            'title': 'Beta paper',
            'url': raw_b.as_uri(),
            'sha256': digest_b,
            'license_note': 'fixture',
        },
        {
            'id': 'paywalled-one',
            'title': 'Paywalled',
            'url': 'https://example.invalid/none.pdf',
            'sha256': None,
            'license_note': 'paywalled',
            'skip': True,
            'skip_reason': 'paywalled',
        },
    ]
    manifest = tmp_path / 'manifest.jsonl'
    manifest.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
    return manifest


def test_fetch_downloads_verifies_and_writes_sidecars(
    local_manifest: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / 'out'
    code = fetch_main(
        ['--manifest', str(local_manifest), '--dest', str(dest)]
    )
    assert code == 0
    assert (dest / 'alpha.pdf').read_bytes().startswith(b'%PDF')
    assert (dest / 'beta.pdf').exists()
    sidecar = json.loads((dest / 'alpha.json').read_text())
    assert sidecar['id'] == 'alpha'
    assert len(sidecar['sha256']) == 64
    assert sidecar['bytes'] == (dest / 'alpha.pdf').stat().st_size
    summary = json.loads(capsys.readouterr().out)
    assert sorted(summary['fetched']) == ['alpha', 'beta']
    assert summary['manifest_skipped'] == ['paywalled-one']


def test_fetch_resumable_second_run_skips_existing(
    local_manifest: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / 'out'
    assert fetch_main(['--manifest', str(local_manifest), '--dest', str(dest)]) == 0
    capsys.readouterr()
    assert fetch_main(['--manifest', str(local_manifest), '--dest', str(dest)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert sorted(summary['skipped_existing']) == ['alpha', 'beta']
    assert summary['fetched'] == []


def test_fetch_digest_mismatch_quarantines_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = tmp_path / 'src.pdf'
    _write_pdf(raw, 'actual content')
    bad_manifest = tmp_path / 'bad.jsonl'
    bad_manifest.write_text(
        json.dumps(
            {
                'id': 'broken',
                'title': 'Broken',
                'url': raw.as_uri(),
                'sha256': 'f' * 64,
                'license_note': 'fixture',
            }
        )
        + '\n'
    )
    dest = tmp_path / 'out'
    assert fetch_main(['--manifest', str(bad_manifest), '--dest', str(dest)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert [failure['id'] for failure in summary['failures']] == ['broken']
    assert not (dest / 'broken.pdf').exists()

    strict_code = fetch_main(
        ['--manifest', str(bad_manifest), '--dest', str(dest), '--strict']
    )
    assert strict_code != 0


def test_register_creates_store_sources_and_corpus(
    local_manifest: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / 'out'
    store_path = tmp_path / 'store.db'
    assert fetch_main(
        [
            '--manifest',
            str(local_manifest),
            '--dest',
            str(dest),
            '--register',
            str(store_path),
            '--corpus',
            'fixtures',
        ]
    ) == 0
    capsys.readouterr()
    store = SqliteStore(str(store_path))
    corpora = {corpus.slug for corpus in store.list_corpora()}
    assert 'fixtures' in corpora
    corpus = store.get_corpus('fixtures')
    assert corpus is not None
    members = set(store.list_corpus_sources(corpus.corpus_id))
    sources = {source.canonical_uri: source for source in store.list_knowledge_sources()}
    registered_uris = {
        uri for uri in sources if uri.startswith('file://')
    }
    assert len(registered_uris) == 2
    assert len(members) == 2
