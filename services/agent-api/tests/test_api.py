"""API-level tests for health, catalog, and experiment lifecycle endpoints.

Cluster and model dependencies are replaced with fakes so the tests exercise
the FastAPI surface and control loop without a real cluster or LLM endpoint.
"""

import logging

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import RuntimeContext, create_app
from app.qwen_client import ChatResponse
from app.state_store import StateStore
from app.summarizer import ResultSummarizer


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


def build_test_client(tmp_path):
    settings = Settings(
        qwen_api_key='fixture-qwen-key',
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
    return TestClient(create_app(settings=settings, runtime=runtime))


def test_health_and_catalog_endpoints(tmp_path) -> None:
    client = build_test_client(tmp_path)

    assert client.get('/health').status_code == 200
    assert client.get('/pipelines').status_code == 200
    assert client.get('/datasets').status_code == 200
