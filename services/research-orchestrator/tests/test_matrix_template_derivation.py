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

from app.contracts import (
    EvaluationContractResolver,
    compute_contract_digest,
)
from app.schemas import RunCreateRequest

from conftest import RUNNER_IMAGE


def _install_methodology_contract(tmp_path: Path, engine) -> str:
    # Install a comparison-methodology contract so _matrix_template_seeds
    # must derive >= minimum_distinct_values seeds. The engine's resolver
    # checks settings.promoted_contract_root first, so install there.
    contract_id = 'comparison-v1'
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
            'methodology_requirements': [
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
            ],
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