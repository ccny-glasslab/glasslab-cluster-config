"""Wire-contract tests against workflow-api's REAL generic-run request model.

``test_workflow_api_contract.py`` validates the sender's body against the
receiver's pydantic models loaded from source. This module closes the next gap:
it drives the orchestrator's real ``WorkflowApiClusterExecutor.submit`` over an
in-process transport bound to a FastAPI app that serves the receiver's real
``GenericExperimentRunRequest``, so the exact bytes the orchestrator POSTs are
parsed by the exact request model -- and rejected with the exact FastAPI 422
shape -- that the service runs in production.

Issue #491 is the canonical defect this catches. The sender once included a
top-level ``resources`` field that ``GenericExperimentRunRequest`` forbids
(``extra='forbid'``), so every live submission was rejected with HTTP 422 while
the fake rehearsal path -- which never sends a request -- advanced happily. The
tests below prove the real sender body is accepted and that the #491 payload is
rejected with the same ``extra_forbidden`` 422 shape the live service produced.

The app is a minimal FastAPI app rather than the full workflow-api ASGI app:
importing ``workflow-api/app/main.py`` drags in ``job_submission`` -> ``urllib3``
-> the Kubernetes client, none of which the orchestrator CI lane installs. The
full app is therefore unimportable in this lane; the request model, its
``extra='forbid'`` config, and FastAPI's 422 body are not. See
``workflow_api_wire_stub.py``.
"""

from __future__ import annotations

import json
import socket
from typing import Any

import httpx
import pytest

from app.cluster import ClusterExecutorError, WorkflowApiClusterExecutor
from app.schemas import ExpandedJobSpec, JobStatus, ResourceRequest

from workflow_api_wire_stub import (
    BASE_URL,
    CALLER_NAME,
    ORCHESTRATOR_TOKEN,
    WireStub,
    build_wire_stub,
    install_wire_transport,
)


def _spec(*, workspace: bool = True) -> ExpandedJobSpec:
    return ExpandedJobSpec(
        orchestrator_job_id='job-1',
        run_id='run-1',
        action_id='action-1',
        variant_name='candidate',
        seed=17,
        idempotency_key='key-1',
        base_config='configs/candidate.yaml',
        overrides={'learning_rate': 0.01},
        runner_image='example.invalid/stale-runner:v1',
        resources=ResourceRequest(
            cpu=2,
            memory_gib=4,
            gpus=0,
            wallclock_minutes=45,
        ),
        required_artifacts=['metrics.json', 'report.md'],
        evaluation_contract_id='classification-metric-v1',
        evaluation_contract_version='1.0.0',
        evaluation_contract_digest='a' * 64,
        workload_id='workspace-cpu-ml-v1' if workspace else 'gpu-experiment',
        experiment_type=(
            'research-workspace-job' if workspace else 'gpu-training-job'
        ),
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


def _wired_executor(stub: WireStub) -> WorkflowApiClusterExecutor:
    executor = WorkflowApiClusterExecutor(
        base_url=BASE_URL,
        workload_id='workspace-cpu-ml-v1',
        experiment_type='research-workspace-job',
        caller_name=CALLER_NAME,
        token=ORCHESTRATOR_TOKEN,
    )
    install_wire_transport(executor, stub)
    return executor


def _bind_client(
    executor: WorkflowApiClusterExecutor,
    transport: httpx.BaseTransport,
) -> None:
    setattr(
        executor,
        '_client',
        lambda: httpx.Client(base_url=BASE_URL, transport=transport),
    )


def _capture_sender_body(spec: ExpandedJobSpec) -> dict[str, Any]:
    # The mutation test needs the unmodified bytes the sender would POST.
    # Capture them on a throwaway in-memory transport, then replay a mutated
    # copy through the real receiver model.
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            201,
            json={'run_id': 'external-1', 'status': {'status': 'accepted'}},
        )

    executor = WorkflowApiClusterExecutor(
        base_url=BASE_URL,
        workload_id='workspace-cpu-ml-v1',
        experiment_type='research-workspace-job',
        caller_name=CALLER_NAME,
        token=ORCHESTRATOR_TOKEN,
    )
    _bind_client(executor, httpx.MockTransport(handler))
    executor.submit(spec)
    return captured


def _auth_headers(*, idempotency_key: str) -> dict[str, str]:
    return {
        'X-Glasslab-Caller': CALLER_NAME,
        'X-Glasslab-Workflow-Token': ORCHESTRATOR_TOKEN,
        'Idempotency-Key': idempotency_key,
    }


def test_real_sender_body_is_accepted_by_the_real_request_model() -> None:
    # Given the real executor wired to a receiver that parses with the real
    # request model, when it submits a workspace job, then the body validates
    # and the receiver sees the sender's workload identity and objective.
    stub = build_wire_stub()
    executor = _wired_executor(stub)

    submission = executor.submit(_spec(workspace=True))

    assert submission.external_run_id
    assert submission.status is JobStatus.QUEUED
    assert len(stub.accepted) == 1
    assert stub.accepted[0].workload_id == 'workspace-cpu-ml-v1'
    assert stub.accepted[0].objective.startswith('Research orchestrator variant')


def test_repeated_submit_is_idempotent_through_the_stub() -> None:
    # The executor replays its own idempotency key, so the second submit returns
    # the original submission and the receiver is hit exactly once.
    stub = build_wire_stub()
    executor = _wired_executor(stub)

    first = executor.submit(_spec(workspace=True))
    second = executor.submit(_spec(workspace=True))

    assert first.external_run_id == second.external_run_id
    assert len(stub.accepted) == 1


def test_forbidden_top_level_resources_is_rejected_with_real_422_shape() -> None:
    # Given the real sender body, when the #491 top-level 'resources' field is
    # reintroduced, then the receiver rejects it with the same 422
    # extra_forbidden shape the live service produced.
    stub = build_wire_stub()
    body = _capture_sender_body(_spec(workspace=True))
    forbidden = {**body, 'resources': {'cpu': 2, 'memory_gib': 4}}

    with stub.client() as client:
        response = client.post(
            '/experiments/runs',
            json=forbidden,
            headers=_auth_headers(idempotency_key='key-491'),
        )

    assert response.status_code == 422
    detail = response.json()['detail']
    assert isinstance(detail, list) and detail
    assert detail[0]['type'] == 'extra_forbidden'
    assert detail[0]['loc'] == ['body', 'resources']
    assert detail[0]['input'] == {'cpu': 2, 'memory_gib': 4}

    # The valid sibling body still validates, so the rejection is caused by the
    # forbidden field alone and not by an unrelated drift in the fixture.
    with stub.client() as client:
        accepted = client.post(
            '/experiments/runs',
            json=body,
            headers=_auth_headers(idempotency_key='key-valid'),
        )
    assert accepted.status_code == 201


def test_forbidden_top_level_resources_is_rejected_for_non_workspace_body() -> None:
    # The extra='forbid' guard is a property of the receiver request model, not
    # of one workload variant: the non-workspace sender body is rejected the
    # same way.
    stub = build_wire_stub()
    body = _capture_sender_body(_spec(workspace=False))
    forbidden = {**body, 'resources': {'cpu': 1}}

    with stub.client() as client:
        response = client.post(
            '/experiments/runs',
            json=forbidden,
            headers=_auth_headers(idempotency_key='key-491-gpu'),
        )

    assert response.status_code == 422
    assert response.json()['detail'][0]['loc'] == ['body', 'resources']


def test_stub_serves_the_request_with_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # The transport must be genuinely in-process. Name resolution and TCP
    # connection creation are the two syscalls every real HTTP transport needs;
    # forbidding them (while leaving AF_UNIX self-pipes used by the event loop
    # alone) proves the request never left the process.
    stub = build_wire_stub()
    executor = _wired_executor(stub)

    def _refuse_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError('wire stub attempted a network connection')

    monkeypatch.setattr(socket, 'getaddrinfo', _refuse_network)
    monkeypatch.setattr(socket, 'create_connection', _refuse_network)

    submission = executor.submit(_spec(workspace=True))

    assert submission.external_run_id
    assert len(stub.accepted) == 1


def test_malformed_body_reports_receiver_error_in_seconds() -> None:
    # The point of the stub: a wire defect surfaces immediately as the
    # receiver's own error text, not silently as a fake-path success.
    stub = build_wire_stub()
    body = _capture_sender_body(_spec(workspace=True))
    malformed = {**body, 'budget': 'not-an-object'}

    with stub.client() as client:
        response = client.post(
            '/experiments/runs',
            json=malformed,
            headers=_auth_headers(idempotency_key='key-malformed'),
        )

    assert response.status_code == 422
    assert response.json()['detail'][0]['loc'] == ['body', 'budget']


def test_executor_raises_cluster_error_on_rejected_payload() -> None:
    # And the executor path turns a rejected submission into a loud
    # ClusterExecutorError rather than a silent no-op.
    stub = build_wire_stub()

    class _ForbiddenFieldTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            payload['resources'] = {'cpu': 2}
            headers = {
                key: value
                for key, value in request.headers.items()
                if key.lower() != 'content-length'
            }
            return stub.transport.handle_request(
                httpx.Request(
                    method=request.method,
                    url=request.url,
                    headers=headers,
                    content=json.dumps(payload).encode(),
                )
            )

    executor = WorkflowApiClusterExecutor(
        base_url=BASE_URL,
        workload_id='workspace-cpu-ml-v1',
        experiment_type='research-workspace-job',
        caller_name=CALLER_NAME,
        token=ORCHESTRATOR_TOKEN,
    )
    forbidden_transport = _ForbiddenFieldTransport()
    _bind_client(executor, forbidden_transport)

    with pytest.raises(ClusterExecutorError) as excinfo:
        executor.submit(_spec(workspace=True))

    assert '422' in str(excinfo.value)
