"""Matrix-preflight convergence and non-convergence (issue #457).

Covers the two halves of the fix for the deterministic revision loop:

* B1 — when a methodology/``config_path`` preflight rejection is about to hand
  Beaker a revision, the engine materializes the required dotted keys in the
  file named by ``matrix.base_config`` (creating the file/skeleton if missing)
  with at least ``minimum_distinct_values`` distinct placeholder values. The
  repair is bounded and idempotent: existing valid values survive, unrelated
  keys are preserved, and a second pass makes no change. The engine owns the
  *shape*; the agent still owns the *values*.
* B2 — a repeated identical ``submit_experiment_matrix`` preflight rejection
  (same normalized error set and same base_config digest) is a deterministic
  fixed point, so the orchestrator stops auto-revising and pauses for human
  resolution instead of burning the revision budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.engine import ResearchOrchestrator
from app.methodology_config import (
    MethodologyConfigRepair,
    repair_methodology_settings,
)
from app.preflight import MethodologyRequirement, preflight_matrix
from app.schemas import (
    ActionRecord,
    AgentName,
    ApprovalStatus,
    ExperimentMatrix,
    PolicyClassification,
    RunCreateRequest,
    RunState,
)
from app.storage import SqliteStore

from conftest import RUNNER_IMAGE


ADULT_CONTRACT_ID = 'ml-benchmark-adult-income-v1'
ADULT_CONTRACT_VERSION = '1.1.0'

ADULT_SOURCE = (
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


def _requirements() -> list[MethodologyRequirement]:
    return [
        MethodologyRequirement(
            requirement_id='model_families',
            config_path='experiment_dimensions.model',
            mode='comparison',
            minimum_distinct_values=2,
            description='Compare two model families.',
        ),
        MethodologyRequirement(
            requirement_id='search_technique',
            config_path='experiment_dimensions.search_technique',
            mode='comparison',
            minimum_distinct_values=2,
            description='Compare two search techniques.',
        ),
        MethodologyRequirement(
            requirement_id='missing_data_strategy',
            config_path='experiment_dimensions.missing_strategy',
            mode='decision',
            minimum_distinct_values=1,
            maximum_distinct_values=1,
            description='Choose one missing-data strategy.',
        ),
    ]


def _matrix(*, base_config: str = 'configs/candidate.yaml') -> ExperimentMatrix:
    return ExperimentMatrix.model_validate(
        {
            'base_config': base_config,
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


def _bind_adult_contract(engine: ResearchOrchestrator, store: SqliteStore, run_id: str) -> None:
    contract = engine.contracts.resolve(ADULT_CONTRACT_ID, ADULT_CONTRACT_VERSION)
    run = store.get_run(run_id)
    store.replace_run(
        run.model_copy(
            update={
                'evaluation_contract_id': ADULT_CONTRACT_ID,
                'evaluation_contract_version': ADULT_CONTRACT_VERSION,
                'evaluation_contract_digest': contract.digest,
            }
        ),
        expected_version=run.version,
    )


def _rejected_matrix_action(
    *,
    run_id: str,
    base_config: str,
    ordinal: int,
) -> ActionRecord:
    return ActionRecord(
        run_id=run_id,
        proposed_by=AgentName.BEAKER,
        type='submit_experiment_matrix',
        arguments=_matrix(base_config=base_config).model_dump(mode='json'),
        policy_classification=PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL,
        approval_status=ApprovalStatus.REJECTED,
        reason='Deterministic matrix preflight failed: missing methodology setting',
        idempotency_key=f'rejected-matrix-{ordinal}',
    )


# ---------------------------------------------------------------------------
# B1 — deterministic, idempotent shape materialization
# ---------------------------------------------------------------------------


def test_repair_materializes_missing_required_shape(tmp_path: Path) -> None:
    base_config = tmp_path / 'configs' / 'candidate.yaml'
    base_config.parent.mkdir(parents=True)
    base_config.write_text('learning_rate: 0.0001\n')

    repair = repair_methodology_settings(
        base_config_path=base_config,
        requirements=_requirements(),
    )

    assert isinstance(repair, MethodologyConfigRepair)
    assert repair.created is False
    assert repair.changed is True
    data = yaml.safe_load(base_config.read_text(encoding='utf-8'))
    # Unrelated keys are preserved.
    assert data['learning_rate'] == 0.0001
    dimensions = data['experiment_dimensions']
    assert len({str(value) for value in dimensions['model']}) >= 2
    assert len({str(value) for value in dimensions['search_technique']}) >= 2
    assert len({str(value) for value in dimensions['missing_strategy']}) >= 1
    # The inserted values are deterministic placeholders the agent must replace.
    text = base_config.read_text(encoding='utf-8')
    assert 'model-candidate-1' in text
    assert 'search_technique-candidate-1' in text
    assert '# placeholder' in text


def test_repair_tops_up_existing_values_without_clobbering(tmp_path: Path) -> None:
    base_config = tmp_path / 'configs' / 'candidate.yaml'
    base_config.parent.mkdir(parents=True)
    base_config.write_text(
        'experiment_dimensions:\n'
        '  model: [linear_regression, random_forest]\n'
    )

    repair = repair_methodology_settings(
        base_config_path=base_config,
        requirements=_requirements(),
    )

    assert repair.changed is True
    data = yaml.safe_load(base_config.read_text(encoding='utf-8'))
    model_values = [str(value) for value in data['experiment_dimensions']['model']]
    # Existing meaningful values survive; only the shortfall is topped up.
    assert 'linear_regression' in model_values
    assert 'random_forest' in model_values
    assert len({*model_values}) >= 2


def test_repair_is_idempotent(tmp_path: Path) -> None:
    base_config = tmp_path / 'configs' / 'candidate.yaml'
    base_config.parent.mkdir(parents=True)
    base_config.write_text('learning_rate: 0.0001\n')

    first = repair_methodology_settings(
        base_config_path=base_config,
        requirements=_requirements(),
    )
    after_first = base_config.read_text(encoding='utf-8')
    second = repair_methodology_settings(
        base_config_path=base_config,
        requirements=_requirements(),
    )

    assert first.changed is True
    assert second.changed is False
    assert base_config.read_text(encoding='utf-8') == after_first


def test_repair_handles_slash_containing_dotted_keys(tmp_path: Path) -> None:
    base_config = tmp_path / 'configs' / 'candidate.yaml'
    base_config.parent.mkdir(parents=True)
    base_config.write_text('seeds: [17]\n')

    repair = repair_methodology_settings(
        base_config_path=base_config,
        requirements=[
            MethodologyRequirement(
                requirement_id='train-visibility',
                config_path='src/train.py',
                mode='comparison',
                minimum_distinct_values=2,
                description='Two trainable entrypoints.',
            )
        ],
    )

    assert repair.changed is True
    data = yaml.safe_load(base_config.read_text(encoding='utf-8'))
    # The key containing a slash stays a single dotted component, exactly as
    # preflight._config_value resolves it.
    assert len({str(value) for value in data['src/train']['py']}) >= 2


def test_repair_replaces_metadata_wrapper_with_value_list(tmp_path: Path) -> None:
    base_config = tmp_path / 'configs' / 'candidate.yaml'
    base_config.parent.mkdir(parents=True)
    base_config.write_text(
        'experiment_dimensions:\n'
        '  model:\n'
        '    description: compare models\n'
        '    values: [linear_regression]\n'
    )

    repair = repair_methodology_settings(
        base_config_path=base_config,
        requirements=[
            MethodologyRequirement(
                requirement_id='model_families',
                config_path='experiment_dimensions.model',
                mode='comparison',
                minimum_distinct_values=2,
                description='Compare model families.',
            )
        ],
    )

    assert repair.changed is True
    data = yaml.safe_load(base_config.read_text(encoding='utf-8'))
    model = data['experiment_dimensions']['model']
    assert isinstance(model, list)
    assert len({str(value) for value in model}) >= 2


def test_repair_creates_missing_base_config(tmp_path: Path) -> None:
    base_config = tmp_path / 'nested' / 'configs' / 'candidate.yaml'
    assert not base_config.exists()

    repair = repair_methodology_settings(
        base_config_path=base_config,
        requirements=_requirements(),
    )

    assert repair.created is True
    assert repair.changed is True
    assert base_config.is_file()
    data = yaml.safe_load(base_config.read_text(encoding='utf-8'))
    assert len({str(value) for value in data['experiment_dimensions']['model']}) >= 2


def test_repaired_config_passes_structural_preflight(orchestrator_bundle) -> None:
    _, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        request=RunCreateRequest(objective='Repair then pass preflight.')
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('learning_rate: 0.0001\n')
    source = workspace / 'benchmark-workspace' / 'adult-income'
    source.mkdir(parents=True)
    (source / 'run.py').write_text(ADULT_SOURCE)

    contract = engine.contracts.resolve(ADULT_CONTRACT_ID, ADULT_CONTRACT_VERSION)
    requirements = [
        MethodologyRequirement.model_validate(item)
        for item in contract.descriptor.manifest['methodology_requirements']
    ]
    repair = repair_methodology_settings(
        base_config_path=config,
        requirements=requirements,
    )
    assert repair.changed is True

    report = preflight_matrix(
        run=run.model_copy(
            update={
                'task_definition': {
                    'source_subdirectory': 'benchmark-workspace/adult-income',
                }
            }
        ),
        matrix=_matrix(),
        contract=contract,
    )

    assert report.passed, report.errors
    assert report.comparisons['model_families']
    assert len(report.comparisons['model_families']) >= 2


def test_beaker_revise_materializes_shape_on_methodology_preflight_failure(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        request=RunCreateRequest(objective='Repair before the revise turn.')
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('learning_rate: 0.0001\n')
    _bind_adult_contract(engine, store, run.run_id)
    store.save_action(
        _rejected_matrix_action(
            run_id=run.run_id,
            base_config='configs/candidate.yaml',
            ordinal=1,
        )
    )
    captured: dict[str, str] = {}

    def capture_turn(**kwargs) -> None:
        captured['prompt'] = kwargs['prompt']
        raise RuntimeError('prompt captured')

    monkeypatch.setattr(engine, '_run_agent_turn', capture_turn)

    with pytest.raises(RuntimeError, match='prompt captured'):
        engine._beaker_revise(
            run.run_id,
            feedback=(
                'Deterministic matrix preflight failed: missing methodology '
                'setting `experiment_dimensions.model`'
            ),
        )

    data = yaml.safe_load(config.read_text(encoding='utf-8'))
    assert len(
        {str(value) for value in data['experiment_dimensions']['model']}
    ) >= 2
    # The revise prompt still tells the agent the engine only guarantees shape.
    assert 'placeholder' in captured['prompt']
    assert 'meaningful' in captured['prompt']


# ---------------------------------------------------------------------------
# B2 — non-convergence detection
# ---------------------------------------------------------------------------


def test_repeated_identical_preflight_rejection_escalates_to_human(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        request=RunCreateRequest(objective='Detect a deterministic fixed point.')
    )
    workspace = Path(run.beaker_workspace)
    config = workspace / 'configs' / 'candidate.yaml'
    config.parent.mkdir(parents=True, exist_ok=True)
    # The agent's rejected matrix never contains the required keys and the
    # deterministic model would emit exactly the same bytes again.
    config.write_text('learning_rate: 0.0001\n')
    _bind_adult_contract(engine, store, run.run_id)
    current = store.get_run(run.run_id)
    store.replace_run(
        current.model_copy(update={'state': RunState.HONEYDEW_REVIEWING}),
        expected_version=current.version,
    )

    def save_pending_matrix() -> None:
        store.save_action(
            ActionRecord(
                run_id=run.run_id,
                proposed_by=AgentName.BEAKER,
                type='submit_experiment_matrix',
                arguments=_matrix().model_dump(mode='json'),
                policy_classification=(
                    PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL
                ),
                approval_status=ApprovalStatus.PENDING,
                reason='Re-proposed the identical matrix.',
                idempotency_key=f'pending-{len(store.list_actions(run.run_id))}',
            )
        )

    revised: list[str] = []

    def fake_revise(run_id: str, *, feedback: str) -> None:
        revised.append(feedback)
        save_pending_matrix()

    monkeypatch.setattr(engine, '_beaker_revise', fake_revise)

    save_pending_matrix()
    engine._honeydew_review(run.run_id, implementation_turn_id='turn-1')
    assert store.get_run(run.run_id).state == RunState.BEAKER_REVISING
    assert len(revised) == 1

    engine._honeydew_review(run.run_id, implementation_turn_id='turn-2')

    paused = store.get_run(run.run_id)
    assert paused.state == RunState.PAUSED
    # The second identical rejection must not consume another revision turn.
    assert len(revised) == 1
    assert paused.methodology_revision_count == 1
    assert any(
        event.event_type == 'methodology.non_convergence_detected'
        for event in store.list_events(run.run_id)
    )
    paused_event = next(
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'run.paused'
    )
    assert 'repeat' in str(paused_event.payload['reason']).lower()
