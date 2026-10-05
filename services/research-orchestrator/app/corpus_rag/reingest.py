"""Reproducible re-ingest of knowledge sources that have no ``rag_chunks``.

GitHub issue #622: dozens of ``orchestrator_knowledge_*`` sources have zero
``orchestrator_rag_chunks``. The arXiv sidecar stages a ``file://`` PDF under
``corpus_rag_raw_root`` but ``ingest_arxiv.py`` SKIPS existing sources, so a
chunk-less one is never retried. This healer re-ingests the local PDF, or
re-downloads and re-stages the exact versioned arXiv PDF, or reports
``unrecoverable`` -- never fabricating bytes or chunks. It is dry by default.

A document that extracts successfully but still yields zero chunks (a
degenerate or non-prose PDF) is reported as ``empty`` -- never as ``healed`` --
so an operator is not misled and a second applied run is a no-op. Old-style
arXiv sources whose staged filename dropped the archive prefix (``hep-th``)
recover their full id from the stored ``metadata`` rather than the filename.
"""

from __future__ import annotations

import hashlib
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.corpus_rag.arxiv import ArxivEntry, download_pdf
from app.corpus_rag.pipeline import ingest_document, stage_raw_pdf
from app.knowledge_manager import KnowledgeError
from app.schemas import KnowledgeSource, SourceType

PdfDownloader = Callable[[str], bytes]

# Staged PDF filename -> arXiv id. Modern: YYMM.NNNNN with optional version.
# Old-style: archive_NNNNNNN, where the underscore is the path separator.
_MODERN_ARXIV_ID = re.compile(r'^\d{4}\.\d{4,5}(?:v\d+)?$')
_OLD_ARXIV_ID = re.compile(r'^([A-Za-z][A-Za-z.\-]*)_(\d{7})(v\d+)?$')
# Full old-style id as preserved in source metadata: archive/YYMMNNN(vN).
_OLD_STYLE_FULL_ARXIV_ID = re.compile(r'^[A-Za-z][A-Za-z.\-]*/\d{7}(?:v\d+)?$')

_EPOCH = date(1970, 1, 1)

STATUS_HEALED_LOCAL = 'healed-local'
STATUS_HEALED_FETCH = 'healed-fetch'
STATUS_WOULD_LOCAL = 'would-heal-local'
STATUS_WOULD_FETCH = 'would-heal-fetch'
STATUS_SKIPPED = 'skipped-has-chunks'
STATUS_UNRECOVERABLE = 'unrecoverable'
STATUS_EMPTY = 'empty'
STATUS_ERROR = 'error'

FAILURE_SECRET = 'secret-rejected'
FAILURE_OTHER = 'other'


@dataclass
class ReingestSourceReport:
    source_id: str
    canonical_uri: str
    status: str
    detail: str | None = None
    added_chunks: int = 0
    failure_class: str | None = None


@dataclass
class ReingestReport:
    apply: bool
    raw_root: str
    considered: int = 0
    added: int = 0
    added_chunks: int = 0
    network_fetches: int = 0
    skipped_has_chunks: int = 0
    unrecoverable: int = 0
    empty: int = 0
    errors: int = 0
    secret_rejected: int = 0
    sources: list[ReingestSourceReport] = field(default_factory=list)


def _failure_class(exc: Exception) -> str:
    # Distinguish the documented scanner false-positive class from real
    # failures; the scanner itself must not be weakened here (security).
    if isinstance(exc, KnowledgeError) and 'secret pattern' in str(exc):
        return FAILURE_SECRET
    return FAILURE_OTHER


def _add(
    report: ReingestReport, source: KnowledgeSource, status: str, **extra: Any
) -> None:
    report.sources.append(
        ReingestSourceReport(source.source_id, source.canonical_uri, status, **extra)
    )


def _strip_pdf_suffix(name: str) -> str:
    # Strip only a trailing ``.pdf``: Path.stem would cut ``2401.00007v2`` at
    # its internal dot, which is not a file extension here.
    return name[:-4] if name.lower().endswith('.pdf') else name


def parse_arxiv_id(name: str) -> str | None:
    stem = _strip_pdf_suffix(name)
    if _MODERN_ARXIV_ID.match(stem):
        return stem
    match = _OLD_ARXIV_ID.match(stem)
    if match is None:
        return None
    version = match.group(3) or ''
    return f'{match.group(1)}/{match.group(2)}{version}'


def _arxiv_id_from_metadata(source: KnowledgeSource) -> str | None:
    metadata = source.metadata or {}
    for key in ('arxiv_id', 'source_url'):
        value = metadata.get(key)
        if not isinstance(value, str):
            continue
        text = value.strip()
        for marker in ('/abs/', '/pdf/'):
            if marker in text:
                text = text.split(marker, 1)[1]
                break
        text = _strip_pdf_suffix(text.rstrip('/'))
        if _MODERN_ARXIV_ID.match(text) or _OLD_STYLE_FULL_ARXIV_ID.match(text):
            return text
    return None


def arxiv_pdf_url(arxiv_id: str) -> str:
    return f'https://arxiv.org/pdf/{arxiv_id}'


def default_downloader(url: str) -> bytes:
    entry = ArxivEntry(arxiv_id=url, title='', authors=(), published=_EPOCH, pdf_url=url)
    data, _digest = download_pdf(entry)
    return data


def _path_from_file_uri(uri: str) -> Path | None:
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost'):
        return None
    return Path(urllib.request.url2pathname(parsed.path))


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _has_rag_chunks(store: Any, source_id: str) -> bool:
    return bool(store.list_rag_chunks(source_ids=[source_id], limit=1))


def scan_missing_chunk_sources(store: Any) -> list[KnowledgeSource]:
    return [
        source
        for source in store.list_knowledge_sources()
        if not _has_rag_chunks(store, source.source_id)
    ]


def _classify(source: KnowledgeSource, root: Path) -> tuple[Any, ...]:
    # Returns ('unrecoverable', detail), ('local', path), or
    # ('fetch', name, arxiv_id, url). A file: URI outside root is unrecoverable:
    # re-staging elsewhere would create a new source, not heal the original.
    path = _path_from_file_uri(source.canonical_uri)
    if path is None:
        scheme = urllib.parse.urlsplit(source.canonical_uri).scheme
        return ('unrecoverable', f'canonical_uri scheme {scheme!r} is not a local file')
    if not _is_under(path, root):
        return ('unrecoverable', 'path is outside the raw root')
    if path.is_file():
        return ('local', path)
    arxiv_id = parse_arxiv_id(path.name) or _arxiv_id_from_metadata(source)
    if arxiv_id is None:
        return ('unrecoverable', 'file missing and no recoverable arXiv id')
    return ('fetch', _strip_pdf_suffix(path.name), arxiv_id, arxiv_pdf_url(arxiv_id))


def _ingest(
    store: Any,
    source: KnowledgeSource,
    *,
    data: bytes,
    canonical_uri: str,
    metadata: dict[str, Any] | None,
) -> int:
    digest = hashlib.sha256(data).hexdigest()
    if digest != source.digest:
        # ingest_document_bytes dedups on (digest, canonical_uri); a re-fetched
        # PDF can hash differently, so align the existing row's digest first to
        # force reuse of this source_id instead of inserting a duplicate row.
        store.save_knowledge_source(source.model_copy(update={'digest': digest}))
    doc_type = 'paper' if source.source_type == SourceType.PAPER else 'reference'
    report = ingest_document(
        store=store,
        data=data,
        canonical_uri=canonical_uri,
        title=source.title,
        doc_type=doc_type,
        authors=source.metadata.get('authors') or None,
        year=source.metadata.get('year'),
        metadata=metadata,
    )
    return report.n_section_units + report.n_evidence_spans


@dataclass
class _HealingContext:
    store: Any
    root: Path
    apply: bool
    download: PdfDownloader
    sleep_seconds: float
    fetches: int = 0

    def fetch(self, url: str) -> bytes:
        # Throttle only between network fetches; the first fetch is immediate.
        if self.fetches > 0 and self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        self.fetches += 1
        return self.download(url)


def _heal_source(
    source: KnowledgeSource, decision: tuple[Any, ...], context: _HealingContext
) -> tuple[str, int]:
    kind = decision[0]
    if kind == 'local':
        if not context.apply:
            return STATUS_WOULD_LOCAL, 0
        data = decision[1].read_bytes()
        chunks = _ingest(
            context.store,
            source,
            data=data,
            canonical_uri=source.canonical_uri,
            metadata=None,
        )
        if chunks == 0:
            return STATUS_EMPTY, 0
        return STATUS_HEALED_LOCAL, chunks

    _kind, name, arxiv_id, url = decision
    if not context.apply:
        return STATUS_WOULD_FETCH, 0
    data = context.fetch(url)
    staged = stage_raw_pdf(data, context.root, name)
    try:
        chunks = _ingest(
            context.store,
            source,
            data=data,
            canonical_uri=staged.resolve().as_uri(),
            metadata={'source_url': url, 'arxiv_id': arxiv_id},
        )
    except BaseException:
        # Fail-closed: a rejected ingest must not leave bytes the persisted
        # source's canonical URI still resolves to.
        staged.unlink(missing_ok=True)
        raise
    if chunks == 0:
        # Keep the staged bytes so the next run re-ingests locally (no network)
        # and reports empty again: a zero-chunk source is never "added".
        return STATUS_EMPTY, 0
    return STATUS_HEALED_FETCH, chunks


def reingest_missing_chunks(
    store: Any,
    *,
    raw_root: Path,
    apply: bool = False,
    limit: int | None = None,
    source_ids: list[str] | None = None,
    sleep_seconds: float = 3.0,
    downloader: PdfDownloader | None = None,
    diagnose: bool = False,
) -> ReingestReport:
    """Heal knowledge sources that have no ``rag_chunks``.

    ``apply=False`` (default) is a dry run: no writes, no network. ``source_ids``
    and ``limit`` narrow a rollout; ``downloader`` is the network seam.
    """
    root = Path(raw_root)
    report = ReingestReport(apply=apply, raw_root=str(root))
    wanted = set(source_ids) if source_ids else None
    context = _HealingContext(
        store=store,
        root=root,
        apply=apply,
        download=downloader if downloader is not None else default_downloader,
        sleep_seconds=sleep_seconds,
    )
    actions = 0

    for source in store.list_knowledge_sources():
        if wanted is not None and source.source_id not in wanted:
            continue
        report.considered += 1
        if _has_rag_chunks(store, source.source_id):
            report.skipped_has_chunks += 1
            _add(report, source, STATUS_SKIPPED)
            continue

        decision = _classify(source, root)
        if decision[0] == 'unrecoverable':
            report.unrecoverable += 1
            _add(report, source, STATUS_UNRECOVERABLE, detail=decision[1])
            continue

        if limit is not None and actions >= limit:
            break

        try:
            status, chunks = _heal_source(source, decision, context)
        except Exception as exc:  # noqa: BLE001 - isolate per-source failures
            report.errors += 1
            failure_class = _failure_class(exc)
            if failure_class == FAILURE_SECRET:
                report.secret_rejected += 1
            detail = (
                traceback.format_exc() if diagnose else f'{type(exc).__name__}: {exc}'
            )
            if diagnose:
                print(f'[reingest] {source.source_id}: {failure_class}', file=sys.stderr)
                traceback.print_exc()
            _add(
                report,
                source,
                STATUS_ERROR,
                detail=detail,
                failure_class=failure_class,
            )
            continue

        actions += 1
        if status in (STATUS_HEALED_LOCAL, STATUS_HEALED_FETCH):
            report.added += 1
            report.added_chunks += chunks
        elif status == STATUS_EMPTY:
            report.empty += 1
        _add(report, source, status, added_chunks=chunks)

    report.network_fetches = context.fetches
    return report
