"""Loopback process broker for the opencode sidecar container (issue #597).

The orchestrator container owns the control-plane secrets; the agent runtime
must not share its UID or its mounts. This module is the sidecar's PID 1 and
the only thing that can fork ``opencode serve`` inside that container. It holds
no control-plane secret: it accepts a bounded spawn request over loopback,
validates the executable, argv shape, working directory, log path, and
environment keys, and returns the child pid. The orchestrator keeps owning port
reservation, the random server password, health polling, and shutdown; it only
delegates the ``exec`` to this broker.

The broker binds loopback only. A prompt-injected agent shares the sidecar UID
and can reach it, but the only capability it exposes is launching the same
``opencode serve`` binary the agent already drives, with a validated argv and a
secret-free environment, so it is not an escalation path.
"""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading
from typing import Any
from uuid import uuid4

from .process_permissions import enable_shared_group_write
from .runtime_env import BENIGN_RUNTIME_VARS, MODEL_AUTH_ENV_VARS

logger = logging.getLogger(__name__)

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 4200
DEFAULT_EXECUTABLE = '/usr/local/bin/opencode'
DEFAULT_WORKSPACE_ROOT = '/mnt/artifacts/research-orchestrator/runs'
_MAX_REQUEST_BYTES = 1024 * 1024
_TERMINATE_GRACE_SECONDS = 5.0

# The only environment keys the broker will pass to a spawned server. The
# orchestrator builds this environment through build_agent_environment, which
# already starts from an empty dict; the broker re-validates so a future caller
# cannot smuggle a control-plane secret into the agent container.
_ALLOWED_ENV_KEYS = frozenset(
    set(BENIGN_RUNTIME_VARS)
    | set(MODEL_AUTH_ENV_VARS)
    | {
        'XDG_CONFIG_HOME',
        'XDG_DATA_HOME',
        'XDG_CACHE_HOME',
        'XDG_STATE_HOME',
        'HOME',
        'OPENCODE_SERVER_USERNAME',
        'OPENCODE_SERVER_PASSWORD',
    }
)


class SidecarRequestError(ValueError):
    """A spawn request the broker refuses to execute."""


@dataclass(frozen=True)
class SidecarConfig:
    host: str
    port: int
    executable: str
    workspace_root: Path

    @classmethod
    def from_env(cls) -> SidecarConfig:
        return cls(
            host=os.environ.get('GLASSLAB_OPENCODE_SIDECAR_HOST', DEFAULT_HOST),
            port=int(
                os.environ.get('GLASSLAB_OPENCODE_SIDECAR_PORT', DEFAULT_PORT)
            ),
            executable=os.environ.get(
                'GLASSLAB_OPENCODE_SIDECAR_EXECUTABLE', DEFAULT_EXECUTABLE
            ),
            workspace_root=Path(
                os.environ.get(
                    'GLASSLAB_OPENCODE_SIDECAR_WORKSPACE_ROOT',
                    DEFAULT_WORKSPACE_ROOT,
                )
            ),
        )


@dataclass
class _Child:
    runtime_id: str
    process: subprocess.Popen[str]
    log_handle: Any


class ServerRegistry:
    """Tracks the opencode servers this broker has spawned."""

    def __init__(self, config: SidecarConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._children: dict[str, _Child] = {}

    def spawn(self, request: dict[str, Any]) -> dict[str, Any]:
        argv = self._validated_argv(request.get('argv'))
        cwd = self._validated_path(request.get('cwd'), 'cwd')
        log_path = self._validated_path(request.get('log_path'), 'log_path')
        environment = self._validated_env(request.get('env'))
        log_handle = log_path.open('a', encoding='utf-8')
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except Exception:
            log_handle.close()
            raise
        runtime_id = f'opencode-sidecar-{uuid4().hex[:12]}'
        with self._lock:
            self._children[runtime_id] = _Child(
                runtime_id=runtime_id,
                process=process,
                log_handle=log_handle,
            )
        return {'runtime_id': runtime_id, 'pid': process.pid}

    def status(self, runtime_id: str) -> dict[str, Any] | None:
        with self._lock:
            child = self._children.get(runtime_id)
        if child is None:
            return None
        returncode = child.process.poll()
        return {
            'runtime_id': runtime_id,
            'running': returncode is None,
            'returncode': returncode,
        }

    def terminate(self, runtime_id: str) -> dict[str, Any] | None:
        with self._lock:
            child = self._children.pop(runtime_id, None)
        if child is None:
            return None
        _terminate_process(child.process)
        child.log_handle.close()
        return {
            'runtime_id': runtime_id,
            'running': False,
            'returncode': child.process.returncode,
        }

    def terminate_all(self) -> None:
        with self._lock:
            children = list(self._children.values())
            self._children.clear()
        for child in children:
            _terminate_process(child.process)
            child.log_handle.close()

    def _validated_argv(self, raw: Any) -> list[str]:
        if not isinstance(raw, list) or not all(
            isinstance(item, str) for item in raw
        ):
            raise SidecarRequestError('argv must be a list of strings')
        if len(raw) != 6 or raw[0] != self._config.executable:
            raise SidecarRequestError(
                'argv must be exactly the configured opencode executable '
                'followed by serve --hostname 127.0.0.1 --port <port>'
            )
        if raw[1] != 'serve' or raw[2] != '--hostname' or raw[3] != '127.0.0.1':
            raise SidecarRequestError('only `opencode serve` on 127.0.0.1 is allowed')
        if raw[4] != '--port' or not raw[5].isdigit():
            raise SidecarRequestError('argv must carry a numeric --port')
        port = int(raw[5])
        if not 1024 <= port <= 65535:
            raise SidecarRequestError(f'port out of range: {port}')
        return list(raw)

    def _validated_path(self, raw: Any, field: str) -> Path:
        if not isinstance(raw, str) or not raw:
            raise SidecarRequestError(f'{field} must be a non-empty path')
        candidate = Path(raw).resolve()
        root = self._config.workspace_root.resolve()
        if not candidate.is_relative_to(root):
            raise SidecarRequestError(f'{field} is outside the workspace root')
        return candidate

    def _validated_env(self, raw: Any) -> dict[str, str]:
        if not isinstance(raw, dict):
            raise SidecarRequestError('env must be an object')
        environment: dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise SidecarRequestError('env keys and values must be strings')
            if key not in _ALLOWED_ENV_KEYS:
                raise SidecarRequestError(f'env key is not allowed: {key}')
            environment[key] = value
        return environment


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)


class _SidecarHandler(BaseHTTPRequestHandler):
    server_version = 'glasslab-opencode-sidecar/1.0'

    @property
    def registry(self) -> ServerRegistry:
        return self.server.registry  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug('%s - %s', self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == '/health':
            self._respond(200, {'status': 'ok'})
            return
        runtime_id = self._server_id(self.path)
        if runtime_id is None:
            self._respond(404, {'error': 'not found'})
            return
        status = self.registry.status(runtime_id)
        if status is None:
            self._respond(404, {'error': 'unknown runtime'})
            return
        self._respond(200, status)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == '/v1/servers':
            self._spawn()
            return
        runtime_id = self._server_id(self.path, suffix='/terminate')
        if runtime_id is None:
            self._respond(404, {'error': 'not found'})
            return
        result = self.registry.terminate(runtime_id)
        if result is None:
            self._respond(404, {'error': 'unknown runtime'})
            return
        self._respond(200, result)

    def _spawn(self) -> None:
        try:
            request = self._read_json()
            result = self.registry.spawn(request)
        except SidecarRequestError as exc:
            self._respond(400, {'error': str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            logger.exception('opencode sidecar spawn failed')
            self._respond(500, {'error': f'spawn failed: {exc}'})
            return
        self._respond(201, result)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get('Content-Length', '0'))
        if length <= 0 or length > _MAX_REQUEST_BYTES:
            raise SidecarRequestError('invalid request size')
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise SidecarRequestError('request body is not JSON') from exc
        if not isinstance(body, dict):
            raise SidecarRequestError('request body must be a JSON object')
        return body

    @staticmethod
    def _server_id(path: str, suffix: str = '') -> str | None:
        prefix = '/v1/servers/'
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        runtime_id = path[len(prefix) : len(path) - len(suffix) or None]
        return runtime_id or None

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SidecarServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: SidecarConfig) -> None:
        super().__init__((config.host, config.port), _SidecarHandler)
        self.registry = ServerRegistry(config)


def create_server(config: SidecarConfig) -> SidecarServer:
    return SidecarServer(config)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    enable_shared_group_write()
    config = SidecarConfig.from_env()
    server = create_server(config)
    logger.info(
        'opencode sidecar broker listening on %s:%s (workspace root %s)',
        config.host,
        config.port,
        config.workspace_root,
    )

    def _stop(signum: int, frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever()
    except SystemExit:
        pass
    finally:
        server.server_close()
        server.registry.terminate_all()


if __name__ == '__main__':
    main()