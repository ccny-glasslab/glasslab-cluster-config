"""Tests for digest and approved-rerun schedule execution with idempotency guarantees.

Validates that schedule execution produces exactly one execution record
per due tick (repeated invocations are no-ops), that schedule metadata is
updated correctly, that reruns clone the source run's contract, and that
mutating run-creation posts honor an idempotency key end-to-end so a crash
between submit and record-save cannot duplicate a Job.
"""

import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fastapi.testclient import TestClient as FastAPITestClient

for module_name in list(sys.modules):
    if module_name == 'app' or module_name.startswith('app.'):
        del sys.modules[module_name]

from app.auth import CallerPolicy
from app.config import Settings
from app.digest_scheduling import build_digest_schedule, execute_due_digest_schedules
from app.job_submission import NullJobSubmitter
from app.main import create_app, create_run_record, execute_due_approved_rerun_schedules
from app.persistence import InMemoryRunStore, JsonFileRunStore
from app.registry import WorkflowRegistry
from app.schemas import DigestScheduleCreateRequest, RunCreateRequest, RunRecord, ScheduledOperationRecord

REPO_ROOT = Path(__file__).resolve().parents[3]


class TestClient(FastAPITestClient):
    """Exercise routes as an explicitly authorized caller."""

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


def build_settings() -> Settings:
    return Settings(registry_dir=str(REPO_ROOT / 'services' / 'workflow-registry' / 'definitions'))


def build_registry() -> WorkflowRegistry:
    registry = WorkflowRegistry(build_settings().registry_dir)
    workflow = registry.get_workflow('generic-tabular-benchmark')
    assert workflow is not None
    registry._entries[workflow.workflow_id] = type(workflow).model_validate(
        {
            **workflow.model_dump(mode='json'),
            'runner_image': (
                'ghcr.io/ccny-glasslab/glasslab-research-workspace-runner@sha256:'
                'dae5bc4967f5ac54edb6c6d63d8d3db9e4652cc46e035118b0c456eb70121061'
            ),
            'default_entrypoint': ['python3', '-m', 'runner'],
            'execution_status': 'ready',
            'submission_backend': 'kubernetes',
            'execution_blockers': [],
            'max_wallclock_minutes': 60,
        }
    )
    return registry


def cron_expr_for(now: datetime) -> str:
    # Converts Python's weekday() (Mon=0..Sun=6) to the standard cron
    # weekday range (Sun=0, Mon=1, …, Sat=6) by shifting +1 and wrapping.
    return f'{now.minute} {now.hour} {now.day} {now.month} {(now.weekday() + 1) % 7}'


def build_source_run(store: InMemoryRunStore, registry: WorkflowRegistry, settings: Settings) -> RunRecord:
    workflow = registry.get_workflow('generic-tabular-benchmark')
    assert workflow is not None

    request = RunCreateRequest(
        workflow_id='generic-tabular-benchmark',
        objective='Create a reviewed benchmark run suitable for scheduled reruns.',
        inputs={
            'dataset_name': 'titanic',
            'train_uri': 's3://datasets/titanic/train.csv',
            'test_uri': 's3://datasets/titanic/test.csv',
            'target_column': 'Survived',
        },
        models=['logistic_regression'],
        resource_profile='cpu-small',
    )
    submitter = NullJobSubmitter(namespace=settings.runner_namespace)
    record = create_run_record(request, workflow, settings, submitter, store)
    succeeded_status = record.status.model_copy(
        update={
            'status': 'succeeded',
            'updated_at': record.status.updated_at,
            'detail': 'Run completed successfully.',
        }
    )
    store.save_run(record.model_copy(update={'status': succeeded_status}))
    return store.get_run(record.run_id) or record


def build_approved_rerun_schedule(
    schedule_id: str,
    now: datetime,
    source_run: RunRecord,
) -> ScheduledOperationRecord:
    return ScheduledOperationRecord(
        schedule_id=schedule_id,
        created_at=now,
        updated_at=now,
        status='active',
        operation_type='approved-rerun',
        approval_tier='tier-2-approved-execution',
        owner='glasslab-operator',
        cron_expr=cron_expr_for(now),
        scope_filter={'workflow_id': source_run.workflow_id, 'source_run_id': source_run.run_id},
        source_run_id=source_run.run_id,
        workflow_id=source_run.workflow_id,
        allowed_dataset_uri=source_run.manifest.inputs.get('train_uri'),
        allowed_model_ids=list(source_run.manifest.requested_models),
        allowed_runner_image=source_run.manifest.runner_image,
        resource_profile=source_run.manifest.resource_profile,
    )


def test_digest_schedule_run_due_is_idempotent_and_auditable() -> None:
    settings = build_settings()
    registry = build_registry()
    store = InMemoryRunStore()
    now = datetime(2026, 3, 26, 14, 30, tzinfo=timezone.utc)

    workflow = registry.get_workflow('generic-tabular-benchmark')
    assert workflow is not None
    run_request = RunCreateRequest(
        workflow_id='generic-tabular-benchmark',
        objective='Create a run for digest schedule audit coverage.',
        inputs={
            'dataset_name': 'titanic',
            'train_uri': 's3://datasets/titanic/train.csv',
            'test_uri': 's3://datasets/titanic/test.csv',
            'target_column': 'Survived',
        },
        models=['logistic_regression'],
        resource_profile='cpu-small',
    )
    submitter = NullJobSubmitter(namespace=settings.runner_namespace)
    create_run_record(run_request, workflow, settings, submitter, store)

    schedule_request = DigestScheduleCreateRequest(
        cron_expr=cron_expr_for(now),
        digest_kind='daily-run-summary',
        scope_filter={'workflow_id': 'generic-tabular-benchmark'},
        owner='glasslab-operator',
    )
    schedule = build_digest_schedule(schedule_request, settings).model_copy(update={'schedule_id': 'digest-1'})
    store.save_schedule(schedule)

    first = execute_due_digest_schedules(store, now)
    assert len(first) == 1
    assert first[0].schedule_id == 'digest-1'
    assert first[0].result_status == 'ok'
    assert first[0].digest_payload['matching_run_count'] == 1
    assert first[0].digest_payload['workflow_ids'] == ['generic-tabular-benchmark']

    stored_schedule = store.get_schedule('digest-1')
    assert stored_schedule is not None
    assert stored_schedule.last_result_status == 'ok'
    assert stored_schedule.last_execution_at == now
    assert stored_schedule.last_result_detail == first[0].result_detail

    second = execute_due_digest_schedules(store, now)
    assert second == []

    executions = store.list_executions(schedule_id='digest-1')
    assert len(executions) == 1
    assert executions[0].execution_id == first[0].execution_id
    assert executions[0].result_detail == first[0].result_detail


def test_approved_rerun_schedule_run_due_is_idempotent_and_auditable() -> None:
    settings = build_settings()
    registry = build_registry()
    store = InMemoryRunStore()
    now = datetime(2026, 3, 26, 15, 45, tzinfo=timezone.utc)

    source_run = build_source_run(store, registry, settings)
    schedule = build_approved_rerun_schedule('rerun-1', now, source_run)
    store.save_schedule(schedule)

    first = execute_due_approved_rerun_schedules(store, now, settings, registry, NullJobSubmitter(namespace=settings.runner_namespace))
    assert len(first) == 1
    assert first[0].schedule_id == 'rerun-1'
    assert first[0].result_status == 'ok'
    assert first[0].produced_run_ids

    produced_run_id = first[0].produced_run_ids[0]
    rerun_record = store.get_run(produced_run_id)
    assert rerun_record is not None
    assert rerun_record.run_purpose == 'approved-rerun'
    assert rerun_record.run_priority == 'autonomous'

    stored_schedule = store.get_schedule('rerun-1')
    assert stored_schedule is not None
    assert stored_schedule.last_result_status == 'ok'
    assert stored_schedule.last_execution_at == first[0].finished_at
    assert stored_schedule.last_result_detail == first[0].result_detail

    repeat_now = stored_schedule.last_execution_at
    assert repeat_now is not None

    second = execute_due_approved_rerun_schedules(
        store,
        repeat_now,
        settings,
        registry,
        NullJobSubmitter(namespace=settings.runner_namespace),
    )
    assert second == []

    executions = store.list_executions(schedule_id='rerun-1')
    assert len(executions) == 1
    assert executions[0].execution_id == first[0].execution_id
    assert executions[0].produced_run_ids == [produced_run_id]


def test_digest_scheduling_helpers_cover_cron_matching_and_default_fields() -> None:
    settings = build_settings()
    now = datetime(2026, 3, 26, 14, 30, tzinfo=timezone.utc)
    request = DigestScheduleCreateRequest(
        cron_expr=f'  {cron_expr_for(now)}  ',
        digest_kind=' daily-run-summary ',
        scope_filter={'workflow_id': 'generic-tabular-benchmark'},
    )

    schedule = build_digest_schedule(request, settings)

    assert schedule.operation_type == 'digest'
    assert schedule.approval_tier == 'tier-1-read-only'
    assert schedule.owner == settings.default_submitted_by
    assert schedule.cron_expr == cron_expr_for(now)
    assert schedule.digest_kind == 'daily-run-summary'


def _generic_experiment_payload() -> dict:
    return {
        'objective': 'Verify idempotent submission across a simulated crash.',
        'experiment_type': 'gpu-training-job',
        'workload_id': 'generic-tabular-benchmark',
        'config_payload': {},
        'dataset_bindings': {},
        'budget': {'max_wallclock_minutes': 5},
    }


class CrashAfterSubmitSubmitter(NullJobSubmitter):
    """Submits the Job, then raises to simulate a crash before the receipt save."""

    def __init__(self, namespace: str) -> None:
        super().__init__(namespace)
        self.submit_count = 0

    def submit_run(self, manifest):
        self.submit_count += 1
        super().submit_run(manifest)
        raise RuntimeError('simulated crash after Job creation')


class CountingSubmitter(NullJobSubmitter):
    def __init__(self, namespace: str) -> None:
        super().__init__(namespace)
        self.submit_count = 0

    def submit_run(self, manifest):
        self.submit_count += 1
        return super().submit_run(manifest)


def test_crash_replay_submission_does_not_duplicate_job(tmp_path: Path) -> None:
    settings = build_settings()
    registry = build_registry()
    state_path = tmp_path / 'state.json'
    store = JsonFileRunStore(state_path)
    submitter = CrashAfterSubmitSubmitter(namespace=settings.runner_namespace)
    client = TestClient(
        create_app(settings=settings, registry=registry, store=store, submitter=submitter)
    )

    with pytest.raises(RuntimeError, match='simulated crash'):
        client.post(
            '/experiments/runs',
            json=_generic_experiment_payload(),
            headers={'Idempotency-Key': 'crash-key-1'},
        )
    assert submitter.submit_count == 1

    restarted_store = JsonFileRunStore(state_path)
    restarted_submitter = CountingSubmitter(namespace=settings.runner_namespace)
    restarted_client = TestClient(
        create_app(
            settings=settings,
            registry=registry,
            store=restarted_store,
            submitter=restarted_submitter,
        )
    )

    response = restarted_client.post(
        '/experiments/runs',
        json=_generic_experiment_payload(),
        headers={'Idempotency-Key': 'crash-key-1'},
    )
    assert response.status_code == 201
    assert len(restarted_store.list_runs()) == 1
    assert restarted_submitter.submit_count == 0


def test_mutating_posts_are_unique_under_concurrency() -> None:
    settings = build_settings()
    registry = build_registry()
    store = InMemoryRunStore()
    submitter = NullJobSubmitter(namespace=settings.runner_namespace)
    client_a = TestClient(
        create_app(settings=settings, registry=registry, store=store, submitter=submitter)
    )
    client_b = TestClient(
        create_app(settings=settings, registry=registry, store=store, submitter=submitter)
    )

    results: list = []
    barrier = threading.Barrier(2)

    def post(client) -> None:
        barrier.wait()
        results.append(
            client.post(
                '/experiments/runs',
                json=_generic_experiment_payload(),
                headers={'Idempotency-Key': 'concurrent-key-1'},
            )
        )

    threads = [
        threading.Thread(target=post, args=(client_a,)),
        threading.Thread(target=post, args=(client_b,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(response.status_code == 201 for response in results)
    run_ids = {response.json()['run_id'] for response in results}
    assert len(run_ids) == 1
    assert len(store.list_runs()) == 1
