"""Policy classification, contract read-only rendering, and matrix preflight.

Covers the ActionPolicy approval tiers (automatic / honeydew-and-human /
deny), immutable read-only evaluation-contract mounts, deterministic matrix
expansion, and the Adult/Wine benchmark preflight rules that gate cluster
submission: root metric keys, evaluator-owned outputs, required evidence
files, and the internal-vs-matrix seed-axis rule.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError

from app.contracts import (
    ContractIntegrityError,
    EvaluationContractResolver,
    compute_contract_digest,
    reject_contract_overrides,
    render_read_only_contract_job,
)
from app.matrix import MatrixExpansionError, expand_experiment_matrix
from app.policy import ActionPolicy
from app.preflight import preflight_matrix
from app.schemas import (
    AgentName,
    ExperimentMatrix,
    PolicyClassification,
    RequestedAction,
    RunCreateRequest,
    RunState,
)

from conftest import RUNNER_IMAGE


def _matrix() -> ExperimentMatrix:
    return ExperimentMatrix.model_validate(
        {
            'base_config': 'configs/baseline.yaml',
            'variants': [
                {'name': 'a', 'overrides': {'learning_rate': 0.1}},
                {'name': 'b', 'overrides': {'learning_rate': 0.2}},
            ],
            'seeds': [17, 31, 49],
            'maximum_parallel_jobs': 2,
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


def test_action_policy_decisions() -> None:
    policy = ActionPolicy(
        permitted_images=[RUNNER_IMAGE],
        maximum_cpu=4,
        maximum_memory_gib=8,
        maximum_gpus=1,
        maximum_parallel_jobs=2,
    )
    assert policy.classify(
        proposed_by=AgentName.BEAKER,
        action=RequestedAction(
            type='run_local_tests',
            reason='Validate locally.',
        ),
    ) == PolicyClassification.AUTOMATIC
    assert policy.classify(
        proposed_by=AgentName.BEAKER,
        action=RequestedAction(
            type='submit_experiment_matrix',
            arguments=_matrix().model_dump(mode='json'),
            reason='Run reviewed experiments.',
        ),
    ) == PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL
    assert policy.classify(
        proposed_by=AgentName.BEAKER,
        action=RequestedAction(
            type='raw_kubectl',
            reason='Bypass the control plane.',
        ),
    ) == PolicyClassification.DENY
    denied = policy.build_record(
        run_id='run-policy-denial',
        proposed_by=AgentName.BEAKER,
        action=RequestedAction(
            type='submit_experiment_matrix',
            arguments={
                **_matrix().model_dump(mode='json'),
                'runner_image': 'example.invalid/untrusted:latest',
            },
            reason='Run this image.',
        ),
        ordinal=1,
    )
    assert denied.approval_status.value == 'denied'
    assert 'not permitted' in denied.reason
    assert RUNNER_IMAGE in denied.reason


def test_submit_validation_job_is_denied_until_an_executor_exists() -> None:
    # The action could previously be proposed and approved, but
    # _resume_approved_action has no branch that runs it, so an approval was
    # recorded while nothing executed. The type is denied outright until an
    # executor exists.
    policy = ActionPolicy(
        permitted_images=[RUNNER_IMAGE],
        maximum_cpu=4,
        maximum_memory_gib=8,
        maximum_gpus=1,
        maximum_parallel_jobs=2,
    )
    classification, reason = policy.evaluate(
        proposed_by=AgentName.HONEYDEW,
        action=RequestedAction(
            type='submit_validation_job',
            arguments={},
            reason='Run the validation job.',
        ),
    )
    assert classification == PolicyClassification.DENY
    assert reason is not None
    assert 'submit_validation_job' in reason
    record = policy.build_record(
        run_id='run-validation-job',
        proposed_by=AgentName.HONEYDEW,
        action=RequestedAction(
            type='submit_validation_job',
            arguments={},
            reason='Run the validation job.',
        ),
        ordinal=1,
    )
    assert record.approval_status.value == 'denied'


def test_evaluation_contract_modification_is_rejected(tmp_path, orchestrator_bundle) -> None:
    settings = orchestrator_bundle[0]
    copied = tmp_path / 'contracts'
    shutil.copytree(settings.evaluation_contract_root, copied)
    resolver = EvaluationContractResolver(str(copied))
    resolver.resolve('example-research-v1', '1.0.0')
    evaluator = (
        copied
        / 'example-research-v1'
        / '1.0.0'
        / 'evaluator.py'
    )
    evaluator.write_text(evaluator.read_text() + '\n# unauthorized drift\n')
    with pytest.raises(ContractIntegrityError, match='digest mismatch'):
        resolver.resolve('example-research-v1', '1.0.0')
    with pytest.raises(ContractIntegrityError, match='cannot override'):
        reject_contract_overrides(
            {'overrides': {'evaluation_entry_point': 'attacker.py'}}
        )


def test_job_spec_validation_and_read_only_contract(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    contract = engine.contracts.resolve('example-research-v1', '1.0.0')
    specs = expand_experiment_matrix(
        run_id='run-1',
        action_id='action-1',
        matrix=_matrix(),
        contract=contract,
    )
    rendered = render_read_only_contract_job(
        specs[0],
        contract,
        namespace=settings.kubernetes_namespace,
    )
    pod = rendered['spec']['template']['spec']
    mount = pod['containers'][0]['volumeMounts'][0]
    assert mount['mountPath'] == '/evaluation-contract'
    assert mount['readOnly'] is True
    assert pod['automountServiceAccountToken'] is False
    assert pod['containers'][0]['command'] == [
        'python',
        '/evaluation-contract/run_contract.py',
    ]
    gpu_matrix = _matrix().model_copy(
        update={
            'resources': _matrix().resources.model_copy(update={'gpus': 1})
        }
    )
    gpu_spec = expand_experiment_matrix(
        run_id='run-gpu',
        action_id='action-gpu',
        matrix=gpu_matrix,
        contract=contract,
    )[0]
    gpu_job = render_read_only_contract_job(
        gpu_spec,
        contract,
        namespace=settings.kubernetes_namespace,
    )
    assert (
        gpu_job['spec']['template']['spec']['containers'][0]['resources']
        ['limits']['nvidia.com/gpu']
        == '1'
    )
    with pytest.raises(ValidationError):
        ExperimentMatrix.model_validate(
            {
                **_matrix().model_dump(mode='json'),
                'evaluation_entry_point': 'attacker.py',
            }
        )


def test_experiment_matrix_expansion_is_deterministic(orchestrator_bundle) -> None:
    engine = orchestrator_bundle[-1]
    contract = engine.contracts.resolve('example-research-v1', '1.0.0')
    first = expand_experiment_matrix(
        run_id='run-1',
        action_id='action-1',
        matrix=_matrix(),
        contract=contract,
    )
    second = expand_experiment_matrix(
        run_id='run-1',
        action_id='action-1',
        matrix=_matrix(),
        contract=contract,
    )
    assert [item.model_dump() for item in first] == [
        item.model_dump() for item in second
    ]
    assert [(item.variant_name, item.seed) for item in first] == [
        ('a', 17),
        ('a', 31),
        ('a', 49),
        ('b', 17),
        ('b', 31),
        ('b', 49),
    ]
    assert len({item.idempotency_key for item in first}) == 6

    oversized = _matrix().model_copy(
        update={
            'resources': _matrix().resources.model_copy(
                update={'wallclock_minutes': 6}
            )
        }
    )
    with pytest.raises(MatrixExpansionError, match='resource constraints'):
        expand_experiment_matrix(
            run_id='run-1',
            action_id='action-oversized',
            matrix=oversized,
            contract=contract,
        )


def test_adult_preflight_distinguishes_comparisons_from_decisions(
    orchestrator_bundle,
) -> None:
    # The preflight must classify config dimensions as either comparison axes
    # (model families, allowed to vary) or fixed decisions (methodology
    # choices that must be pinned), so the contract can flag ambiguity.
    _, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        request=RunCreateRequest(
            objective='Validate the Adult methodology preflight contract.'
        )
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.write_text(
        'experiment_dimensions:\n'
        '  model: [logistic_regression, random_forest]\n'
        '  missing_strategy: [impute_unknown]\n'
        '  include_fnlwgt: [false]\n'
        '  encoding: [one_hot]\n'
    )
    source = workspace / 'benchmark-workspace' / 'adult-income'
    source.mkdir(parents=True)
    (source / 'run.py').write_text(
        'import json\n'
        'metrics = {\n'
        '    "accuracy": 0.9, "balanced_accuracy": 0.8,\n'
        '    "precision": 0.8, "recall": 0.8, "f1": 0.8,\n'
        '    "roc_auc": 0.9, "headline_ci_low": 0.85,\n'
        '    "headline_ci_high": 0.95, "bootstrap_resamples": 1000,\n'
        '    "test_rows": 16281,\n'
        '}\n'
        'payload = {**metrics, "models": {}}\n'
        'with open("metrics.json", "w") as handle:\n'
        '    json.dump(payload, handle)\n'
        'open("report.md", "w").write("report")\n'
        'open("tables/metrics.csv", "w").write("metrics")\n'
        'open("tables/fairness.csv", "w").write("fairness")\n'
    )
    run = run.model_copy(
        update={
            'task_definition': {
                'source_subdirectory': 'benchmark-workspace/adult-income',
            }
        }
    )
    contract = engine.contracts.resolve(
        'ml-benchmark-adult-income-v1',
        '1.1.0',
    )
    report = preflight_matrix(
        run=run,
        matrix=_matrix().model_copy(
            update={'base_config': 'configs/candidate.yaml'}
        ),
        contract=contract,
    )
    assert report.passed
    assert report.comparisons == {
        'model_families': ['logistic_regression', 'random_forest']
    }
    assert report.decisions == {
        'missing_data_strategy': ['impute_unknown'],
        'fnlwgt_handling': ['False'],
        'categorical_encoding': ['one_hot'],
    }


def test_adult_preflight_rejects_nested_only_metrics_json(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        request=RunCreateRequest(objective='Reject nested-only contract metrics.')
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.write_text(
        'experiment_dimensions:\n'
        '  model: [logistic_regression, random_forest]\n'
        '  missing_strategy: [impute_unknown]\n'
        '  include_fnlwgt: [false]\n'
        '  encoding: [one_hot]\n'
    )
    source = workspace / 'benchmark-workspace' / 'adult-income'
    source.mkdir(parents=True)
    (source / 'run.py').write_text(
        'import json\n'
        'metrics = {"gradient_boosted": {"metrics": {"accuracy": 0.9}}}\n'
        'with open("metrics.json", "w") as handle:\n'
        '    json.dump(metrics, handle)\n'
    )
    report = preflight_matrix(
        run=run.model_copy(
            update={
                'task_definition': {
                    'source_subdirectory': 'benchmark-workspace/adult-income',
                }
            }
        ),
        matrix=_matrix().model_copy(
            update={'base_config': 'configs/candidate.yaml'}
        ),
        contract=engine.contracts.resolve(
            'ml-benchmark-adult-income-v1',
            '1.1.0',
        ),
    )

    assert not report.passed
    assert any(
        'serializes metrics.json without required root key(s)' in error
        for error in report.errors
    )


def test_adult_preflight_accepts_assigned_path_and_annotated_metrics(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        request=RunCreateRequest(objective='Accept common typed output patterns.')
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.write_text(
        'experiment_dimensions:\n'
        '  model: [logistic_regression, random_forest]\n'
        '  missing_strategy: [impute_unknown]\n'
        '  include_fnlwgt: [false]\n'
        '  encoding: [one_hot]\n'
    )
    source = workspace / 'benchmark-workspace' / 'adult-income'
    source.mkdir(parents=True)
    (source / 'run.py').write_text(
        'import json\n'
        'from pathlib import Path\n'
        'def build_metrics():\n'
        '  metrics: dict[str, object] = {\n'
        '    "accuracy": 0.9, "balanced_accuracy": 0.8,\n'
        '    "precision": 0.8, "recall": 0.8, "f1": 0.8,\n'
        '    "roc_auc": 0.9, "headline_ci_low": 0.85,\n'
        '    "headline_ci_high": 0.95, "bootstrap_resamples": 1000,\n'
        '    "test_rows": 16281,\n'
        '  }\n'
        '  return metrics\n'
        'metrics = build_metrics()\n'
        'metrics_path = Path("output") / "metrics.json"\n'
        'with open(metrics_path, "w") as handle:\n'
        '    json.dump(metrics, handle)\n'
        'Path("report.md").write_text("report")\n'
        'Path("tables/metrics.csv").write_text("metrics")\n'
        'Path("tables/fairness.csv").write_text("fairness")\n'
    )
    report = preflight_matrix(
        run=run.model_copy(
            update={
                'task_definition': {
                    'source_subdirectory': 'benchmark-workspace/adult-income',
                }
            }
        ),
        matrix=_matrix().model_copy(
            update={'base_config': 'configs/candidate.yaml'}
        ),
        contract=engine.contracts.resolve(
            'ml-benchmark-adult-income-v1',
            '1.1.0',
        ),
    )

    assert report.passed


def test_adult_preflight_rejects_metadata_wrapped_methodology_values(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        request=RunCreateRequest(objective='Reject ambiguous methodology values.')
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.write_text(
        'experiment_dimensions:\n'
        '  model:\n'
        '    description: compare models\n'
        '    values: [logistic_regression, random_forest]\n'
    )
    contract = engine.contracts.resolve(
        'ml-benchmark-adult-income-v1',
        '1.1.0',
    )
    report = preflight_matrix(
        run=run,
        matrix=_matrix().model_copy(
            update={'base_config': 'configs/candidate.yaml'}
        ),
        contract=contract,
    )
    assert not report.passed
    assert any(
        'must directly contain a scalar or list of values' in error
        for error in report.errors
    )


def test_preflight_rejects_workload_owned_evaluation_output(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        request=RunCreateRequest(
            objective='Reject workload code that impersonates the evaluator.'
        )
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.write_text(
        'experiment_dimensions:\n'
        '  model: [logistic_regression, random_forest]\n'
        '  missing_strategy: [impute_unknown]\n'
        '  include_fnlwgt: [false]\n'
        '  encoding: [one_hot]\n'
    )
    source = workspace / 'benchmark-workspace' / 'adult-income'
    source.mkdir(parents=True)
    (source / 'run.py').write_text(
        'open("evaluation.json", "w").write(\'{"rubric_score": 100}\')\n'
    )
    report = preflight_matrix(
        run=run.model_copy(
            update={
                'task_definition': {
                    'source_subdirectory': 'benchmark-workspace/adult-income',
                }
            }
        ),
        matrix=_matrix().model_copy(
            update={'base_config': 'configs/candidate.yaml'}
        ),
        contract=engine.contracts.resolve(
            'ml-benchmark-adult-income-v1',
            '1.1.0',
        ),
    )
    assert not report.passed
    assert any('evaluator-owned output' in error for error in report.errors)


def test_wine_preflight_rejects_missing_required_plot(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        request=RunCreateRequest(objective='Catch missing evidence before execution.')
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.write_text('seeds: [17]\n')
    source = workspace / 'benchmark-workspace' / 'wine-clustering'
    source.mkdir(parents=True)
    (source / 'run.py').write_text(
        'import json\n'
        'metrics = {\n'
        '  "algorithm_count": 4, "sample_count": 178,\n'
        '  "silhouette": 0.2, "davies_bouldin": 1.0,\n'
        '  "adjusted_rand": 0.8, "normalized_mutual_info": 0.8,\n'
        '  "stability_seeds": 10, "pca_variance_2d": 0.6,\n'
        '}\n'
        'with open("metrics.json", "w") as handle:\n'
        '  json.dump(metrics, handle)\n'
        'open("report.md", "w").write("report")\n'
        'open("tables/comparison.csv", "w").write("table")\n'
    )
    report = preflight_matrix(
        run=run.model_copy(
            update={
                'task_definition': {
                    'source_subdirectory': 'benchmark-workspace/wine-clustering',
                }
            }
        ),
        matrix=_matrix().model_copy(
            update={
                'base_config': 'configs/candidate.yaml',
                'seeds': [17],
            }
        ),
        contract=engine.contracts.resolve(
            'ml-benchmark-wine-clustering-v1',
            '1.0.0',
        ),
    )

    assert not report.passed
    assert (
        'workload source does not statically reference required artifact: '
        'plots/clusters.png'
    ) in report.errors


def test_preflight_rejects_duplicate_internal_and_matrix_seed_axes(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        request=RunCreateRequest(objective='Avoid duplicate cluster work.')
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.write_text('seeds: [17, 31, 49]\n')
    report = preflight_matrix(
        run=run,
        matrix=_matrix().model_copy(
            update={
                'base_config': 'configs/candidate.yaml',
                'seeds': [17, 31, 49],
            }
        ),
        contract=engine.contracts.resolve('example-research-v1', '1.0.0'),
    )

    assert not report.passed
    assert any(
        'internal stability seeds must run inside one job' in error
        for error in report.errors
    )


def test_methodology_revision_limit_pauses_for_human_resolution(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    engine.settings.maximum_methodology_revisions = 2
    run = engine.create_run(
        request=RunCreateRequest(
            objective='Bound repeated methodology review disagreements.'
        )
    )
    store.replace_run(
        run.model_copy(update={'state': RunState.HONEYDEW_REVIEWING}),
        expected_version=run.version,
    )
    monkeypatch.setattr(engine, '_beaker_revise', lambda *_args, **_kwargs: None)

    engine._request_methodology_revision(run.run_id, feedback='first')
    engine._request_methodology_revision(run.run_id, feedback='second')
    engine._request_methodology_revision(run.run_id, feedback='third')

    paused = store.get_run(run.run_id)
    assert paused.state == RunState.PAUSED
    assert paused.resume_state == RunState.BEAKER_REVISING
    assert paused.methodology_revision_count == 3
    assert any(
        event.event_type == 'methodology.human_resolution_requested'
        for event in store.list_events(run.run_id)
    )


def test_comparison_contract_rejects_single_seed(orchestrator_bundle) -> None:
    """Comparison mode contract with 1 seed must fail preflight."""
    _, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        request=RunCreateRequest(
            objective='Reject comparison contract with insufficient seeds.'
        )
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.write_text(
        'experiment_dimensions:\n'
        '  model: [logistic_regression, random_forest]\n'
    )
    source = workspace / 'benchmark-workspace' / 'adult-income'
    source.mkdir(parents=True)
    (source / 'run.py').write_text(
        'import json\n'
        'metrics = {\n'
        '    "accuracy": 0.9, "balanced_accuracy": 0.8,\n'
        '    "precision": 0.8, "recall": 0.8, "f1": 0.8,\n'
        '    "roc_auc": 0.9, "headline_ci_low": 0.85,\n'
        '    "headline_ci_high": 0.95, "bootstrap_resamples": 1000,\n'
        '    "test_rows": 16281,\n'
        '}\n'
        'payload = {**metrics, "models": {}}\n'
        'with open("metrics.json", "w") as handle:\n'
        '    json.dump(payload, handle)\n'
        'open("report.md", "w").write("report")\n'
        'open("tables/metrics.csv", "w").write("metrics")\n'
        'open("tables/fairness.csv", "w").write("fairness")\n'
    )
    run = run.model_copy(
        update={
            'task_definition': {
                'source_subdirectory': 'benchmark-workspace/adult-income',
            }
        }
    )
    contract = engine.contracts.resolve(
        'ml-benchmark-adult-income-v1',
        '1.1.0',
    )
    matrix = ExperimentMatrix.model_validate(
        {
            'base_config': 'configs/candidate.yaml',
            'variants': [
                {'name': 'a', 'overrides': {'learning_rate': 0.1}},
            ],
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
    report = preflight_matrix(run=run, matrix=matrix, contract=contract)

    assert not report.passed
    assert any(
        'comparison contract requires at least 3 matrix seeds' in error
        for error in report.errors
    )


def test_non_comparison_contract_accepts_single_seed(orchestrator_bundle) -> None:
    """Non-comparison mode contract with 1 seed must pass preflight."""
    _, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        request=RunCreateRequest(
            objective='Accept non-comparison contract with single seed.'
        )
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.write_text('seeds: [17]\n')
    source = workspace / 'benchmark-workspace' / 'adult-income'
    source.mkdir(parents=True)
    (source / 'run.py').write_text(
        'import json\n'
        'metrics = {\n'
        '    "accuracy": 0.9, "balanced_accuracy": 0.8,\n'
        '    "precision": 0.8, "recall": 0.8, "f1": 0.8,\n'
        '    "roc_auc": 0.9, "headline_ci_low": 0.85,\n'
        '    "headline_ci_high": 0.95, "bootstrap_resamples": 1000,\n'
        '    "test_rows": 16281,\n'
        '}\n'
        'payload = {**metrics, "models": {}}\n'
        'with open("metrics.json", "w") as handle:\n'
        '    json.dump(payload, handle)\n'
        'open("report.md", "w").write("report")\n'
    )
    run = run.model_copy(
        update={
            'task_definition': {
                'source_subdirectory': 'benchmark-workspace/adult-income',
            }
        }
    )
    contract = engine.contracts.resolve('example-research-v1', '1.0.0')
    matrix = ExperimentMatrix.model_validate(
        {
            'base_config': 'configs/candidate.yaml',
            'variants': [
                {'name': 'a', 'overrides': {'learning_rate': 0.1}},
            ],
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
    report = preflight_matrix(run=run, matrix=matrix, contract=contract)

    assert report.passed


ACROSS_JOBS_REQUIREMENT = {
    'requirement_id': 'model_families',
    'config_path': 'experiment_dimensions.model',
    'mode': 'comparison',
    'comparison_scope': 'across_jobs',
    'minimum_distinct_values': 2,
    'description': 'Compare two model families across separate jobs.',
}

_ACROSS_JOBS_SOURCE = (
    'import json\n'
    'json.dump({"accuracy": 0.9}, open("metrics.json", "w"))\n'
    'open("report.md", "w").write("report")\n'
)


def _install_across_jobs_contract(tmp_path: Path, engine) -> str:
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
        'required_artifacts': ['metrics.json'],
        'resource_constraints': {
            'cpu': 1.0,
            'memory_gib': 2.0,
            'gpus': 0,
            'wallclock_minutes': 30,
        },
        'container_image_digest': None,
        'manifest': {
            'primary_metric': 'accuracy',
            'primary_metric_direction': 'maximize',
            'methodology_requirements': [ACROSS_JOBS_REQUIREMENT],
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
    return engine.contracts.resolve(contract_id, version).digest


def _across_jobs_run(engine, *, config_body: str):
    run = engine.create_run(
        request=RunCreateRequest(
            objective='Exercise the across-jobs comparison preflight.'
        )
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(config_body)
    source = workspace / 'benchmark-workspace' / 'adult-income'
    source.mkdir(parents=True)
    (source / 'run.py').write_text(_ACROSS_JOBS_SOURCE)
    return run.model_copy(
        update={
            'task_definition': {
                'source_subdirectory': 'benchmark-workspace/adult-income',
            }
        }
    )


def _across_jobs_matrix(
    *,
    overrides: list[dict],
    seeds: list[int],
) -> ExperimentMatrix:
    return ExperimentMatrix.model_validate(
        {
            'base_config': 'configs/candidate.yaml',
            'variants': [
                {'name': f'variant-{index}', 'overrides': override}
                for index, override in enumerate(overrides)
            ],
            'seeds': seeds,
            'maximum_parallel_jobs': 2,
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


def test_across_jobs_comparison_accepts_two_methods_one_seed(
    tmp_path,
    orchestrator_bundle,
) -> None:
    # Each compared methodology is its own job: two methods and one seed
    # therefore expand to exactly two jobs, and the per-job evaluator scores a
    # single configuration each.
    _, _, _, _, engine = orchestrator_bundle
    _install_across_jobs_contract(tmp_path, engine)
    contract = engine.contracts.resolve('across-jobs-v1', '1.0.0')
    run = _across_jobs_run(
        engine,
        config_body='experiment_dimensions:\n  model: model-candidate-1\n',
    )
    matrix = _across_jobs_matrix(
        overrides=[
            {'experiment_dimensions.model': 'model-candidate-1'},
            {'experiment_dimensions.model': 'model-candidate-2'},
        ],
        seeds=[17],
    )

    report = preflight_matrix(run=run, matrix=matrix, contract=contract)

    assert report.passed, report.errors
    assert report.job_count == 2


def test_across_jobs_comparison_rejects_list_base_config(
    tmp_path,
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    _install_across_jobs_contract(tmp_path, engine)
    contract = engine.contracts.resolve('across-jobs-v1', '1.0.0')
    run = _across_jobs_run(
        engine,
        config_body=(
            'experiment_dimensions:\n'
            '  model: [model-candidate-1, model-candidate-2]\n'
        ),
    )
    matrix = _across_jobs_matrix(
        overrides=[
            {'experiment_dimensions.model': 'model-candidate-1'},
            {'experiment_dimensions.model': 'model-candidate-2'},
        ],
        seeds=[17],
    )

    report = preflight_matrix(run=run, matrix=matrix, contract=contract)

    assert not report.passed
    assert any(
        'must contain exactly one scalar value' in error
        for error in report.errors
    )


def test_across_jobs_comparison_rejects_empty_overrides(
    tmp_path,
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    _install_across_jobs_contract(tmp_path, engine)
    contract = engine.contracts.resolve('across-jobs-v1', '1.0.0')
    run = _across_jobs_run(
        engine,
        config_body='experiment_dimensions:\n  model: model-candidate-1\n',
    )
    matrix = _across_jobs_matrix(
        overrides=[{}, {'experiment_dimensions.model': 'model-candidate-2'}],
        seeds=[17],
    )

    report = preflight_matrix(run=run, matrix=matrix, contract=contract)

    assert not report.passed
    assert any(
        'requires a non-empty `overrides` object' in error
        for error in report.errors
    )


def test_across_jobs_comparison_rejects_non_scalar_override(
    tmp_path,
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    _install_across_jobs_contract(tmp_path, engine)
    contract = engine.contracts.resolve('across-jobs-v1', '1.0.0')
    run = _across_jobs_run(
        engine,
        config_body='experiment_dimensions:\n  model: model-candidate-1\n',
    )
    matrix = _across_jobs_matrix(
        overrides=[
            {'experiment_dimensions.model': ['a', 'b']},
            {'experiment_dimensions.model': 'model-candidate-2'},
        ],
        seeds=[17],
    )

    report = preflight_matrix(run=run, matrix=matrix, contract=contract)

    assert not report.passed
    assert any(
        'override must be a distinct scalar value' in error
        for error in report.errors
    )


def test_across_jobs_comparison_rejects_duplicate_override_values(
    tmp_path,
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    _install_across_jobs_contract(tmp_path, engine)
    contract = engine.contracts.resolve('across-jobs-v1', '1.0.0')
    run = _across_jobs_run(
        engine,
        config_body='experiment_dimensions:\n  model: model-candidate-1\n',
    )
    matrix = _across_jobs_matrix(
        overrides=[
            {'experiment_dimensions.model': 'same'},
            {'experiment_dimensions.model': 'same'},
        ],
        seeds=[17],
    )

    report = preflight_matrix(run=run, matrix=matrix, contract=contract)

    assert not report.passed
    assert any(
        'across_jobs comparison requires at least 2 distinct variant override'
        in error
        for error in report.errors
    )


def test_across_jobs_comparison_rejects_single_element_list_base_config(
    tmp_path,
    orchestrator_bundle,
) -> None:
    # F8: the across_jobs base_config path must hold one SCALAR, not a
    # single-element list.
    _, _, _, _, engine = orchestrator_bundle
    _install_across_jobs_contract(tmp_path, engine)
    contract = engine.contracts.resolve('across-jobs-v1', '1.0.0')
    run = _across_jobs_run(
        engine,
        config_body=(
            'experiment_dimensions:\n  model: [model-candidate-1]\n'
        ),
    )
    matrix = _across_jobs_matrix(
        overrides=[
            {'experiment_dimensions.model': 'model-candidate-1'},
            {'experiment_dimensions.model': 'model-candidate-2'},
        ],
        seeds=[17],
    )

    report = preflight_matrix(run=run, matrix=matrix, contract=contract)

    assert not report.passed
    assert any(
        'must contain exactly one scalar value' in error
        for error in report.errors
    )


def test_across_jobs_report_records_scope_and_topology(
    tmp_path,
    orchestrator_bundle,
) -> None:
    # F5: the preflight report carries the resolved scope and canonical shape so
    # the agent review does not mistake the correct across_jobs topology.
    _, _, _, _, engine = orchestrator_bundle
    _install_across_jobs_contract(tmp_path, engine)
    contract = engine.contracts.resolve('across-jobs-v1', '1.0.0')
    run = _across_jobs_run(
        engine,
        config_body='experiment_dimensions:\n  model: model-candidate-1\n',
    )
    matrix = _across_jobs_matrix(
        overrides=[
            {'experiment_dimensions.model': 'model-candidate-1'},
            {'experiment_dimensions.model': 'model-candidate-2'},
        ],
        seeds=[17],
    )

    report = preflight_matrix(run=run, matrix=matrix, contract=contract)

    assert report.comparison_scope == 'across_jobs'
    assert 'one non-empty variant per compared methodology' in (
        report.comparison_topology
    )


def test_within_job_report_records_scope_and_topology(
    orchestrator_bundle,
) -> None:
    # F5: a within_job report states the single-candidate/empty-overrides shape
    # is canonical, so Honeydew's review does not re-enter the #474 loop.
    _, _, _, _, engine = orchestrator_bundle
    contract = engine.contracts.resolve(
        'ml-benchmark-adult-income-v1',
        '1.1.0',
    )
    run = engine.create_run(
        request=RunCreateRequest(objective='Within-job scope topology.')
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        'experiment_dimensions:\n'
        '  model: [logistic-regression, random-forest]\n'
        '  missing_strategy: median-imputation\n'
        '  include_fnlwgt: false\n'
        '  encoding: ordinal\n'
    )
    matrix = ExperimentMatrix.model_validate(
        {
            'base_config': 'configs/candidate.yaml',
            'variants': [{'name': 'candidate', 'overrides': {}}],
            'seeds': [17, 31, 49],
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

    report = preflight_matrix(run=run, matrix=matrix, contract=contract)

    assert report.passed, report.errors
    assert report.comparison_scope == 'within_job'
    assert 'candidate' in report.comparison_topology
    assert 'EMPTY overrides' in report.comparison_topology


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


def _generic_task_run(engine, *, metric_body: str):
    run = engine.create_run(
        request=RunCreateRequest(
            objective='Exercise the generic task metric-key preflight.'
        )
    )
    workspace = Path(run.beaker_workspace)
    (workspace / 'configs' / 'candidate.yaml').write_text('seeds: [17]\n')
    source = workspace / 'benchmark-workspace' / 'adult-income'
    source.mkdir(parents=True)
    (source / 'run.py').write_text(
        'import json\n'
        'with open("metrics.json", "w") as handle:\n'
        f'    json.dump({metric_body}, handle)\n'
        'open("report.md", "w").write("report")\n'
    )
    return run.model_copy(
        update={
            'task_definition': {
                'source_subdirectory': 'benchmark-workspace/adult-income',
                'task_spec': {
                    'required_metric_keys': [
                        'accuracy',
                        'test_unseen_accuracy',
                    ],
                    'required_artifacts': ['metrics.json', 'report.md'],
                },
            }
        }
    )


def test_generic_task_preflight_rejects_omitted_task_spec_metric_keys(
    orchestrator_bundle,
) -> None:
    # Issue #497: the generic evaluator checks task_spec.required_metric_keys
    # after the job runs; the deterministic preflight must catch the omission
    # before any cluster job is submitted.
    _, _, _, _, engine = orchestrator_bundle
    run = _generic_task_run(engine, metric_body='{"accuracy": 0.9}')

    report = preflight_matrix(
        run=run,
        matrix=_single_variant_matrix(),
        contract=engine.contracts.resolve(
            'generic-task-integrity-v1',
            '1.0.0',
        ),
    )

    assert not report.passed
    metric_error = next(
        error for error in report.errors if 'test_unseen_accuracy' in error
    )
    assert 'metrics.json' in metric_error


def test_generic_task_preflight_accepts_task_spec_metric_keys(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    run = _generic_task_run(
        engine,
        metric_body='{"accuracy": 0.9, "test_unseen_accuracy": 0.5}',
    )

    report = preflight_matrix(
        run=run,
        matrix=_single_variant_matrix(),
        contract=engine.contracts.resolve(
            'generic-task-integrity-v1',
            '1.0.0',
        ),
    )

    assert report.passed
    assert report.errors == []
