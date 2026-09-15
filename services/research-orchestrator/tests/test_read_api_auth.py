"""Read endpoints require the operator token; only health/readiness are public.

Regression coverage for issue #369: the research-orchestrator read API was
reachable without authentication through the documented contributor
port-forward, exposing unredacted run state. Every application GET route is
now gated by ``Depends(require_operator)`` except ``/health`` and ``/ready``,
which the deployment probes anonymously.
"""

from __future__ import annotations

import re

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest

from app.main import create_app

OPERATOR_TOKEN = 'test-operator-token'
AUTH_HEADERS = {'X-Glasslab-Operator-Token': OPERATOR_TOKEN}

# Every application GET path (concrete substitutes for path params). Mix of
# routes that query real state and routes that 404 on the substitute id: the
# auth dependency must run before any handler, so an unauthenticated request
# is 401 regardless of whether the record exists.
GATED_GET_PATHS = [
    '/task-bundles',
    '/task-bundles/missing-task',
    '/task-bundles/missing-task/preflight',
    '/datasets',
    '/datasets/catalog',
    '/datasets/missing-dataset',
    '/knowledge/sources',
    '/knowledge/packets/missing-packet',
    '/context-packets/missing-packet',
    '/runs',
    '/runs/missing-run',
    '/runs/missing-run/events',
    '/runs/missing-run/events/stream',
    '/runs/missing-run/artifacts',
    '/runs/missing-run/turns',
    '/runs/missing-run/context-packets',
    '/actions/missing-action',
    '/chat/missing-conversation',
]

_PATH_PARAM = re.compile(r'\{[^}]+\}')


def _secured_client(settings, engine) -> TestClient:
    secured = settings.model_copy(
        update={
            'require_operator_auth': True,
            'operator_api_token': OPERATOR_TOKEN,
        }
    )
    app = create_app(secured, engine=engine, start_watcher=False)
    return TestClient(app)


@pytest.fixture
def secured_client(orchestrator_bundle):
    settings, _, _, _, engine = orchestrator_bundle
    with _secured_client(settings, engine) as client:
        yield client


@pytest.mark.parametrize('path', GATED_GET_PATHS)
def test_read_paths_require_operator_token(secured_client, path) -> None:
    unauthenticated = secured_client.get(path)
    assert unauthenticated.status_code == 401, path

    authenticated = secured_client.get(path, headers=AUTH_HEADERS)
    assert authenticated.status_code != 401, path


def test_health_and_ready_stay_anonymous(secured_client) -> None:
    # The kubelet probes these without a token; they must never be gated.
    assert secured_client.get('/health').status_code == 200
    assert secured_client.get('/ready').status_code == 200


def test_every_get_route_is_gated_except_health_and_ready(
    secured_client,
) -> None:
    public_paths = {'/health', '/ready'}
    gated_templates = []
    for route in secured_client.app.routes:
        if not isinstance(route, APIRoute) or 'GET' not in route.methods:
            continue
        if route.path in public_paths:
            continue
        gated_templates.append(route.path)
        concrete = _PATH_PARAM.sub('missing', route.path)
        assert secured_client.get(concrete).status_code != 200, route.path

    # The guard above is vacuous if the route walk finds nothing; assert the
    # walk actually covered the read surface.
    assert '/runs' in gated_templates
    assert '/runs/{run_id}/events' in gated_templates
    assert '/actions/{action_id}' in gated_templates


def test_get_routes_expose_no_unauthenticated_docs_surface(
    secured_client,
) -> None:
    # The auto-generated OpenAPI/docs routes are plain unauthenticated GETs;
    # they are disabled rather than left outside the operator-token boundary.
    for path in ('/openapi.json', '/docs', '/redoc', '/docs/oauth2-redirect'):
        assert secured_client.get(path).status_code == 404, path
