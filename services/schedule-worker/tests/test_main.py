"""Integration-style tests for the schedule worker's FastAPI endpoints.

The service package is loaded dynamically so the tests can run without an
installed package build step. Each test uses TestClient against the live app
instance and monkeypatches urllib to avoid hitting the real workflow API.
"""

import json
import sys
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient

SERVICE_ROOT = Path(__file__).resolve().parents[1]
# Load the app module as if it were a package so relative imports resolve.
APP_ROOT = SERVICE_ROOT / 'app'
PACKAGE_NAME = 'schedule_worker_app'


def load_package_module(module_name: str, path: Path):
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

models_module = load_package_module(f'{PACKAGE_NAME}.models', APP_ROOT / 'models.py')
main_module = load_package_module(f'{PACKAGE_NAME}.main', APP_ROOT / 'main.py')
app = main_module.app


def test_healthz() -> None:
    client = TestClient(app)
    response = client.get('/healthz')
    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == 'ok'
    assert payload['worker_config']['workflow_api_url'].endswith(':8080')


def test_run_once_calls_workflow_api(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(self.payload).encode('utf-8')

    def fake_urlopen(request_obj, timeout):
        # Route by URL suffix: digest and rerun endpoints return distinct
        # payloads so the combined result verifies both cycles executed.
        if request_obj.full_url.endswith('/digest-schedules/run-due'):
            return FakeResponse(
                [
                    {
                        'execution_id': 'exec-1',
                        'schedule_id': 'sched-1',
                        'operation_type': 'digest',
                        'result_status': 'ok',
                        'result_detail': 'Digest daily-run-summary matched 2 runs.',
                        'digest_payload': {'matching_run_count': 2},
                    }
                ]
            )
        return FakeResponse(
            [
                {
                    'execution_id': 'exec-2',
                    'schedule_id': 'sched-2',
                    'operation_type': 'approved-rerun',
                    'result_status': 'ok',
                    'result_detail': 'Approved rerun submitted as run-2.',
                    'digest_payload': {},
                }
            ]
        )

    monkeypatch.setattr(main_module.urllib_request, 'urlopen', fake_urlopen)
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_CALLER_NAME', 'schedule-worker')
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_TOKEN', 'schedule-secret')

    client = TestClient(app)
    response = client.post('/run-once')
    assert response.status_code == 200
    payload = response.json()
    assert payload['executed_count'] == 2
    assert payload['executions'][0]['schedule_id'] == 'sched-1'
    assert payload['executions'][1]['schedule_id'] == 'sched-2'


def test_schedule_mutations_send_caller_identity(monkeypatch) -> None:
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'[]'

    def fake_urlopen(request_obj, timeout):
        requests.append(request_obj)
        return FakeResponse()

    monkeypatch.setattr(main_module.urllib_request, 'urlopen', fake_urlopen)
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_CALLER_NAME', 'schedule-worker')
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_TOKEN', 'schedule-secret')

    main_module.run_due_digest_cycle()
    main_module.run_due_approved_rerun_cycle()

    for request_obj in requests:
        headers = dict(request_obj.header_items())
        assert headers['X-glasslab-caller'] == 'schedule-worker'
        assert headers['X-glasslab-workflow-token'] == 'schedule-secret'


def test_schedule_mutation_fails_closed_without_token(monkeypatch) -> None:
    monkeypatch.setenv('GLASSLAB_WORKFLOW_API_CALLER_NAME', 'schedule-worker')
    monkeypatch.delenv('GLASSLAB_WORKFLOW_API_TOKEN', raising=False)

    try:
        main_module.run_due_digest_cycle()
    except RuntimeError as exc:
        assert str(exc) == 'workflow API mutation credentials are not configured'
    else:
        raise AssertionError('mutation unexpectedly proceeded without credentials')
