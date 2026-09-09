"""Deterministic experiment-matrix expansion into per-job specs.

The same approved matrix always expands to the same job identities, so
re-expansion after a restart or replay cannot duplicate cluster jobs.
"""

from __future__ import annotations

from hashlib import sha256
import json
from uuid import uuid5, NAMESPACE_URL

from .contracts import reject_contract_overrides
from .schemas import (
    ExpandedJobSpec,
    ExperimentMatrix,
    ResolvedEvaluationContract,
)


class MatrixExpansionError(ValueError):
    pass


def canonical_json(value: object) -> str:
    # Deterministic serialization (sorted keys, compact separators) so the same
    # matrix yields identical identity bytes across processes and Python
    # versions; json.dumps default key order is insertion order, not sorted.
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def expand_experiment_matrix(
    *,
    run_id: str,
    action_id: str,
    matrix: ExperimentMatrix,
    contract: ResolvedEvaluationContract,
    execution: dict[str, object] | None = None,
) -> list[ExpandedJobSpec]:
    # Contract keys are rejected before expansion so overrides/base_config can
    # never smuggle evaluator or contract control into the submitted job.
    reject_contract_overrides(matrix.model_dump(mode='json'))
    ceiling = contract.descriptor.resource_constraints
    requested = matrix.resources
    # Per-contract ceiling: policy.py enforces the global limits, this enforces
    # what the approved contract allows; both must hold before submission.
    if (
        requested.cpu > ceiling.cpu
        or requested.memory_gib > ceiling.memory_gib
        or requested.gpus > ceiling.gpus
        or requested.wallclock_minutes > ceiling.wallclock_minutes
    ):
        raise MatrixExpansionError(
            'experiment matrix exceeds evaluation-contract resource constraints'
        )
    expanded: list[ExpandedJobSpec] = []
    required_artifacts = list(
        dict.fromkeys(
            [
                # Union of contract-mandated and matrix-requested artifacts,
                # order-preserving so the contract's requirements stay first.
                *contract.descriptor.required_artifacts,
                *matrix.required_artifacts,
            ]
        )
    )
    for variant in matrix.variants:
        for seed in matrix.seeds:
            identity = {
                'run_id': run_id,
                'action_id': action_id,
                'variant': variant.name,
                'seed': seed,
                'base_config': matrix.base_config,
                'overrides': variant.overrides,
                'contract_digest': contract.digest,
            }
            # Job identity covers variant, seed, config, and contract digest
            # but deliberately excludes resources and image: re-expanding the
            # same approved matrix must reproduce the same jobs, while any
            # change to the matrix produces a new key and therefore new jobs.
            # The key is transmitted to workflow-api (Idempotency-Key header)
            # so server-side dedupe survives an orchestrator restart.
            idempotency_key = sha256(canonical_json(identity).encode()).hexdigest()
            expanded.append(
                ExpandedJobSpec(
                    # Derived deterministically (uuid5) from the idempotency
                    # key, so the orchestrator_job_id is stable across restarts
                    # and cluster submission stays dedupe-able.
                    orchestrator_job_id=uuid5(
                        NAMESPACE_URL,
                        f'glasslab-job:{idempotency_key}',
                    ).hex,
                    run_id=run_id,
                    action_id=action_id,
                    variant_name=variant.name,
                    seed=seed,
                    idempotency_key=idempotency_key,
                    base_config=matrix.base_config,
                    overrides=variant.overrides,
                    runner_image=matrix.runner_image,
                    resources=matrix.resources,
                    required_artifacts=required_artifacts,
                    evaluation_contract_id=contract.descriptor.contract_id,
                    evaluation_contract_version=contract.descriptor.version,
                    evaluation_contract_digest=contract.digest,
                    **(execution or {}),
                )
            )
    return expanded
