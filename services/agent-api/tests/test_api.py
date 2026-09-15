"""API-level tests for health, catalog, and experiment lifecycle endpoints.

Cluster and model dependencies are replaced with fakes so the tests exercise
the FastAPI surface and control loop without a real cluster or LLM endpoint.
"""

import logging

from fastapi.testclient import TestClient

from app.auth import TOKEN_HEADER
from app.config import Settings
from app.main import RuntimeContext, create_app
from app.qwen_client import ChatResponse
from app.state_store import StateStore
from app.summarizer import ResultSummarizer


TOKEN = 'fixture-agent-token'


class FakeQwenClient:
    def chat(self, messages, max_tokens=None):
        return ChatResponse(
            content='{"pipeline":"titanic_baseline","dataset":"titanic","models":["logistic_regression","random_forest"],"feature_profile":"basic","resource_profile":"cpu-small","compare_to":"none","produce_submission":true}',
            raw_payload={},
        )


class FakeJobSubmitter:
    def submit_job(self, spec, experiment_id, trace_id):
        return type('Submission', (), {'job_name': 'titanic-baseline-test', 'namespace': 'glasslab-agents', 'manifest_name': 'titanic-baseline-test'})()


class FakeJobStatusService:
    def __init__(self):
        self.calls = 0

    def get_job_status(self, job_name):
        self.calls += 1
        # First status call reports running, later calls report succeeded,
        # simulating a Job that finishes between two polls.
        if self.calls == 1:
            return {'job_name': job_name, 'status': 'running'}
        return {'job_name': job_name, 'status': 'succeeded'}

    def list_artifacts(self, experiment_id):
        return []

    def read_result_payload(self, experiment_id):
        return {
            'models_ran': ['logistic_regression', 'random_forest'],
            'best_model': 'random_forest',
            'metric_name': 'accuracy',
            'best_metric': 0.8125,
            'submission_created': True,
            'artifact_dir': f'/mnt/artifacts/{experiment_id}',
        }

    def read_failure_message(self, job_name):
        return None


def build_test_app(tmp_path):
    settings = Settings(
        qwen_api_key='fixture-qwen-key',
        api_token=TOKEN,
        state_db_path=str(tmp_path / 'agent.db'),
        auto_monitor_submitted_jobs=False,
        llm_summary_enabled=False,
    )
    runtime = RuntimeContext(
        settings=settings,
        state_store=StateStore(settings.state_db_path),
        qwen_client=FakeQwenClient(),
        job_submitter=FakeJobSubmitter(),
        job_status_service=FakeJobStatusService(),
        summarizer=ResultSummarizer(settings, None),
        logger=logging.getLogger('test-agent-api'),
    )
    return create_app(settings=settings, runtime=runtime)


def build_test_client(tmp_path):
    return TestClient(build_test_app(tmp_path), headers={TOKEN_HEADER: TOKEN})


def test_health_and_catalog_endpoints(tmp_path) -> None:
    client = build_test_client(tmp_path)

    assert client.get('/health').status_code == 200
    assert client.get('/pipelines').status_code == 200
    assert client.get('/datasets').status_code == 200


def test_health_is_exempt_from_agent_token(tmp_path) -> None:
    client = TestClient(build_test_app(tmp_path))

    assert client.get('/health').status_code == 200


def test_experiments_requires_agent_token(tmp_path) -> None:
    client = TestClient(build_test_app(tmp_path))
    body = {'request_text': 'Run a Titanic baseline.'}

    missing = client.post('/experiments', json=body)
    assert missing.status_code == 401

    wrong = client.post('/experiments', json=body, headers={TOKEN_HEADER: 'wrong-token'})
    assert wrong.status_code == 401

    authorized = client.post('/experiments', json=body, headers={TOKEN_HEADER: TOKEN})
    assert authorized.status_code != 401


def test_every_route_except_health_requires_agent_token(tmp_path) -> None:
    app = build_test_app(tmp_path)
    client = TestClient(app)

    protected_paths = set()
    for route in app.routes:
        path = getattr(route, 'path_format', None) or getattr(route, 'path', '')
        if not path or path == '/health':
            continue
        methods = getattr(route, 'methods', None) or {'GET'}
        method = sorted(methods)[0].lower()
        probe_path = path.replace('{experiment_id}', 'missing-id')
        protected_paths.add(path)
        kwargs = {'json': {'request_text': 'probe'}} if method in {'post', 'put', 'patch'} else {}
        response = getattr(client, method)(probe_path, **kwargs)
        assert response.status_code == 401, f'{method.upper()} {path} was not guarded'

    assert {
        '/pipelines',
        '/datasets',
        '/experiments',
        '/experiments/{experiment_id}',
        '/experiments/{experiment_id}/logs',
        '/experiments/{experiment_id}/artifacts',
    } <= protected_paths
