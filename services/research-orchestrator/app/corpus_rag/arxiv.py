"""Allowlisted arXiv ingestion for the corpus-RAG pipeline.

Boundary design (security/bounded-source-fetch): deterministic queries
against ``export.arxiv.org/api/query``, a category allowlist enforced before
any network I/O, per-entry size caps enforced before and during download,
and digest-verified payloads so re-runs can skip already-ingested sources.
Stdlib only (urllib + xml.etree); no new dependencies.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable

ALLOWED_CATEGORIES = frozenset({'cs.LG', 'stat.ML', 'cs.CL', 'cs.AI'})
MAX_PDF_BYTES = 20 * 1024 * 1024  # 20 MiB per preprint
_DOWNLOAD_TIMEOUT_SECONDS = 90
_RETRIES = 2
_API_ENDPOINT = 'https://export.arxiv.org/api/query'
_ATOM_NS = 'http://www.w3.org/2005/Atom'
_ARXIV_NS = 'http://arxiv.org/schemas/atom'
_USER_AGENT = 'GlasslabResearchPrototype/0.1'

Urlopen = Callable[..., Any]


class PdfTooLargeError(ValueError):
    """Raised when a PDF payload exceeds the configured size cap."""


@dataclass(frozen=True, slots=True)
class ArxivEntry:
    """One parsed Atom entry; ``pdf_bytes`` is the declared size when known."""

    arxiv_id: str
    title: str
    authors: tuple[str, ...]
    published: _dt.date
    pdf_url: str
    pdf_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ArxivQuery:
    """Deterministic, allowlisted query against the arXiv API.

    ``__post_init__`` enforces the category allowlist fail-closed: a query
    naming a category outside ``ALLOWED_CATEGORIES`` cannot be constructed,
    so no fetch can ever target a non-allowlisted category.
    """

    categories: tuple[str, ...] = ('cs.LG', 'stat.ML')
    date_from: _dt.date | None = None
    max_results: int = 50
    max_pdf_bytes: int = MAX_PDF_BYTES

    def __post_init__(self) -> None:
        if not self.categories:
            raise ValueError('at least one category is required')
        for category in self.categories:
            if category not in ALLOWED_CATEGORIES:
                raise ValueError(f'category {category!r} is not allowlisted')
        if not 1 <= self.max_results <= 2000:
            raise ValueError('max_results must be in [1, 2000]')
        if self.max_pdf_bytes < 1:
            raise ValueError('max_pdf_bytes must be positive')

    def query_url(self) -> str:
        terms = ' OR '.join(f'cat:{category}' for category in self.categories)
        params = {
            'search_query': terms,
            'start': 0,
            'max_results': self.max_results,
            'sortBy': 'submittedDate',
            'sortOrder': 'descending',
        }
        return f'{_API_ENDPOINT}?{urllib.parse.urlencode(params)}'


def is_oversized(entry: ArxivEntry, max_pdf_bytes: int) -> bool:
    """True when the entry's declared PDF size exceeds the cap (skip, no fetch)."""
    return entry.pdf_bytes is not None and entry.pdf_bytes > max_pdf_bytes


def _entry_text(entry: ET.Element, tag: str) -> str:
    node = entry.find(f'{{{_ATOM_NS}}}{tag}')
    return (node.text or '').strip() if node is not None else ''


def _parse_published(raw: str) -> _dt.date:
    # arXiv publishes ISO-8601 timestamps like 2024-01-01T00:00:00Z.
    return _dt.datetime.fromisoformat(raw.replace('Z', '+00:00')).date()


def _parse_entry(node: ET.Element) -> ArxivEntry:
    arxiv_id = _entry_text(node, 'id')
    title = _entry_text(node, 'title')
    authors = tuple(
        name.text.strip()
        for author in node.findall(f'{{{_ATOM_NS}}}author')
        if (name := author.find(f'{{{_ATOM_NS}}}name')) is not None
        and name.text
    )
    published = _parse_published(_entry_text(node, 'published'))
    pdf_url = ''
    for link in node.findall(f'{{{_ATOM_NS}}}link'):
        if link.get('rel') == 'related' and link.get('type') == 'application/pdf':
            pdf_url = link.get('href', '')
            break
    if not pdf_url:
        for link in node.findall(f'{{{_ATOM_NS}}}link'):
            if link.get('title') == 'pdf':
                pdf_url = link.get('href', '')
                break
    return ArxivEntry(
        arxiv_id=arxiv_id,
        title=title,
        authors=authors,
        published=published,
        pdf_url=pdf_url,
    )


def fetch_entries(
    query: ArxivQuery,
    *,
    urlopen: Urlopen | None = None,
) -> list[ArxivEntry]:
    """Query the arXiv API and parse the Atom response into entries.

    Entries published before ``query.date_from`` are dropped client-side so
    the date window is enforced deterministically regardless of API sort
    behavior. ``urlopen`` is injectable for tests; it must behave like
    ``urllib.request.urlopen`` (context manager, ``.read``, ``.headers``).
    """
    if urlopen is None:
        urlopen = urllib.request.urlopen
    request = urllib.request.Request(
        query.query_url(), headers={'User-Agent': _USER_AGENT}
    )
    with urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        payload = response.read()
    root = ET.fromstring(payload)
    entries = [
        _parse_entry(node)
        for node in root.findall(f'{{{_ATOM_NS}}}entry')
    ]
    if query.date_from is not None:
        entries = [entry for entry in entries if entry.published >= query.date_from]
    return entries


def download_pdf(
    entry: ArxivEntry,
    *,
    max_pdf_bytes: int = MAX_PDF_BYTES,
    timeout: int = _DOWNLOAD_TIMEOUT_SECONDS,
    retries: int = _RETRIES,
    urlopen: Urlopen | None = None,
) -> tuple[bytes, str]:
    """Download one PDF, size-capped and digest-verified.

    Returns ``(payload, sha256)``. The cap is enforced twice: against the
    declared ``Content-Length`` before reading the body, and against the
    accumulated stream while reading (a lying server cannot exceed the cap).
    Retries mirror ``fetch_corpus.py``; the payload must start with ``%PDF``.
    """
    if urlopen is None:
        urlopen = urllib.request.urlopen
    request = urllib.request.Request(
        entry.pdf_url, headers={'User-Agent': _USER_AGENT}
    )
    last_error: Exception | None = None
    for _attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                declared = response.headers.get('Content-Length')
                if declared is not None and int(declared) > max_pdf_bytes:
                    # Deterministic rejection, not a transport failure: do
                    # not retry a payload that is oversized by declaration.
                    raise PdfTooLargeError(
                        f'{entry.pdf_url} declares {declared} bytes '
                        f'> cap {max_pdf_bytes}'
                    )
                data = bytearray()
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > max_pdf_bytes:
                        raise PdfTooLargeError(
                            f'{entry.pdf_url} exceeded {max_pdf_bytes} bytes'
                        )
            payload = bytes(data)
            if not payload.startswith(b'%PDF'):
                raise ValueError(f'{entry.pdf_url} did not return a PDF payload')
            return payload, hashlib.sha256(payload).hexdigest()
        except PdfTooLargeError:
            raise
        except Exception as exc:  # noqa: BLE001 - retry any transport failure
            last_error = exc
    raise RuntimeError(
        f'download failed after {retries + 1} attempts: {last_error}'
    )