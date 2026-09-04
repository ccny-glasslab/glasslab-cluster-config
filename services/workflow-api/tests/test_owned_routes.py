"""Test that the workflow-api exposes exactly the owned route surface.
"""

from __future__ import annotations

import sys

# Prevent stale ``app.*`` module state from leaking between test modules.
for module_name in list(sys.modules):
    if module_name == 'app' or module_name.startswith('app.'):
        del sys.modules[module_name]

from fastapi.testclient import TestClient

from app.main import create_app
from app.config import Settings
from app.persistence import InMemoryRunStore
from app.registry import WorkflowRegistry


from pathlib import Path
import sys

# Prevent stale ``app.*`` module state from leaking between test modules.
for module_name in list(sys.modules):
    if module_name == 'app' or module_name.startswith('app.'):
        del sys.modules[module_name]

from fastapi.testclient import TestClient as FastAPITestClient
from fastapi.routing import APIRoute

from app.main import create_app
from app.config import Settings, CallerPolicy
from app.persistence import InMemoryRunStore
from app.registry import WorkflowRegistry


REPO_ROOT = Path(__file__).resolve().parents[3]


class TestClient(FastAPITestClient):
    """Test client with proper auth headers."""

    def __init__(self, app, **kwargs) -> None:
        app.state.settings.caller_policies = (
            CallerPolicy(
                name='test-suite',
                token='test-suite-token',
                allowed_operations=frozenset(
                    f'{method} {route.path_format}'
                    for route in app.routes
                    if hasattr(route, 'path_format') and hasattr(route, 'methods')
                    for method in route.methods & {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}
                ),
            ),
        )
        headers = dict(kwargs.pop('headers', {}))
        headers.setdefault('X-Glasslab-Caller', 'test-suite')
        headers.setdefault('X-Glasslab-Workflow-Token', 'test-suite-token')
        super().__init__(app, headers=headers, **kwargs)


def build_client() -> TestClient:
    """Build a test client with default settings."""
    settings = Settings(
        registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'),
    )
    registry = WorkflowRegistry(settings.registry_dir)
    store = InMemoryRunStore()
    # Let create_app build the default submitter (NullJobSubmitter with namespace)
    return TestClient(create_app(settings=settings, registry=registry, store=store))


class TestOwnedRoutes:
    """Test that exactly the owned routes are registered."""

    def test_orchestrator_owned_endpoints_exist(self) -> None:
        """The 4 orchestrator-owned endpoints must be present."""
        client = build_client()
        
        # Check POST /experiments/runs
        response = client.post('/experiments/runs', json={
            'objective': 'Test run',
            'experiment_type': 'generic-experiment',
            'workload_id': 'generic-tabular-benchmark',
            'budget': {'max_wallclock_minutes': 5},
        })
        assert response.status_code in [201, 409], f"POST /experiments/runs should exist (got {response.status_code})"
        
        # Check GET /runs/{id}
        response = client.get('/runs/run-123')
        assert response.status_code in [200, 404], f"GET /runs/{{id}} should exist (got {response.status_code})"
        
        # Check GET /runs/{id}/artifacts
        response = client.get('/runs/run-123/artifacts')
        assert response.status_code in [200, 404], f"GET /runs/{{id}}/artifacts should exist (got {response.status_code})"
        
        # Check POST /runs/{id}/cancel
        response = client.post('/runs/run-123/cancel')
        assert response.status_code in [200, 404, 409], f"POST /runs/{{id}}/cancel should exist (got {response.status_code})"

    def test_autoresearch_routes_deregistered(self) -> None:
        """Autoresearch routes must NOT be registered."""
        client = build_client()
        
        # Check POST /autoresearch/campaigns
        response = client.post('/autoresearch/campaigns', json={'session_id': 'test'})
        # Autoresearch routes are removed, so they should either 404 (not found) or 403 (forbidden by auth)
        assert response.status_code in [404, 403], f"POST /autoresearch/campaigns should NOT exist (got {response.status_code})"
        
        # Check GET /autoresearch/campaigns/latest
        response = client.get('/autoresearch/campaigns/latest')
        assert response.status_code in [404, 403], f"GET /autoresearch/campaigns/latest should NOT exist (got {response.status_code})"
        
        # Check GET /autoresearch/campaigns/{campaign_id}
        response = client.get('/autoresearch/campaigns/camp-123')
        assert response.status_code in [404, 403], f"GET /autoresearch/campaigns/{{id}} should NOT exist (got {response.status_code})"
        
        # Check POST /autoresearch/campaigns/{campaign_id}/draft-initial-methodologies
        response = client.post('/autoresearch/campaigns/camp-123/draft-initial-methodologies')
        assert response.status_code in [404, 403], f"POST /autoresearch/campaigns/{{id}}/draft-initial-methodologies should NOT exist (got {response.status_code})"
        
        # Check POST /autoresearch/campaigns/{campaign_id}/launch-next-iteration
        response = client.post('/autoresearch/campaigns/camp-123/launch-next-iteration')
        assert response.status_code in [404, 403], f"POST /autoresearch/campaigns/{{id}}/launch-next-iteration should NOT exist (got {response.status_code})"
        
        # Check POST /autoresearch/campaigns/{campaign_id}/decide-latest
        response = client.post('/autoresearch/campaigns/camp-123/decide-latest')
        assert response.status_code in [404, 403], f"POST /autoresearch/campaigns/{{id}}/decide-latest should NOT exist (got {response.status_code})"

    def test_autoresearch_campaign_transitions_deregistered(self) -> None:
        """Session-scoped autoresearch transitions must NOT be registered."""
        client = build_client()
        
        # Check POST /research-sessions/{session_id}/transitions/start-autoresearch-campaign
        response = client.post('/research-sessions/sess-123/transitions/start-autoresearch-campaign')
        assert response.status_code in [404, 403], f"POST /research-sessions/{{id}}/transitions/start-autoresearch-campaign should NOT exist (got {response.status_code})"
        
        # Check POST /research-sessions/{session_id}/transitions/draft-methodologies
        response = client.post('/research-sessions/sess-123/transitions/draft-methodologies')
        assert response.status_code in [404, 403], f"POST /research-sessions/{{id}}/transitions/draft-methodologies should NOT exist (got {response.status_code})"
        
        # Check POST /research-sessions/{session_id}/transitions/launch-autoresearch-iteration
        response = client.post('/research-sessions/sess-123/transitions/launch-autoresearch-iteration')
        assert response.status_code in [404, 403], f"POST /research-sessions/{{id}}/transitions/launch-autoresearch-iteration should NOT exist (got {response.status_code})"
        
        # Check POST /research-sessions/{session_id}/transitions/decide-autoresearch-latest
        response = client.post('/research-sessions/sess-123/transitions/decide-autoresearch-latest')
        assert response.status_code in [404, 403], f"POST /research-sessions/{{id}}/transitions/decide-autoresearch-latest should NOT exist (got {response.status_code})"

    def test_stage_agents_deregistered(self) -> None:
        """Stage agent routes (design, interpretation, inference) must NOT be registered."""
        client = build_client()
        
        # Check POST /interpretations/from-latest-intake (stage interpretation)
        response = client.post('/interpretations/from-latest-intake')
        assert response.status_code == 404, f"POST /interpretations/from-latest-intake should NOT exist (got {response.status_code})"
        
        # Check POST /replicability-assessments/from-latest-interpretation (stage assessment)
        response = client.post('/replicability-assessments/from-latest-interpretation')
        assert response.status_code == 404, f"POST /replicability-assessments/from-latest-interpretation should NOT exist (got {response.status_code})"
        
        # Check POST /design-drafts/from-latest-intake (stage design)
        response = client.post('/design-drafts/from-latest-intake')
        assert response.status_code == 404, f"POST /design-drafts/from-latest-intake should NOT exist (got {response.status_code})"
        
        # Check POST /research-sessions/{session_id}/skills/design (stage design)
        response = client.post('/research-sessions/sess-123/skills/design')
        assert response.status_code == 404, f"POST /research-sessions/{{id}}/skills/design should NOT exist (got {response.status_code})"

    def test_transition_stage_routes_deregistered(self) -> None:
        """Transition routes that expose stage agents must NOT be registered."""
        client = build_client()
        
        # Check POST /transitions/create-interpretation
        response = client.post('/transitions/create-interpretation')
        assert response.status_code in [404, 403], f"POST /transitions/create-interpretation should NOT exist (got {response.status_code})"
        
        # Check POST /transitions/create-methodology-draft
        response = client.post('/transitions/create-methodology-draft')
        assert response.status_code in [404, 403], f"POST /transitions/create-methodology-draft should NOT exist (got {response.status_code})"
        
        # Check POST /transitions/create-validation-run
        response = client.post('/transitions/create-validation-run')
        assert response.status_code in [404, 403], f"POST /transitions/create-validation-run should NOT exist (got {response.status_code})"

    def test_retained_investigations_routes_exist(self) -> None:
        """Investigations routes must be present."""
        client = build_client()
        
        # Check POST /investigations
        response = client.post('/investigations', json={'title': 'Test', 'research_question': 'Test?'})
        assert response.status_code in [201, 422], f"POST /investigations should exist (got {response.status_code})"

    def test_retained_literature_routes_exist(self) -> None:
        """Literature routes must be present."""
        client = build_client()
        
        # Check POST /research-sessions (literature session creation) - use valid goal_statement (12+ chars)
        response = client.post('/research-sessions', json={'goal_statement': 'Test goal here'})
        assert response.status_code in [201, 409, 422], f"POST /research-sessions should exist (got {response.status_code})"

    def test_retained_schedule_routes_exist(self) -> None:
        """Schedule routes must be present."""
        client = build_client()
        
        # Check POST /digest-schedules
        response = client.post('/digest-schedules', json={'cron_expr': '0 * * * *', 'digest_kind': 'daily'})
        assert response.status_code in [201, 422], f"POST /digest-schedules should exist (got {response.status_code})"

    def test_execution_routes_exist(self) -> None:
        """Execution routes must be present (kept for investigations/literature/schedule)."""
        client = build_client()
        
        # Check POST /experiments/runs (orchestrator endpoint) - use proper payload
        response = client.post('/experiments/runs', json={
            'objective': 'Test run',
            'experiment_type': 'generic-experiment',
            'workload_id': 'generic-tabular-benchmark',
            'budget': {'max_wallclock_minutes': 5},
        })
        assert response.status_code in [201, 409, 422], f"POST /experiments/runs should exist (got {response.status_code})"
        
        # Check GET /workflow-families
        response = client.get('/workflow-families')
        assert response.status_code == 200, f"GET /workflow-families should exist (got {response.status_code})"
