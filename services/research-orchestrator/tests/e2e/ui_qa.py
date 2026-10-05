"""Live-server fixtures for the ``/ui`` corpus chat + PDF viewer browser QA.

Starts a real ``uvicorn app.main:app`` against a throwaway SQLite corpus
(seeded by ``scripts/qa/seed_ui_corpus.py``) and TWO loopback
operator-token-injecting UI proxies
(``scripts/glasslab-orchestrator-ui-proxy.py``): the page listener, which
forwards every path, and the #620 viewer listener, which is scoped to
``/ui/pdf/``. Both share the same upstream and token; the app is configured
with ``GLASSLAB_ORCHESTRATOR_UI_ORIGIN``/``GLASSLAB_ORCHESTRATOR_UI_PDF_VIEWER_ORIGIN``
so the page frames the viewer from the second origin. Everything is torn
down and a teardown receipt is written.

The browser test runs under the Playwright venv; the app server runs under an
interpreter that can import the service dependencies. ``GLASSLAB_QA_APP_PYTHON``
overrides the auto-detection (the Playwright venv itself is tried first, then
``python3``).

Scratch state lives under ``<tempdir>/glasslab-ui-qa/run`` and artifacts under
``<tempdir>/glasslab-ui-qa/artifacts`` (both overridable through the
``GLASSLAB_QA_SCRATCH`` and ``GLASSLAB_QA_ARTIFACTS`` environment variables);
nothing is written to the repository.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVICE_ROOT.parents[1]
PROXY_PATH = REPO_ROOT / 'scripts' / 'glasslab-orchestrator-ui-proxy.py'
SEED_SCRIPT = SERVICE_ROOT / 'scripts' / 'qa' / 'seed_ui_corpus.py'

OPERATOR_TOKEN = 'qa-operator-token-618-619'
TOKEN_ENV = 'GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN'

DEFAULT_ARTIFACTS_DIR = Path(tempfile.gettempdir()) / 'glasslab-ui-qa' / 'artifacts'
DEFAULT_SCRATCH_ROOT = Path(tempfile.gettempdir()) / 'glasslab-ui-qa' / 'run'

ARTIFACTS_DIR = Path(
    os.environ.get('GLASSLAB_QA_ARTIFACTS', str(DEFAULT_ARTIFACTS_DIR))
)
SCRATCH_ROOT = Path(
    os.environ.get('GLASSLAB_QA_SCRATCH', str(DEFAULT_SCRATCH_ROOT))
)

_ARTIFACT_FILES = (
    'action-log.jsonl',
    'action-log.md',
    'console-log.jsonl',
    'seed-manifest.json',
    'server-env.json',
    'teardown.txt',
)


@dataclasses.dataclass
class UiQaEnvironment:
    """Everything the browser test needs to reach the live QA stack."""

    app_origin: str
    proxy_origin: str
    viewer_origin: str
    token: str
    manifest: dict
    artifacts_dir: Path
    scratch_dir: Path
    app_python: str
    service_root: Path
    app_process: subprocess.Popen
    proxy_server: object
    proxy_thread: threading.Thread
    viewer_proxy_server: object
    viewer_proxy_thread: threading.Thread
    app_port: int
    proxy_port: int
    viewer_proxy_port: int


def _app_python() -> str:
    """Return an interpreter that can import the service dependencies."""
    override = os.environ.get('GLASSLAB_QA_APP_PYTHON')
    if override:
        return override
    candidates = [sys.executable, shutil.which('python3') or 'python3']
    for candidate in candidates:
        probe = subprocess.run(
            [candidate, '-c', 'import fastapi, uvicorn, pymupdf'],
            capture_output=True,
        )
        if probe.returncode == 0:
            return candidate
    raise RuntimeError(
        'no interpreter with fastapi/uvicorn/pymupdf found; set '
        'GLASSLAB_QA_APP_PYTHON'
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _port_closed(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(('127.0.0.1', port)) != 0


def _wait_for_http(
    url: str,
    timeout: float = 90.0,
    process: subprocess.Popen | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(
                f'server process exited early with code {process.returncode}'
            )
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - retried until the deadline
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f'{url} did not become ready: {last_error!r}')


def _load_proxy_module():
    spec = importlib.util.spec_from_file_location(
        'glasslab_orchestrator_ui_proxy_qa', PROXY_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules, so the
    # dynamically loaded module must be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _server_env(
    scratch: Path,
    db_path: Path,
    raw_root: Path,
    *,
    ui_origin: str,
    viewer_origin: str,
) -> dict[str, str]:
    """The app environment: sqlite store, seeded DB, auth on, scratch roots.

    ``ui_origin``/``viewer_origin`` are the two loopback proxy origins. The
    app emits an absolute viewer iframe URL on the second origin and admits
    the first in the viewer's ``frame-ancestors`` (#620).
    """
    return {
        **os.environ,
        'PYTHONPATH': str(SERVICE_ROOT),
        'GLASSLAB_ORCHESTRATOR_STORE_BACKEND': 'sqlite',
        'GLASSLAB_ORCHESTRATOR_DATABASE_PATH': str(db_path),
        'GLASSLAB_ORCHESTRATOR_REQUIRE_OPERATOR_AUTH': 'true',
        'GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN': OPERATOR_TOKEN,
        'GLASSLAB_ORCHESTRATOR_UI_ORIGIN': ui_origin,
        'GLASSLAB_ORCHESTRATOR_UI_PDF_VIEWER_ORIGIN': viewer_origin,
        'GLASSLAB_ORCHESTRATOR_CORPUS_RAG_RAW_ROOT': str(raw_root),
        'GLASSLAB_ORCHESTRATOR_CLUSTER_EXECUTION_MODE': 'fake',
        'GLASSLAB_ORCHESTRATOR_UI_CHAT_ENABLED': 'true',
        'GLASSLAB_ORCHESTRATOR_UI_PDF_ENABLED': 'true',
        'GLASSLAB_ORCHESTRATOR_RAG_LLM_ENABLED': 'false',
        'GLASSLAB_ORCHESTRATOR_DISCORD_ENABLED': 'false',
        'GLASSLAB_ORCHESTRATOR_WORKSPACE_ROOT': str(scratch / 'runs'),
        'GLASSLAB_ORCHESTRATOR_ARTIFACT_ROOT': str(scratch / 'artifacts'),
        'GLASSLAB_ORCHESTRATOR_PROMOTED_CONTRACT_ROOT': str(
            scratch / 'trusted-contracts'
        ),
        'GLASSLAB_ORCHESTRATOR_SEALED_CONTRACT_CANDIDATE_ROOT': str(
            scratch / 'contract-candidates'
        ),
        'GLASSLAB_ORCHESTRATOR_TRUSTED_CONTRACT_CATALOG_PATH': str(
            scratch / 'trusted-contracts' / 'catalog.json'
        ),
        'GLASSLAB_ORCHESTRATOR_SHARED_MOUNT_ROOT': str(scratch),
        'GLASSLAB_ORCHESTRATOR_TASK_BUNDLE_ROOT': str(scratch / 'task-bundles'),
        'GLASSLAB_ORCHESTRATOR_TASK_ASSET_ROOT': str(scratch / 'task-assets'),
        'GLASSLAB_ORCHESTRATOR_DATASET_UPLOAD_ROOT': str(
            scratch / 'dataset-uploads'
        ),
        'GLASSLAB_ORCHESTRATOR_BENCHMARK_DATASET_CATALOG_PATH': str(
            scratch / 'datasets' / 'catalog.json'
        ),
        'GLASSLAB_ORCHESTRATOR_KNOWLEDGE_ROOT': str(scratch / 'knowledge'),
        'GLASSLAB_ORCHESTRATOR_APPROVED_REPO_PATH': str(scratch / 'repo'),
    }


_ACTIVE_ENVIRONMENT: UiQaEnvironment | None = None


@pytest.fixture(scope='session')
def ui_qa() -> UiQaEnvironment:
    """Seed the corpus, start the app + both proxies, and tear them down."""
    global _ACTIVE_ENVIRONMENT
    if _ACTIVE_ENVIRONMENT is not None:
        # A fixture imported into a test module registers once per importing
        # module, so pytest builds one session instance per test module. A
        # second instance would wipe the first module's action log and start
        # a duplicate stack; reuse the live one instead.
        yield _ACTIVE_ENVIRONMENT
        return
    artifacts_dir = ARTIFACTS_DIR
    scratch = SCRATCH_ROOT
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    for name in _ARTIFACT_FILES:
        path = artifacts_dir / name
        if path.exists():
            path.unlink()

    app_python = _app_python()
    raw_root = scratch / 'rag' / 'raw'
    db_path = scratch / 'orchestrator.db'
    manifest_path = scratch / 'seed-manifest.json'
    seed = subprocess.run(
        [
            app_python,
            str(SEED_SCRIPT),
            '--db',
            str(db_path),
            '--raw-root',
            str(raw_root),
            '--run-root',
            str(scratch),
            '--manifest',
            str(manifest_path),
        ],
        cwd=str(SERVICE_ROOT),
        env={**os.environ, 'PYTHONPATH': str(SERVICE_ROOT)},
        capture_output=True,
        text=True,
    )
    if seed.returncode != 0:
        raise RuntimeError(
            f'seed_ui_corpus.py failed ({seed.returncode}):\n'
            f'{seed.stdout}\n{seed.stderr}'
        )
    manifest = json.loads(manifest_path.read_text())
    (artifacts_dir / 'seed-manifest.json').write_text(
        json.dumps(manifest, indent=2) + '\n'
    )

    app_port = _free_port()
    app_origin = f'http://127.0.0.1:{app_port}'

    # Both proxies are bound (but not yet serving) before the app starts, so
    # their origins can be handed to the app through the environment: the
    # page is framed from ``ui_origin`` and the #620 viewer iframe from the
    # path-scoped ``ui_pdf_viewer_origin``.
    proxy_module = _load_proxy_module()
    proxy_config = proxy_module.parse_config(
        [
            '--listen',
            '127.0.0.1:0',
            '--upstream',
            app_origin,
            '--token-env',
            TOKEN_ENV,
            # The chat turn, run launch, and gate forms are POST; the proxy
            # guards every state-changing request with Fetch Metadata.
            '--allow-methods',
            'GET,HEAD,POST',
        ],
        env={TOKEN_ENV: OPERATOR_TOKEN},
    )
    proxy_server = proxy_module.build_server(proxy_config)
    proxy_port = int(proxy_server.server_address[1])
    proxy_origin = f'http://127.0.0.1:{proxy_port}'

    # The viewer listener shares the upstream + token but forwards only the
    # viewer paths, so a script running on the viewer origin cannot reach the
    # operator read API (#620).
    viewer_proxy_config = proxy_module.parse_config(
        [
            '--listen',
            '127.0.0.1:0',
            '--upstream',
            app_origin,
            '--token-env',
            TOKEN_ENV,
            '--allow-methods',
            'GET,HEAD',
            '--allow-paths',
            '/ui/pdf/',
        ],
        env={TOKEN_ENV: OPERATOR_TOKEN},
    )
    viewer_proxy_server = proxy_module.build_server(viewer_proxy_config)
    viewer_proxy_port = int(viewer_proxy_server.server_address[1])
    viewer_origin = f'http://127.0.0.1:{viewer_proxy_port}'

    env = _server_env(
        scratch,
        db_path,
        raw_root,
        ui_origin=proxy_origin,
        viewer_origin=viewer_origin,
    )
    (artifacts_dir / 'server-env.json').write_text(
        json.dumps(
            {
                key: ('<redacted>' if key == TOKEN_ENV else value)
                for key, value in sorted(env.items())
                if key.startswith('GLASSLAB_ORCHESTRATOR_')
            },
            indent=2,
        )
        + '\n'
    )
    log_path = scratch / 'uvicorn.log'
    log_file = log_path.open('wb')
    app_process = subprocess.Popen(
        [
            app_python,
            '-m',
            'uvicorn',
            'app.main:app',
            '--host',
            '127.0.0.1',
            '--port',
            str(app_port),
            '--log-level',
            'warning',
        ],
        cwd=str(SERVICE_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_http(app_origin + '/health', process=app_process)
    except Exception:
        app_process.terminate()
        try:
            app_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app_process.kill()
        log_file.close()
        proxy_server.server_close()
        viewer_proxy_server.server_close()
        raise RuntimeError(
            f'app server failed to start; log:\n{log_path.read_text()}'
        )

    proxy_thread = threading.Thread(
        target=proxy_server.serve_forever, daemon=True
    )
    proxy_thread.start()
    viewer_proxy_thread = threading.Thread(
        target=viewer_proxy_server.serve_forever, daemon=True
    )
    viewer_proxy_thread.start()

    environment = UiQaEnvironment(
        app_origin=app_origin,
        proxy_origin=proxy_origin,
        viewer_origin=viewer_origin,
        token=OPERATOR_TOKEN,
        manifest=manifest,
        artifacts_dir=artifacts_dir,
        scratch_dir=scratch,
        app_python=app_python,
        service_root=SERVICE_ROOT,
        app_process=app_process,
        proxy_server=proxy_server,
        proxy_thread=proxy_thread,
        viewer_proxy_server=viewer_proxy_server,
        viewer_proxy_thread=viewer_proxy_thread,
        app_port=app_port,
        proxy_port=proxy_port,
        viewer_proxy_port=viewer_proxy_port,
    )
    _ACTIVE_ENVIRONMENT = environment
    try:
        yield environment
    finally:
        _ACTIVE_ENVIRONMENT = None
        proxy_server.shutdown()
        proxy_server.server_close()
        proxy_thread.join(timeout=5)
        viewer_proxy_server.shutdown()
        viewer_proxy_server.server_close()
        viewer_proxy_thread.join(timeout=5)
        app_process.terminate()
        try:
            app_process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app_process.kill()
            app_process.wait(timeout=10)
        log_file.close()
        receipt = [
            f'app_python={app_python}',
            f'app_pid={app_process.pid} app_port={app_port} '
            f'app_exit={app_process.poll()}',
            f'proxy_port={proxy_port} '
            f'proxy_thread_alive={proxy_thread.is_alive()}',
            f'viewer_proxy_port={viewer_proxy_port} '
            f'viewer_proxy_thread_alive={viewer_proxy_thread.is_alive()}',
            f'app_port_closed={_port_closed(app_port)}',
            f'proxy_port_closed={_port_closed(proxy_port)}',
            f'viewer_proxy_port_closed={_port_closed(viewer_proxy_port)}',
            f'uvicorn_log={log_path}',
        ]
        (artifacts_dir / 'teardown.txt').write_text('\n'.join(receipt) + '\n')
        print('\n[ui_qa teardown]\n' + '\n'.join(receipt))
