"""Deterministic run-level cross-job comparison for across_jobs contracts.

When a contract declares an ``across_jobs`` comparison requirement, each
compared methodology runs as its own Kubernetes job, so no single evaluator
sees the whole comparison. This module joins the per-job evaluator outputs into
one deterministic, digest-pinned document recording which methodologies
completed, whether each passed its per-job evaluator, the recorded primary
metric, and the mechanical comparability invariants.

The builder reads only already-ingested artifacts through its ``artifact_reader``
(which is expected to verify the recorded digest); it never executes evaluator
code, never reads the shared mount directly, and never trusts a path the run did
not record. Output lists and keys are sorted so re-running yields a
byte-identical document.

The verdict, job-index, and comparability primitives live in
:mod:`app.comparison_checks`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .comparison_checks import (
    ArtifactReader,
    ComparisonContext,
    JobEvidence,
    build_value_index,
    comparability_reasons,
    evaluation_artifacts_by_job,
    job_document,
)
from .preflight import MethodologyRequirement, resolve_comparison_scope
from .schemas import (
    ArtifactRecord,
    JobRecord,
    ResolvedEvaluationContract,
    RunRecord,
)

COMPARISON_SCHEMA_VERSION = 'glasslab-cross-job-comparison-v1'
COMPARABILITY_NOTE = (
    'base_config file content is not stored on the job record, so mechanical '
    'comparability checks base_config path identity and the override axis only'
)


def _parse_requirements(
    contract: ResolvedEvaluationContract,
) -> list[MethodologyRequirement] | None:
    raw_requirements = contract.descriptor.manifest.get(
        'methodology_requirements',
        [],
    )
    try:
        return [
            MethodologyRequirement.model_validate(item)
            for item in raw_requirements
        ]
    except (ValueError, TypeError):
        return None


def _job_evidence(
    job: JobRecord,
    artifacts_by_job: Mapping[str, list[ArtifactRecord]],
    artifact_reader: ArtifactReader,
) -> JobEvidence:
    candidates = artifacts_by_job.get(job.job_id, [])
    artifact = candidates[0] if candidates else None
    evaluation = artifact_reader(artifact) if artifact is not None else None
    return JobEvidence(artifact=artifact, evaluation=evaluation)


def _value_document(
    value: str,
    jobs_for_value: Sequence[JobRecord],
    context: ComparisonContext,
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    reasons: list[str] = []
    passed = False
    for job in sorted(jobs_for_value, key=lambda item: item.job_id):
        evidence = context.evidence_by_job.get(
            job.job_id,
            JobEvidence(artifact=None, evaluation=None),
        )
        document, passing, reason = job_document(job, evidence, context)
        documents.append(document)
        if passing:
            passed = True
        elif reason is not None:
            reasons.append(f'job {job.job_id}: {reason}')
    if not passed:
        reasons.append(
            'no succeeded job for this value carries a passing evaluation'
        )
    return {
        'value': value,
        'jobs': documents,
        'satisfied': passed,
        'reasons': reasons,
    }


def _requirement_document(
    requirement: MethodologyRequirement,
    jobs: Sequence[JobRecord],
    context: ComparisonContext,
) -> dict[str, Any]:
    index = build_value_index(requirement, jobs)
    values = [
        _value_document(value, index.by_value[value], context)
        for value in sorted(index.by_value)
    ]
    reasons = comparability_reasons(requirement, index, context)
    satisfied = (
        bool(values)
        and not reasons
        and all(value['satisfied'] for value in values)
    )
    return {
        'requirement_id': requirement.requirement_id,
        'config_path': requirement.config_path,
        'satisfied': satisfied,
        'reasons': reasons,
        'values': values,
    }


def build_comparison_report(
    *,
    run: RunRecord,
    contract: ResolvedEvaluationContract,
    jobs: Sequence[JobRecord],
    artifacts: Sequence[ArtifactRecord],
    artifact_reader: ArtifactReader,
) -> dict[str, Any] | None:
    """Build the deterministic cross-job comparison document, or ``None``.

    Returns ``None`` for a contract with no ``across_jobs`` comparison
    requirement, so within-job and no-comparison runs never gain the artifact.
    """
    requirements = _parse_requirements(contract)
    if requirements is None:
        return None
    if resolve_comparison_scope(requirements) != 'across_jobs':
        return None
    across_jobs = sorted(
        (
            requirement
            for requirement in requirements
            if (
                requirement.mode == 'comparison'
                and requirement.comparison_scope == 'across_jobs'
            )
        ),
        key=lambda requirement: requirement.requirement_id,
    )
    if not across_jobs:
        return None
    primary_metric_key = contract.descriptor.manifest.get('primary_metric')
    if not isinstance(primary_metric_key, str) or not primary_metric_key:
        primary_metric_key = None
    artifacts_by_job = evaluation_artifacts_by_job(artifacts)
    context = ComparisonContext(
        run=run,
        primary_metric_key=primary_metric_key,
        evidence_by_job={
            job.job_id: _job_evidence(
                job,
                artifacts_by_job,
                artifact_reader,
            )
            for job in jobs
        },
    )
    requirement_documents = [
        _requirement_document(requirement, jobs, context)
        for requirement in across_jobs
    ]
    reasons: list[str] = []
    for document in requirement_documents:
        for reason in document['reasons']:
            reasons.append(f"{document['requirement_id']}: {reason}")
        for value in document['values']:
            if value['satisfied']:
                continue
            for reason in value['reasons']:
                reasons.append(
                    f"{document['requirement_id']} [{value['value']}]: {reason}"
                )
    return {
        'schema_version': COMPARISON_SCHEMA_VERSION,
        'run_id': run.run_id,
        'contract_id': contract.descriptor.contract_id,
        'contract_version': contract.descriptor.version,
        'contract_digest': contract.digest,
        'comparison_scope': 'across_jobs',
        'requirements': requirement_documents,
        'satisfied': all(
            document['satisfied'] for document in requirement_documents
        ),
        'reasons': reasons,
        'notes': [COMPARABILITY_NOTE],
    }
