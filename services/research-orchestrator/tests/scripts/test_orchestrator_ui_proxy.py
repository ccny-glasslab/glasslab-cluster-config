"""Hermetic tests for the operator-header-injecting loopback UI proxy.

The proxy (:file:`scripts/glasslab-orchestrator-ui-proxy.py`) is a top-level
standalone script, not an importable package module, so it is loaded here by
path with ``importlib.util.spec_from_file_location``.  Every test runs against
a local fake upstream on an ephemeral loopback port: no real orchestrator and
no non-loopback network is involved.

Covered contracts:
1. the operator header is injected on the upstream request (every request);
2. a streamed/chunked upstream body arrives in multiple flushes, not one blob;
3. the operator token never appears in the proxy's stdout/stderr or repr;
4. a non-loopback listen address or upstream is refused;
5. startup is refused when the token environment variable is unset or empty;
6. only allowlisted HTTP methods reach the upstream (default GET,HEAD);
7. a rebinding ``Host`` for another name is refused before the upstream.
8. a state-changing method (``POST``) is refused unless ``Origin``/``Referer``
   names a loopback origin for the listen port.
"""

from __future__ import annotations

import contextlib
import http.client
import http.server
import importlib.util
import io
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
PROXY_PATH = REPO_ROOT / 'scripts' / 'glasslab-orchestrator-ui-proxy.py'

TOKEN_ENV = 'GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN'
SECRET = 'operator-secret-token-value-1234'


def _load_proxy_module():
    spec = importlib.util.spec_from_file_location('glasslab_orchestrator_ui_proxy', PROXY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules, so the
    # dynamically loaded module must be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROXY = _load_proxy_module()


def _start_proxy(upstream_port: int, extra_args: tuple[str, ...] = ()) -> tuple[object, int]:
    config = PROXY.parse_config(
        [
            '--listen',
            '127.0.0.1:0',
            '--upstream',
            f'http://127.0.0.1:{upstream_port}',
            '--token-env',
            TOKEN_ENV,
            *extra_args,
        ],
        env={TOKEN_ENV: SECRET},
    )
    server = PROXY.build_server(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


class _FakeUpstreamHandler(http.server.BaseHTTPRequestHandler):
    """Minimal upstream: echoes request headers, or streams SSE-like events."""

    protocol_version = 'HTTP/1.1'
    stream_release = threading.Event()
    requests: list[tuple[str, str]] = []

    @classmethod
    def reset_requests(cls) -> None:
        cls.requests = []

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:
        self._record()
        self._dispatch()

    def do_POST(self) -> None:
        self._record()
        self._dispatch()

    def _record(self) -> None:
        _FakeUpstreamHandler.requests.append((self.command, self.path))

    def _dispatch(self) -> None:
        if self.path.startswith('/stream'):
            self._stream()
        else:
            self._echo()

    def _echo(self) -> None:
        received = {name.lower(): value for name, value in self.headers.items()}
        body = json.dumps({'headers': received}).encode('utf-8')
        self.send_response_only(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _stream(self) -> None:
        # Close-delimited (no Content-Length, no chunked) so the proxy must
        # relay it incrementally and re-frame it as chunked.
        self.send_response_only(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(b'data: one\n\n')
        self.wfile.flush()
        _FakeUpstreamHandler.stream_release.wait(5)
        self.wfile.write(b'data: two\n\n')
        self.wfile.flush()
        self.close_connection = True


class ProxyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _FakeUpstreamHandler)
        cls.upstream_port = cls.upstream.server_address[1]
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.proxy, cls.proxy_port = _start_proxy(cls.upstream_port)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self) -> None:
        _FakeUpstreamHandler.stream_release.clear()
        _FakeUpstreamHandler.reset_requests()

    def _request(
        self,
        method: str,
        path: str,
        *,
        port: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection('127.0.0.1', port or self.proxy_port, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def _get(self, path: str, *, port: int | None = None) -> tuple[int, bytes]:
        status, body, _ = self._request('GET', path, port=port)
        return status, body

    def test_operator_header_is_injected_on_every_request(self) -> None:
        for path in ('/echo', '/echo?run_id=abc'):
            status, body = self._get(path)
            self.assertEqual(200, status)
            headers = json.loads(body)['headers']
            self.assertEqual(SECRET, headers[PROXY.DEFAULT_HEADER.lower()])

    def test_client_supplied_operator_header_is_replaced(self) -> None:
        connection = http.client.HTTPConnection('127.0.0.1', self.proxy_port, timeout=5)
        try:
            connection.request(
                'GET',
                '/echo',
                headers={PROXY.DEFAULT_HEADER: 'attacker-supplied'},
            )
            response = connection.getresponse()
            headers = json.loads(response.read())['headers']
        finally:
            connection.close()
        self.assertEqual(SECRET, headers[PROXY.DEFAULT_HEADER.lower()])

    def test_streamed_upstream_body_is_relayed_in_multiple_flushes(self) -> None:
        sock = socket.create_connection(('127.0.0.1', self.proxy_port), timeout=5)
        try:
            sock.sendall(
                f'GET /stream HTTP/1.1\r\n'
                f'Host: 127.0.0.1:{self.proxy_port}\r\n'
                f'Accept: text/event-stream\r\n'
                f'Connection: close\r\n\r\n'.encode('ascii')
            )
            chunks: list[bytes] = []
            deadline = time.monotonic() + 5
            while b'data: one' not in b''.join(chunks):
                block = sock.recv(65536)
                if not block or time.monotonic() > deadline:
                    break
                chunks.append(block)
            self.assertIn(b'data: one', b''.join(chunks))

            # With the first event already delivered, release the second one.
            # If the proxy buffered the whole body, this second read could not
            # have arrived before the upstream finished.
            _FakeUpstreamHandler.stream_release.set()
            while True:
                try:
                    block = sock.recv(65536)
                except socket.timeout:
                    break
                if not block:
                    break
                chunks.append(block)
        finally:
            sock.close()

        payload = b''.join(chunks)
        self.assertIn(b'data: two', payload)
        non_empty = [block for block in chunks if block]
        self.assertGreater(len(non_empty), 1, 'body arrived as a single buffered blob')

    def test_token_never_appears_in_stdout_stderr_or_repr(self) -> None:
        config = PROXY.parse_config([], env={TOKEN_ENV: SECRET})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status, _ = self._get('/echo')
            self.assertEqual(200, status)
            self.assertNotIn(SECRET, repr(config))
        self.assertNotIn(SECRET, out.getvalue())
        self.assertNotIn(SECRET, err.getvalue())

    def test_disallowed_method_is_rejected_without_calling_upstream(self) -> None:
        status, _, headers = self._request('POST', '/echo')
        self.assertEqual(405, status)
        self.assertEqual('GET, HEAD', headers.get('Allow'))
        self.assertEqual([], _FakeUpstreamHandler.requests)

    def test_unknown_method_is_rejected_without_calling_upstream(self) -> None:
        status, _, headers = self._request('TRACE', '/echo')
        self.assertEqual(405, status)
        self.assertEqual('GET, HEAD', headers.get('Allow'))
        self.assertEqual([], _FakeUpstreamHandler.requests)

    def test_extra_allowed_method_reaches_upstream(self) -> None:
        server, port = _start_proxy(self.upstream_port, ('--allow-methods', 'GET,HEAD,POST'))
        try:
            status, _, _ = self._request(
                'POST',
                '/echo',
                port=port,
                headers={'Origin': f'http://127.0.0.1:{port}'},
            )
            self.assertEqual(200, status)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual([('POST', '/echo')], _FakeUpstreamHandler.requests)

    def test_state_changing_request_without_origin_is_rejected(self) -> None:
        server, port = _start_proxy(self.upstream_port, ('--allow-methods', 'GET,HEAD,POST'))
        try:
            status, _, _ = self._request('POST', '/echo', port=port)
            self.assertEqual(403, status)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual([], _FakeUpstreamHandler.requests)

    def test_cross_site_origin_is_rejected(self) -> None:
        server, port = _start_proxy(self.upstream_port, ('--allow-methods', 'GET,HEAD,POST'))
        try:
            for origin in ('https://evil.example', 'http://127.0.0.1:1', 'null'):
                status, _, _ = self._request(
                    'POST',
                    '/echo',
                    port=port,
                    headers={'Origin': origin},
                )
                self.assertEqual(403, status, origin)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual([], _FakeUpstreamHandler.requests)

    def test_loopback_origin_and_referer_are_accepted(self) -> None:
        server, port = _start_proxy(self.upstream_port, ('--allow-methods', 'GET,HEAD,POST'))
        try:
            for headers in (
                {'Origin': f'http://127.0.0.1:{port}'},
                {'Origin': f'http://localhost:{port}'},
                {'Origin': f'http://[::1]:{port}'},
                {'Referer': f'http://127.0.0.1:{port}/ui/'},
            ):
                _FakeUpstreamHandler.reset_requests()
                status, _, _ = self._request('POST', '/echo', port=port, headers=headers)
                self.assertEqual(200, status, headers)
                self.assertEqual([('POST', '/echo')], _FakeUpstreamHandler.requests)
        finally:
            server.shutdown()
            server.server_close()

    def test_get_is_not_subject_to_origin_check(self) -> None:
        status, _ = self._get('/echo')
        self.assertEqual(200, status)
        self.assertEqual([('GET', '/echo')], _FakeUpstreamHandler.requests)

    def test_rebinding_host_is_rejected_without_calling_upstream(self) -> None:
        status, _, _ = self._request(
            'GET',
            '/echo',
            headers={'Host': f'evil.example:{self.proxy_port}'},
        )
        self.assertEqual(421, status)
        self.assertEqual([], _FakeUpstreamHandler.requests)

    def test_loopback_host_names_are_accepted(self) -> None:
        for host in ('127.0.0.1', 'localhost', '[::1]'):
            _FakeUpstreamHandler.reset_requests()
            status, _, _ = self._request(
                'GET',
                '/echo',
                headers={'Host': f'{host}:{self.proxy_port}'},
            )
            self.assertEqual(200, status, host)
            self.assertEqual([('GET', '/echo')], _FakeUpstreamHandler.requests)

    def test_wrong_port_in_host_is_rejected(self) -> None:
        status, _, _ = self._request(
            'GET',
            '/echo',
            headers={'Host': f'127.0.0.1:{self.proxy_port + 1}'},
        )
        self.assertEqual(421, status)
        self.assertEqual([], _FakeUpstreamHandler.requests)


class ProxyConfigTests(unittest.TestCase):
    def _config(self, extra: list[str]) -> object:
        return PROXY.parse_config(extra, env={TOKEN_ENV: SECRET})

    def test_defaults_match_the_orchestrator_contract(self) -> None:
        config = self._config([])
        self.assertEqual('X-Glasslab-Operator-Token', PROXY.DEFAULT_HEADER)
        self.assertEqual('X-Glasslab-Operator-Token', config.header_name)
        self.assertEqual('127.0.0.1', config.listen_host)
        self.assertEqual('127.0.0.1', config.upstream_host)
        self.assertEqual(frozenset({'GET', 'HEAD'}), config.allow_methods)

    def test_allow_methods_option_is_parsed_and_normalized(self) -> None:
        config = self._config(['--allow-methods', 'get, Head ,POST'])
        self.assertEqual(frozenset({'GET', 'HEAD', 'POST'}), config.allow_methods)

    def test_invalid_method_name_is_refused(self) -> None:
        with self.assertRaises(PROXY.ProxyConfigError):
            self._config(['--allow-methods', 'GET,BO AD'])

    def test_abbreviated_flag_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self._config(['--tok', TOKEN_ENV])

    def test_host_header_allowlist_and_ipv6_parsing(self) -> None:
        config = self._config(['--listen', '127.0.0.1:19090'])
        self.assertTrue(PROXY.host_header_allowed('127.0.0.1:19090', config))
        self.assertTrue(PROXY.host_header_allowed('localhost:19090', config))
        self.assertTrue(PROXY.host_header_allowed('[::1]:19090', config))
        self.assertFalse(PROXY.host_header_allowed('127.0.0.1:19091', config))
        self.assertFalse(PROXY.host_header_allowed('127.0.0.1', config))
        self.assertFalse(PROXY.host_header_allowed('evil.example:19090', config))
        self.assertFalse(PROXY.host_header_allowed(None, config))

    def test_origin_header_allowlist_and_referer_fallback(self) -> None:
        config = self._config(['--listen', '127.0.0.1:19090'])
        self.assertTrue(PROXY.origin_header_allowed('http://127.0.0.1:19090', None, config))
        self.assertTrue(PROXY.origin_header_allowed('http://localhost:19090', None, config))
        self.assertTrue(PROXY.origin_header_allowed('http://[::1]:19090', None, config))
        self.assertTrue(
            PROXY.origin_header_allowed(None, 'http://127.0.0.1:19090/ui/', config)
        )
        self.assertFalse(PROXY.origin_header_allowed('https://evil.example', None, config))
        self.assertFalse(PROXY.origin_header_allowed('http://127.0.0.1:19091', None, config))
        self.assertFalse(PROXY.origin_header_allowed('null', None, config))
        self.assertFalse(PROXY.origin_header_allowed(None, None, config))

    def test_non_loopback_listen_is_refused(self) -> None:
        with self.assertRaises(PROXY.ProxyConfigError):
            self._config(['--listen', '0.0.0.0:19090'])

    def test_non_loopback_upstream_is_refused(self) -> None:
        with self.assertRaises(PROXY.ProxyConfigError):
            self._config(['--upstream', 'http://192.168.1.44:18080'])

    def test_missing_token_env_is_refused(self) -> None:
        with self.assertRaises(PROXY.ProxyConfigError):
            PROXY.parse_config(['--listen', '127.0.0.1:0'], env={})

    def test_empty_token_env_is_refused(self) -> None:
        with self.assertRaises(PROXY.ProxyConfigError):
            PROXY.parse_config(['--listen', '127.0.0.1:0'], env={TOKEN_ENV: ''})


if __name__ == '__main__':
    unittest.main()
