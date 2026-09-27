"""Behavioral tests for the interpretation agent.

The service is loaded from its source files under a synthetic package name
rather than imported as `app`, so these tests run regardless of the image layout
the service is deployed in. Loading order matters: main.py imports `.models`,
so models must be registered in sys.modules first.
"""

import sys
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = SERVICE_ROOT / 'app'
PACKAGE_NAME = 'interpretation_agent_app'


def load_package_module(module_name: str, path: Path):
    # Executes the file under the synthetic package name so relative imports
    # (`from .models import ...`) resolve; registering in sys.modules keeps the
    # module identity single even though pytest imports these files directly.
    spec = spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(APP_ROOT)]
sys.modules[PACKAGE_NAME] = package

# models must load before main: main.py does `from .models import ...` and the
# relative import is resolved through the synthetic package above.
models_module = load_package_module(f'{PACKAGE_NAME}.models', APP_ROOT / 'models.py')
main_module = load_package_module(f'{PACKAGE_NAME}.main', APP_ROOT / 'main.py')

app = main_module.app
build_interpretation_draft = main_module.build_interpretation_draft
interpret_with_backends = main_module.interpret_with_backends
InterpretationRequest = models_module.InterpretationRequest

INTERNAL_TOKEN_HEADER = 'X-Glasslab-Internal-Token'
INTERNAL_TOKEN_ENV = 'GLASSLAB_AGENT_INTERNAL_TOKEN'
INTERNAL_TOKEN = 'test-internal-token'


def internal_auth_headers() -> dict[str, str]:
    return {INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN}


@pytest.fixture(autouse=True)
def configure_internal_token(monkeypatch):
    monkeypatch.setenv(INTERNAL_TOKEN_ENV, INTERNAL_TOKEN)


def build_request() -> InterpretationRequest:
    return InterpretationRequest(
        request_id='intake-1',
        intake={
            'intake_id': 'intake-1',
            'source_type': 'paper-link',
            'source_refs': ['https://example.org/paper'],
            'document_refs': ['doc-1'],
            'raw_request': 'Read this paper and propose a bounded reproduction path for the Titanic benchmark.',
            'normalized_summary': 'Paper-derived reproduction request for a bounded benchmark.',
            'workflow_family_candidates': ['literature-to-experiment', 'replication-lite', 'generic-tabular-benchmark'],
            'notes': [
                'The paper compares a baseline on Titanic.',
                'Focus on the reported metrics and evaluation method.',
            ],
            'submitted_by': 'glasslab-operator',
        },
    )


def test_healthz() -> None:
    client = TestClient(app)
    response = client.get('/healthz')
    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == 'ok'
    assert payload['model_backend']['model'] == 'mlx-community/Qwen3-Coder-Next-4bit'


def test_build_interpretation_draft_prefers_matching_candidates() -> None:
    draft = build_interpretation_draft(build_request())

    assert draft.candidate_workflow_families[0] == 'generic-tabular-benchmark'
    assert 'titanic' in draft.dataset_hints
    assert 'reported metrics' in draft.evaluation_targets
    assert draft.literature_state_summary.startswith('Current bounded literature view:')
    assert draft.extracted_claims[0].startswith('The paper compares')
    assert draft.bounded_experiment_ideas


def test_interpret_intake_endpoint_returns_bounded_draft_shape(monkeypatch) -> None:
    def fake_interpret_with_backends(request):
        return (
            build_interpretation_draft(request),
            main_module.PRIMARY_BACKEND.metadata(),
            ['stubbed interpretation backend'],
        )

    monkeypatch.setattr(main_module, 'interpret_with_backends', fake_interpret_with_backends)
    client = TestClient(app)
    response = client.post(
        '/interpret-intake',
        json=build_request().model_dump(),
        headers=internal_auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['request_id'] == 'intake-1'
    assert payload['draft']['source_type'] == 'paper-link'
    assert payload['draft']['candidate_workflow_families'][0] == 'generic-tabular-benchmark'
    assert payload['draft']['literature_state_summary'].startswith('Current bounded literature view:')
    assert 'research_gaps' in payload['draft']
    assert payload['draft']['bounded_experiment_ideas']
    assert payload['model_backend']['provider'] == 'openai-compatible'
    assert payload['warnings'] == ['stubbed interpretation backend']


def test_interpretation_agent_uses_fallback_backend(monkeypatch) -> None:
    request = build_request()
    calls: list[str] = []
    monkeypatch.setattr(
        main_module,
        'FALLBACK_BACKEND',
        main_module.ProviderConfig(
            provider='openai-compatible',
            base_url='http://192.168.1.22:52415',
            model='mlx-community/Qwen3-Coder-Next-4bit',
            timeout_seconds=60.0,
        ),
    )

    def fake_call_backend(req, backend):
        calls.append(backend.base_url)
        # The primary backend is distinguished by its host (192.168.1.21); the
        # stub fails exactly the primary and lets the patched fallback (.22)
        # succeed so the fallback path is exercised end to end.
        if backend.base_url.endswith('.21:52415'):
            raise ValueError('primary unavailable')
        draft = build_interpretation_draft(req)
        draft.extracted_method_summary = 'Fallback model interpretation.'
        return draft

    monkeypatch.setattr(main_module, 'call_backend', fake_call_backend)
    draft, backend, warnings = interpret_with_backends(request)

    assert draft.extracted_method_summary == 'Fallback model interpretation.'
    assert backend.base_url == 'http://192.168.1.22:52415'
    assert calls == ['http://192.168.1.21:52415', 'http://192.168.1.22:52415']
    assert 'used fallback interpretation backend' in warnings


def test_interpretation_agent_falls_back_to_deterministic_scaffold(monkeypatch) -> None:
    request = build_request()

    # Both backends fail, so interpret_with_backends must return the pure
    # deterministic scaffold while still reporting which backend was used.
    def failing_call_backend(_req, _backend):
        raise ValueError('backend failed')

    monkeypatch.setattr(main_module, 'call_backend', failing_call_backend)
    draft, backend, warnings = interpret_with_backends(request)

    assert draft.candidate_workflow_families[0] == 'generic-tabular-benchmark'
    assert backend.base_url == 'http://192.168.1.21:52415'
    assert any('all model backends failed' in warning for warning in warnings)


def test_interpret_intake_rejects_missing_internal_token() -> None:
    client = TestClient(app)
    response = client.post('/interpret-intake', json=build_request().model_dump())
    assert response.status_code == 401


def test_interpret_intake_rejects_wrong_internal_token() -> None:
    client = TestClient(app)
    response = client.post(
        '/interpret-intake',
        json=build_request().model_dump(),
        headers={INTERNAL_TOKEN_HEADER: 'wrong-token'},
    )
    assert response.status_code == 401


def test_interpret_intake_fails_closed_when_token_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv(INTERNAL_TOKEN_ENV)
    client = TestClient(app)
    response = client.post(
        '/interpret-intake',
        json=build_request().model_dump(),
        headers=internal_auth_headers(),
    )
    assert response.status_code == 503


def test_healthz_stays_anonymous() -> None:
    client = TestClient(app)
    assert client.get('/healthz').status_code == 200
