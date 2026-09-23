"""Reusable in-process wire stub for workflow-api's REAL FastAPI app.

The orchestrator's fake rehearsal path never sends an HTTP request, so it
cannot see wire-contract defects: issue #491 was a forbidden top-level
``resources`` field that made every live ``POST /experiments/runs`` return 422
while the fake path advanced. This helper serves the real ASGI application in
the test process so ``WorkflowApiClusterExecutor.submit`` exercises the real
request models (``extra='forbid'``), the real auth middleware, and the real
422 body shape -- with no cluster and no socket.

The workflow-api service also owns a top-level ``app`` package, which collides
with the orchestrator's own ``app`` package in one interpreter. The real
package is therefore loaded under the unique import name
``workflow_api_under_test`` via an explicit file-location spec, and no
workflow-api directory is added to ``sys.path`` (only the repository root, for
``services.common`` imports, which the package also adds itself).
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Final

import anyio
from fastapi import FastAPI
import httpx
from pydantic import SecretStr

from app.cluster import WorkflowApiClusterExecutor

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_API_ROOT = _REPO_ROOT / 'services' / 'workflow-api'
_WORKFLOW_API_PACKAGE = 'workflow_api_under_test'

BASE_URL: Final = 'http://workflow-api.test'
CALLER_NAME: Final = 'research-orchestrator'
ORCHESTRATOR_TOKEN: Final = 'wire-stub-orchestrator-token'

_CONTRACT_ID: Final = 'classification-metric-v1'
_CONTRACT_VERSION: Final = '1.0.0'
_CONTRACT_DIGEST: Final = 'a' * 64

# The workflow-api process resolves evaluation contracts from a trusted,
# digest-pinned catalog supplied by configuration. The stub supplies the same
# catalog for the contract the orchestrator's expanded specs reference, so a
# schema-valid submission reaches the handler and is accepted.
TRUSTED_EVALUATION_CONTRACTS: Final[dict[str, dict[str, str]]] = {
    f'{_CONTRACT_ID}@{_CONTRACT_VERSION}': {
        'contract_id': _CONTRACT_ID,
        'version': _CONTRACT_VERSION,
        'digest': _CONTRACT_DIGEST,
        'execution_wrapper': 'wrapper.py',
        'evaluation_entry_point': 'evaluate.py',
        'container_image_digest': f'example.invalid/evaluator@sha256:{"0" * 64}',
    },
}


class WorkflowApiWireStubError(RuntimeError):
    """The workflow-api source package could not be loaded into this process."""


def _load_workflow_api_package() -> ModuleType:
    existing = sys.modules.get(_WORKFLOW_API_PACKAGE)
    if existing is not None:
        return existing
    if str(_REPO_ROOT) not in sys.path:
        # services.common.schemas is imported by the workflow-api source; its
        # import root is the repository root.
        sys.path.insert(0, str(_REPO_ROOT))
    package_dir = _WORKFLOW_API_ROOT / 'app'
    spec = importlib.util.spec_from_file_location(
        _WORKFLOW_API_PACKAGE,
        package_dir / '__init__.py',
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise WorkflowApiWireStubError(
            f'cannot load workflow-api package from {package_dir}'
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_WORKFLOW_API_PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_WORKFLOW_API_PACKAGE_MODULE = _load_workflow_api_package()
_WORKFLOW_API_MAIN = importlib.import_module(f'{_WORKFLOW_API_PACKAGE}.main')
_WORKFLOW_API_CONFIG = importlib.import_module(f'{_WORKFLOW_API_PACKAGE}.config')
_WORKFLOW_API_AUTH = importlib.import_module(f'{_WORKFLOW_API_PACKAGE}.auth')
_WORKFLOW_API_PERSISTENCE = importlib.import_module(
    f'{_WORKFLOW_API_PACKAGE}.persistence'
)
_WORKFLOW_API_REGISTRY = importlib.import_module(f'{_WORKFLOW_API_PACKAGE}.registry')
_WORKFLOW_API_JOB_SUBMISSION = importlib.import_module(
    f'{_WORKFLOW_API_PACKAGE}.job_submission'
)

Settings = _WORKFLOW_API_CONFIG.Settings
CallerPolicy = _WORKFLOW_API_AUTH.CallerPolicy
InMemoryRunStore = _WORKFLOW_API_PERSISTENCE.InMemoryRunStore
WorkflowRegistry = _WORKFLOW_API_REGISTRY.WorkflowRegistry
NullJobSubmitter = _WORKFLOW_API_JOB_SUBMISSION.NullJobSubmitter
create_app = _WORKFLOW_API_MAIN.create_app
DEFAULT_CALLER_OPERATIONS = _WORKFLOW_API_CONFIG.DEFAULT_CALLER_OPERATIONS


class InProcessAsgiTransport(httpx.BaseTransport):
    """Synchronous httpx transport that drives an ASGI app without sockets.

    ``WorkflowApiClusterExecutor`` uses a synchronous ``httpx.Client``; the
    real app is an ASGI callable. This bridge runs the ASGI request to
    completion on its own event loop and returns a buffered ``httpx.Response``,
    so no file descriptor or network name is ever touched.
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return anyio.run(self._handle_request, request)

    async def _handle_request(self, request: httpx.Request) -> httpx.Response:
        asgi_transport = httpx.ASGITransport(app=self._app)
        response = await asgi_transport.handle_async_request(request)
        body = await response.aread()
        await response.aclose()
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=body,
            request=request,
        )


@dataclass(frozen=True, slots=True)
class WireStub:
    """A real workflow-api app plus the state and transport backing it."""

    app: FastAPI
    store: InMemoryRunStore
    settings: Settings
    transport: InProcessAsgiTransport

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=BASE_URL, transport=self.transport)


def build_wire_stub() -> WireStub:
    """Build a fresh in-process workflow-api app with an isolated memory store."""
    settings = Settings(
        _env_file=None,
        caller_policies=(
            CallerPolicy(
                name=CALLER_NAME,
                token=SecretStr(ORCHESTRATOR_TOKEN),
                allowed_operations=DEFAULT_CALLER_OPERATIONS[CALLER_NAME],
            ),
        ),
        job_submission_mode='null',
        evaluation_contracts=dict(TRUSTED_EVALUATION_CONTRACTS),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    submitter = NullJobSubmitter(namespace=settings.runner_namespace)
    app = create_app(
        settings=settings,
        registry=registry,
        store=store,
        submitter=submitter,
    )
    return WireStub(
        app=app,
        store=store,
        settings=settings,
        transport=InProcessAsgiTransport(app),
    )


def install_wire_transport(
    executor: WorkflowApiClusterExecutor,
    stub: WireStub,
) -> None:
    """Bind a real executor's HTTP client factory to the stub transport.

    Only the ``_client`` seam is replaced; ``submit``/``inspect``/``cancel``
    run their production code paths unchanged.
    """
    setattr(executor, '_client', stub.client)
