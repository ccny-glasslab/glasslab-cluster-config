"""Verdict, job-index, and mechanical-comparability checks for cross-job runs.

Split from :mod:`app.comparison` so the run-level assembly stays small. These
helpers are pure: they read typed records and evaluator dicts, never the shared
mount, and never execute evaluator code. List and key ordering is deterministic.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .preflight import MethodologyRequirement
from .schemas import (
    ArtifactRecord,
    JobRecord,
    JobStatus,
    RunRecord,
)

EVALUATION_FILENAME = 'evaluation.json'
_CHECK_PASS_KEYS = ('passed', 'pass', 'ok', 'success')

ArtifactReader = Callable[[ArtifactRecord], dict[str, Any] | None]


@dataclass(frozen=True, slots=True)
class JobEvidence:
    artifact: ArtifactRecord | None
    evaluation: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class ComparisonContext:
    run: RunRecord
    primary_metric_key: str | None
    evidence_by_job: Mapping[str, JobEvidence]


@dataclass(frozen=True, slots=True)
class ValueIndex:
    participating: tuple[JobRecord, ...]
    by_value: dict[str, list[JobRecord]]


def check_passed(check: Any) -> bool:
    if isinstance(check, Mapping):
        for key in _CHECK_PASS_KEYS:
            if key in check:
                return bool(check[key])
        return False
    return bool(check)


def evaluation_passed(evaluation: Mapping[str, Any]) -> bool:
    # The evaluator verdict is either an explicit `integrity_pass` flag or a
    # `checks` collection that must be entirely true; anything else is not a
    # pass.
    if 'integrity_pass' in evaluation:
        return bool(evaluation['integrity_pass'])
    checks = evaluation.get('checks')
    if isinstance(checks, Mapping):
        return bool(checks) and all(bool(value) for value in checks.values())
    if isinstance(checks, (list, tuple)):
        return bool(checks) and all(check_passed(value) for value in checks)
    return False


def evaluation_contract_matches(
    evaluation: Mapping[str, Any],
    run: RunRecord,
) -> bool:
    # An evaluation that names its contract must name the run's bound contract;
    # evaluators that do not embed the binding are still gated by the job
    # record's own contract fields (checked among the comparability invariants).
    for key, expected in (
        ('contract_id', run.evaluation_contract_id),
        ('contract_version', run.evaluation_contract_version),
        ('contract_digest', run.evaluation_contract_digest),
    ):
        if key in evaluation and evaluation[key] != expected:
            return False
    return True


def scalar_override(job: JobRecord, config_path: str) -> tuple[bool, Any]:
    if config_path not in job.spec.overrides:
        return False, None
    value = job.spec.overrides[config_path]
    if isinstance(value, (str, int, float, bool)):
        return True, value
    return False, None


def evaluation_artifacts_by_job(
    artifacts: Sequence[ArtifactRecord],
) -> dict[str, list[ArtifactRecord]]:
    by_job: dict[str, list[ArtifactRecord]] = {}
    for artifact in artifacts:
        if artifact.job_id is None:
            continue
        if Path(artifact.uri).name != EVALUATION_FILENAME:
            continue
        by_job.setdefault(artifact.job_id, []).append(artifact)
    for entries in by_job.values():
        entries.sort(key=lambda artifact: (artifact.uri, artifact.sha256))
    return by_job


def build_value_index(
    requirement: MethodologyRequirement,
    jobs: Sequence[JobRecord],
) -> ValueIndex:
    by_value: dict[str, list[JobRecord]] = {}
    participating: list[JobRecord] = []
    for job in jobs:
        present, value = scalar_override(job, requirement.config_path)
        if not present:
            continue
        participating.append(job)
        by_value.setdefault(str(value), []).append(job)
    return ValueIndex(
        participating=tuple(sorted(participating, key=lambda job: job.job_id)),
        by_value=by_value,
    )


def comparison_key_reasons(
    index: ValueIndex,
    context: ComparisonContext,
) -> list[str]:
    # across_jobs comparability is the evaluator's attestation: each per-job
    # evaluator emits a deterministic digest of the protocol constants (folds,
    # data split, metric definitions) that must be shared across jobs. Reasons
    # are value-free so an identical rejection keeps a stable signature.
    if not index.participating:
        return []
    keys: list[str] = []
    missing = False
    for job in index.participating:
        evidence = context.evidence_by_job.get(job.job_id)
        evaluation = evidence.evaluation if evidence is not None else None
        key = (
            evaluation.get('comparison_key')
            if isinstance(evaluation, Mapping)
            else None
        )
        if not isinstance(key, str) or not key.strip():
            missing = True
        else:
            keys.append(key)
    if missing:
        return [
            'an across_jobs evaluation is missing the required comparison_key'
        ]
    if len(set(keys)) > 1:
        return ['across_jobs evaluations do not share one comparison_key']
    return []


def comparability_reasons(
    requirement: MethodologyRequirement,
    index: ValueIndex,
    context: ComparisonContext,
) -> list[str]:
    reasons: list[str] = []
    count = len(index.by_value)
    if count == 0:
        reasons.append('no job overrides the compared config_path')
    if count < requirement.minimum_distinct_values:
        reasons.append(
            'requires at least '
            f'{requirement.minimum_distinct_values} distinct compared '
            f'value(s); found {count}'
        )
    if (
        requirement.maximum_distinct_values is not None
        and count > requirement.maximum_distinct_values
    ):
        reasons.append(
            'allows at most '
            f'{requirement.maximum_distinct_values} distinct compared '
            f'value(s); found {count}'
        )
    run = context.run
    if any(
        job.evaluation_contract_id != run.evaluation_contract_id
        or job.evaluation_contract_version != run.evaluation_contract_version
        or job.evaluation_contract_digest != run.evaluation_contract_digest
        for job in index.participating
    ):
        reasons.append('compared jobs do not share the run contract binding')
    if len({job.spec.base_config for job in index.participating}) > 1:
        reasons.append('compared jobs do not share one base_config path')
    axes = {
        json.dumps(
            {
                key: value
                for key, value in job.spec.overrides.items()
                if key != requirement.config_path
            },
            sort_keys=True,
            separators=(',', ':'),
            default=str,
        )
        for job in index.participating
    }
    if len(axes) > 1:
        reasons.append(
            'compared jobs differ on more than the compared config_path'
        )
    seed_sets = {
        tuple(sorted({job.seed for job in value_jobs}))
        for value_jobs in index.by_value.values()
    }
    if len(seed_sets) > 1:
        reasons.append('compared values are not backed by the same seed set')
    reasons.extend(comparison_key_reasons(index, context))
    return reasons


def job_document(
    job: JobRecord,
    evidence: JobEvidence,
    context: ComparisonContext,
) -> tuple[dict[str, Any], bool, str | None]:
    artifact = evidence.artifact
    evaluation = evidence.evaluation
    integrity_pass: bool | None = None
    primary_metric: Any = None
    passing = False
    reason: str | None = None
    if artifact is None:
        reason = 'no evaluation.json artifact recorded for the job'
    elif evaluation is None:
        reason = 'evaluation.json artifact could not be read'
    elif not evaluation_contract_matches(evaluation, context.run):
        integrity_pass = evaluation_passed(evaluation)
        reason = 'evaluation contract does not match the run contract'
    else:
        integrity_pass = evaluation_passed(evaluation)
        if context.primary_metric_key is not None:
            primary_metric = evaluation.get(context.primary_metric_key)
        if job.status != JobStatus.SUCCEEDED:
            reason = f'job status is {job.status.value}'
        elif not integrity_pass:
            reason = 'per-job evaluator did not pass'
        elif context.primary_metric_key is not None and primary_metric is None:
            reason = f'primary metric `{context.primary_metric_key}` is missing'
        else:
            passing = True
    document = {
        'job_id': job.job_id,
        'variant_name': job.variant_name,
        'seed': job.seed,
        'status': job.status.value,
        'integrity_pass': integrity_pass,
        'primary_metric': primary_metric,
        'evaluation_uri': artifact.uri if artifact is not None else None,
        'artifact_digest': artifact.sha256 if artifact is not None else None,
    }
    return document, passing, reason
