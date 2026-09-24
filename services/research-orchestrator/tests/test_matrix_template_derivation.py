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

from app import matrix_naming
from app.contracts import (
    EvaluationContractResolver,
    compute_contract_digest,
)
from app.matrix_naming import (
    VARIANT_NAME_PATTERN,
    VARIANT_NAME_RE,
    render_variant_rules_guidance,
    variant_name_from_value,
)
from app.schemas import ExperimentMatrix, ExperimentVariant, RunCreateRequest

from conftest import RUNNER_IMAGE


MODEL_COMPARISON_REQUIREMENTS = [
    {
        'requirement_id': 'model_families',
        'config_path': 'experiment_dimensions.model',
        'mode': 'comparison',
        'comparison_scope': 'across_jobs',
        'minimum_distinct_values': 3,
        'description': 'Compare three model families.',
    },
]

WITHIN_JOB_COMPARISON_REQUIREMENTS = [
    {
        'requirement_id': 'model_families',
        'config_path': 'experiment_dimensions.model',
        'mode': 'comparison',
        'comparison_scope': 'within_job',
        'minimum_distinct_values': 3,
        'description': 'Compare three model families within one job.',
    },
]

MIXED_COMPARISON_REQUIREMENTS = [
    {
        'requirement_id': 'model_families',
        'config_path': 'experiment_dimensions.model',
        'mode': 'comparison',
        'comparison_scope': 'across_jobs',
        'minimum_distinct_values': 2,
        'description': 'Split model families into separate jobs.',
    },
    {
        'requirement_id': 'feature_sets',
        'config_path': 'experiment_dimensions.feature_sets',
        'mode': 'comparison',
        'comparison_scope': 'within_job',
        'minimum_distinct_values': 2,
        'description': 'Compare feature sets inside each job.',
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
                'comparison_scope': 'within_job',
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


def _bind_comparison_contract(
    tmp_path: Path,
    store,
    engine,
    *,
    requirements: list[dict] | None = None,
):
    digest = _install_methodology_contract(
        tmp_path,
        engine,
        requirements=requirements or MODEL_COMPARISON_REQUIREMENTS,
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


def test_variant_name_pattern_has_one_source() -> None:
    # Issue #501: the schema validator must derive from the shared constant
    # rather than restating the pattern.
    assert _variant_name_pattern() == VARIANT_NAME_PATTERN


def test_variant_rules_guidance_renders_the_pattern_argument() -> None:
    # The prompt text must be rendered from the pattern it is given, not from
    # a stored copy in the prose: rendering an arbitrary pattern yields that
    # pattern, and the shipped guidance carries the single-source constant.
    custom_pattern = r'^[a-z]{2,5}$'
    rendered = render_variant_rules_guidance(custom_pattern)
    assert custom_pattern in rendered
    assert VARIANT_NAME_PATTERN not in rendered
    assert VARIANT_NAME_PATTERN in render_variant_rules_guidance(
        VARIANT_NAME_PATTERN
    )


def test_variant_rules_guidance_guard_rejects_a_dropped_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The runtime guard: if a prompt edit removes the pattern placeholder, the
    # renderer must fail loudly instead of shipping guidance that no longer
    # names the validator pattern.
    monkeypatch.setattr(
        matrix_naming,
        '_VARIANT_RULES_TEMPLATE',
        'Variant naming rules with no pattern placeholder at all.',
    )
    with pytest.raises(RuntimeError, match='placeholder'):
        render_variant_rules_guidance(VARIANT_NAME_PATTERN)


@pytest.mark.parametrize(
    'value',
    [
        '',
        '   ',
        '--__--',
        '0',
        'ABC',
        'already-valid',
        'Logistic Regression C=1.0',
        'method/v2: beta',
        'Ünïcode Çhars',
        'x' * 200,
    ],
)
def test_variant_name_sanitizer_output_conforms_to_the_single_pattern(
    value: str,
) -> None:
    name = variant_name_from_value(value)
    assert VARIANT_NAME_RE.fullmatch(name) is not None
    assert ExperimentVariant(name=name, overrides={}).name == name
    assert variant_name_from_value(value) == name


def test_variant_name_sanitizer_derives_readable_names() -> None:
    assert variant_name_from_value('gradient-boosting') == 'gradient-boosting'
    assert (
        variant_name_from_value('Logistic Regression C=1.0')
        == 'logistic-regression-c-1-0'
    )
    assert variant_name_from_value('') == 'candidate'


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


def test_template_emits_non_empty_variants_for_across_jobs_comparison(
    tmp_path,
    orchestrator_bundle,
) -> None:
    # Issue #474: an across_jobs comparison runs one job per method, so the
    # demonstrated shape must carry one distinct, non-empty variant per
    # required compared method and a single canonical seed.
    _, store, _, _, engine = orchestrator_bundle
    run = _bind_comparison_contract(tmp_path, store, engine)
    template = engine._matrix_action_template(run)
    matrix = ExperimentMatrix.model_validate(template['arguments'])
    assert len(matrix.variants) == 3
    assert matrix.seeds == [17]
    assert all(variant.overrides for variant in matrix.variants)
    assert len({variant.name for variant in matrix.variants}) == 3
    values = [
        variant.overrides['experiment_dimensions.model']
        for variant in matrix.variants
    ]
    assert len(set(values)) == 3, values


def test_template_emits_single_candidate_for_within_job_comparison(
    tmp_path,
    orchestrator_bundle,
) -> None:
    # A within_job comparison runs every compared method inside one job, so the
    # demonstrated shape is a single candidate variant with empty overrides and
    # the replication seeds live in the matrix, not the variants.
    _, store, _, _, engine = orchestrator_bundle
    run = _bind_comparison_contract(
        tmp_path,
        store,
        engine,
        requirements=WITHIN_JOB_COMPARISON_REQUIREMENTS,
    )
    template = engine._matrix_action_template(run)
    matrix = ExperimentMatrix.model_validate(template['arguments'])
    assert [variant.model_dump() for variant in matrix.variants] == [
        {'name': 'candidate', 'overrides': {}}
    ]
    assert len(matrix.seeds) >= 3
    assert len(set(matrix.seeds)) == len(matrix.seeds)


def test_template_mixed_scope_splits_primary_axis_and_replicates(
    tmp_path,
    orchestrator_bundle,
) -> None:
    # A mixed contract splits the across_jobs axis into one variant per value
    # while leaving the within_job axis to run inside each job; the seed floor
    # follows the within_job requirement (>= MIN_COMPARISON_SEEDS).
    _, store, _, _, engine = orchestrator_bundle
    run = _bind_comparison_contract(
        tmp_path,
        store,
        engine,
        requirements=MIXED_COMPARISON_REQUIREMENTS,
    )
    template = engine._matrix_action_template(run)
    matrix = ExperimentMatrix.model_validate(template['arguments'])
    assert len(matrix.variants) == 2
    assert all(variant.overrides for variant in matrix.variants)
    assert all(
        'experiment_dimensions.feature_sets' not in variant.overrides
        for variant in matrix.variants
    )
    split_values = {
        variant.overrides['experiment_dimensions.model']
        for variant in matrix.variants
    }
    assert len(split_values) == 2, split_values
    assert len(matrix.seeds) >= 3


def test_execution_note_pure_across_jobs_runs_one_configuration(
    tmp_path,
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = _bind_comparison_contract(tmp_path, store, engine)

    note = engine._across_jobs_execution_note(run.run_id)

    assert 'EXACTLY ONE effective configuration' in note


def test_execution_note_mixed_honors_split_and_iterates_within_axes(
    tmp_path,
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = _bind_comparison_contract(
        tmp_path,
        store,
        engine,
        requirements=MIXED_COMPARISON_REQUIREMENTS,
    )

    note = engine._across_jobs_execution_note(run.run_id)

    assert 'experiment_dimensions.feature_sets' in note
    assert 'iterate' in note.lower()
    assert 'EXACTLY ONE effective configuration' not in note


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
