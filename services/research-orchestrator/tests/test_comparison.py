"""Deterministic run-level cross-job comparison (Phase 2, across_jobs).

When a contract declares an ``across_jobs`` comparison requirement, every
compared methodology is its own job, so the run-level comparison joins the
per-job evaluator outputs. These tests build the report from in-memory,
digest-pinned artifacts (a stub reader) and lock: all-passing satisfaction, a
missing value, failing/missing evaluations, mechanical incomparability,
determinism, and that within_job/no-comparison runs produce no report.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from hashlib import sha256
from pathlib import Path

import pytest

from app.comparison import COMPARISON_SCHEMA_VERSION, build_comparison_report
from app.comparison_checks import (
    comparison_artifact_filename,
    comparison_input_fingerprint,
    evaluation_passed,
    is_authoritative_comparison,
    is_comparison_filename,
    latest_submission_jobs,
)
from app.contracts import compute_contract_digest
from app.evidence import EvidencePhase, build_evidence_snapshot
from app.preflight import MethodologyRequirement
from app.schemas import (
    ActionRecord,
    AgentName,
    ApprovalStatus,
    ArtifactRecord,
    EvaluationContractDescriptor,
    ExpandedJobSpec,
    JobRecord,
    JobStatus,
    PolicyClassification,
    ResolvedEvaluationContract,
    ResourceRequest,
    RunCreateRequest,
)

from conftest import RUNNER_IMAGE

DIGEST = 'a' * 64
_ARTIFACT_DIGEST = 'b' * 64
_CREATED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _requirement(
    *,
    comparison_scope: str = 'across_jobs',
    minimum_distinct_values: int = 2,
) -> dict:
    return {
        'requirement_id': 'model_families',
        'config_path': 'experiment_dimensions.model',
        'mode': 'comparison',
        'comparison_scope': comparison_scope,
        'minimum_distinct_values': minimum_distinct_values,
        'description': 'Compare model families across jobs.',
    }


def _contract(
    *,
    requirements: list[dict],
    primary_metric: str | None = 'rubric_score',
) -> ResolvedEvaluationContract:
    manifest: dict = {
        'primary_metric_direction': 'maximize',
        'methodology_requirements': requirements,
    }
    if primary_metric is not None:
        manifest['primary_metric'] = primary_metric
    descriptor = EvaluationContractDescriptor(
        contract_id='across-jobs-v1',
        version='1.0.0',
        manifest=manifest,
        execution_wrapper='run_contract.py',
        evaluation_entry_point='evaluator.py',
        expected_input_schema='input.schema.json',
        expected_output_schema='output.schema.json',
        required_artifacts=['metrics.json', 'evaluation.json'],
        resource_constraints=ResourceRequest(
            cpu=1, memory_gib=2, gpus=0, wallclock_minutes=30
        ),
    )
    return ResolvedEvaluationContract(
        descriptor=descriptor,
        digest=DIGEST,
        root_path='/tmp/across-jobs-v1',
    )


def _resources() -> ResourceRequest:
    return ResourceRequest(cpu=1, memory_gib=2, gpus=0, wallclock_minutes=5)


def _job(
    *,
    run_id: str,
    index: int,
    value: str,
    seed: int = 17,
    status: JobStatus = JobStatus.SUCCEEDED,
    overrides: dict | None = None,
    contract_digest: str = DIGEST,
    action_id: str = 'action-1',
    created_at: datetime = _CREATED_AT,
) -> JobRecord:
    variant_name = f'model-{index}'
    job_id = f'job-{index}'
    spec = ExpandedJobSpec(
        orchestrator_job_id=f'orchestrator-{index}',
        run_id=run_id,
        action_id=action_id,
        variant_name=variant_name,
        seed=seed,
        idempotency_key=f'key-{index}',
        base_config='configs/candidate.yaml',
        overrides=(
            overrides
            if overrides is not None
            else {'experiment_dimensions.model': value}
        ),
        comparison_scope='across_jobs',
        runner_image=RUNNER_IMAGE,
        resources=_resources(),
        required_artifacts=['metrics.json', 'evaluation.json'],
        evaluation_contract_id='across-jobs-v1',
        evaluation_contract_version='1.0.0',
        evaluation_contract_digest=contract_digest,
    )
    return JobRecord(
        job_id=job_id,
        run_id=run_id,
        action_id=action_id,
        kubernetes_namespace='glasslab-v2',
        status=status,
        requested_resources=_resources(),
        evaluation_contract_id='across-jobs-v1',
        evaluation_contract_version='1.0.0',
        evaluation_contract_digest=contract_digest,
        idempotency_key=f'key-{index}',
        variant_name=variant_name,
        seed=seed,
        spec=spec,
        created_at=created_at,
    )


def _evaluation_artifact(*, run_id: str, job_id: str) -> ArtifactRecord:
    return ArtifactRecord(
        run_id=run_id,
        job_id=job_id,
        type='evaluation',
        uri=f'artifacts/{job_id}/evaluation.json',
        sha256=_ARTIFACT_DIGEST,
    )


def _evaluation(
    *,
    metric: float = 0.9,
    integrity_pass: bool = True,
    contract_digest: str = DIGEST,
    comparison_key: str | None = 'protocol-v1',
    sealed: bool = False,
) -> dict:
    evaluation = {
        'integrity_pass': integrity_pass,
        'contract_id': 'across-jobs-v1',
        'contract_version': '1.0.0',
        'contract_digest': contract_digest,
    }
    if sealed:
        evaluation['metrics'] = {'rubric_score': metric}
    else:
        evaluation['rubric_score'] = metric
    if comparison_key is not None:
        evaluation['comparison_key'] = comparison_key
    return evaluation


def _reader(evaluations: dict[str, dict]):
    return lambda artifact: evaluations.get(artifact.job_id)


def _run(engine, contract: ResolvedEvaluationContract):
    run = engine.create_run(
        RunCreateRequest(objective='Compare two model families across jobs.')
    )
    return run.model_copy(
        update={
            'evaluation_contract_id': contract.descriptor.contract_id,
            'evaluation_contract_version': contract.descriptor.version,
            'evaluation_contract_digest': contract.digest,
        }
    )


def _report(*, run, contract, jobs, artifacts, evaluations):
    return build_comparison_report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        artifact_reader=_reader(evaluations),
    )


def test_all_values_with_passing_evaluations_are_satisfied(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {
        'job-1': _evaluation(metric=0.81),
        'job-2': _evaluation(metric=0.87),
    }

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['schema_version'] == COMPARISON_SCHEMA_VERSION
    assert report['comparison_scope'] == 'across_jobs'
    assert report['satisfied'] is True
    assert report['reasons'] == []
    requirement = report['requirements'][0]
    assert requirement['satisfied'] is True
    assert [value['value'] for value in requirement['values']] == [
        'forest',
        'logistic',
    ]
    metrics = {
        document['variant_name']: document['primary_metric']
        for value in requirement['values']
        for document in value['jobs']
    }
    assert metrics == {'model-1': 0.81, 'model-2': 0.87}


def test_sealed_metrics_object_resolves_primary_metric(
    orchestrator_bundle,
) -> None:
    # The sealed task-specific evaluator echoes the metric roots under the
    # output's `metrics` object with no flat top-level metric, so the primary
    # metric must resolve from there or every job reads as missing.
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {
        'job-1': _evaluation(metric=0.81, sealed=True),
        'job-2': _evaluation(metric=0.87, sealed=True),
    }

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is True
    assert report['reasons'] == []
    requirement = report['requirements'][0]
    assert requirement['satisfied'] is True
    metrics = {
        document['variant_name']: document['primary_metric']
        for value in requirement['values']
        for document in value['jobs']
    }
    assert metrics == {'model-1': 0.81, 'model-2': 0.87}


def test_flat_top_level_metric_still_satisfies(orchestrator_bundle) -> None:
    # Backward compatibility: an evaluator that flattens the primary metric to
    # the top level keeps resolving even though the sealed shape is preferred.
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {
        'job-1': _evaluation(metric=0.81),
        'job-2': _evaluation(metric=0.87),
    }

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is True
    metrics = {
        document['variant_name']: document['primary_metric']
        for value in report['requirements'][0]['values']
        for document in value['jobs']
    }
    assert metrics == {'model-1': 0.81, 'model-2': 0.87}


def test_named_primary_metric_dict_resolves_metric(orchestrator_bundle) -> None:
    # The evaluator may also carry only a `primary_metric` dict naming the
    # manifest key; its `value` is the resolved metric.
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {}
    for job_id, metric in (('job-1', 0.77), ('job-2', 0.83)):
        evaluation = _evaluation(metric=metric)
        evaluation.pop('rubric_score')
        evaluation['primary_metric'] = {
            'name': 'rubric_score',
            'value': metric,
        }
        evaluations[job_id] = evaluation

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is True
    metrics = {
        document['variant_name']: document['primary_metric']
        for value in report['requirements'][0]['values']
        for document in value['jobs']
    }
    assert metrics == {'model-1': 0.77, 'model-2': 0.83}


def test_null_sealed_metric_falls_through_to_primary_metric_dict(
    orchestrator_bundle,
) -> None:
    # A sealed evaluator reporting `metrics: {key: null}` alongside a valid
    # `primary_metric` dict must still resolve instead of reading as missing.
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {}
    for job_id, metric in (('job-1', 0.71), ('job-2', 0.79)):
        evaluation = _evaluation(metric=metric)
        evaluation.pop('rubric_score')
        evaluation['metrics'] = {'rubric_score': None}
        evaluation['primary_metric'] = {'name': 'rubric_score', 'value': metric}
        evaluations[job_id] = evaluation

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is True
    metrics = {
        document['variant_name']: document['primary_metric']
        for value in report['requirements'][0]['values']
        for document in value['jobs']
    }
    assert metrics == {'model-1': 0.71, 'model-2': 0.79}


def test_equal_comparison_keys_are_satisfied(orchestrator_bundle) -> None:
    # Equal per-job comparison_key digests are the evaluator's attestation that
    # the jobs are mechanically comparable; the requirement stays satisfied and
    # emits no comparability reason.
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {
        'job-1': _evaluation(comparison_key='shared-protocol-digest'),
        'job-2': _evaluation(comparison_key='shared-protocol-digest'),
    }

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is True
    assert report['reasons'] == []
    assert report['requirements'][0]['reasons'] == []


def test_mismatched_comparison_key_marks_requirement_unsatisfied(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {
        'job-1': _evaluation(comparison_key='protocol-a'),
        'job-2': _evaluation(comparison_key='protocol-b'),
    }

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is False
    assert report['requirements'][0]['satisfied'] is False
    assert any(
        'across_jobs evaluations do not share one comparison_key' in reason
        for reason in report['reasons']
    )
    # The reason must not embed either differing key (stable signature).
    assert all(
        'protocol-a' not in reason and 'protocol-b' not in reason
        for reason in report['reasons']
    )


def test_missing_comparison_key_marks_requirement_unsatisfied(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {
        'job-1': _evaluation(),
        'job-2': _evaluation(comparison_key=None),
    }

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is False
    assert report['requirements'][0]['satisfied'] is False
    assert any(
        'missing the required comparison_key' in reason
        for reason in report['reasons']
    )


def test_missing_compared_value_marks_comparison_unsatisfied(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(
        requirements=[_requirement(minimum_distinct_values=3)]
    )
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {
        'job-1': _evaluation(),
        'job-2': _evaluation(),
    }

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is False
    assert any(
        'requires at least 3 distinct compared value(s); found 2' in reason
        for reason in report['reasons']
    )
    assert report['requirements'][0]['satisfied'] is False


def test_failing_evaluation_marks_value_unsatisfied(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {
        'job-1': _evaluation(metric=0.81),
        'job-2': _evaluation(metric=0.87, integrity_pass=False),
    }

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is False
    forest = next(
        value
        for value in report['requirements'][0]['values']
        if value['value'] == 'forest'
    )
    assert forest['satisfied'] is False
    assert forest['jobs'][0]['integrity_pass'] is False
    assert any(
        'per-job evaluator did not pass' in reason
        for reason in forest['reasons']
    )


def test_missing_evaluation_artifact_marks_value_unsatisfied(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    # Only job-1 recorded an evaluation.json artifact.
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id='job-1')
    ]
    evaluations = {'job-1': _evaluation()}

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is False
    forest = next(
        value
        for value in report['requirements'][0]['values']
        if value['value'] == 'forest'
    )
    assert forest['satisfied'] is False
    assert forest['jobs'][0]['evaluation_uri'] is None
    assert any(
        'no evaluation.json artifact recorded' in reason
        for reason in forest['reasons']
    )


def test_comparability_requires_shared_contract_and_override_axis(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(
            run_id=run.run_id,
            index=2,
            value='forest',
            overrides={
                'experiment_dimensions.model': 'forest',
                'experiment_dimensions.encoding': 'ordinal',
            },
        ),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {'job-1': _evaluation(), 'job-2': _evaluation()}

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is False
    assert any(
        'differ on more than the compared config_path' in reason
        for reason in report['reasons']
    )


def test_report_is_deterministic_across_rebuilds(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {'job-1': _evaluation(metric=0.8), 'job-2': _evaluation()}

    first = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )
    second = _report(
        run=run,
        contract=contract,
        jobs=list(reversed(jobs)),
        artifacts=list(reversed(artifacts)),
        evaluations=evaluations,
    )

    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_latest_submission_jobs_returns_only_the_newest_wave() -> None:
    older = [
        _job(run_id='run-waves', index=1, value='logistic'),
        _job(run_id='run-waves', index=2, value='forest'),
    ]
    newer = [
        _job(
            run_id='run-waves',
            index=4,
            value='forest',
            action_id='action-2',
            created_at=_CREATED_AT + timedelta(minutes=5),
        ),
        _job(
            run_id='run-waves',
            index=3,
            value='logistic',
            action_id='action-2',
            created_at=_CREATED_AT + timedelta(minutes=5),
        ),
    ]

    assert latest_submission_jobs([]) == []
    assert [
        job.job_id for job in latest_submission_jobs(older + newer)
    ] == ['job-3', 'job-4']
    assert [
        job.job_id for job in latest_submission_jobs(newer + older)
    ] == ['job-3', 'job-4']


def test_comparison_adjudicates_only_the_latest_submission_wave(
    orchestrator_bundle,
) -> None:
    # The superseded wave's failed jobs have no evaluation.json; only the newest
    # approval action's wave may participate, so a re-submission can still
    # satisfy the comparison.
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(
            run_id=run.run_id,
            index=1,
            value='logistic',
            status=JobStatus.FAILED,
            action_id='action-old',
        ),
        _job(
            run_id=run.run_id,
            index=2,
            value='forest',
            status=JobStatus.FAILED,
            action_id='action-old',
        ),
        _job(
            run_id=run.run_id,
            index=3,
            value='logistic',
            action_id='action-new',
            created_at=_CREATED_AT + timedelta(minutes=5),
        ),
        _job(
            run_id=run.run_id,
            index=4,
            value='forest',
            action_id='action-new',
            created_at=_CREATED_AT + timedelta(minutes=5),
        ),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id='job-3'),
        _evaluation_artifact(run_id=run.run_id, job_id='job-4'),
    ]
    evaluations = {
        'job-3': _evaluation(metric=0.91),
        'job-4': _evaluation(metric=0.93),
    }

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is True
    assert report['reasons'] == []
    document_ids = [
        document['job_id']
        for requirement in report['requirements']
        for value in requirement['values']
        for document in value['jobs']
    ]
    assert sorted(document_ids) == ['job-3', 'job-4']


def test_failing_latest_wave_marks_comparison_unsatisfied(
    orchestrator_bundle,
) -> None:
    # The superseded wave's passing jobs must not mask the newest wave's
    # incomplete and failing evidence.
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(
            run_id=run.run_id,
            index=1,
            value='logistic',
            action_id='action-old',
        ),
        _job(
            run_id=run.run_id,
            index=2,
            value='forest',
            action_id='action-old',
        ),
        _job(
            run_id=run.run_id,
            index=3,
            value='logistic',
            action_id='action-new',
            created_at=_CREATED_AT + timedelta(minutes=5),
        ),
        _job(
            run_id=run.run_id,
            index=4,
            value='forest',
            status=JobStatus.FAILED,
            action_id='action-new',
            created_at=_CREATED_AT + timedelta(minutes=5),
        ),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id='job-1'),
        _evaluation_artifact(run_id=run.run_id, job_id='job-2'),
        _evaluation_artifact(run_id=run.run_id, job_id='job-3'),
    ]
    evaluations = {
        'job-1': _evaluation(),
        'job-2': _evaluation(),
        'job-3': _evaluation(),
    }

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is False
    document_ids = [
        document['job_id']
        for requirement in report['requirements']
        for value in requirement['values']
        for document in value['jobs']
    ]
    assert sorted(document_ids) == ['job-3', 'job-4']
    forest = next(
        value
        for value in report['requirements'][0]['values']
        if value['value'] == 'forest'
    )
    assert forest['satisfied'] is False
    assert forest['jobs'][0]['status'] == 'failed'


def test_comparison_input_fingerprint_tracks_the_latest_wave() -> None:
    wave_one = [
        _job(run_id='run-fingerprint', index=1, value='logistic'),
        _job(run_id='run-fingerprint', index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id='run-fingerprint', job_id=job.job_id)
        for job in wave_one
    ]
    baseline = comparison_input_fingerprint(wave_one, artifacts)

    # Stable for the same wave, regardless of input order.
    assert (
        comparison_input_fingerprint(
            list(reversed(wave_one)),
            list(reversed(artifacts)),
        )
        == baseline
    )

    # A changed wave membership changes the fingerprint.
    extended = wave_one + [
        _job(run_id='run-fingerprint', index=3, value='boosting')
    ]
    assert comparison_input_fingerprint(extended, artifacts) != baseline

    # A new submission wave replaces the scoped set entirely.
    new_wave = wave_one + [
        _job(
            run_id='run-fingerprint',
            index=3,
            value='logistic',
            action_id='action-2',
            created_at=_CREATED_AT + timedelta(minutes=5),
        ),
        _job(
            run_id='run-fingerprint',
            index=4,
            value='forest',
            action_id='action-2',
            created_at=_CREATED_AT + timedelta(minutes=5),
        ),
    ]
    new_baseline = comparison_input_fingerprint(new_wave, artifacts)
    assert new_baseline != baseline
    assert (
        comparison_input_fingerprint(list(reversed(new_wave)), artifacts)
        == new_baseline
    )


def test_within_job_contract_produces_no_report(orchestrator_bundle) -> None:
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(
        requirements=[_requirement(comparison_scope='within_job')]
    )
    run = _run(engine, contract)
    jobs = [_job(run_id=run.run_id, index=1, value='logistic')]

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=[],
        evaluations={},
    )

    assert report is None


def test_no_comparison_requirement_produces_no_report(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(
        requirements=[
            {
                'requirement_id': 'missing_data_strategy',
                'config_path': 'experiment_dimensions.missing_strategy',
                'mode': 'decision',
                'minimum_distinct_values': 1,
                'maximum_distinct_values': 1,
                'description': 'Choose one missing-data strategy.',
            }
        ]
    )
    run = _run(engine, contract)
    jobs = [_job(run_id=run.run_id, index=1, value='logistic')]

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=[],
        evaluations={},
    )

    assert report is None


def test_requirement_parser_tolerates_scope_default(
    orchestrator_bundle,
) -> None:
    # A legacy within_job requirement (no explicit scope) must never be treated
    # as across_jobs by the comparison builder.
    _, _, _, _, engine = orchestrator_bundle
    legacy = _requirement()
    legacy.pop('comparison_scope')
    contract = _contract(requirements=[legacy])
    run = _run(engine, contract)
    parsed = MethodologyRequirement.model_validate(legacy)
    assert parsed.comparison_scope == 'within_job'

    report = _report(
        run=run,
        contract=contract,
        jobs=[_job(run_id=run.run_id, index=1, value='logistic')],
        artifacts=[],
        evaluations={},
    )

    assert report is None


def _install_across_jobs_contract(tmp_path: Path, engine) -> ResolvedEvaluationContract:
    contract_id = 'across-jobs-v1'
    version = '1.0.0'
    root = tmp_path / 'trusted-contracts' / contract_id / version
    root.mkdir(parents=True)
    descriptor = {
        'contract_id': contract_id,
        'version': version,
        'evaluation_entry_point': 'evaluator.py',
        'execution_wrapper': 'run_contract.py',
        'expected_input_schema': 'input.schema.json',
        'expected_output_schema': 'output.schema.json',
        'required_artifacts': ['metrics.json', 'evaluation.json'],
        'resource_constraints': {
            'cpu': 1.0,
            'memory_gib': 2.0,
            'gpus': 0,
            'wallclock_minutes': 30,
        },
        'container_image_digest': None,
        'manifest': {
            'primary_metric': 'rubric_score',
            'primary_metric_direction': 'maximize',
            'methodology_requirements': [_requirement()],
        },
    }
    (root / 'contract.json').write_text(json.dumps(descriptor))
    for name in (
        'evaluator.py',
        'run_contract.py',
        'input.schema.json',
        'output.schema.json',
    ):
        (root / name).write_text('{}\n' if name.endswith('.json') else '# ok\n')
    (root / 'contract.sha256').write_text(compute_contract_digest(root))
    return engine.contracts.resolve(contract_id, version)


def _persist_job(store, run_id: str, job: JobRecord) -> None:
    store.save_action(
        ActionRecord(
            action_id=job.action_id,
            run_id=run_id,
            proposed_by=AgentName.BEAKER,
            type='submit_experiment_matrix',
            policy_classification=PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL,
            approval_status=ApprovalStatus.APPROVED,
            reason='comparison integration test',
            idempotency_key=f'idem-{job.action_id}',
        )
    )
    store.create_job_if_absent(job)


def _bind_across_jobs_run(engine, store, contract):
    run = engine.create_run(
        RunCreateRequest(objective='Persist the cross-job comparison artifact.')
    )
    return store.replace_run(
        run.model_copy(
            update={
                'evaluation_contract_id': contract.descriptor.contract_id,
                'evaluation_contract_version': contract.descriptor.version,
                'evaluation_contract_digest': contract.digest,
            }
        ),
        expected_version=run.version,
    )


def _persist_passing_job(
    settings,
    store,
    run,
    contract,
    *,
    index: int,
    value: str,
) -> JobRecord:
    job = _job(
        run_id=run.run_id,
        index=index,
        value=value,
        contract_digest=contract.digest,
    )
    _persist_job(store, run.run_id, job)
    content = json.dumps(
        {
            'rubric_score': 0.8 + index / 100,
            'integrity_pass': True,
            'contract_id': contract.descriptor.contract_id,
            'contract_version': contract.descriptor.version,
            'contract_digest': contract.digest,
            'comparison_key': 'protocol-v1',
        },
        sort_keys=True,
    ).encode()
    path = Path(settings.shared_mount_root) / f'artifacts/job-{index}/evaluation.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    store.save_artifact(
        ArtifactRecord(
            run_id=run.run_id,
            job_id=f'job-{index}',
            type='evaluation',
            uri=f'artifacts/job-{index}/evaluation.json',
            sha256=sha256(content).hexdigest(),
        )
    )
    return job


def _authoritative_comparison_artifacts(store, run_id: str) -> list[ArtifactRecord]:
    return [
        artifact
        for artifact in store.list_artifacts(run_id)
        if is_authoritative_comparison(artifact)
    ]


def test_engine_persists_and_verifies_comparison_artifact(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    contract = _install_across_jobs_contract(tmp_path, engine)
    run = _bind_across_jobs_run(engine, store, contract)
    _persist_passing_job(
        settings, store, run, contract, index=1, value='logistic'
    )
    _persist_passing_job(settings, store, run, contract, index=2, value='forest')

    engine._build_comparison_artifact(run.run_id)
    engine._build_comparison_artifact(run.run_id)

    authoritative = _authoritative_comparison_artifacts(store, run.run_id)
    assert len(authoritative) == 1
    assert authoritative[0].job_id is None
    document = json.loads(
        Path(authoritative[0].metadata['path']).read_text()
    )
    assert document['satisfied'] is True

    snapshot = build_evidence_snapshot(
        settings, store, run.run_id, phase=EvidencePhase.VERIFICATION
    )
    entry = next(
        item
        for item in snapshot['artifact_contents']
        if is_comparison_filename(
            Path(str(item['uri']).split('://', 1)[-1]).name
        )
    )
    assert entry['digest_verified'] is True
    assert entry['content']['satisfied'] is True


def test_job_comparison_artifact_does_not_suppress_authoritative_build(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    # F2: a workload/job artifact named comparison.json must never mask the
    # authoritative orchestrator build nor become the artifact the verifier is
    # told to check (identity is provenance, not filename).
    settings, store, _, _, engine = orchestrator_bundle
    contract = _install_across_jobs_contract(tmp_path, engine)
    run = _bind_across_jobs_run(engine, store, contract)
    _persist_passing_job(
        settings, store, run, contract, index=1, value='logistic'
    )
    _persist_passing_job(settings, store, run, contract, index=2, value='forest')
    store.save_artifact(
        ArtifactRecord(
            run_id=run.run_id,
            job_id='job-1',
            type='comparison',
            uri='artifacts/job-1/comparison.json',
            sha256='c' * 64,
        )
    )

    # With only a job artifact present, no authoritative comparison exists.
    assert engine._comparison_verification_note(run.run_id) == ''

    engine._build_comparison_artifact(run.run_id)

    authoritative = _authoritative_comparison_artifacts(store, run.run_id)
    assert len(authoritative) == 1
    assert authoritative[0].job_id is None
    assert engine._comparison_verification_note(run.run_id) != ''


def test_second_job_wave_rebuilds_comparison_artifact(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    contract = _install_across_jobs_contract(tmp_path, engine)
    run = _bind_across_jobs_run(engine, store, contract)
    _persist_passing_job(
        settings, store, run, contract, index=1, value='logistic'
    )
    _persist_passing_job(
        settings, store, run, contract, index=2, value='forest'
    )
    engine._build_comparison_artifact(run.run_id)
    first = _authoritative_comparison_artifacts(store, run.run_id)
    assert len(first) == 1

    # A second job wave changes the inputs, so the artifact is rebuilt.
    _persist_passing_job(
        settings, store, run, contract, index=3, value='boosting'
    )
    engine._build_comparison_artifact(run.run_id)
    rebuilt = _authoritative_comparison_artifacts(store, run.run_id)
    assert len(rebuilt) == 2
    fingerprints = {
        artifact.metadata.get('input_fingerprint') for artifact in rebuilt
    }
    assert len(fingerprints) == 2


def test_rebuild_versions_artifact_and_note_cites_newest(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    # R1: a second wave must not invalidate the superseded record's digest, and
    # the evidence snapshot plus the verification note must point at exactly the
    # newest authoritative comparison.
    settings, store, _, _, engine = orchestrator_bundle
    contract = _install_across_jobs_contract(tmp_path, engine)
    run = _bind_across_jobs_run(engine, store, contract)
    _persist_passing_job(
        settings, store, run, contract, index=1, value='logistic'
    )
    _persist_passing_job(
        settings, store, run, contract, index=2, value='forest'
    )
    engine._build_comparison_artifact(run.run_id)

    _persist_passing_job(
        settings, store, run, contract, index=3, value='boosting'
    )
    engine._build_comparison_artifact(run.run_id)

    authoritative = _authoritative_comparison_artifacts(store, run.run_id)
    assert len(authoritative) == 2
    older, newest = authoritative
    older_path = Path(older.metadata['path'])
    newest_path = Path(newest.metadata['path'])
    assert older_path != newest_path
    assert older_path.name == comparison_artifact_filename(
        older.metadata['input_fingerprint']
    )
    # No authoritative entry points at overwritten content.
    assert sha256(older_path.read_bytes()).hexdigest() == older.sha256
    assert sha256(newest_path.read_bytes()).hexdigest() == newest.sha256

    note = engine._comparison_verification_note(run.run_id)
    assert f'artifact://{newest.uri}' in note

    snapshot = build_evidence_snapshot(
        settings, store, run.run_id, phase=EvidencePhase.VERIFICATION
    )
    comparison_entries = [
        item
        for item in snapshot['artifact_contents']
        if is_comparison_filename(
            Path(str(item['uri']).split('://', 1)[-1]).name
        )
    ]
    assert len(comparison_entries) == 1
    assert comparison_entries[0]['uri'] == f'artifact://{newest.uri}'
    assert comparison_entries[0]['digest_verified'] is True
    assert 'content_unavailable' not in comparison_entries[0]


@pytest.mark.parametrize(
    'evaluation',
    [
        {'integrity_pass': 'false'},
        {'integrity_pass': 'no'},
        {'integrity_pass': 1},
        {'checks': {'x': 'false'}},
        {'checks': ['false']},
        {'checks': {}},
        {'checks': []},
        {},
    ],
)
def test_evaluation_passed_requires_real_booleans(evaluation: dict) -> None:
    assert evaluation_passed(evaluation) is False


def test_evaluation_passed_accepts_bool_verdicts() -> None:
    assert evaluation_passed({'integrity_pass': True}) is True
    assert evaluation_passed({'checks': {'x': True, 'y': True}}) is True
    assert evaluation_passed({'checks': [True, {'passed': True}]}) is True



def test_primary_metric_name_with_primary_value_resolves(
    orchestrator_bundle,
) -> None:
    # A sealed evaluator may name the metric in `primary_metric` (a const
    # string) and carry the value in `primary_value`, with no `metrics` object.
    _, _, _, _, engine = orchestrator_bundle
    contract = _contract(requirements=[_requirement()])
    run = _run(engine, contract)
    jobs = [
        _job(run_id=run.run_id, index=1, value='logistic'),
        _job(run_id=run.run_id, index=2, value='forest'),
    ]
    artifacts = [
        _evaluation_artifact(run_id=run.run_id, job_id=job.job_id)
        for job in jobs
    ]
    evaluations = {}
    for job_id, metric in (('job-1', 0.72), ('job-2', 0.78)):
        evaluations[job_id] = {
            'primary_metric': 'rubric_score',
            'primary_value': metric,
            'integrity_pass': True,
            'comparison_key': 'protocol-v1',
            'contract_id': 'across-jobs-v1',
            'contract_version': '1.0.0',
            'contract_digest': DIGEST,
        }

    report = _report(
        run=run,
        contract=contract,
        jobs=jobs,
        artifacts=artifacts,
        evaluations=evaluations,
    )

    assert report is not None
    assert report['satisfied'] is True
    metrics = {
        document['variant_name']: document['primary_metric']
        for value in report['requirements'][0]['values']
        for document in value['jobs']
    }
    assert metrics == {'model-1': 0.72, 'model-2': 0.78}
