"""Expose a /run-once endpoint that triggers one full due-digest and
approved-rerun cycle against the workflow API.

The worker is stateless: it calls the workflow API synchronously and returns
execution records. A Kubernetes CronJob owns the retry and schedule cadence.
"""

from __future__ import annotations

import json
import os
from urllib import request as urllib_request

from fastapi import FastAPI

from .models import HealthResponse, RunOnceResponse, ScheduledExecutionPayload, WorkerConfigMetadata

WORKFLOW_API_URL = os.environ.get(
    'GLASSLAB_SCHEDULE_WORKER_WORKFLOW_API_URL',
    'http://glasslab-workflow-api.glasslab-v2.svc.cluster.local:8080',
).rstrip('/')
TIMEOUT_SECONDS = float(os.environ.get('GLASSLAB_SCHEDULE_WORKER_TIMEOUT_SECONDS', '30'))
def mutation_headers() -> dict[str, str]:
    caller_name = os.environ.get('GLASSLAB_WORKFLOW_API_CALLER_NAME', '').strip()
    token = os.environ.get('GLASSLAB_WORKFLOW_API_TOKEN', '')
    if not caller_name or not token:
        raise RuntimeError('workflow API mutation credentials are not configured')
    return {
        'Content-Type': 'application/json',
        'X-Glasslab-Caller': caller_name,
        'X-Glasslab-Workflow-Token': token,
    }


def worker_config() -> WorkerConfigMetadata:
    return WorkerConfigMetadata(
        workflow_api_url=WORKFLOW_API_URL,
        timeout_seconds=TIMEOUT_SECONDS,
    )


def run_due_digest_cycle() -> RunOnceResponse:
    # POST with empty body: the workflow API resolves due digest schedules
    # server-side from its own persisted definitions.
    request_obj = urllib_request.Request(
        f'{WORKFLOW_API_URL}/digest-schedules/run-due',
        data=b'',
        headers=mutation_headers(),
        method='POST',
    )
    with urllib_request.urlopen(request_obj, timeout=TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode('utf-8'))
    executions = [ScheduledExecutionPayload.model_validate(item) for item in payload]
    return RunOnceResponse(
        worker_status='ok',
        executed_count=len(executions),
        executions=executions,
        worker_config=worker_config(),
    )


def run_due_approved_rerun_cycle() -> list[ScheduledExecutionPayload]:
    request_obj = urllib_request.Request(
        f'{WORKFLOW_API_URL}/approved-rerun-schedules/run-due',
        data=b'',
        headers=mutation_headers(),
        method='POST',
    )
    with urllib_request.urlopen(request_obj, timeout=TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode('utf-8'))
    return [ScheduledExecutionPayload.model_validate(item) for item in payload]


app = FastAPI(title='glasslab-schedule-worker', version='0.1.0')


@app.get('/healthz', response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(status='ok', worker_config=worker_config())


@app.post('/run-once', response_model=RunOnceResponse)
def run_once() -> RunOnceResponse:
    digest_result = run_due_digest_cycle()
    # Digest cycle runs first so execution ordering is stable; rerun
    # executions are appended so the combined list reflects both phases
    # in a single response.
    rerun_executions = run_due_approved_rerun_cycle()
    all_executions = list(digest_result.executions) + rerun_executions
    return RunOnceResponse(
        worker_status='ok',
        executed_count=len(all_executions),
        executions=all_executions,
        worker_config=worker_config(),
    )
