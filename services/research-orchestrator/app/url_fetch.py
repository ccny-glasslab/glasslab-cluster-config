"""Streaming fetch of public HTTPS resources with SSRF hardening.

Task-asset and dataset-URL ingestion share these mechanics once: only public
HTTPS targets (no credentials or custom port), every redirect hop re-validated,
the connected peer re-checked after connect (closing the DNS-rebinding TOCTOU
gap), the body streamed under a byte ceiling, and SHA-256 computed while
streaming. Failures carry a machine-readable ``kind`` so callers classify them
without leaking network detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import ipaddress
from pathlib import Path
import socket
from urllib.parse import unquote, urljoin, urlparse

import httpx


class UrlFetchErrorKind(StrEnum):
    """Stable classification of a public-URL fetch failure."""

    MALFORMED_URL = 'malformed_url'
    PRIVATE_TARGET = 'private_target'
    PEER_UNVERIFIABLE = 'peer_unverifiable'
    REDIRECT_REJECTED = 'redirect_rejected'
    SIZE_EXCEEDED = 'size_exceeded'
    EMPTY_BODY = 'empty_body'
    NETWORK_FAILURE = 'network_failure'
    CHECKSUM_MISMATCH = 'checksum_mismatch'


class UrlFetchError(ValueError):
    """A public-HTTPS fetch failed; ``kind`` classifies the failure."""

    def __init__(self, kind: UrlFetchErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class FetchedUrl:
    """Provenance captured while streaming one public HTTPS resource."""

    original_url: str
    final_url: str
    sha256: str
    size_bytes: int
    media_type: str | None
    filename: str | None
    retrieved_at: datetime


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _media_type(response: httpx.Response) -> str | None:
    value = response.headers.get('content-type')
    if value is None:
        return None
    return value.split(';', 1)[0].strip().lower() or None


def _filename(url: str) -> str | None:
    candidate = Path(unquote(urlparse(url).path)).name.strip()
    if not candidate or candidate in {'.', '..'} or len(candidate) > 255:
        return None
    return candidate


class PublicHttpsFetcher:
    """Stream a public HTTPS resource to a local path under strict limits."""

    def __init__(
        self,
        *,
        maximum_bytes: int,
        timeout_seconds: float = 300.0,
        connect_timeout_seconds: float = 15.0,
        max_retries: int = 2,
        max_redirects: int = 5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.maximum_bytes = maximum_bytes
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.max_retries = max_retries
        self.max_redirects = max_redirects
        self._transport = transport

    @staticmethod
    def validate_url(url: str) -> None:
        """Reject anything that is not a public HTTPS URL (pre-request)."""
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError as exc:
            raise UrlFetchError(
                UrlFetchErrorKind.MALFORMED_URL,
                'URL is malformed',
            ) from exc
        if (
            parsed.scheme != 'https'
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port not in {None, 443}
        ):
            raise UrlFetchError(
                UrlFetchErrorKind.MALFORMED_URL,
                'only public HTTPS URLs with no credentials or custom port '
                'are accepted',
            )
        try:
            addresses = socket.getaddrinfo(
                parsed.hostname,
                443,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise UrlFetchError(
                UrlFetchErrorKind.NETWORK_FAILURE,
                f'cannot resolve URL host: {parsed.hostname}',
            ) from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise UrlFetchError(
                    UrlFetchErrorKind.PRIVATE_TARGET,
                    f'URL host resolves to a non-public address: {ip}',
                )

    @staticmethod
    def revalidate_peer(response: httpx.Response) -> None:
        """Verify the connected peer is globally routable, before body reads."""
        stream = response.extensions.get('network_stream')
        if stream is None:
            raise UrlFetchError(
                UrlFetchErrorKind.PEER_UNVERIFIABLE,
                'connection peer address is not verifiable',
            )
        peername = stream.get_extra_info('server_addr')
        if not peername:
            raise UrlFetchError(
                UrlFetchErrorKind.PEER_UNVERIFIABLE,
                'connection peer address is not verifiable',
            )
        try:
            ip = ipaddress.ip_address(peername[0])
        except ValueError as exc:
            raise UrlFetchError(
                UrlFetchErrorKind.PEER_UNVERIFIABLE,
                'connection peer address is not verifiable',
            ) from exc
        if not ip.is_global:
            raise UrlFetchError(
                UrlFetchErrorKind.PRIVATE_TARGET,
                f'URL peer resolves to a non-public address: {ip}',
            )

    def download(
        self,
        url: str,
        destination: Path,
        *,
        expected_sha256: str | None = None,
    ) -> FetchedUrl:
        """Stream ``url`` into ``destination``, retrying transient failures."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._download_once(
                    url,
                    destination,
                    expected_sha256=expected_sha256,
                )
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise UrlFetchError(
                        UrlFetchErrorKind.NETWORK_FAILURE,
                        f'download failed: {exc}',
                    ) from exc
                last_error = str(exc)
                continue
            except httpx.HTTPError as exc:
                raise UrlFetchError(
                    UrlFetchErrorKind.NETWORK_FAILURE,
                    f'download failed: {exc}',
                ) from exc
        raise UrlFetchError(
            UrlFetchErrorKind.NETWORK_FAILURE,
            last_error or 'download failed',
        )

    def _download_once(
        self,
        url: str,
        destination: Path,
        *,
        expected_sha256: str | None,
    ) -> FetchedUrl:
        with httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(
                self.timeout_seconds,
                connect=self.connect_timeout_seconds,
            ),
            transport=self._transport,
        ) as client:
            current = url
            for _ in range(self.max_redirects + 1):
                self.validate_url(current)
                with client.stream('GET', current) as response:
                    self.revalidate_peer(response)
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get('location')
                        if not location:
                            raise UrlFetchError(
                                UrlFetchErrorKind.REDIRECT_REJECTED,
                                'redirect response is missing a Location header',
                            )
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    digest = sha256()
                    size = 0
                    with destination.open('wb') as output:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > self.maximum_bytes:
                                raise UrlFetchError(
                                    UrlFetchErrorKind.SIZE_EXCEEDED,
                                    'content exceeds the configured size limit '
                                    f'of {self.maximum_bytes} bytes',
                                )
                            digest.update(chunk)
                            output.write(chunk)
                    if size == 0:
                        raise UrlFetchError(
                            UrlFetchErrorKind.EMPTY_BODY,
                            'content is empty',
                        )
                    actual = digest.hexdigest()
                    if expected_sha256 and actual != expected_sha256:
                        raise UrlFetchError(
                            UrlFetchErrorKind.CHECKSUM_MISMATCH,
                            'content does not match expected_sha256',
                        )
                    return FetchedUrl(
                        original_url=url,
                        final_url=current,
                        sha256=actual,
                        size_bytes=size,
                        media_type=_media_type(response),
                        filename=_filename(current),
                        retrieved_at=datetime.now(timezone.utc),
                    )
            raise UrlFetchError(
                UrlFetchErrorKind.REDIRECT_REJECTED,
                'too many redirects',
            )
