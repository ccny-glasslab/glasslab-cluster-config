"""Authentication and authorization coverage for workflow-api mutations."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient


# Keep this module isolated from ``app.*`` settings caches used by other tests.
for module_name in list(sys.modules):
    if module_name == 'app' or module_name.startswith('app.'):
        del sys.modules[module_name]

from app.config import Settings
from app.job_submission import NullJobSubmitter
from app.main import create_app
from app.persistence import InMemoryRunStore
from app.registry import WorkflowRegistry


REPO_ROOT = Path(__file__).resolve().parents[3]
PROTECTED_METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE'})
ANONYMOUS_OPERATIONS = frozenset({'GET /healthz'})
def build_app():
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
    )
    return create_app(
        settings=settings,
        registry=WorkflowRegistry(settings.registry_dir),
        store=InMemoryRunStore(),
        submitter=NullJobSubmitter(namespace=settings.runner_namespace),
    )


def protected_routes() -> list[tuple[str, str]]:
    app = build_app()
    return [
        (method, route.path_format)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods & PROTECTED_METHODS)
        if f'{method} {route.path_format}' not in ANONYMOUS_OPERATIONS
    ]


def request_path(path_template: str) -> str:
    return re.sub(r'\{[^}]+\}', 'test-id', path_template)


PROTECTED_ROUTES = protected_routes()


@pytest.fixture(scope='module')
def client() -> TestClient:
    previous_value = os.environ.get('GLASSLAB_WORKFLOW_API_CALLER_POLICIES')
    os.environ['GLASSLAB_WORKFLOW_API_CALLER_POLICIES'] = json.dumps(
        [
            {
                'name': 'authorized-caller',
                'token': 'authorized-token',
                'allowed_operations': [
                    f'{method} {path_template}'
                    for method, path_template in PROTECTED_ROUTES
                ],
            },
            {
                'name': 'unauthorized-caller',
                'token': 'unauthorized-token',
                'allowed_operations': [],
            },
        ]
    )
    try:
        with TestClient(build_app()) as test_client:
            yield test_client
    finally:
        if previous_value is None:
            del os.environ['GLASSLAB_WORKFLOW_API_CALLER_POLICIES']
        else:
            os.environ['GLASSLAB_WORKFLOW_API_CALLER_POLICIES'] = previous_value


@pytest.mark.parametrize(
    ('method', 'path_template'),
    PROTECTED_ROUTES,
    ids=lambda value: value.replace('/', '_') if isinstance(value, str) else value,
)
@pytest.mark.parametrize(
    'headers',
    [
        {},
        {
            'X-Glasslab-Caller': 'authorized-caller',
            'X-Glasslab-Workflow-Token': 'wrong-token',
        },
    ],
    ids=['missing-credentials', 'invalid-token'],
)
def test_each_mutation_rejects_missing_or_invalid_credentials(
    client: TestClient,
    method: str,
    path_template: str,
    headers: dict[str, str],
) -> None:
    """Removing token validation must make every mutation route fail this test."""
    response = client.request(
        method,
        request_path(path_template),
        headers=headers,
        json={},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    ('method', 'path_template'),
    PROTECTED_ROUTES,
    ids=lambda value: value.replace('/', '_') if isinstance(value, str) else value,
)
def test_each_mutation_rejects_authenticated_caller_without_operation_permission(
    client: TestClient,
    method: str,
    path_template: str,
) -> None:
    """Removing the operation allowlist check must make every route fail this test."""
    response = client.request(
        method,
        request_path(path_template),
        headers={
            'X-Glasslab-Caller': 'unauthorized-caller',
            'X-Glasslab-Workflow-Token': 'unauthorized-token',
        },
        json={},
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ('method', 'path_template'),
    PROTECTED_ROUTES,
    ids=lambda value: value.replace('/', '_') if isinstance(value, str) else value,
)
def test_each_mutation_reaches_its_existing_response_for_an_authorized_caller(
    client: TestClient,
    method: str,
    path_template: str,
) -> None:
    """Removing the authorization success path must make every route fail this test."""
    response = client.request(
        method,
        request_path(path_template),
        headers={
            'X-Glasslab-Caller': 'authorized-caller',
            'X-Glasslab-Workflow-Token': 'authorized-token',
        },
        json={},
    )

    assert response.status_code not in {401, 403}


def test_healthz_remains_available_without_caller_credentials(client: TestClient) -> None:
    response = client.get('/healthz')

    assert response.status_code == 200


def test_caller_policy_rejects_whitespace_token() -> None:
    with pytest.raises(ValueError, match='token must not be empty'):
        Settings(caller_policies=[{'name': 'caller', 'token': '   ', 'allowed_operations': []}])


def test_settings_reject_duplicate_caller_tokens() -> None:
    with pytest.raises(ValueError, match='tokens must be unique'):
        Settings(caller_policies=[
            {'name': 'reader', 'token': 'shared', 'allowed_operations': ['GET /runs']},
            {'name': 'writer', 'token': 'shared', 'allowed_operations': ['POST /datasets']},
        ])
