"""WorkflowApiClusterExecutor submission behavior over a mocked HTTP client.

Covers which fields the submission payload carries for research-workspace
workloads (fixed registry image, task/source bundles) versus GPU-training
workloads (also registry-owned), and that workflow-api rejection detail is
preserved verbatim as ClusterExecutorError.
"""

from __future__ import annotations

import httpx
import json
import pytest

from app.cluster import ClusterExecutorError, WorkflowApiClusterExecutor
from app.schemas import ExpandedJobSpec, ResourceRequest


def _spec(*, workspace: bool) -> ExpandedJobSpec:
    return ExpandedJobSpec(
        orchestrator_job_id='job-1',
        run_id='run-1',
        action_id='action-1',
        variant_name='candidate',
        seed=17,
        idempotency_key='key-1',
        base_config='configs/candidate.yaml',
        overrides={},
        runner_image='example.invalid/stale-runner:v1',
        resources=ResourceRequest(
            cpu=1,
            memory_gib=2,
            gpus=0,
            wallclock_minutes=10,
        ),
        required_artifacts=['metrics.json'],
        evaluation_contract_id='contract-v1',
        evaluation_contract_version='1.0.0',
        evaluation_contract_digest='a' * 64,
        workload_id='workspace-cpu-ml-v1' if workspace else 'gpu-experiment',
        experiment_type='research-workspace-job' if workspace else 'gpu-training-job',
        task_bundle=(
            {'uri': 's3://artifacts/task.zip', 'sha256': 'b' * 64}
            if workspace
            else None
        ),
        source_bundle=(
            {'uri': 's3://artifacts/source.zip', 'sha256': 'c' * 64}
            if workspace
            else None
        ),
        workspace_command=['python3', 'run.py'] if workspace else [],
    )


def _executor(handler, *, caller_name='research-orchestrator', token='orchestrator-secret') -> WorkflowApiClusterExecutor:
    executor = WorkflowApiClusterExecutor(
        base_url='http://workflow-api.test',
        workload_id='gpu-experiment',
        experiment_type='gpu-training-job',
        caller_name=caller_name,
        token=token,
    )
    # Swap the real HTTP client for an in-memory MockTransport so the tests
    # exercise the exact payload and error mapping without a live workflow-api.
    transport = httpx.MockTransport(handler)
    executor._client = lambda: httpx.Client(  # type: ignore[method-assign]
        base_url='http://workflow-api.test',
        transport=transport,
    )
    return executor


def test_mutations_send_caller_identity() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(201, json={'run_id': 'external-1', 'status': {'status': 'accepted'}})

    _executor(handler).submit(_spec(workspace=True))

    assert captured['x-glasslab-caller'] == 'research-orchestrator'
    assert captured['x-glasslab-workflow-token'] == 'orchestrator-secret'


def test_mutation_fails_closed_without_token() -> None:
    executor = _executor(lambda _: pytest.fail('request must not be sent'), token='   ')

    with pytest.raises(ClusterExecutorError, match='credentials are not configured'):
        executor.submit(_spec(workspace=True))


def test_reads_send_caller_identity() -> None:
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers))
        if request.url.path.endswith('/artifacts'):
            return httpx.Response(404, json={'detail': 'artifacts not found'})
        return httpx.Response(200, json={'status': {'status': 'running'}})

    _executor(handler).inspect('external-1')

    assert len(captured) == 2
    assert all(headers['x-glasslab-caller'] == 'research-orchestrator' for headers in captured)
    assert all(headers['x-glasslab-workflow-token'] == 'orchestrator-secret' for headers in captured)


def test_cancellation_sends_caller_identity() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={})

    _executor(handler).cancel('external-1')

    assert captured['x-glasslab-caller'] == 'research-orchestrator'
    assert captured['x-glasslab-workflow-token'] == 'orchestrator-secret'


def test_workspace_submission_defers_fixed_image_to_workflow_registry() -> None:
    # Research-workspace jobs must NOT send a custom image_ref: the runner
    # image is pinned by the workload registry, so omitting it is the point.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            201,
            json={'run_id': 'external-1', 'status': {'status': 'accepted'}},
        )

    _executor(handler).submit(_spec(workspace=True))
    assert 'image_ref' not in captured


def test_non_workspace_submission_also_defers_image_to_workflow_registry() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            201,
            json={'run_id': 'external-1', 'status': {'status': 'accepted'}},
        )

    _executor(handler).submit(_spec(workspace=False))
    assert 'image_ref' not in captured


def test_submission_error_preserves_workflow_api_detail() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={'detail': 'workflow execution policy rejected the submission'},
        )

    with pytest.raises(
        ClusterExecutorError,
        match='workflow execution policy rejected the submission',
    ):
        _executor(handler).submit(_spec(workspace=False))
