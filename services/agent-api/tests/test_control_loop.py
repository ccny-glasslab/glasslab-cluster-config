"""End-to-end experiment flow through create_app with fake dependencies.

Covers the full lifecycle on one request — plan, validate, submit, refresh to a
terminal state, summarize the result — plus the logs endpoint after the record
is finalized.
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
            content='{"pipeline":"titanic_baseline","dataset":"titanic","models":["logistic_regression","random_forest"],"feature_profile":"extended","resource_profile":"cpu-small","compare_to":"none","produce_submission":true}',
            raw_payload={},
        )


class FakeJobSubmitter:
    def submit_job(self, spec, experiment_id, trace_id):
        return type('Submission', (), {'job_name': 'titanic-baseline-integration', 'namespace': 'glasslab-agents', 'manifest_name': 'titanic-baseline-integration'})()


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
            'best_metric': 0.845,
            'submission_created': True,
            'artifact_dir': f'/mnt/artifacts/{experiment_id}',
        }

    def read_failure_message(self, job_name):
        return None


def test_experiment_post_and_get_flow(tmp_path) -> None:
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
        logger=logging.getLogger('test-control-loop'),
    )
    client = TestClient(create_app(settings=settings, runtime=runtime))

    response = client.post(
        '/experiments',
        json={
            'request_text': 'Run a Titanic baseline with logistic regression and random forest, compare them, and prepare a submission file.',
        },
    )
    assert response.status_code == 200
    created = response.json()
    assert created['status'] == 'running'

    fetched = client.get(f"/experiments/{created['id']}")
    assert fetched.status_code == 200
    payload = fetched.json()
    assert payload['status'] == 'succeeded'
    assert 'random_forest won' in payload['result_summary']

    logs = client.get(f"/experiments/{created['id']}/logs")
    assert logs.status_code == 200
    assert any(entry['message'] == 'job_status_change' for entry in logs.json()['logs'])
