"""RED tests for the compact research-agent evidence snapshot (issue #93).

The planned `app.evidence` module does not exist yet, so this file must fail
at import time with ModuleNotFoundError until `build_evidence_snapshot` and
`EvidencePhase` land. The tests lock the projected job summaries, per-phase
artifact scoping, sha256 deduplication, verbatim evaluator/metrics tier,
total-size bound, and determinism of the new module and the engine wrapper.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from app.evidence import EvidencePhase, build_evidence_snapshot
from app.schemas import (
    ActionRecord,
    AgentName,
    ApprovalStatus,
    ArtifactRecord,
    ExpandedJobSpec,
    JobRecord,
    JobStatus,
    PolicyClassification,
    ResourceRequest,
    RunCreateRequest,
)

from conftest import RUNNER_IMAGE


def _write_artifact(
    settings,
    store,
    run_id: str,
    *,
    type: str,
    uri: str,
    content: bytes,
) -> None:
    path = Path(settings.shared_mount_root) / uri
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    store.save_artifact(
        ArtifactRecord(
            run_id=run_id,
            type=type,
            uri=uri,
            sha256=sha256(content).hexdigest(),
        )
    )


def _job_record(store, run_id: str, *, job_id: str) -> JobRecord:
    # The jobs table enforces a foreign key on actions(action_id), so the
    # referenced action row must exist before the job can be stored.
    store.save_action(
        ActionRecord(
            action_id=f'action-{job_id}',
            run_id=run_id,
            proposed_by=AgentName.BEAKER,
            type='submit_experiment',
            policy_classification=PolicyClassification.HUMAN_APPROVAL,
            approval_status=ApprovalStatus.PENDING,
            reason='evidence snapshot test',
            idempotency_key=f'idem-action-{job_id}',
        )
    )
    resources = ResourceRequest(cpu=1, memory_gib=2, gpus=0, wallclock_minutes=5)
    spec = ExpandedJobSpec(
        orchestrator_job_id=job_id,
        run_id=run_id,
        action_id=f'action-{job_id}',
        variant_name='baseline',
        seed=17,
        idempotency_key=f'idem-{job_id}',
        base_config='configs/baseline.yaml',
        overrides={},
        runner_image=RUNNER_IMAGE,
        resources=resources,
        required_artifacts=[],
        evaluation_contract_id='example-research-v1',
        evaluation_contract_version='1.0.0',
        evaluation_contract_digest='0' * 64,
    )
    return JobRecord(
        job_id=job_id,
        run_id=run_id,
        action_id=spec.action_id,
        kubernetes_namespace='glasslab-v2',
        status=JobStatus.SUCCEEDED,
        requested_resources=resources,
        evaluation_contract_id=spec.evaluation_contract_id,
        evaluation_contract_version=spec.evaluation_contract_version,
        evaluation_contract_digest=spec.evaluation_contract_digest,
        idempotency_key=spec.idempotency_key,
        variant_name='baseline',
        seed=17,
        spec=spec,
    )


def test_snapshot_default_phase_preserves_existing_behavior(
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Preserve the existing evidence snapshot shape.')
    )
    log_content = (
        'unimportant prefix that should be truncated\n'
        'ValueError: feature names should match fit\n'
    ).encode()
    _write_artifact(
        settings,
        store,
        run.run_id,
        type='runner_log',
        uri='artifacts/job-1/runner.log',
        content=log_content,
    )
    _write_artifact(
        settings,
        store,
        run.run_id,
        type='status',
        uri='artifacts/job-1/status.json',
        content=b'{"status":"failed","exit_code":1}',
    )
    _write_artifact(
        settings,
        store,
        run.run_id,
        type='fairness_table',
        uri='artifacts/job-1/fairness.csv',
        content=b'group,accuracy\nA,0.75\n',
    )
    engine.settings.evidence_excerpt_max_bytes = 48

    contents = engine._evidence_snapshot(run.run_id)['artifact_contents']

    log = next(item for item in contents if item['type'] == 'runner_log')
    assert log['digest_verified'] is True
    assert log['truncated'] is True
    assert log['excerpt_position'] == 'tail'
    assert 'ValueError: feature names should match fit' in log['content']
    status = next(item for item in contents if item['type'] == 'status')
    assert status['content'] == {'status': 'failed', 'exit_code': 1}
    fairness = next(item for item in contents if item['type'] == 'fairness_table')
    assert fairness['digest_verified'] is True
    assert fairness['content'] == 'group,accuracy\nA,0.75\n'


def test_snapshot_analysis_jobs_projected_spec_trimmed(
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Project jobs without the full spec.')
    )
    store.create_job_if_absent(_job_record(store, run.run_id, job_id='job-1'))

    analysis = engine._evidence_snapshot(
        run.run_id, phase=EvidencePhase.ANALYSIS
    )['jobs']
    assert len(analysis) == 1
    projected = analysis[0]
    for key in ('job_id', 'status', 'exit_information', 'variant_name', 'seed'):
        assert key in projected
    assert 'spec' not in projected
    assert 'requested_resources' not in projected

    for phase in (EvidencePhase.VERIFICATION, EvidencePhase.REPORT):
        jobs = engine._evidence_snapshot(run.run_id, phase=phase)['jobs']
        assert len(jobs) == 1
        assert set(jobs[0].keys()) == {
            'job_id',
            'action_id',
            'status',
            'variant_name',
            'seed',
        }


@pytest.mark.parametrize(
    ('phase', 'expected_uris'),
    [
        (
            EvidencePhase.ANALYSIS,
            {
                'artifact://artifacts/job-1/runner.log',
                'artifact://artifacts/job-1/status.json',
                'artifact://artifacts/job-1/evaluation.json',
                'artifact://artifacts/job-1/metrics.json',
                'artifact://artifacts/job-1/metrics.csv',
                'artifact://artifacts/job-1/fairness.csv',
            },
        ),
        (
            EvidencePhase.VERIFICATION,
            {
                'artifact://artifacts/job-1/status.json',
                'artifact://artifacts/job-1/evaluation.json',
                'artifact://artifacts/job-1/metrics.json',
                'artifact://artifacts/job-1/report.md',
            },
        ),
        (
            EvidencePhase.REPORT,
            {
                'artifact://artifacts/job-1/evaluation.json',
                'artifact://artifacts/job-1/metrics.json',
            },
        ),
    ],
)
def test_snapshot_phase_scoping(
    orchestrator_bundle,
    phase: EvidencePhase,
    expected_uris: set[str],
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Scope artifact contents by evidence phase.')
    )
    artifacts = {
        'runner.log': ('runner_log', b'log line\n'),
        'status.json': ('status', b'{"status":"complete"}'),
        'evaluation.json': ('evaluation', b'{"score":0.9}'),
        'metrics.json': ('metrics', b'{"loss":0.1}'),
        'metrics.csv': ('metrics_table', b'metric,value\nloss,0.1\n'),
        'fairness.csv': ('fairness_table', b'group,accuracy\nA,0.75\n'),
        'report.md': ('report', b'# Report\n'),
    }
    for name, (type, content) in artifacts.items():
        _write_artifact(
            settings,
            store,
            run.run_id,
            type=type,
            uri=f'artifacts/job-1/{name}',
            content=content,
        )

    snapshot = build_evidence_snapshot(settings, store, run.run_id, phase=phase)

    assert {item['uri'] for item in snapshot['artifact_contents']} == expected_uris


def test_snapshot_dedupes_identical_content_by_sha256(
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Deduplicate identical artifact content.')
    )
    content = b'{"status":"complete"}'
    now = datetime.now(UTC)
    for index, uri in enumerate(
        ['artifacts/job-1/status.json', 'artifacts/job-2/status.json']
    ):
        path = Path(settings.shared_mount_root) / uri
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        store.save_artifact(
            ArtifactRecord(
                run_id=run.run_id,
                type='status',
                uri=uri,
                sha256=sha256(content).hexdigest(),
                created_at=now + timedelta(seconds=index),
            )
        )

    contents = build_evidence_snapshot(settings, store, run.run_id)[
        'artifact_contents'
    ]
    assert [item['uri'] for item in contents] == [
        'artifact://artifacts/job-1/status.json',
        'artifact://artifacts/job-2/status.json',
    ]
    first, second = contents
    assert first['content'] == {'status': 'complete'}
    assert second['duplicate_of'] == first['uri']
    assert 'content' not in second
    assert len([item for item in contents if 'content' in item]) == 1

    metrics_path = Path(settings.shared_mount_root) / 'artifacts/job-3/metrics.json'
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_bytes(content)
    store.save_artifact(
        ArtifactRecord(
            run_id=run.run_id,
            type='metrics',
            uri='artifacts/job-3/metrics.json',
            sha256=sha256(content).hexdigest(),
            created_at=now + timedelta(seconds=2),
        )
    )

    contents = build_evidence_snapshot(settings, store, run.run_id)[
        'artifact_contents'
    ]
    status_items = [item for item in contents if item['type'] == 'status']
    metrics_items = [item for item in contents if item['type'] == 'metrics']
    assert len(status_items) == 2
    assert status_items[0]['content'] == {'status': 'complete'}
    assert status_items[1]['duplicate_of'] == status_items[0]['uri']
    assert metrics_items[0]['content'] == {'status': 'complete'}


def test_snapshot_verbatim_evaluator_and_metrics(orchestrator_bundle) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Keep evaluator and metrics JSON verbatim.')
    )
    engine.settings.evidence_excerpt_max_bytes = 32 * 1024
    engine.settings.evidence_verbatim_max_bytes = 64 * 1024
    evaluation_payload = {'metric': 'accuracy', 'detail': 'x' * (40 * 1024)}
    metrics_payload = {'metric': 'loss', 'detail': 'y' * (44 * 1024)}
    _write_artifact(
        settings,
        store,
        run.run_id,
        type='evaluation',
        uri='artifacts/job-1/evaluation.json',
        content=json.dumps(evaluation_payload).encode(),
    )
    _write_artifact(
        settings,
        store,
        run.run_id,
        type='metrics',
        uri='artifacts/job-1/metrics.json',
        content=json.dumps(metrics_payload).encode(),
    )

    snapshot = build_evidence_snapshot(settings, store, run.run_id)
    evaluation = next(
        item
        for item in snapshot['artifact_contents']
        if item['uri'] == 'artifact://artifacts/job-1/evaluation.json'
    )
    assert evaluation['content'] == evaluation_payload
    assert evaluation['truncated'] is False
    assert evaluation['digest_verified'] is True
    metrics = next(
        item
        for item in snapshot['artifact_contents']
        if item['uri'] == 'artifact://artifacts/job-1/metrics.json'
    )
    assert metrics['content'] == metrics_payload
    assert metrics['truncated'] is False
    assert metrics['digest_verified'] is True

    oversized = {'detail': 'z' * (80 * 1024)}
    _write_artifact(
        settings,
        store,
        run.run_id,
        type='evaluation',
        uri='artifacts/job-2/evaluation.json',
        content=json.dumps(oversized).encode(),
    )
    snapshot = build_evidence_snapshot(settings, store, run.run_id)
    omitted = next(
        item
        for item in snapshot['artifact_contents']
        if item['uri'] == 'artifact://artifacts/job-2/evaluation.json'
    )
    assert 'content_omitted' in omitted
    assert 'content' not in omitted
    assert isinstance(omitted['content_omitted'], str)
    assert omitted['content_omitted'].startswith('artifact://')


def test_snapshot_total_size_bounded_and_trim_note(orchestrator_bundle) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Bound the serialized evidence snapshot.')
    )
    engine.settings.evidence_snapshot_max_bytes = 4096
    engine.settings.evidence_excerpt_max_bytes = 1024 * 1024
    _write_artifact(
        settings,
        store,
        run.run_id,
        type='runner_log',
        uri='artifacts/job-1/runner.log',
        content=b'log line\n' * 6000,
    )

    snapshot = engine._evidence_snapshot(run.run_id)
    serialized = json.dumps(snapshot, sort_keys=True)
    assert len(serialized) <= 4096
    assert 'truncation' in snapshot
    assert 'artifact://artifacts/job-1/runner.log' in json.dumps(
        snapshot['truncation'], sort_keys=True
    )
    assert (
        json.dumps(engine._evidence_snapshot(run.run_id), sort_keys=True)
        == serialized
    )


def test_snapshot_size_bound_includes_truncation_note(orchestrator_bundle) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='The truncation note counts toward the bound.')
    )
    engine.settings.evidence_snapshot_max_bytes = 4096
    engine.settings.evidence_excerpt_max_bytes = 1024 * 1024
    _write_artifact(
        settings,
        store,
        run.run_id,
        type='runner_log',
        uri='artifacts/job-1/runner.log',
        content=b'log line\n' * 6000,
    )
    # The retained status.json is padded so the post-drop snapshot lands within
    # a few hundred bytes of the cap: a truncation note (~200 bytes) appended
    # after the budget loop would push the final serialized size over the cap.
    _write_artifact(
        settings,
        store,
        run.run_id,
        type='status',
        uri='artifacts/job-1/status.json',
        content=b'{"status":"complete","pad":"' + b'x' * 2700 + b'"}',
    )

    snapshot = engine._evidence_snapshot(run.run_id)
    assert len(json.dumps(snapshot, sort_keys=True)) <= 4096
    assert 'truncation' in snapshot
    assert snapshot['truncation']['omitted_count'] >= 1


def test_snapshot_deterministic_across_calls(orchestrator_bundle) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Produce byte-identical evidence snapshots.')
    )
    _write_artifact(
        settings,
        store,
        run.run_id,
        type='status',
        uri='artifacts/job-1/status.json',
        content=b'{"status":"complete"}',
    )
    store.create_job_if_absent(_job_record(store, run.run_id, job_id='job-1'))

    first = build_evidence_snapshot(settings, store, run.run_id)
    second = build_evidence_snapshot(settings, store, run.run_id)
    wrapped = engine._evidence_snapshot(run.run_id)
    assert first == second
    assert wrapped == first
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert json.dumps(wrapped, sort_keys=True) == json.dumps(first, sort_keys=True)