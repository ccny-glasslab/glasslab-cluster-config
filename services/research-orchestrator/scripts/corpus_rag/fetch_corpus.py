"""Fetch (or register locally staged) corpus sources listed in a manifest.

Resumable, digest-verified acquisition for the corpus-RAG prototype:
downloads each non-skipped manifest entry into --dest, verifies sha256 when
the manifest pins one, writes a sidecar <id>.json with provenance, and can
optionally register fetched files as KnowledgeSource rows inside a corpus.

Network-free operation is supported: file:// URLs and pre-staged files are
first-class; re-runs skip files whose digest already matches.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

_SERVICE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SERVICE_DIR))

from app.corpus_rag import CorpusManifestEntry, CorpusRecord  # noqa: E402
from app.corpus_rag.documents import ingest_document_bytes  # noqa: E402
from app.storage import SqliteStore  # noqa: E402

_BOOK_IDS = frozenset({'islr2', 'esl'})
_DOWNLOAD_TIMEOUT_SECONDS = 90
_RETRIES = 2


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, target: Path, timeout: int) -> None:
    partial = target.with_suffix(target.suffix + '.part')
    request = urllib.request.Request(
        url, headers={'User-Agent': 'GlasslabResearchPrototype/0.1'}
    )
    last_error: Exception | None = None
    for _attempt in range(_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                with partial.open('wb') as handle:
                    for chunk in iter(lambda: response.read(1024 * 1024), b''):
                        handle.write(chunk)
            if not partial.read_bytes().startswith(b'%PDF'):
                raise ValueError(f'{url} did not return a PDF payload')
            partial.replace(target)
            return
        except Exception as exc:  # noqa: BLE001 - retry any transport failure
            last_error = exc
    if partial.exists():
        partial.unlink()
    raise RuntimeError(f'download failed after {_RETRIES + 1} attempts: {last_error}')


def _register(
    store_path: Path, corpus_slug: str, fetched: list[Path]
) -> int:
    store = SqliteStore(str(store_path))
    corpus = store.get_corpus(corpus_slug)
    if corpus is None:
        corpus = store.create_corpus(CorpusRecord(slug=corpus_slug))
    registered = 0
    for path in fetched:
        doc_type = 'book' if path.stem in _BOOK_IDS else 'paper'
        _, record = ingest_document_bytes(
            store=store,
            data=path.read_bytes(),
            canonical_uri=path.resolve().as_uri(),
            title=path.stem,
            doc_type=doc_type,
            corpus_slug=None,
        )
        if store.add_corpus_source(corpus.corpus_id, record.source_id):
            registered += 1
    return registered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--manifest',
        default=str(_SERVICE_DIR / 'eval' / 'corpus_rag' / 'manifest.jsonl'),
    )
    parser.add_argument('--dest', default='/home/gr66ss/rag-data/raw')
    parser.add_argument('--only', action='append', dest='only_ids')
    parser.add_argument('--timeout', type=int, default=_DOWNLOAD_TIMEOUT_SECONDS)
    parser.add_argument('--register', default=None, help='SQLite store path')
    parser.add_argument('--corpus', default='statistical-learning-methods')
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    entries = [
        CorpusManifestEntry.model_validate(json.loads(line))
        for line in manifest_path.read_text().splitlines()
        if line.strip()
    ]
    wanted = set(args.only_ids or [])

    summary: dict[str, Any] = {
        'fetched': [],
        'skipped_existing': [],
        'manifest_skipped': [],
        'failures': [],
    }
    fetched_paths: list[Path] = []

    for entry in entries:
        if entry.skip:
            summary['manifest_skipped'].append(entry.id)
            continue
        if wanted and entry.id not in wanted:
            continue
        target = dest / f'{entry.id}.pdf'
        try:
            if target.exists() and (
                entry.sha256 is None or _sha256_of(target) == entry.sha256
            ):
                summary['skipped_existing'].append(entry.id)
                fetched_paths.append(target)
                continue
            _download(entry.url, target, args.timeout)
            actual_digest = _sha256_of(target)
            if entry.sha256 is not None and actual_digest != entry.sha256:
                target.unlink(missing_ok=True)
                raise ValueError(
                    f'sha256 mismatch for {entry.id}: expected {entry.sha256}, got {actual_digest}'
                )
            sidecar = {
                'id': entry.id,
                'url': entry.url,
                'sha256': actual_digest,
                'bytes': target.stat().st_size,
                'fetched_at': _dt.datetime.now(_dt.UTC).isoformat(),
            }
            (dest / f'{entry.id}.json').write_text(json.dumps(sidecar, indent=2))
            summary['fetched'].append(entry.id)
            fetched_paths.append(target)
        except Exception as exc:  # noqa: BLE001 - per-source isolation
            summary['failures'].append({'id': entry.id, 'reason': str(exc)})
            print(f'[fetch] failure {entry.id}: {exc}', file=sys.stderr)

    if args.register:
        store_path = Path(args.register)
        summary['registered'] = _register(store_path, args.corpus, fetched_paths)

    print(json.dumps(summary))
    if args.strict and summary['failures']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
