"""Tests for on-disk run artifact loading, status resolution, and terminal bundle verification.

Covers loading status/artifacts/logs from the artifacts mount, preferring
on-disk terminal state over in-memory job-submission status, validating
complete terminal bundles, and rejecting symlink artifacts.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.job_submission import KubernetesJobSubmitter, LiveStatusUnavailableError
from app.run_artifacts import (
    artifact_run_dir,
    build_artifacts_from_directory,
    load_artifacts_from_disk,
    load_logs_from_disk,
    load_status_from_disk,
    load_terminal_bundle,
    parse_log_line,
    resolve_run_status,
)
from app.schemas import JobSubmissionReceipt, RunRecord
from services.common.schemas import RunManifest, RunStatus


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        registry_dir=str(tmp_path),
        artifacts_mount_path=str(tmp_path / 'artifacts'),
    )


def build_run_record(run_id: str, status: RunStatus) -> RunRecord:
    now = datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc)
    manifest = RunManifest(
        run_id=run_id,
        workflow_id='generic-tabular-benchmark',
        workflow_family='tabular-benchmark',
        display_name='Tabular Benchmark',
        objective='Test artifact helpers.',
        submitted_by='tester',
        submitted_at=now,
        run_priority='user',
        inputs={'dataset_name': 'titanic'},
        requested_models=['logistic_regression'],
        resource_profile='cpu-small',
        resource_requests={},
        resource_limits={},
        node_selector={},
        runner_image='busybox:latest',
        evaluator_type='none',
        approval_tier='tier-1-read-only',
        expected_artifacts={'required': ['status.json'], 'optional': ['logs/runner.log']},
    )
    return RunRecord(
        run_id=run_id,
        workflow_id='generic-tabular-benchmark',
        created_at=now,
        updated_at=now,
        manifest=manifest,
        status=status,
        job_submission=JobSubmissionReceipt(
            job_name='job',
            namespace='default',
            accepted_at=now,
            status='accepted',
            detail='ok',
        ),
    )


def test_load_status_artifacts_and_logs_from_disk(tmp_path) -> None:
    settings = build_settings(tmp_path)
    run_id = 'run-123'
    run_dir = artifact_run_dir(settings, run_id)
    (run_dir / 'logs').mkdir(parents=True)
    (run_dir / 'status.json').write_text(
        '{"run_id":"run-123","status":"succeeded","updated_at":"2026-03-26T12:00:00Z","detail":"done"}'
    )
    (run_dir / 'artifacts_index.json').write_text(
        '{"run_id":"run-123","artifacts":[{"name":"status.json","path":"artifacts/run-123/status.json","media_type":"application/json","required":true}]}'
    )
    (run_dir / 'logs' / 'runner.log').write_text(
        '2026-03-26 12:00:00,123 INFO glasslab.runner completed run\n'
    )

    status = load_status_from_disk(settings, run_id)
    assert status is not None
    assert status.status == 'succeeded'

    artifacts = load_artifacts_from_disk(settings, run_id)
    assert artifacts is not None
    assert artifacts.artifacts[0].name == 'status.json'

    fallback_artifacts = build_artifacts_from_directory(settings, run_id)
    assert fallback_artifacts is not None
    assert any(entry.name == 'logs/' for entry in fallback_artifacts.artifacts)
    assert any(entry.name == 'logs/runner.log' for entry in fallback_artifacts.artifacts)

    logs = load_logs_from_disk(settings, run_id)
    assert len(logs) == 1
    assert logs[0].message == 'completed run'
    assert logs[0].payload == {'logger': 'glasslab.runner'}


def test_parse_log_line_and_resolve_run_status_prefers_disk(tmp_path) -> None:
    settings = build_settings(tmp_path)
    run_id = 'run-456'
    run_dir = artifact_run_dir(settings, run_id)
    run_dir.mkdir(parents=True)
    (run_dir / 'status.json').write_text(
        '{"run_id":"run-456","status":"succeeded","updated_at":"2026-03-26T12:00:00Z","detail":"done"}'
    )

    parsed = parse_log_line('2026-03-26 12:00:00,123 INFO glasslab.runner completed run')
    assert parsed.level == 'INFO'
    assert parsed.message == 'completed run'

    class FakeSubmitter:
        def get_live_status(self, record):
            return RunStatus(
                run_id=record.run_id,
                status='running',
                updated_at=datetime(2026, 3, 26, 12, 1, tzinfo=timezone.utc),
                detail='live',
            )

    record = build_run_record(
        run_id,
        RunStatus(
            run_id=run_id,
            status='queued',
            updated_at=datetime(2026, 3, 26, 11, 59, tzinfo=timezone.utc),
            detail='queued',
        ),
    )
    resolved = resolve_run_status(record, settings, FakeSubmitter())
    assert resolved.status == 'succeeded'


def test_load_terminal_bundle_requires_real_complete_artifacts(tmp_path) -> None:
    settings = build_settings(tmp_path)
    run_id = 'run-bundle'
    record = build_run_record(
        run_id,
        RunStatus(
            run_id=run_id,
            status='running',
            updated_at=datetime(2026, 3, 26, 11, 59, tzinfo=timezone.utc),
        ),
    )
    required = [
        'run_manifest.json',
        'config.json',
        'metrics.json',
        'artifacts_index.json',
        'report.md',
        'status.json',
        'logs/',
    ]
    record = record.model_copy(
        update={
            'manifest': record.manifest.model_copy(
                update={'expected_artifacts': {'required': required, 'optional': []}}
            )
        }
    )
    run_dir = artifact_run_dir(settings, run_id)
    (run_dir / 'logs').mkdir(parents=True)
    for name in ['run_manifest.json', 'config.json', 'artifacts_index.json']:
        (run_dir / name).write_text('{}')
    (run_dir / 'metrics.json').write_text('{"rubric_score": 92}')
    (run_dir / 'report.md').write_text('# Report\n')
    (run_dir / 'status.json').write_text('{"status":"succeeded"}')
    (run_dir / 'logs' / 'runner.log').write_text('complete\n')

    status, metrics, refs, artifacts = load_terminal_bundle(settings, record)

    assert status.status == 'succeeded'
    assert metrics['rubric_score'] == 92
    assert refs['report.md'] == f'artifacts/{run_id}/report.md'
    report_entry = next(
        entry for entry in artifacts.artifacts if entry.name == 'report.md'
    )
    assert report_entry.sha256 is not None
    assert len(report_entry.sha256) == 64
    assert any(entry.name == 'artifacts_index.json' for entry in artifacts.artifacts)

    (run_dir / 'report.md').unlink()
    try:
        load_terminal_bundle(settings, record)
    except ValueError as exc:
        assert 'report.md' in str(exc)
    else:
        raise AssertionError('missing required report must reject successful ingestion')


def test_terminal_bundle_rejects_symlink_artifact(tmp_path) -> None:
    settings = build_settings(tmp_path)
    run_id = 'run-symlink'
    record = build_run_record(
        run_id,
        RunStatus(
            run_id=run_id,
            status='running',
            updated_at=datetime(2026, 3, 26, 11, 59, tzinfo=timezone.utc),
        ),
    )
    record = record.model_copy(
        update={
            'manifest': record.manifest.model_copy(
                update={
                    'expected_artifacts': {
                        'required': ['status.json', 'report.md'],
                        'optional': [],
                    }
                }
            )
        }
    )
    run_dir = artifact_run_dir(settings, run_id)
    run_dir.mkdir(parents=True)
    (run_dir / 'status.json').write_text('{"status":"succeeded"}')
    (run_dir / 'report.md').symlink_to('/etc/hosts')

    try:
        load_terminal_bundle(settings, record)
    except ValueError as exc:
        assert 'report.md' in str(exc)
    else:
        raise AssertionError('symlink artifact must reject successful ingestion')


class _UnavailableSubmitter:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def get_live_status(self, record):
        raise self.exc


def _queued_record(run_id: str) -> RunRecord:
    return build_run_record(
        run_id,
        RunStatus(
            run_id=run_id,
            status='queued',
            updated_at=datetime(2026, 3, 26, 11, 59, tzinfo=timezone.utc),
            detail='accepted, awaiting scheduling',
        ),
    )


def test_resolve_run_status_degrades_on_live_status_unavailable(tmp_path) -> None:
    settings = build_settings(tmp_path)
    record = _queued_record('run-degrade')
    submitter = _UnavailableSubmitter(
        LiveStatusUnavailableError('Kubernetes API error during live status lookup')
    )

    resolved = resolve_run_status(record, settings, submitter)

    assert resolved.status == 'queued'
    assert resolved.run_id == 'run-degrade'
    assert 'accepted, awaiting scheduling' in resolved.detail
    assert 'Live Kubernetes status unavailable' in resolved.detail
    assert 'showing durable stored status' in resolved.detail


def test_resolve_run_status_unexpected_exception_propagates(tmp_path) -> None:
    settings = build_settings(tmp_path)
    record = _queued_record('run-program-error')
    submitter = _UnavailableSubmitter(RuntimeError('unexpected bug'))

    with pytest.raises(RuntimeError):
        resolve_run_status(record, settings, submitter)


def test_resolve_run_status_disk_terminal_authoritative_during_outage(tmp_path) -> None:
    settings = build_settings(tmp_path)
    run_id = 'run-disk-authoritative'
    run_dir = artifact_run_dir(settings, run_id)
    run_dir.mkdir(parents=True)
    (run_dir / 'status.json').write_text(
        '{"run_id":"run-disk-authoritative","status":"succeeded",'
        '"updated_at":"2026-03-26T12:00:00Z","detail":"done"}'
    )
    record = _queued_record(run_id)
    submitter = _UnavailableSubmitter(
        LiveStatusUnavailableError('Kubernetes transport failure during live status lookup')
    )

    resolved = resolve_run_status(record, settings, submitter)

    assert resolved.status == 'succeeded'
    assert 'Live Kubernetes status unavailable' not in (resolved.detail or '')


def test_get_live_status_maps_expected_exceptions_to_unavailable() -> None:
    import socket
    import ssl
    import urllib3
    from kubernetes.client.exceptions import ApiException

    submitter = KubernetesJobSubmitter.__new__(KubernetesJobSubmitter)
    submitter.api_exception = ApiException
    record = _queued_record('run-live-status')

    cases = [
        ApiException(status=503, reason='Service Unavailable'),
        socket.gaierror('Name or service not known'),
        ssl.SSLError(1, 'TLS handshake failure'),
        urllib3.exceptions.MaxRetryError(
            urllib3.connectionpool.HTTPConnectionPool(host='k8s', port=443),
            '/apis/batch/v1/namespaces/default/jobs/job',
        ),
        urllib3.exceptions.NewConnectionError(
            urllib3.connectionpool.HTTPConnectionPool(host='k8s', port=443),
            'Connection refused',
        ),
        urllib3.exceptions.SSLError('TLS handshake failure'),
        urllib3.exceptions.ConnectTimeoutError(
            urllib3.connectionpool.HTTPConnectionPool(host='k8s', port=443),
            'connection timed out',
        ),
        urllib3.exceptions.ReadTimeoutError(
            urllib3.connectionpool.HTTPConnectionPool(host='k8s', port=443),
            '/apis/batch/v1/namespaces/default/jobs/job',
            'read timed out',
        ),
    ]

    for exc in cases:
        class _FailingBatchApi:
            def read_namespaced_job(self, **kwargs):
                raise exc

        submitter.batch_api = _FailingBatchApi()
        with pytest.raises(LiveStatusUnavailableError):
            submitter.get_live_status(record)


def test_get_live_status_lets_programming_errors_propagate() -> None:
    from kubernetes.client.exceptions import ApiException

    submitter = KubernetesJobSubmitter.__new__(KubernetesJobSubmitter)
    submitter.api_exception = ApiException

    class _ExplodingBatchApi:
        def read_namespaced_job(self, **kwargs):
            raise RuntimeError('unexpected bug')

    submitter.batch_api = _ExplodingBatchApi()
    with pytest.raises(RuntimeError):
        submitter.get_live_status(_queued_record('run-live-program-error'))


def test_cancel_run_deletes_kubernetes_job_with_foreground_propagation() -> None:
    class _ApiException(Exception):
        status = None

    class _BatchApi:
        def __init__(self) -> None:
            self.calls = []

        def delete_namespaced_job(self, **kwargs):
            self.calls.append(kwargs)

    submitter = KubernetesJobSubmitter.__new__(KubernetesJobSubmitter)
    submitter.api_exception = _ApiException
    submitter.batch_api = _BatchApi()
    record = _queued_record('run-cancel')

    submitter.cancel_run(record)

    assert submitter.batch_api.calls == [
        {
            'name': record.job_submission.job_name,
            'namespace': record.job_submission.namespace,
            'propagation_policy': 'Foreground',
        }
    ]


def test_cancel_run_treats_missing_kubernetes_job_as_already_cancelled() -> None:
    class _ApiException(Exception):
        def __init__(self, status):
            self.status = status

    class _BatchApi:
        def delete_namespaced_job(self, **kwargs):
            raise _ApiException(404)

    submitter = KubernetesJobSubmitter.__new__(KubernetesJobSubmitter)
    submitter.api_exception = _ApiException
    submitter.batch_api = _BatchApi()

    submitter.cancel_run(_queued_record('run-already-gone'))


def test_cancel_run_maps_kubernetes_api_failure_to_unavailable() -> None:
    class _ApiException(Exception):
        def __init__(self, status):
            self.status = status

    class _BatchApi:
        def delete_namespaced_job(self, **kwargs):
            raise _ApiException(503)

    submitter = KubernetesJobSubmitter.__new__(KubernetesJobSubmitter)
    submitter.api_exception = _ApiException
    submitter.batch_api = _BatchApi()

    with pytest.raises(LiveStatusUnavailableError):
        submitter.cancel_run(_queued_record('run-cancel-api-error'))
