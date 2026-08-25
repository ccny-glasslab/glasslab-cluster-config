from __future__ import annotations

from email.message import Message
import socket

import pytest

import app.source_documents as source_documents
from app.config import Settings
from app.persistence import InMemoryRunStore
from app.source_documents import SourceDocumentFetchError, fetch_source_document_bytes


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = 'text/html',
        status: int = 200,
        location: str | None = None,
    ):
        self._body = body
        self._offset = 0
        self.status = status
        self.headers = Message()
        self.headers['Content-Type'] = content_type
        if location is not None:
            self.headers['Location'] = location

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise AssertionError('fetcher must stream using bounded reads')
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        pass


def public_resolver(host: str, port: int, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]


def test_fetch_rejects_non_https_before_connecting() -> None:
    with pytest.raises(SourceDocumentFetchError, match='HTTPS'):
        fetch_source_document_bytes('http://example.org/paper')


def test_fetch_rejects_url_credentials_before_connecting() -> None:
    with pytest.raises(SourceDocumentFetchError, match='credentials'):
        fetch_source_document_bytes('https://user:secret@example.org/paper')


@pytest.mark.parametrize('address', ['127.0.0.1', '169.254.169.254', '10.96.0.1', '224.0.0.1', '::1', 'fc00::1'])
def test_fetch_rejects_non_public_resolved_addresses(address: str) -> None:
    family = socket.AF_INET6 if ':' in address else socket.AF_INET

    def resolver(host: str, port: int, **kwargs):
        return [(family, socket.SOCK_STREAM, 6, '', (address, port))]

    with pytest.raises(SourceDocumentFetchError, match='non-public'):
        fetch_source_document_bytes('https://example.org/paper', resolver=resolver)


def test_fetch_revalidates_redirect_destination() -> None:
    responses = [FakeResponse(b'', status=302, location='https://metadata.internal/latest')]

    def connection_factory(host: str, port: int, address: str, timeout: float):
        class Connection:
            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return responses.pop(0)

            def close(self):
                pass

        return Connection()

    def resolver(host: str, port: int, **kwargs):
        address = '169.254.169.254' if host == 'metadata.internal' else '93.184.216.34'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, port))]

    with pytest.raises(SourceDocumentFetchError, match='non-public'):
        fetch_source_document_bytes(
            'https://example.org/paper', resolver=resolver, connection_factory=connection_factory
        )


def test_fetch_enforces_redirect_ceiling() -> None:
    def connection_factory(host: str, port: int, address: str, timeout: float):
        class Connection:
            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return FakeResponse(b'', status=302, location='/again')

            def close(self):
                pass

        return Connection()

    with pytest.raises(SourceDocumentFetchError, match='redirect'):
        fetch_source_document_bytes(
            'https://example.org/paper', resolver=public_resolver,
            connection_factory=connection_factory, max_redirects=2,
        )


def test_fetch_streams_and_rejects_oversized_response() -> None:
    cleanup = {'response': False, 'connection': False}

    def connection_factory(host: str, port: int, address: str, timeout: float):
        class Response(FakeResponse):
            def close(self):
                cleanup['response'] = True

        class Connection:
            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return Response(b'x' * 17)

            def close(self):
                cleanup['connection'] = True

        return Connection()

    with pytest.raises(SourceDocumentFetchError, match='byte limit'):
        fetch_source_document_bytes(
            'https://example.org/paper', resolver=public_resolver,
            connection_factory=connection_factory, max_bytes=16, chunk_size=8,
        )
    assert cleanup == {'response': True, 'connection': True}


def test_fetch_rejects_slow_drip_that_exceeds_total_deadline() -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()

    def connection_factory(host: str, port: int, address: str, timeout: float):
        class Response(FakeResponse):
            def read(self, size: int = -1) -> bytes:
                clock.value += 0.6
                return super().read(size)

        class Connection:
            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return Response(b'abc')

            def close(self):
                pass

        return Connection()

    with pytest.raises(SourceDocumentFetchError, match='deadline'):
        fetch_source_document_bytes(
            'https://example.org/paper', timeout=1.0, chunk_size=1,
            resolver=public_resolver, connection_factory=connection_factory, monotonic=clock,
        )


def test_redirects_share_one_total_deadline() -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    calls = 0

    def connection_factory(host: str, port: int, address: str, timeout: float):
        nonlocal calls
        calls += 1

        class Connection:
            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                clock.value += 0.6
                return FakeResponse(b'', status=302, location=f'/redirect-{calls}')

            def close(self):
                pass

        return Connection()

    with pytest.raises(SourceDocumentFetchError, match='deadline'):
        fetch_source_document_bytes(
            'https://example.org/paper', timeout=1.0, max_redirects=4,
            resolver=public_resolver, connection_factory=connection_factory, monotonic=clock,
        )
    assert calls == 2


def test_fetch_rejects_ipv4_mapped_private_ipv6() -> None:
    def resolver(host: str, port: int, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::ffff:127.0.0.1', port))]

    with pytest.raises(SourceDocumentFetchError, match='non-public'):
        fetch_source_document_bytes('https://example.org/paper', resolver=resolver)


def test_fetch_rejects_unapproved_media_type() -> None:
    def connection_factory(host: str, port: int, address: str, timeout: float):
        class Connection:
            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return FakeResponse(b'PK', content_type='application/zip')

            def close(self):
                pass

        return Connection()

    with pytest.raises(SourceDocumentFetchError, match='media type'):
        fetch_source_document_bytes(
            'https://example.org/archive', resolver=public_resolver, connection_factory=connection_factory
        )


def test_fetch_returns_allowed_streamed_document() -> None:
    def connection_factory(host: str, port: int, address: str, timeout: float):
        class Connection:
            def request(self, method, path, headers):
                assert method == 'GET'
                assert path == '/paper?version=2'
                assert headers['Host'] == 'example.org'

            def getresponse(self):
                return FakeResponse(b'<title>Paper</title>')

            def close(self):
                pass

        return Connection()

    content, content_type = fetch_source_document_bytes(
        'https://example.org/paper?version=2', resolver=public_resolver,
        connection_factory=connection_factory, max_bytes=100, chunk_size=8,
    )

    assert content == b'<title>Paper</title>'
    assert content_type == 'text/html'


def test_ingest_applies_configured_fetch_policy(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_fetch(source_url: str, **kwargs):
        captured.update(kwargs)
        return b'<title>Paper</title>', 'text/html'

    monkeypatch.setattr(source_documents, 'fetch_source_document_bytes', fake_fetch)
    monkeypatch.setattr(source_documents, 'persist_source_document_bytes', lambda **kwargs: 'file:///source')
    settings = Settings(
        source_document_fetch_timeout_seconds=7,
        source_document_max_bytes=1234,
        source_document_max_redirects=2,
        source_document_allowed_hosts=('example.org',),
    )

    record = source_documents.ingest_source_document(
        'https://example.org/paper', 'tester', settings, InMemoryRunStore()
    )

    assert record.status == 'fetched'
    assert captured == {
        'timeout': 7.0,
        'max_bytes': 1234,
        'max_redirects': 2,
        'allowed_hosts': ('example.org',),
    }
