#!/usr/bin/env python3
"""Serializing guard in front of one mlx_lm.server (OpenAI-compatible).

The M4 Max cannot serve multiple large-context agent turns concurrently:
parallel KV-cache allocations OOM the GPU and kill the mlx_lm process (seen
live 2026-09-07: '[METAL] Command buffer execution failed: Insufficient
Memory' under 4 concurrent assistant sequences). The orchestrator serializes
turns within one run, but concurrent runs and stale agent sessions still
stack server load.

This guard accepts OpenAI-compatible requests on --port and forwards them to
the backing mlx_lm.server on --upstream one at a time. Completions queue in
FIFO order; /v1/models passes through concurrently. The queue is bounded
(--max-queue); excess requests get HTTP 503 with Retry-After so the
orchestrator's bounded retry treats them as transient.

Stdlib only (http.server + urllib) so it runs on any Mac or container.
Run on each Mac:
    python3 model_guard.py --port 52417 --upstream http://127.0.0.1:52416/v1
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Full, Queue

RETRY_AFTER_SECONDS = 5
MAX_BODY_BYTES = 64 * 1024 * 1024


class SerializingCompletions:
    """Serializes chat completion bodies to the upstream model server."""

    class FutureResult:
        def __init__(self) -> None:
            self.done = threading.Event()
            self.status = 500
            self.body = b''
            self.headers: dict[str, str] = {}

    def __init__(self, upstream: str, max_queue: int) -> None:
        self.upstream = upstream.rstrip('/')
        self.max_queue = max_queue
        self._queue: Queue[tuple[bytes, dict[str, str], "SerializingCompletions.FutureResult"]] = Queue(maxsize=max_queue)
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def submit(self, body: bytes, headers: dict[str, str]):
        if self._queue.full():
            return None
        future = self.FutureResult()
        try:
            self._queue.put_nowait((body, headers, future))
        except Full:
            return None
        return future

    def _run(self) -> None:
        while True:
            try:
                body, headers, future = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                request = urllib.request.Request(
                    self.upstream + '/chat/completions',
                    data=body,
                    headers=headers,
                    method='POST',
                )
                with urllib.request.urlopen(request, timeout=3600) as response:
                    future.status = response.status
                    future.body = response.read()
                    future.headers = dict(response.headers)
            except urllib.error.HTTPError as exc:
                future.status = exc.code
                future.body = exc.read()
                future.headers = dict(exc.headers)
            except Exception as exc:
                future.status = 500
                future.body = json.dumps({'error': {'message': str(exc)}}).encode()
            finally:
                future.done.set()


def _forward(upstream: str, path: str):
    request = urllib.request.Request(
        upstream + path, method='GET'
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return (
                response.status,
                response.read(),
                response.headers.get('Content-Type', 'application/json'),
            )
    except urllib.error.HTTPError as exc:
        return (exc.code, exc.read(), 'application/json')


class Handler(BaseHTTPRequestHandler):
    server_version = 'ModelGuard/1.0'

    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == '/v1/models':
            try:
                status, body, ctype = _forward(self.server.upstream, '/models')
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self._json(502, {'error': {'message': str(exc)}})
            return
        self._json(404, {'error': {'message': f'not found: {self.path}'}})

    def do_POST(self) -> None:
        if self.path != '/v1/chat/completions':
            self._json(404, {'error': {'message': f'not found: {self.path}'}})
            return
        length = int(self.headers.get('Content-Length', 0))
        if length > MAX_BODY_BYTES:
            self._json(413, {'error': {'message': 'request body too large'}})
            return
        body = self.rfile.read(length)
        future = self.server.serializer.submit(
            body, {'Content-Type': 'application/json'}
        )
        if future is None:
            self._json(
                503,
                {
                    'error': {
                        'message': 'model server is busy; retry after %ds' % RETRY_AFTER_SECONDS,
                        'type': 'server_busy',
                    }
                },
            )
            return
        future.done.wait(timeout=3600)
        self.send_response(future.status)
        for key, value in future.headers.items():
            if key.lower() not in {'content-length', 'transfer-encoding'}:
                self.send_header(key, value)
        self.send_header('Content-Length', str(len(future.body)))
        self.end_headers()
        self.wfile.write(future.body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=52417)
    parser.add_argument('--upstream', default='http://127.0.0.1:52416/v1')
    parser.add_argument('--max-queue', type=int, default=4)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    server.upstream = args.upstream
    server.serializer = SerializingCompletions(args.upstream, args.max_queue)
    print(
        json.dumps(
            {
                'guard': 'listening',
                'port': args.port,
                'upstream': args.upstream,
                'max_queue': args.max_queue,
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())