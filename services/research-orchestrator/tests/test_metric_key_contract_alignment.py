"""Cross-layer metric-key alignment between task spec and bound contract.

The compiled task spec's ``required_metric_keys`` are written once by the
compiler model from ``problem.md`` and are only read by the generic evaluator;
a task-specific contract instead declares its own metric roots in its sealed
``expected_output_schema``. When the two disagree the #497 union made the
deterministic matrix preflight unsatisfiable: the workload emitted the
contract's keys, preflight demanded the stale task-spec keys, and the
temperature-0 model re-emitted the identical matrix until the run paused
(issue #492, finding A.5; live run ``df9995aa78c24170ad7a4c33702d14d3``).

The tests pin the corrected rule: the bound contract's sealed keys are
authoritative whenever it declares any, and the task-spec keys are the
fallback only for a contract that declares none - the generic integrity
contract, whose evaluator reads them from the job payload.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.contracts import compute_contract_digest
from app.preflight import (
    _contract_output_schema_metric_keys,
    _reconcile_required_metric_keys,
    preflight_matrix,
)
from app.schemas import ExperimentMatrix, RunCreateRequest

from conftest import RUNNER_IMAGE


CONTRACT_ID = 'titanic-survival-methodology-v1'
CONTRACT_VERSION = '1.0.0'
TITANIC_SOURCE = 'benchmark-workspace/titanic'

# The live compiled bundle's task-spec keys (bundle task-d777d26b32557f22),
# captured 2026-09-18: un-prefixed names that predate the sealed contract.
TITANIC_TASK_SPEC_KEYS = [
    'accuracy_mean',
    'accuracy_std',
    'roc_auc_mean',
    'roc_auc_std',
    'f1_macro_mean',
    'f1_macro_std',
    'fold_accuracy',
    'fold_roc_auc',
    'fold_f1_macro',
]
# ``schemas/output_schema.json`` of titanic-survival-methodology-v1@1.0.0.
TITANIC_CONTRACT_METRIC_KEYS = [
    'cv_accuracy_mean',
    'cv_roc_auc_mean',
    'cv_f1_macro_mean',
]


def _output_schema() -> dict:
    return {
        'type': 'object',
        'required': ['metrics', 'guardrails'],
        'properties': {
            'metrics': {
                'type': 'object',
                'required': list(TITANIC_CONTRACT_METRIC_KEYS),
                'properties': {
                    key: {'type': 'number'} for key in TITANIC_CONTRACT_METRIC_KEYS
                },
            },
            'guardrails': {'type': 'object'},
        },
    }


def _install_titanic_contract(tmp_path: Path, engine):
    # A task-specific contract whose manifest declares no required_metric_keys
    # (the live case) and whose sealed output schema carries the cv_ roots.
    root = tmp_path / 'trusted-contracts' / CONTRACT_ID / CONTRACT_VERSION
    schemas = root / 'schemas'
    schemas.mkdir(parents=True)
    descriptor = {
        'contract_id': CONTRACT_ID,
        'version': CONTRACT_VERSION,
        'manifest': {
            'primary_metric': 'cv_accuracy_mean',
            'primary_metric_direction': 'maximize',
        },
        'execution_wrapper': 'scripts/evaluator_wrapper.py',
        'evaluation_entry_point': 'scripts/titanic_evaluator.py',
        'expected_input_schema': 'schemas/input_schema.json',
        'expected_output_schema': 'schemas/output_schema.json',
        'required_artifacts': ['metrics.json', 'evaluation.json', 'report.md'],
        'resource_constraints': {
            'cpu': 4.0,
            'memory_gib': 8.0,
            'gpus': 0,
            'wallclock_minutes': 60,
        },
        'container_image_digest': None,
    }
    (root / 'contract.json').write_text(json.dumps(descriptor, indent=2))
    (schemas / 'input_schema.json').write_text('{"type": "object"}\n')
    (schemas / 'output_schema.json').write_text(
        json.dumps(_output_schema(), indent=2) + '\n'
    )
    scripts = root / 'scripts'
    scripts.mkdir()
    (scripts / 'evaluator_wrapper.py').write_text('# wrapper\n')
    (scripts / 'titanic_evaluator.py').write_text('# evaluator\n')
    (root / 'contract.sha256').write_text(compute_contract_digest(root))
    return engine.contracts.resolve(CONTRACT_ID, CONTRACT_VERSION)


def _titanic_task_definition() -> dict:
    return {
        'source_subdirectory': TITANIC_SOURCE,
        'task_spec': {
            'required_metric_keys': list(TITANIC_TASK_SPEC_KEYS),
            'required_artifacts': ['metrics.json', 'report.md'],
        },
    }


def _titanic_run(engine, *, metric_body: str):
    run = engine.create_run(
        request=RunCreateRequest(objective='Exercise titanic metric alignment.')
    )
    workspace = Path(run.beaker_workspace)
    (workspace / 'configs').mkdir(parents=True, exist_ok=True)
    (workspace / 'configs' / 'candidate.yaml').write_text('seeds: [17]\n')
    source = workspace / TITANIC_SOURCE
    source.mkdir(parents=True)
    packaged_config = source / 'configs' / 'candidate.yaml'
    packaged_config.parent.mkdir(parents=True, exist_ok=True)
    packaged_config.write_text('seeds: [17]\n')
    (source / 'run.py').write_text(
        'import json\n'
        'with open("metrics.json", "w") as handle:\n'
        f'    json.dump({metric_body}, handle)\n'
        'open("report.md", "w").write("report")\n'
    )
    return run.model_copy(update={'task_definition': _titanic_task_definition()})


def _single_variant_matrix() -> ExperimentMatrix:
    return ExperimentMatrix.model_validate(
        {
            'base_config': 'configs/candidate.yaml',
            'variants': [{'name': 'a', 'overrides': {}}],
            'seeds': [17],
            'maximum_parallel_jobs': 1,
            'runner_image': RUNNER_IMAGE,
            'resources': {
                'cpu': 1,
                'memory_gib': 1,
                'gpus': 0,
                'wallclock_minutes': 5,
            },
            'required_artifacts': ['metrics.json'],
        }
    )


def test_titanic_task_spec_keys_reconcile_to_contract_output_schema(
    orchestrator_bundle,
    tmp_path: Path,
) -> None:
    # Given the live compiled bundle's stale un-prefixed keys and the sealed
    # contract, the effective required roots used for the run are the contract's.
    _, _, _, _, engine = orchestrator_bundle
    contract = _install_titanic_contract(tmp_path, engine)

    contract_keys = _contract_output_schema_metric_keys(contract)
    effective, superseded = _reconcile_required_metric_keys(
        contract=contract,
        task_definition=_titanic_task_definition(),
    )

    assert contract_keys == TITANIC_CONTRACT_METRIC_KEYS
    assert set(effective) <= set(contract_keys)
    assert set(effective) == set(contract_keys)
    assert set(superseded) == set(TITANIC_TASK_SPEC_KEYS) - set(contract_keys)


def test_titanic_preflight_accepts_contract_keys_over_stale_task_spec(
    orchestrator_bundle,
    tmp_path: Path,
) -> None:
    # Given a run.py that serializes the contract's cv_ roots, the stale
    # un-prefixed task-spec keys must not make the matrix unsatisfiable.
    _, _, _, _, engine = orchestrator_bundle
    contract = _install_titanic_contract(tmp_path, engine)
    run = _titanic_run(
        engine,
        metric_body=(
            '{"cv_accuracy_mean": 0.8, "cv_accuracy_std": 0.02, '
            '"cv_roc_auc_mean": 0.85, "cv_roc_auc_std": 0.03, '
            '"cv_f1_macro_mean": 0.77, "cv_f1_macro_std": 0.04, '
            '"fold_accuracies": [0.8], "fold_roc_auc": [0.85], '
            '"fold_f1_macro": [0.77]}'
        ),
    )

    report = preflight_matrix(
        run=run,
        matrix=_single_variant_matrix(),
        contract=contract,
    )

    assert report.passed
    assert report.errors == []
    assert any(
        'stale compiled task-spec key(s) superseded' in check
        for check in report.checks
    )


def test_titanic_preflight_rejects_missing_contract_metric_keys(
    orchestrator_bundle,
    tmp_path: Path,
) -> None:
    # The reconciliation must not weaken the #497 check: a workload that omits
    # a contract-required root still fails deterministically before submission.
    _, _, _, _, engine = orchestrator_bundle
    contract = _install_titanic_contract(tmp_path, engine)
    run = _titanic_run(engine, metric_body='{"cv_accuracy_mean": 0.8}')

    report = preflight_matrix(
        run=run,
        matrix=_single_variant_matrix(),
        contract=contract,
    )

    assert not report.passed
    metric_error = next(
        error for error in report.errors if 'cv_roc_auc_mean' in error
    )
    assert 'metrics.json' in metric_error
