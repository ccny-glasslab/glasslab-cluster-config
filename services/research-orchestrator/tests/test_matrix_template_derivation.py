"""Matrix action template derivation from proposal and contract.

The submit_experiment_matrix template shown to Beaker must derive from the
run's approved proposal and bound contract rather than hardcoded defaults;
otherwise the real-model matrix is rejected for resources, seeds, or
artifacts that contradict the methodology (caught by the rehearsal
harness). These unit tests exercise the derivation without any model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.contracts import (
    EvaluationContractResolver,
    compute_contract_digest,
)
from app.schemas import ExperimentMatrix, ExperimentVariant, RunCreateRequest

from conftest import RUNNER_IMAGE


MODEL_COMPARISON_REQUIREMENTS = [
    {
        'requirement_id': 'model_families',
        'config_path': 'experiment_dimensions.model',
        'mode': 'comparison',
        'minimum_distinct_values': 3,
        'description': 'Compare three model families.',
    },
]


def _install_methodology_contract(
    tmp_path: Path,
    engine,
    *,
    requirements: list[dict] | None = None,
) -> str:
    # Install a comparison-methodology contract so _matrix_template_seeds
    # must derive >= minimum_distinct_values seeds. The engine's resolver
    # checks settings.promoted_contract_root first, so install there.
    contract_id = 'comparison-v1'
    version = '1.0.0'
    root = tmp_path / 'trusted-contracts' / contract_id / version
    root.mkdir(parents=True)
    if requirements is None:
        requirements = [
            {
                'requirement_id': 'method-comparison',
                'config_path': 'config.json',
                'mode': 'comparison',
                'minimum_distinct_values': 3,
                'description': (
                    'Compare three bounded decoding methods: beam search '
                    '(k=5), constrained beam search (k=5), and '
                    'bounded-tree-search (max_nodes=50).'
                ),
            },
            {
                'requirement_id': 'fixed-seed',
                'config_path': 'config.json',
                'mode': 'decision',
                'minimum_distinct_values': 1,
                'description': 'Use fixed seed=42 for reproducibility.',
            },
        ]
    descriptor = {
        'contract_id': contract_id,
        'version': version,
        'evaluation_entry_point': 'evaluator.py',
        'execution_wrapper': 'run_contract.py',
        'expected_input_schema': 'input.schema.json',
        'expected_output_schema': 'output.schema.json',
        'required_artifacts': [
            'metrics.json',
            'metrics.csv',
            'config.json',
            'embeddings/',
            'report.md',
        ],
        'resource_constraints': {
            'cpu': 8.0,
            'memory_gib': 32.0,
            'gpus': 1,
            'wallclock_minutes': 120,
        },
        'container_image_digest': 'sha256:' + '0' * 64,
        'manifest': {
            'primary_metric': 'score',
            'primary_metric_direction': 'maximize',
            'methodology_requirements': requirements,
        },
    }
    (root / 'contract.json').write_text(json.dumps(descriptor, indent=2))
    for name in (
        'evaluator.py',
        'run_contract.py',
        'input.schema.json',
        'output.schema.json',
    ):
        (root / name).write_text('{}\n' if name.endswith('.json') else '# ok\n')
    (root / 'contract.sha256').write_text(compute_contract_digest(root))
    resolved = engine.contracts.resolve(contract_id, version)
    return resolved.digest


def _bind_comparison_contract(tmp_path: Path, store, engine):
    digest = _install_methodology_contract(
        tmp_path,
        engine,
        requirements=MODEL_COMPARISON_REQUIREMENTS,
    )
    run = engine.create_run(
        RunCreateRequest(objective='compare three model families')
    )
    return store.replace_run(
        run.model_copy(
            update={
                'evaluation_contract_id': 'comparison-v1',
                'evaluation_contract_version': '1.0.0',
                'evaluation_contract_digest': digest,
            }
        ),
        expected_version=run.version,
    )


def _variant_name_pattern() -> str:
    # Read the pattern from the schema so the prompt rule cannot silently
    # drift from the validator the agent's matrix is checked against.
    for metadata in ExperimentVariant.model_fields['name'].metadata:
        pattern = getattr(metadata, 'pattern', None)
        if pattern:
            return str(pattern)
    raise AssertionError('ExperimentVariant.name has no pattern constraint')


def test_template_derives_resources_from_proposal(
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='compare three bounded decoding methods')
    )
    proposal = {
        'evaluator_type': 'comparison-v1',
        'primary_metric': {'name': 'score', 'direction': 'maximize'},
        'guardrails': [],
        'required_artifacts': [
            'metrics.json',
            'metrics.csv',
            'config.json',
            'embeddings/',
            'report.md',
        ],
        'budget_mode': 'wallclock',
        'max_wallclock_minutes': 120,
        'resource_constraints': {
            'cpu': 8.0,
            'memory_gib': 32.0,
            'gpus': 1,
            'wallclock_minutes': 120,
        },
        'rationale': 'rehearsal unit test',
    }
    store._save_local_artifact = store.save_artifact
    engine._save_local_artifact(
        run_id=run.run_id,
        artifact_type='evaluation_contract_proposal',
        uri=f'artifact://{run.run_id}/shared/evaluation-contract-proposal.json',
        digest='a' * 64,
        metadata={'proposal': proposal},
    )
    template = engine._matrix_action_template(run)
    args = template['arguments']
    assert args['resources'] == {
        'cpu': 8.0,
        'memory_gib': 32.0,
        'gpus': 1,
        'wallclock_minutes': 120,
    }
    assert 'metrics.csv' in args['required_artifacts']
    assert 'config.json' in args['required_artifacts']
    assert 'embeddings/' in args['required_artifacts']


def test_template_derives_seeds_from_comparison_contract(
    tmp_path,
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    digest = _install_methodology_contract(tmp_path, engine)
    run = engine.create_run(
        RunCreateRequest(objective='compare three bounded decoding methods')
    )
    run = store.replace_run(
        run.model_copy(
            update={
                'evaluation_contract_id': 'comparison-v1',
                'evaluation_contract_version': '1.0.0',
                'evaluation_contract_digest': digest,
            }
        ),
        expected_version=run.version,
    )
    proposal = {
        'evaluator_type': 'comparison-v1',
        'primary_metric': {'name': 'score', 'direction': 'maximize'},
        'guardrails': [],
        'required_artifacts': ['metrics.json'],
        'budget_mode': 'wallclock',
        'max_wallclock_minutes': 120,
        'resource_constraints': {
            'cpu': 1.0,
            'memory_gib': 2.0,
            'gpus': 0,
            'wallclock_minutes': 30,
        },
        'rationale': 'rehearsal unit test',
    }
    engine._save_local_artifact(
        run_id=run.run_id,
        artifact_type='evaluation_contract_proposal',
        uri=f'artifact://{run.run_id}/shared/evaluation-contract-proposal.json',
        digest='b' * 64,
        metadata={'proposal': proposal},
    )
    template = engine._matrix_action_template(run)
    seeds = template['arguments']['seeds']
    assert len(seeds) >= 3, f'comparison contract needs >=3 seeds, got {seeds}'
    assert len(set(seeds)) == len(seeds), 'seeds must be unique'


def test_template_emits_non_empty_variants_for_comparison_requirement(
    tmp_path,
    orchestrator_bundle,
) -> None:
    # Issue #474: the demonstrated shape must carry one distinct, non-empty
    # variant per required compared method. A single variant with empty
    # overrides is exactly what Honeydew's methodology review rejects.
    _, store, _, _, engine = orchestrator_bundle
    run = _bind_comparison_contract(tmp_path, store, engine)
    template = engine._matrix_action_template(run)
    matrix = ExperimentMatrix.model_validate(template['arguments'])
    assert len(matrix.variants) == 3
    assert len(matrix.seeds) >= 3
    assert all(variant.overrides for variant in matrix.variants)
    assert len({variant.name for variant in matrix.variants}) == 3
    values = [
        variant.overrides['experiment_dimensions.model']
        for variant in matrix.variants
    ]
    assert len(set(values)) == 3, values


def test_template_keeps_single_candidate_without_comparison_requirement(
    tmp_path,
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    digest = _install_methodology_contract(
        tmp_path,
        engine,
        requirements=[
            {
                'requirement_id': 'fixed_seed',
                'config_path': 'experiment_dimensions.seed',
                'mode': 'decision',
                'minimum_distinct_values': 1,
                'description': 'Use one fixed seed.',
            },
        ],
    )
    run = engine.create_run(
        RunCreateRequest(objective='run one fixed decision only')
    )
    run = store.replace_run(
        run.model_copy(
            update={
                'evaluation_contract_id': 'comparison-v1',
                'evaluation_contract_version': '1.0.0',
                'evaluation_contract_digest': digest,
            }
        ),
        expected_version=run.version,
    )
    template = engine._matrix_action_template(run)
    assert template['arguments']['variants'] == [
        {'name': 'candidate', 'overrides': {}}
    ]


def _capture_prompt(monkeypatch, engine, call) -> str:
    captured: dict[str, str] = {}

    def capture_turn(**kwargs):
        captured['prompt'] = kwargs['prompt']
        raise RuntimeError('prompt captured')

    monkeypatch.setattr(engine, '_run_agent_turn', capture_turn)
    with pytest.raises(RuntimeError, match='prompt captured'):
        call()
    return captured['prompt']


def test_implementation_prompt_states_variant_rules(
    tmp_path,
    orchestrator_bundle,
    monkeypatch,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = _bind_comparison_contract(tmp_path, store, engine)
    (Path(run.beaker_workspace) / 'implementation-plan.md').write_text('# Plan\n')
    prompt = _capture_prompt(
        monkeypatch,
        engine,
        lambda: engine._beaker_implement(run.run_id),
    )
    assert _variant_name_pattern() in prompt
    assert 'one variant per required distinct method' in prompt
    assert 'NON-EMPTY' in prompt
    assert 'experiment_dimensions.model' in prompt


def test_revision_prompt_states_variant_rules(
    tmp_path,
    orchestrator_bundle,
    monkeypatch,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = _bind_comparison_contract(tmp_path, store, engine)
    prompt = _capture_prompt(
        monkeypatch,
        engine,
        lambda: engine._beaker_revise(
            run.run_id,
            feedback=(
                'Methodologically unacceptable: the matrix proposes one '
                'variant with empty overrides for a required comparison.'
            ),
        ),
    )
    assert _variant_name_pattern() in prompt
    assert 'one variant per required distinct method' in prompt
    assert 'NON-EMPTY' in prompt
