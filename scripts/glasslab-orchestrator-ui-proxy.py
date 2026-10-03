#!/usr/bin/env python3
"""Loopback-only reverse proxy that injects the orchestrator operator header.

Decision D2 for issue #592.  The operator-gated research-orchestrator read API
and its Server-Sent-Events stream require the header

    X-Glasslab-Operator-Token: <operator token>

on every request (see ``services/research-orchestrator/app/main.py``
``require_operator``).  A browser cannot attach that header to a top-level
navigation or to an ``EventSource`` connection, and putting the token in page
JavaScript would move the operator credential into the browser.  This proxy
runs on the workstation, keeps the token in process memory only, and adds the
header to every upstream request, so the token never enters the browser.

Prerequisite: the orchestrator is reached through the provisioner's persistent
port-forward, mapped onto the workstation by SSH, for example

    ssh -L 18080:127.0.0.1:18080 glasslab-provisioner

The default upstream is therefore ``http://127.0.0.1:18080``.

Security properties
-------------------
* **Loopback only.**  The listen address and the upstream must both resolve to
  a loopback address.  There is deliberately no flag to bind ``0.0.0.0`` or to
  reach a non-loopback upstream: the whole point of the proxy is that the
  credential stays on one machine.
* **Fail closed.**  The process refuses to start when the token environment
  variable is unset or empty, rather than starting without authentication.
* **Environment only.**  The token is read from the environment and is never
  accepted as a command-line argument (process arguments are visible to other
  local users through ``ps``).  It is never logged, echoed, or written to disk.
* **Concurrent + unbuffered.**  Requests are served by ``ThreadingHTTPServer``
  so a long-lived SSE stream does not block concurrent XHR requests.  Upstream
  response bodies are relayed unbuffered (chunked when the upstream sets no
  ``Content-Length``) so events arrive incrementally.
* **Read-only by default + anti-CSRF.**  Only the allowlisted HTTP methods
  (default ``GET,HEAD``) are forwarded.  The write surface is opt-in: run with
  ``--allow-methods GET,HEAD,POST`` to let the browser submit the ``/ui``
  launch and gate forms.  Two guards protect the token-injecting path either
  way: the request ``Host`` must name a loopback host for the configured
  listen port (defeats a DNS-rebinding name), and every state-changing method
  must be same-origin per Fetch Metadata ``Sec-Fetch-Site`` (falling back to a
  loopback ``Origin``/``Referer``), so a cross-site form the operator's browser
  is induced into submitting is refused before the token is injected.

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import ipaddress
import os
import re
import socket
import sys
import urllib.parse
from dataclasses import dataclass, field

DEFAULT_LISTEN = '127.0.0.1:19090'
DEFAULT_UPSTREAM = 'http://127.0.0.1:18080'
DEFAULT_TOKEN_ENV = 'GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN'
DEFAULT_HEADER = 'X-Glasslab-Operator-Token'
DEFAULT_ALLOW_METHODS = 'GET,HEAD'

STREAM_BLOCK = 65536
MAX_REQUEST_BODY = 64 * 1024 * 1024

# Methods that can change upstream state.  For these the request must prove it
# came from this loopback listener (Origin, falling back to Referer), so a
# cross-site form the operator's browser is induced into submitting cannot ride
# the token-injecting path.  GET/HEAD are excluded: browsers attach no Origin
# to a simple navigation and they change nothing upstream.
STATE_CHANGING_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})

# RFC 7230 hop-by-hop headers, dropped in both directions.
HOP_BY_HOP = frozenset(
    {
        'connection',
        'keep-alive',
        'proxy-authenticate',
        'proxy-authorization',
        'te',
        'trailer',
        'transfer-encoding',
        'upgrade',
    }
)
# Request-only: the body has already been consumed, so Expect is meaningless.
REQUEST_DROP = HOP_BY_HOP | {'host', 'expect'}

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class ProxyConfigError(ValueError):
    """Raised when the proxy cannot be configured safely."""


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """Validated, loopback-only proxy configuration.

    ``token`` is excluded from ``repr`` so an accidental traceback or log line
    can never surface the credential.
    """

    listen_host: str
    listen_port: int
    upstream_scheme: str
    upstream_host: str
    upstream_port: int
    header_name: str
    allow_methods: frozenset[str]
    token: str = field(repr=False)


def _split_host_port(value: str, *, what: str) -> tuple[str, int]:
    """Parse ``host:port`` (IPv6 in brackets) into a host and a port."""
    text = value.strip()
    if not text:
        raise ProxyConfigError(f'{what} must not be empty')
    if text.startswith('['):
        end = text.find(']')
        if end == -1:
            raise ProxyConfigError(f'{what} has an unterminated IPv6 bracket: {value!r}')
        host = text[1:end]
        rest = text[end + 1 :]
        if rest == '':
            raise ProxyConfigError(f'{what} is missing a port: {value!r}')
        if not rest.startswith(':'):
            raise ProxyConfigError(f'{what} is malformed: {value!r}')
        port_text = rest[1:]
    elif ':' in text:
        host, _, port_text = text.rpartition(':')
    else:
        raise ProxyConfigError(f'{what} is missing a port: {value!r}')
    if not host:
        raise ProxyConfigError(f'{what} is missing a host: {value!r}')
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ProxyConfigError(f'{what} has a non-numeric port: {value!r}') from exc
    if not 0 <= port <= 65535:
        raise ProxyConfigError(f'{what} port out of range: {port}')
    return host, port


def assert_loopback(host: str, *, what: str) -> None:
    """Refuse any host that is not entirely comprised of loopback addresses."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ProxyConfigError(f'{what} {host!r} cannot be resolved: {exc}') from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(
        ipaddress.ip_address(address).is_loopback for address in addresses
    ):
        raise ProxyConfigError(
            f'{what} must be a loopback address; refusing {host!r}'
        )


def parse_upstream(value: str) -> tuple[str, str, int]:
    """Validate a loopback upstream origin and return (scheme, host, port)."""
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ('http', 'https'):
        raise ProxyConfigError(
            f'upstream scheme must be http or https, not {parts.scheme!r}'
        )
    if parts.hostname is None:
        raise ProxyConfigError(f'upstream is missing a host: {value!r}')
    if parts.path not in ('', '/') or parts.query or parts.fragment:
        raise ProxyConfigError(
            f'upstream must be a bare origin without path/query/fragment: {value!r}'
        )
    default_port = 443 if parts.scheme == 'https' else 80
    port = parts.port if parts.port is not None else default_port
    assert_loopback(parts.hostname, what='upstream host')
    return parts.scheme, parts.hostname, port


def validate_header_name(name: str) -> str:
    """Reject header names that are empty, invalid, or hop-by-hop."""
    if not _HEADER_NAME_RE.match(name):
        raise ProxyConfigError(f'invalid header name: {name!r}')
    if name.lower() in HOP_BY_HOP:
        raise ProxyConfigError(f'header {name!r} is hop-by-hop and cannot be injected')
    return name


def parse_allow_methods(value: str) -> frozenset[str]:
    """Parse a comma-separated HTTP method allowlist, uppercased and validated."""
    methods: set[str] = set()
    for raw in value.split(','):
        name = raw.strip().upper()
        if not name:
            raise ProxyConfigError('allow-methods contains an empty entry')
        if not _HEADER_NAME_RE.match(name):
            raise ProxyConfigError(f'invalid HTTP method name: {raw!r}')
        methods.add(name)
    if not methods:
        raise ProxyConfigError('allow-methods must list at least one method')
    return frozenset(methods)


def _parse_port(text: str) -> int | None:
    if text == '':
        return None
    try:
        port = int(text)
    except ValueError:
        return None
    return port if 0 <= port <= 65535 else None


def split_host_header(value: str | None) -> tuple[str | None, int | None]:
    """Split a Host header into (host, port), handling IPv6 bracket syntax."""
    if value is None:
        return None, None
    text = value.strip()
    if not text:
        return None, None
    if text.startswith('['):
        end = text.find(']')
        if end == -1:
            return None, None
        host = text[1:end]
        rest = text[end + 1 :]
        if rest == '':
            port = None
        elif rest.startswith(':'):
            port = _parse_port(rest[1:])
        else:
            return None, None
    elif text.count(':') == 1:
        host, _, port_text = text.partition(':')
        port = _parse_port(port_text)
    elif ':' in text:
        host, port = text, None
    else:
        host, port = text, None
    return (host or None), port


_LOOPBACK_HOST_NAMES = frozenset({'127.0.0.1', 'localhost', '::1', '0:0:0:0:0:0:0:1'})


def host_header_allowed(
    value: str | None,
    config: ProxyConfig,
    *,
    listen_port: int | None = None,
) -> bool:
    """Accept only a loopback Host name bound to the listen port.

    This is the DNS-rebinding guard: a browser resolves a rebinding name to
    127.0.0.1 but still sends that attacker-controlled Host, which never
    matches this allowlist.  ``listen_port`` overrides the configured port with
    the port the socket actually bound (relevant only for ``:0`` test binds).
    """
    host, port = split_host_header(value)
    effective_port = config.listen_port if listen_port is None else listen_port
    if host is None or port != effective_port:
        return False
    normalized = host.lower()
    return (
        normalized in _LOOPBACK_HOST_NAMES
        or normalized == config.listen_host.lower()
    )


def origin_header_allowed(
    origin: str | None,
    referer: str | None,
    config: ProxyConfig,
    *,
    listen_port: int | None = None,
    sec_fetch_site: str | None = None,
) -> bool:
    """Accept a state-changing request only if it is same-origin.

    CSRF guard: the operator's browser can be induced by a page it visits to
    submit a form to the loopback proxy, whose ``Host`` still names the
    loopback listener and so passes :func:`host_header_allowed` while the proxy
    injects the operator token.  Fetch Metadata ``Sec-Fetch-Site`` is the
    primary signal: the browser sets it on every request, page script cannot
    forge it, and -- unlike ``Origin`` -- it is not reduced to the opaque
    ``null`` by the page's ``Referrer-Policy: no-referrer`` (which the
    orchestrator sends, and which nulls a legitimate same-origin form POST's
    ``Origin``).  Only ``same-origin`` (and user-initiated ``none``) pass.
    When the header is absent (older browsers, direct clients) fall back to
    ``Origin``/``Referer`` naming a loopback host on the bound listen port.
    """
    site = (sec_fetch_site or '').strip().lower()
    if site:
        return site in ('same-origin', 'none')
    raw = (origin or referer or '').strip()
    if not raw:
        return False
    parsed = urllib.parse.urlsplit(raw.split()[0])
    if parsed.scheme not in ('http', 'https') or parsed.hostname is None:
        return False
    port = parsed.port
    if port is None:
        port = 80 if parsed.scheme == 'http' else 443
    effective_port = config.listen_port if listen_port is None else listen_port
    if port != effective_port:
        return False
    normalized = parsed.hostname.lower()
    return (
        normalized in _LOOPBACK_HOST_NAMES
        or normalized == config.listen_host.lower()
    )


def resolve_token(token_env: str, env: dict[str, str] | None = None) -> str:
    """Read the operator token from the environment, failing closed if absent."""
    if not token_env:
        raise ProxyConfigError('token environment variable name must not be empty')
    environ = os.environ if env is None else env
    token = environ.get(token_env)
    if token is None or token == '':
        raise ProxyConfigError(
            f'operator token environment variable {token_env!r} is unset or empty; '
            'refusing to start'
        )
    if '\r' in token or '\n' in token:
        raise ProxyConfigError('operator token contains a newline; refusing to start')
    return token


def parse_config(
    argv: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> ProxyConfig:
    """Parse and validate command-line arguments and the operator token."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    listen_host, listen_port = _split_host_port(args.listen, what='listen address')
    assert_loopback(listen_host, what='listen address')
    scheme, upstream_host, upstream_port = parse_upstream(args.upstream)
    validate_header_name(args.header)
    allow_methods = parse_allow_methods(args.allow_methods)
    token = resolve_token(args.token_env, env)
    return ProxyConfig(
        listen_host=listen_host,
        listen_port=listen_port,
        upstream_scheme=scheme,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        header_name=args.header,
        allow_methods=allow_methods,
        token=token,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='glasslab-orchestrator-ui-proxy',
        allow_abbrev=False,
        description=(
            'Loopback-only reverse proxy that injects the orchestrator operator '
            'header so a browser can read the operator-gated orchestrator and its '
            'SSE stream without holding the token.'
        ),
        epilog=(
            'The orchestrator is reached through the provisioner port-forward; map '
            'it onto this workstation first:\n'
            '  ssh -L 18080:127.0.0.1:18080 glasslab-provisioner\n\n'
            f'The token is read from ${DEFAULT_TOKEN_ENV} and is deliberately not '
            'accepted as a command-line argument.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--listen', default=DEFAULT_LISTEN, help='loopback host:port to bind')
    parser.add_argument('--upstream', default=DEFAULT_UPSTREAM, help='loopback orchestrator origin')
    parser.add_argument('--token-env', default=DEFAULT_TOKEN_ENV, help='env var holding the operator token')
    parser.add_argument('--header', default=DEFAULT_HEADER, help='header to inject on every request')
    parser.add_argument(
        '--allow-methods',
        default=DEFAULT_ALLOW_METHODS,
        help='comma-separated HTTP methods to forward (read-only v1 defaults to GET,HEAD)',
    )
    return parser


def _connection_tokens(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(',') if item.strip()}


def build_upstream_headers(
    client_headers: list[tuple[str, str]],
    config: ProxyConfig,
) -> dict[str, str]:
    """Copy client headers onto the upstream request, injecting the token.

    Hop-by-hop headers (and any header named by ``Connection``) are dropped.
    The client's own ``Host`` is dropped so ``http.client`` sets it.  Any
    client-supplied copy of the configured operator header is removed before
    the trusted token is set, so the client can never smuggle its own value.
    """
    connection_tokens: set[str] = set()
    for name, value in client_headers:
        if name.lower() == 'connection':
            connection_tokens |= _connection_tokens(value)
    drop = REQUEST_DROP | connection_tokens
    target = config.header_name.lower()

    headers: dict[str, str] = {}
    for name, value in client_headers:
        lowered = name.lower()
        if lowered in drop or lowered == target:
            continue
        headers[name] = value
    headers[config.header_name] = config.token
    return headers


class _BodyReadError(Exception):
    """Raised when an inbound request body cannot be consumed."""


def _read_exact(stream, size: int) -> bytes:
    remaining = size
    blocks: list[bytes] = []
    while remaining > 0:
        block = stream.read(min(remaining, STREAM_BLOCK))
        if not block:
            raise _BodyReadError('client closed the request body early')
        blocks.append(block)
        remaining -= len(block)
    return b''.join(blocks)


class ProxyRequestHandler(http.server.BaseHTTPRequestHandler):
    """Relay one request to the orchestrator with the operator header added."""

    protocol_version = 'HTTP/1.1'
    server_version = 'GlasslabOrchestratorUiProxy/1.0'
    sys_version = ''
    # SSE and long XHRs are common; do not let the socket layer coalesce flushes.
    disable_nagle_algorithm = True

    # Request logging is intentionally silent: the default access log includes
    # the request line, and signed link tokens can appear in query strings.
    # No header (and therefore no operator token) is ever logged.
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def log_error(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:
        self._proxy()

    def do_HEAD(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def do_OPTIONS(self) -> None:
        self._proxy()

    def __getattr__(self, name: str):
        # Any other HTTP method (TRACE, extension methods) is routed here so the
        # allowlist decides: disallowed methods get a 405 and never reach the
        # upstream.  Only ``do_*`` dispatch names are synthesized.
        if name.startswith('do_'):
            return self._proxy
        raise AttributeError(name)

    def _reject(
        self,
        status: int,
        message: str,
        extra: list[tuple[str, str]] | None = None,
    ) -> None:
        body = message.encode('utf-8')
        self.send_response(status)
        for name, value in extra or []:
            self.send_header(name, value)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _proxy(self) -> None:
        config = self.server.config  # type: ignore[attr-defined]
        method = self.command.upper()
        if method not in config.allow_methods:
            allow = ', '.join(sorted(config.allow_methods))
            self._reject(405, 'method not allowed', [('Allow', allow)])
            return
        if not host_header_allowed(
            self.headers.get('Host'),
            config,
            listen_port=self.server.server_address[1],  # type: ignore[attr-defined]
        ):
            self._reject(421, 'invalid Host header')
            return
        if method in STATE_CHANGING_METHODS and not origin_header_allowed(
            self.headers.get('Origin'),
            self.headers.get('Referer'),
            config,
            listen_port=self.server.server_address[1],  # type: ignore[attr-defined]
            sec_fetch_site=self.headers.get('Sec-Fetch-Site'),
        ):
            self._reject(403, 'cross-origin state-changing request rejected')
            return

        try:
            body = self._read_request_body()
        except _BodyReadError as exc:
            self.send_error(400, str(exc))
            return

        headers = build_upstream_headers(list(self.headers.items()), config)
        if body is not None:
            headers['Content-Length'] = str(len(body))

        connection = self._open_upstream(config)
        try:
            connection.request(method, self.path, body=body, headers=headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            self.send_error(502, f'upstream request failed: {exc.__class__.__name__}')
            return

        try:
            self._relay_response(response, body_allowed=method != 'HEAD')
        finally:
            response.close()
            connection.close()

    def _open_upstream(self, config: ProxyConfig) -> http.client.HTTPConnection:
        if config.upstream_scheme == 'https':
            return http.client.HTTPSConnection(
                config.upstream_host, config.upstream_port, timeout=30
            )
        return http.client.HTTPConnection(
            config.upstream_host, config.upstream_port, timeout=30
        )

    def _read_request_body(self) -> bytes | None:
        encoding = (self.headers.get('Transfer-Encoding') or '').lower()
        if 'chunked' in encoding:
            return self._read_chunked_body()
        content_length = self.headers.get('Content-Length')
        if content_length is None:
            return None
        try:
            size = int(content_length)
        except ValueError as exc:
            raise _BodyReadError('invalid Content-Length') from exc
        if size < 0 or size > MAX_REQUEST_BODY:
            raise _BodyReadError('request body too large')
        if size == 0:
            return b''
        try:
            return _read_exact(self.rfile, size)
        except _BodyReadError:
            raise

    def _read_chunked_body(self) -> bytes:
        blocks: list[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(STREAM_BLOCK)
            if not line:
                raise _BodyReadError('truncated chunked request body')
            try:
                size = int(line.split(b';', 1)[0].strip(), 16)
            except ValueError as exc:
                raise _BodyReadError('invalid chunk size') from exc
            if size == 0:
                while True:
                    trailer = self.rfile.readline(STREAM_BLOCK)
                    if trailer in (b'', b'\r\n', b'\n'):
                        break
                break
            total += size
            if total > MAX_REQUEST_BODY:
                raise _BodyReadError('request body too large')
            blocks.append(_read_exact(self.rfile, size))
            self.rfile.read(2)  # trailing CRLF
        return b''.join(blocks)

    def _relay_response(self, response: http.client.HTTPResponse, *, body_allowed: bool) -> None:
        connection_tokens = _connection_tokens(response.getheader('Connection'))
        content_length = response.getheader('Content-Length')
        chunked = body_allowed and content_length is None

        self.send_response_only(response.status, response.reason)
        for name, value in response.getheaders():
            lowered = name.lower()
            if lowered in HOP_BY_HOP or lowered in connection_tokens:
                continue
            if lowered == 'content-length' and not body_allowed:
                continue
            self.send_header(name, value)
        if chunked:
            self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()

        if not body_allowed:
            return
        try:
            while True:
                block = response.read1(STREAM_BLOCK)
                if not block:
                    break
                self._write_block(block, chunked)
            if chunked:
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            # The client or upstream went away mid-stream; the connection dies.
            self.close_connection = True

    def _write_block(self, block: bytes, chunked: bool) -> None:
        if chunked:
            self.wfile.write(b'%X\r\n' % len(block))
            self.wfile.write(block)
            self.wfile.write(b'\r\n')
        else:
            self.wfile.write(block)
        self.wfile.flush()


class ProxyServer(http.server.ThreadingHTTPServer):
    """Threaded HTTP server carrying the validated proxy configuration."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: ProxyConfig, handler_cls: type[ProxyRequestHandler] = ProxyRequestHandler):
        self.config = config
        super().__init__((config.listen_host, config.listen_port), handler_cls)


def build_server(config: ProxyConfig) -> ProxyServer:
    """Bind (but do not serve) the proxy server for ``config``."""
    return ProxyServer(config)


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    try:
        config = parse_config(argv, env)
    except ProxyConfigError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2

    server = build_server(config)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    print(
        f'listening on {bound_host}:{bound_port} -> '
        f'{config.upstream_scheme}://{config.upstream_host}:{config.upstream_port} '
        f'(injecting {config.header_name})',
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
