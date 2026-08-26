"""Build the operator knowledge corpus: fetch -> ingest -> embed.

One reproducible command that turns a manifest of open-access sources into a
production-ready KnowledgeManager store:

    python scripts/build_knowledge_corpus.py \
        --store /var/lib/glasslab-research-orchestrator/knowledge-corpus.db \
        --raw-dir /home/gr66ss/rag-data/raw \
        --embedding arctic-m

Steps: (1) download any missing manifest sources (sha256-verified,
resumable); (2) ingest every fetched document through KnowledgeManager's
fail-closed ingestion (secret/path checks included), creating canonical
knowledge_sources/knowledge_chunks rows; (3) embed those chunks for dense
retrieval. Safe to re-run: completed downloads and already-indexed chunks
are skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SERVICE_DIR))

from app.corpus_rag.contracts import CorpusManifestEntry  # noqa: E402
from app.knowledge_dense import build_dense_index  # noqa: E402
from app.knowledge_manager import KnowledgeManager  # noqa: E402
from app.schemas import SourceType  # noqa: E402
from app.storage import SqliteStore  # noqa: E402

_BOOK_IDS = frozenset({'islr2', 'esl'})


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class _BuildSettings:
    """Minimal settings shim for the standalone corpus builder."""

    def __init__(self, database_path: str, allowlist_root: str) -> None:
        self.database_path = database_path
        self.knowledge_allowlist_roots = [allowlist_root]
        self.knowledge_chunk_size = 1500
        self.knowledge_chunk_overlap = 150
        self.knowledge_max_source_bytes = 64 * 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    parser.add_argument('--raw-dir', default='/home/gr66ss/rag-data/raw')
    parser.add_argument(
        '--manifest',
        default=str(_SERVICE_DIR / 'eval' / 'corpus_rag' / 'manifest.jsonl'),
    )
    parser.add_argument('--corpus', default='statistical-learning-methods')
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument(
        '--embedding',
        choices=['offline', 'arctic-m', 'arctic-s'],
        default='arctic-m',
        help='offline = deterministic hash vectors (tests only)',
    )
    args = parser.parse_args(argv)

    entries = [
        CorpusManifestEntry.model_validate(json.loads(line))
        for line in Path(args.manifest).read_text().splitlines()
        if line.strip()
    ]
    raw_dir = Path(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    fetched: list[Path] = []

    for entry in entries:
        if entry.skip:
            continue
        target = raw_dir / f'{entry.id}.pdf'
        if target.exists() and (
            entry.sha256 is None or _sha256_of(target) == entry.sha256
        ):
            fetched.append(target)
            continue
        try:
            request = urllib.request.Request(
                entry.url,
                headers={'User-Agent': 'GlasslabResearchPrototype/0.1'},
            )
            with urllib.request.urlopen(request, timeout=args.timeout) as resp:
                data = resp.read()
            if not data.startswith(b'%PDF'):
                raise ValueError('not a PDF payload')
            actual = hashlib.sha256(data).hexdigest()
            if entry.sha256 is not None and actual != entry.sha256:
                raise ValueError(f'sha256 mismatch (got {actual})')
            target.write_bytes(data)
            fetched.append(target)
        except Exception as exc:  # noqa: BLE001 - per-source isolation
            errors.append(f'{entry.id}: fetch failed ({exc})')

    settings = _BuildSettings(
        database_path=args.store,
        allowlist_root=str(raw_dir),
    )
    store = SqliteStore(settings.database_path)
    knowledge_root = Path(settings.database_path).parent / 'knowledge'
    knowledge_root.mkdir(parents=True, exist_ok=True)
    km = KnowledgeManager(
        store=store,
        root=knowledge_root,
        allowlist_roots=[str(raw_dir)],
    )

    indexed_docs = 0
    extractor = None
    for path in fetched:
        entry_id = path.stem
        manifest_entry = next(e for e in entries if e.id == entry_id)
        try:
            if extractor is None:
                from app.corpus_rag.pdf_backend import PyMuPdfBackend

                extractor = PyMuPdfBackend()
            # Extract FIRST, then fail-close on secrets over the extracted
            # text: raw PDF bytes are compressed streams that trip generic
            # secret heuristics without containing any human-readable secret.
            document = extractor.extract(path.read_bytes())
            from app.corpus_rag.documents import assert_no_secrets

            assert_no_secrets(document.text)
            km.ingest_text(
                source_type=(
                    SourceType.PAPER
                    if doc_type_of(entry_id) == 'paper'
                    else SourceType.DOCUMENTATION
                ),
                canonical_uri=path.resolve().as_uri(),
                text=document.text,
                title=manifest_entry.title,
                metadata={'manifest_id': entry_id},
            )
            indexed_docs += 1
        except Exception as exc:  # noqa: BLE001 - per-document isolation
            errors.append(f'{entry_id}: ingest failed ({exc})')

    dense_summary = None
    if args.embedding != 'offline':
        from app.corpus_rag.embeddings import get_provider

        provider = get_provider(args.embedding)
        try:
            dense_summary = build_dense_index(store, provider)
        except Exception as exc:  # noqa: BLE001 - report, do not crash rebuild
            errors.append(f'embedding failed ({exc})')
        finally:
            unload = getattr(provider, 'unload', None)
            if callable(unload):
                unload()

    print(json.dumps({
        'indexed_documents': indexed_docs,
        'errors': errors,
        'dense': dense_summary,
        'store': args.store,
        'corpus': args.corpus,
    }, indent=2))
    return 0


def doc_type_of(entry_id: str) -> str:
    return 'book' if entry_id in _BOOK_IDS else 'paper'


if __name__ == '__main__':
    raise SystemExit(main())
