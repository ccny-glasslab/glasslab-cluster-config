"""CI-safe in-process wire stub for workflow-api's generic-run request contract.

Issue #491 was a sender/receiver wire defect: the orchestrator's
``WorkflowApiClusterExecutor`` sent a top-level ``resources`` field that
workflow-api's ``GenericExperimentRunRequest`` forbids (``extra='forbid'``), so
every live ``POST /experiments/runs`` returned 422 while the fake rehearsal path
advanced. This helper serves that request model in-process so the sender body is
parsed by the receiver's real pydantic model and rejected with the real FastAPI
422 shape -- with no cluster, no socket, and no workflow-api runtime install.

The stub deliberately does **not** import workflow-api's ASGI ``main`` module.
That module's import graph reaches ``app/job_submission.py``, which imports the
Kubernetes ``urllib3`` transport at module import time. The orchestrator CI lane
installs only ``services/research-orchestrator/requirements.txt`` (plus pytest
and httpx), so importing the full app raised ``ModuleNotFoundError: urllib3``
during collection and failed the whole lane. The full ASGI app is unavailable in
this dependency set; the request contract is not.

Instead the REAL request models are loaded from
``services/workflow-api/app/schemas.py`` (pydantic-only) with the same
file-location bootstrap the CI-green ``test_workflow_api_contract.py`` uses. A
minimal FastAPI app serves them, so the receiver model, its ``extra='forbid'``
config, and FastAPI's default 422 body shape are all real. Only the production
route handler, auth middleware, and run store are omitted, because none of those
are importable here.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Final

import anyio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx

from app.cluster import WorkflowApiClusterExecutor

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_API_SCHEMAS_SOURCE = (
    _REPO_ROOT / 'services' / 'workflow-api' / 'app' / 'schemas.py'
)

if str(_REPO_ROOT) not in sys.path:
    # workflow-api's schemas.py imports the shared services.common.schemas
    # package, whose import root is the repository root.
    sys.path.insert(0, str(_REPO_ROOT))

BASE_URL: Final = 'http://workflow-api.test'
CALLER_NAME: Final = 'research-orchestrator'
ORCHESTRATOR_TOKEN: Final = 'wire-stub-orchestrator-token'

_GENERIC_RUN_PATH: Final = '/experiments/runs'


def _load_workflow_api_schemas() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        'workflow_api_wire_stub_schemas',
        _WORKFLOW_API_SCHEMAS_SOURCE,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f'cannot load workflow-api schemas from {_WORKFLOW_API_SCHEMAS_SOURCE}'
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


WORKFLOW_API_SCHEMAS = _load_workflow_api_schemas()
GENERIC_RUN_REQUEST = WORKFLOW_API_SCHEMAS.GenericExperimentRunRequest


class InProcessAsgiTransport(httpx.BaseTransport):
    """Synchronous httpx transport that drives an ASGI app without sockets.

    ``WorkflowApiClusterExecutor`` uses a synchronous ``httpx.Client``; the app
    is an ASGI callable. This bridge runs the ASGI request to completion on its
    own event loop and returns a buffered ``httpx.Response``, so no file
    descriptor or network name is ever touched.
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
    """A minimal FastAPI app, its in-process transport, and the parsed bodies.

    ``accepted`` holds the ``GenericExperimentRunRequest`` instances the real
    receiver model accepted, in arrival order, so tests can assert on what the
    receiver parsed without a durable run store.
    """

    app: FastAPI
    transport: InProcessAsgiTransport
    accepted: list[Any]

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=BASE_URL, transport=self.transport)


def build_wire_stub() -> WireStub:
    """Build a fresh in-process app that serves the real request model."""
    accepted: list[Any] = []
    app = FastAPI()

    @app.post(_GENERIC_RUN_PATH, status_code=201)
    def create_generic_experiment_run(
        request: GENERIC_RUN_REQUEST,  # type: ignore[valid-type]
    ) -> JSONResponse:
        # The parameter annotation IS the real receiver model, so FastAPI
        # validates the raw body with extra='forbid' and emits the same 422
        # detail shape the live service produces.
        accepted.append(request)
        return JSONResponse(
            status_code=201,
            content={
                'run_id': f'wire-stub-run-{len(accepted)}',
                'status': {'status': 'accepted'},
                'job_submission': {},
            },
        )

    return WireStub(
        app=app,
        transport=InProcessAsgiTransport(app),
        accepted=accepted,
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
