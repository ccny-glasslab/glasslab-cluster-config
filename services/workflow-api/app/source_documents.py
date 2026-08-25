"""Fetch, parse, validate, and persist source documents (papers, web pages, PDFs).

Source documents are fetched via HTTP, parsed for title/abstract/hint metadata,
validated against an expected title when supplied, and persisted to either
local filesystem or MinIO. The extracted hints feed the interpretation and
design stages without requiring a live model call per document.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import hashlib
import html
import http.client
import ipaddress
import mimetypes
import re
import socket
import ssl
import time
from pathlib import Path
from collections.abc import Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status

from .config import Settings
from .persistence import RunStore
from .schemas import SourceDocumentRecord

HTML_TAG_RE = re.compile(r'<[^>]+>')
HTML_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)
TITLE_WORD_RE = re.compile(r"[A-Za-z0-9]+")
TITLE_NORMALIZE_RE = re.compile(r'[^a-z0-9]+')
COMMON_TITLE_WORDS = {
    'the', 'and', 'for', 'with', 'from', 'using', 'into', 'towards', 'through',
    'over', 'under', 'based', 'study', 'learning', 'vision', 'method', 'methods',
    'approach', 'approaches', 'analysis', 'data', 'model', 'models', 'paper',
}
METHOD_KEYWORDS = [
    'vision transformer',
    'transformer',
    'cnn',
    'convolutional neural network',
    'resnet',
    'vit',
    'focal loss',
    'cross entropy',
    'contrastive learning',
    'diffusion',
    'gan',
]
LOSS_KEYWORDS = [
    'cross entropy',
    'focal loss',
    'triplet loss',
    'contrastive loss',
    'hinge loss',
    'dice loss',
    'l1 loss',
    'l2 loss',
    'mean squared error',
    'binary cross entropy',
]
ARCHITECTURE_KEYWORDS = [
    'vision transformer',
    'transformer',
    'cnn',
    'convolutional neural network',
    'resnet',
    'unet',
    'u-net',
    'efficientnet',
    'clip',
    'bert',
    'lstm',
    'graph neural network',
    'gnn',
    'autoencoder',
    'gan',
]
BASELINE_KEYWORDS = [
    'baseline',
    'random forest',
    'logistic regression',
    'linear probe',
    'svm',
    'xgboost',
    'catboost',
    'ablation',
]
METRIC_KEYWORDS = [
    'accuracy',
    'f1 score',
    'precision',
    'recall',
    'auc',
    'roc auc',
    'mean average precision',
    'mse',
    'rmse',
    'bleu',
    'iou',
    'intersection over union',
]
DATASET_KEYWORDS = [
    'cifar',
    'imagenet',
    'mnist',
    'coco',
    'laion',
    'artbench',
    'wikiart',
    'kaggle',
    'openml',
    'titanic',
]
DOMAIN_TASK_KEYWORDS = [
    'object detection',
    'image classification',
    'segmentation',
    'forgery detection',
    'anomaly detection',
    'retrieval',
    'generation',
    'captioning',
    'time series forecasting',
    'tabular classification',
    'benchmarking',
]
PYTHON_LIBRARY_KEYWORDS = [
    'torch',
    'torchvision',
    'pytorch lightning',
    'lightning',
    'transformers',
    'diffusers',
    'accelerate',
    'timm',
    'scikit-learn',
    'sklearn',
    'xgboost',
    'catboost',
    'tensorflow',
    'keras',
    'jax',
    'flax',
]

ALLOWED_SOURCE_DOCUMENT_MEDIA_TYPES = frozenset({
    'application/pdf',
    'application/xhtml+xml',
    'text/html',
    'text/plain',
})
SOURCE_DOCUMENT_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class SourceDocumentFetchError(ValueError):
    """Raised when a remote source violates the bounded-fetch policy."""


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP destination is the address we validated."""

    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host=host, port=port, timeout=timeout)
        self._validated_address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._validated_address, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self.sock = raw_socket
            self._tunnel()
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


Resolver = Callable[..., list[tuple[object, ...]]]
ConnectionFactory = Callable[[str, int, str, float], object]


def _default_connection_factory(host: str, port: int, address: str, timeout: float) -> _PinnedHTTPSConnection:
    return _PinnedHTTPSConnection(host, port, address, timeout)


def _validate_source_url(
    source_url: str,
    *,
    resolver: Resolver,
    allowed_hosts: Iterable[str] | None,
    deadline: float,
    monotonic: Callable[[], float],
) -> tuple[str, int, str, str, str]:
    parsed = urlsplit(source_url)
    if parsed.scheme.lower() != 'https':
        raise SourceDocumentFetchError('source document URL must use HTTPS')
    if parsed.username is not None or parsed.password is not None:
        raise SourceDocumentFetchError('source document URL must not contain credentials')
    if not parsed.hostname:
        raise SourceDocumentFetchError('source document URL must contain a hostname')

    host = parsed.hostname.rstrip('.').lower()
    approved_hosts = {item.rstrip('.').lower() for item in (allowed_hosts or ()) if item.strip()}
    if approved_hosts and host not in approved_hosts:
        raise SourceDocumentFetchError(f'source document host is not allowed: {host}')

    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise SourceDocumentFetchError('source document URL contains an invalid port') from exc
    try:
        resolved = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceDocumentFetchError(f'source document DNS resolution failed: {exc}') from exc
    if not resolved:
        raise SourceDocumentFetchError('source document hostname resolved to no addresses')
    _remaining_fetch_time(deadline, monotonic)

    addresses: list[str] = []
    for result in resolved:
        sockaddr = result[4]
        address = str(sockaddr[0])
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise SourceDocumentFetchError('source document DNS returned an invalid address') from exc
        if (
            not parsed_address.is_global
            or parsed_address.is_multicast
            or parsed_address.is_unspecified
            or parsed_address.is_reserved
        ):
            raise SourceDocumentFetchError(
                f'source document hostname resolved to a non-public address: {address}'
            )
        addresses.append(address)

    path = parsed.path or '/'
    if parsed.query:
        path = f'{path}?{parsed.query}'
    normalized_url = urlunsplit(('https', parsed.netloc, parsed.path, parsed.query, ''))
    return host, port, addresses[0], path, normalized_url


def _remaining_fetch_time(deadline: float, monotonic: Callable[[], float]) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise SourceDocumentFetchError('source document fetch exceeded total deadline')
    return remaining


def _set_connection_timeout(connection: object, timeout: float) -> None:
    sock = getattr(connection, 'sock', None)
    if sock is not None:
        sock.settimeout(timeout)


def derive_arxiv_pdf_url(source_url: str | None) -> str | None:
    if not source_url:
        return None
    normalized = source_url.strip()
    if not normalized:
        return None
    if 'arxiv.org/pdf/' in normalized:
        return normalized
    match = re.search(r'arxiv\.org/abs/([^?#/]+)', normalized)
    if match:
        return f'https://arxiv.org/pdf/{match.group(1)}.pdf'
    return None


def build_source_fetch_candidates(official_page: str | None, pdf_url: str | None) -> list[str]:
    candidates: list[str] = []

    derived_pdf = derive_arxiv_pdf_url(pdf_url) or derive_arxiv_pdf_url(official_page)
    if derived_pdf:
        candidates.append(derived_pdf)
    if pdf_url and pdf_url.strip():
        candidates.append(pdf_url.strip())
    if official_page and official_page.strip():
        candidates.append(official_page.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        deduped.append(candidate)
        seen.add(candidate)
    return deduped


def guess_document_title(source_url: str) -> str:
    parsed = source_url.rstrip('/').rsplit('/', 1)[-1]
    return parsed or 'source-document'


def _title_terms(value: str | None) -> list[str]:
    if not value:
        return []
    terms: list[str] = []
    for match in TITLE_WORD_RE.findall(value.lower()):
        if len(match) < 4 or match in COMMON_TITLE_WORDS:
            continue
        terms.append(match)
    return list(dict.fromkeys(terms))


def _normalize_title(value: str | None) -> str:
    if not value:
        return ''
    return TITLE_NORMALIZE_RE.sub(' ', value.lower()).strip()


def validate_document_identity(
    *,
    expected_title: str | None,
    fetched_title: str | None,
    text_excerpt: str | None,
) -> tuple[str, list[str]]:
    if not expected_title:
        return 'unknown', []

    expected_terms = _title_terms(expected_title)
    if not expected_terms:
        return 'unknown', ['expected title had no distinctive validation terms']

    normalized_expected = _normalize_title(expected_title)
    normalized_fetched = _normalize_title(fetched_title)
    if normalized_expected and normalized_fetched and normalized_expected == normalized_fetched:
        return 'matched', ['fetched title exactly matched the expected paper title']

    # Validation uses term-level matching, not fuzzy string similarity,
    # because paper titles from different sources often differ by punctuation
    # or word order rather than content. Two matching terms is sufficient
    # to confirm identity when the expected title has at least two
    # distinctive words.
    haystack = ' '.join(part for part in [fetched_title or '', text_excerpt or '']).lower()
    matched_terms = [term for term in expected_terms if term in haystack]
    if len(matched_terms) >= min(2, len(expected_terms)):
        return 'matched', [f"matched title terms: {', '.join(matched_terms[:4])}"]

    if not text_excerpt and (not fetched_title or re.fullmatch(r'[\w.-]+(?:\.pdf|\.html)?', fetched_title)):
        return 'mismatch', ['fetched document did not expose a usable title or extracted text for validation']

    return 'mismatch', [f"expected title terms not found: {', '.join(expected_terms[:4])}"]


def _truncate_text(value: str | None, limit: int = 1200) -> str | None:
    if not value:
        return None
    normalized = ' '.join(value.split()).strip()
    if not normalized:
        return None
    return normalized[:limit]


def sanitize_document_title(title: str | None, fallback: str) -> str:
    cleaned = _truncate_text(title, 300)
    if not cleaned:
        return fallback
    suspicious_markers = ('@import', '{', '}', ';', 'function(', 'var ', 'const ')
    if any(marker in cleaned.lower() for marker in suspicious_markers):
        return fallback
    return cleaned


def extract_html_title(content: bytes) -> str | None:
    decoded = content.decode('utf-8', errors='ignore')
    match = HTML_TITLE_RE.search(decoded)
    if not match:
        return None
    title = html.unescape(match.group(1))
    return _truncate_text(title, 300)


def extract_document_metadata(
    *,
    source_url: str,
    guessed_title: str | None,
    text_excerpt: str | None,
) -> dict[str, object]:
    excerpt = text_excerpt or ''
    normalized = ' '.join(excerpt.split())

    extracted_title = guessed_title
    authors: list[str] = []
    abstract_excerpt = None

    arxiv_match = re.search(
        r'Title:\s*(.*?)\s+Authors:\s*(.*?)\s+View PDF\s+HTML.*?Abstract:\s*(.*?)\s+Subjects:',
        normalized,
        flags=re.IGNORECASE,
    )
    if arxiv_match:
        extracted_title = _truncate_text(arxiv_match.group(1), 300) or extracted_title
        authors = [
            author.strip()
            for author in re.split(r',| and ', arxiv_match.group(2))
            if author.strip()
        ][:8]
        abstract_excerpt = _truncate_text(arxiv_match.group(3), 1500)
    else:
        abstract_match = re.search(
            r'Abstract[:\s]+(.*?)(?:\s+(?:Index Terms|Keywords|Introduction|I\.\s+INTRODUCTION)\b)',
            normalized,
            flags=re.IGNORECASE,
        )
        if abstract_match:
            abstract_excerpt = _truncate_text(abstract_match.group(1), 1500)

        # For PDFs, a coarse first-line title guess is still better than the filename.
        if guessed_title and re.fullmatch(r'[\w.-]+(?:\.pdf|\.html)?', guessed_title):
            lines = [line.strip() for line in excerpt.splitlines() if line.strip()]
            if lines:
                extracted_title = _truncate_text(lines[0], 300) or guessed_title

    haystack = f'{normalized} {(abstract_excerpt or "")}'.lower()
    method_hints = [keyword for keyword in METHOD_KEYWORDS if keyword in haystack]
    dataset_hints = [keyword for keyword in DATASET_KEYWORDS if keyword in haystack]
    loss_hints = [keyword for keyword in LOSS_KEYWORDS if keyword in haystack]
    architecture_hints = [keyword for keyword in ARCHITECTURE_KEYWORDS if keyword in haystack]
    baseline_hints = [keyword for keyword in BASELINE_KEYWORDS if keyword in haystack]
    metric_hints = [keyword for keyword in METRIC_KEYWORDS if keyword in haystack]
    domain_task_hints = [keyword for keyword in DOMAIN_TASK_KEYWORDS if keyword in haystack]
    python_library_hints = [keyword for keyword in PYTHON_LIBRARY_KEYWORDS if keyword in haystack]

    return {
        'title': extracted_title,
        'authors': list(dict.fromkeys(authors)),
        'abstract_excerpt': abstract_excerpt,
        'method_hints': list(dict.fromkeys(method_hints)),
        'dataset_hints': list(dict.fromkeys(dataset_hints)),
        'loss_hints': list(dict.fromkeys(loss_hints)),
        'architecture_hints': list(dict.fromkeys(architecture_hints)),
        'baseline_hints': list(dict.fromkeys(baseline_hints)),
        'metric_hints': list(dict.fromkeys(metric_hints)),
        'domain_task_hints': list(dict.fromkeys(domain_task_hints)),
        'python_library_hints': list(dict.fromkeys(python_library_hints)),
    }


def extract_text_excerpt(content: bytes, content_type: str | None, source_url: str) -> str | None:
    media_type = (content_type or '').split(';', 1)[0].strip().lower()
    try:
        if media_type in {'text/html', 'application/xhtml+xml'} or source_url.lower().endswith(('.html', '.htm')):
            decoded = content.decode('utf-8', errors='ignore')
            stripped = HTML_TAG_RE.sub(' ', decoded)
            normalized = ' '.join(html.unescape(stripped).split())
            return normalized[:4000] or None
        if media_type == 'text/plain':
            normalized = ' '.join(content.decode('utf-8', errors='ignore').split())
            return normalized[:4000] or None
        if media_type == 'application/pdf' or source_url.lower().endswith('.pdf'):
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(content))
            parts: list[str] = []
            for page in reader.pages[:5]:
                text = page.extract_text() or ''
                text = ' '.join(text.split())
                if text:
                    parts.append(text)
            joined = ' '.join(parts)
            return joined[:4000] or None
    except Exception:
        return None
    return None


def fetch_source_document_bytes(
    source_url: str,
    *,
    timeout: float = 30.0,
    max_bytes: int = 20 * 1024 * 1024,
    max_redirects: int = 4,
    chunk_size: int = 64 * 1024,
    allowed_hosts: Iterable[str] | None = None,
    resolver: Resolver = socket.getaddrinfo,
    connection_factory: ConnectionFactory = _default_connection_factory,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[bytes, str | None]:
    """Fetch one public HTTPS document through a validated, bounded stream.

    The connection is pinned to an address returned by the validation lookup,
    preventing a second DNS lookup from redirecting the socket to an internal
    address. Every redirect starts the complete validation process again.
    """
    if timeout <= 0 or max_bytes <= 0 or max_redirects < 0 or chunk_size <= 0:
        raise SourceDocumentFetchError('source document fetch limits must be positive')

    deadline = monotonic() + timeout
    current_url = source_url
    for redirect_count in range(max_redirects + 1):
        host, port, address, path, normalized_url = _validate_source_url(
            current_url,
            resolver=resolver,
            allowed_hosts=allowed_hosts,
            deadline=deadline,
            monotonic=monotonic,
        )
        connection = connection_factory(
            host, port, address, _remaining_fetch_time(deadline, monotonic)
        )
        response = None
        try:
            host_header = host if port == 443 else f'{host}:{port}'
            connection.request(
                'GET',
                path,
                headers={
                    'Host': host_header,
                    'User-Agent': 'glasslab-workflow-api/0.1.0',
                    'Accept': 'text/html,application/pdf,application/xhtml+xml,text/plain;q=0.9',
                    'Connection': 'close',
                },
            )
            _remaining_fetch_time(deadline, monotonic)
            _set_connection_timeout(connection, _remaining_fetch_time(deadline, monotonic))
            response = connection.getresponse()
            _remaining_fetch_time(deadline, monotonic)
            if response.status in SOURCE_DOCUMENT_REDIRECT_STATUSES:
                location = response.headers.get('Location')
                if not location:
                    raise SourceDocumentFetchError('source document redirect omitted Location')
                if redirect_count >= max_redirects:
                    raise SourceDocumentFetchError('source document redirect limit exceeded')
                current_url = urljoin(normalized_url, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise SourceDocumentFetchError(f'source document server returned HTTP {response.status}')

            content_type = response.headers.get('Content-Type')
            media_type = (content_type or '').split(';', 1)[0].strip().lower()
            if media_type not in ALLOWED_SOURCE_DOCUMENT_MEDIA_TYPES:
                raise SourceDocumentFetchError(
                    f'source document media type is not allowed: {media_type or "missing"}'
                )
            content_length = response.headers.get('Content-Length')
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise SourceDocumentFetchError('source document exceeds response byte limit')
                except ValueError as exc:
                    raise SourceDocumentFetchError('source document has invalid Content-Length') from exc

            chunks: list[bytes] = []
            total = 0
            while True:
                _set_connection_timeout(connection, _remaining_fetch_time(deadline, monotonic))
                chunk = response.read(min(chunk_size, max_bytes - total + 1))
                _remaining_fetch_time(deadline, monotonic)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SourceDocumentFetchError('source document exceeds response byte limit')
                chunks.append(chunk)
            return b''.join(chunks), content_type
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise SourceDocumentFetchError(f'source document fetch failed: {exc}') from exc
        finally:
            if response is not None:
                response.close()
            connection.close()

    raise SourceDocumentFetchError('source document redirect limit exceeded')


def persist_source_document_bytes(
    *,
    document_id: str,
    source_url: str,
    content: bytes,
    content_type: str | None,
    settings: Settings,
) -> str:
    guessed_ext = mimetypes.guess_extension((content_type or '').split(';', 1)[0].strip()) or ''
    if not guessed_ext:
        if source_url.lower().endswith('.pdf'):
            guessed_ext = '.pdf'
        elif source_url.lower().endswith(('.html', '.htm')):
            guessed_ext = '.html'
    key_name = f'{document_id}/source{guessed_ext}'

    if settings.source_document_storage_mode == 'minio':
        try:
            from minio import Minio
        except ImportError as exc:
            raise RuntimeError('minio package is required for source_document_storage_mode=minio') from exc

        if not settings.minio_access_key or not settings.minio_secret_key:
            raise RuntimeError('minio credentials are required for source_document_storage_mode=minio')

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        bucket = settings.source_document_bucket
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        client.put_object(
            bucket,
            key_name,
            BytesIO(content),
            length=len(content),
            content_type=(content_type or 'application/octet-stream'),
        )
        return f's3://{bucket}/{key_name}'

    base_dir = Path(settings.source_document_storage_dir)
    target = base_dir / document_id / f'source{guessed_ext}'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target.as_uri()


def ingest_source_document(
    source_url: str,
    submitted_by: str,
    settings: Settings,
    store: RunStore,
    session_id: str | None = None,
    expected_title: str | None = None,
) -> SourceDocumentRecord:
    now = datetime.now(timezone.utc)
    document_id = uuid4().hex
    try:
        content, content_type = fetch_source_document_bytes(
            source_url,
            timeout=settings.source_document_fetch_timeout_seconds,
            max_bytes=settings.source_document_max_bytes,
            max_redirects=settings.source_document_max_redirects,
            allowed_hosts=settings.source_document_allowed_hosts,
        )
        fetched_title = guess_document_title(source_url)
        media_type = (content_type or '').split(';', 1)[0].strip().lower()
        if media_type in {'text/html', 'application/xhtml+xml'} or source_url.lower().endswith(('.html', '.htm', '/')):
            fetched_title = sanitize_document_title(extract_html_title(content), fetched_title)
        text_excerpt = extract_text_excerpt(content, content_type, source_url)
        metadata = extract_document_metadata(
            source_url=source_url,
            guessed_title=fetched_title,
            text_excerpt=text_excerpt,
        )
        fetched_title = sanitize_document_title(str(metadata.get('title') or fetched_title), fetched_title)
        validation_status, validation_notes = validate_document_identity(
            expected_title=expected_title,
            fetched_title=fetched_title,
            text_excerpt=text_excerpt,
        )
        storage_uri = persist_source_document_bytes(
            document_id=document_id,
            source_url=source_url,
            content=content,
            content_type=content_type,
            settings=settings,
        )
        record = SourceDocumentRecord(
            document_id=document_id,
            created_at=now,
            updated_at=now,
            status='fetched',
            source_url=source_url,
            submitted_by=submitted_by,
            storage_uri=storage_uri,
            content_type=content_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            title=fetched_title,
            text_excerpt=text_excerpt,
            authors=list(metadata.get('authors') or []),
            abstract_excerpt=metadata.get('abstract_excerpt'),
            method_hints=list(metadata.get('method_hints') or []),
            dataset_hints=list(metadata.get('dataset_hints') or []),
            loss_hints=list(metadata.get('loss_hints') or []),
            architecture_hints=list(metadata.get('architecture_hints') or []),
            baseline_hints=list(metadata.get('baseline_hints') or []),
            metric_hints=list(metadata.get('metric_hints') or []),
            domain_task_hints=list(metadata.get('domain_task_hints') or []),
            python_library_hints=list(metadata.get('python_library_hints') or []),
            expected_title=expected_title,
            validation_status=validation_status,
            validation_notes=validation_notes,
            session_id=session_id,
        )
    except Exception as exc:
        record = SourceDocumentRecord(
            document_id=document_id,
            created_at=now,
            updated_at=now,
            status='fetch-failed',
            source_url=source_url,
            submitted_by=submitted_by,
            fetch_error=str(exc),
            title=guess_document_title(source_url),
            expected_title=expected_title,
            session_id=session_id,
        )
    store.save_source_document(record)
    return record


def register_source_document_routes(app: FastAPI, *, store: RunStore) -> None:
    @app.get('/source-documents', response_model=list[SourceDocumentRecord])
    def list_source_documents() -> list[SourceDocumentRecord]:
        return store.list_source_documents()

    @app.get('/source-documents/latest', response_model=SourceDocumentRecord)
    def get_latest_source_document() -> SourceDocumentRecord:
        record = store.get_latest_source_document()
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='source document not found')
        return record

    @app.get('/source-documents/{document_id}', response_model=SourceDocumentRecord)
    def get_source_document(document_id: str) -> SourceDocumentRecord:
        record = store.get_source_document(document_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='source document not found')
        return record
