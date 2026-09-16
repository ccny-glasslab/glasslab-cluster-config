"""Single-valued resource authority for imported benchmarks (issue #483).

An imported benchmark run carries a compiled task with a preselected runtime
resource profile. That profile is the single authority for the experiment
matrix's resources; the evaluation contract's ``resource_constraints`` are a
compatibility envelope the profile must fit inside. The live Titanic run
(profile wallclock 60, contract wallclock 30) oscillated between the two
deterministic preflight rules -- exact profile match vs. contract ceiling --
and burned its revision budget. These tests pin the replacement behavior:

* a profile that cannot fit the contract fails closed once, with an actionable
  message naming the dimension and both values, and never becomes a revision;
* a contract that accommodates the profile converges on the first proposal;
* the matrix action template emits the profile's exact resources;
* the contradiction is caught at contract-seal time without a redraft.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.contracts import compute_contract_digest
from app.discord_adapter import DisabledDiscordAdapter
from app.engine import ResearchOrchestrator
from app.mock_runtime import ScriptedMockRuntime
from app.preflight import profile_contract_resource_conflicts
from app.schemas import (
    ActionRecord,
    AgentName,
    AgentTurnResult,
    ApprovalStatus,
    PolicyClassification,
    RequestedAction,
    ResourceRequest,
    RunCreateRequest,
    RunState,
    TurnKind,
)

from conftest import RUNNER_IMAGE


# The exact values of the platform's cpu-ml-standard-v1 runtime profile
# (app/task_bundles.py) and the live contract's resource_constraints.
CPU_PROFILE: dict[str, object] = {
    'cpu': 4,
    'memory_gib': 8,
    'gpus': 0,
    'wallclock_minutes': 60,
}
CONTRACT_CONSTRAINTS: dict[str, object] = {
    'cpu': 8.0,
    'memory_gib': 32.0,
    'gpus': 0,
    'wallclock_minutes': 30,
}
ACCOMMODATING_CONSTRAINTS: dict[str, object] = {
    'cpu': 8.0,
    'memory_gib': 32.0,
    'gpus': 0,
    'wallclock_minutes': 120,
}

CPU_PROFILE_TASK_DEFINITION: dict[str, object] = {
    'source_subdirectory': 'benchmark-workspace/titanic',
    'runner_image': RUNNER_IMAGE,
    'resources': dict(CPU_PROFILE),
    'required_artifacts': ['metrics.json'],
    'datasets': [],
}


def _install_contract(
    tmp_path: Path,
    engine: ResearchOrchestrator,
    *,
    resource_constraints: dict[str, object],
    contract_id: str = 'example-research-v1',
    version: str = '9.9.9',
) -> object:
    # The engine resolver checks settings.promoted_contract_root first, so the
    # synthetic descriptor is installed there. Its identity matches the mock
    # protocol proposal's evaluator_type so the binding stays compatible.
    root = tmp_path / 'trusted-contracts' / contract_id / version
    root.mkdir(parents=True, exist_ok=True)
    descriptor = {
        'contract_id': contract_id,
        'version': version,
        'manifest': {
            'primary_metric': 'score',
            'primary_metric_direction': 'maximize',
        },
        'execution_wrapper': 'run_contract.py',
        'evaluation_entry_point': 'evaluator.py',
        'expected_input_schema': 'input.schema.json',
        'expected_output_schema': 'output.schema.json',
        'required_artifacts': ['metrics.json', 'evaluation.json'],
        'resource_constraints': resource_constraints,
        'container_image_digest': None,
    }
    (root / 'contract.json').write_text(json.dumps(descriptor, indent=2))
    (root / 'run_contract.py').write_text('# wrapper\n')
    (root / 'evaluator.py').write_text('# evaluator\n')
    (root / 'input.schema.json').write_text('{"type": "object"}\n')
    (root / 'output.schema.json').write_text('{"type": "object"}\n')
    (root / 'contract.sha256').write_text(compute_contract_digest(root))
    return engine.contracts.resolve(contract_id, version)


def _bind_task_profile_and_contract(
    tmp_path: Path,
    store,
    engine: ResearchOrchestrator,
    run,
    *,
    resource_constraints: dict[str, object],
    task_definition: dict[str, object] | None = None,
):
    contract = _install_contract(
        tmp_path,
        engine,
        resource_constraints=resource_constraints,
    )
    current = store.get_run(run.run_id)
    return (
        store.replace_run(
            current.model_copy(
                update={
                    'task_definition': dict(
                        task_definition or CPU_PROFILE_TASK_DEFINITION
                    ),
                    'evaluation_contract_id': contract.descriptor.contract_id,
                    'evaluation_contract_version': contract.descriptor.version,
                    'evaluation_contract_digest': contract.digest,
                }
            ),
            expected_version=current.version,
        ),
        contract,
    )


def _matrix_arguments(resources: dict[str, object]) -> dict[str, object]:
    return {
        'base_config': 'configs/candidate.yaml',
        'variants': [{'name': 'candidate', 'overrides': {}}],
        'seeds': [17],
        'maximum_parallel_jobs': 1,
        'runner_image': RUNNER_IMAGE,
        'resources': dict(resources),
        'required_artifacts': ['metrics.json'],
    }


def _save_pending_matrix(
    store,
    *,
    run_id: str,
    resources: dict[str, object],
    ordinal: str,
) -> ActionRecord:
    return store.save_action(
        ActionRecord(
            run_id=run_id,
            proposed_by=AgentName.BEAKER,
            type='submit_experiment_matrix',
            arguments=_matrix_arguments(resources),
            policy_classification=(
                PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL
            ),
            approval_status=ApprovalStatus.PENDING,
            reason='Proposed the bounded matrix.',
            idempotency_key=f'pending-matrix-{ordinal}',
        )
    )


def _pending_action(store, run_id: str, action_type: str) -> ActionRecord:
    return next(
        action
        for action in store.list_actions(run_id)
        if action.type == action_type
        and action.approval_status == ApprovalStatus.PENDING
    )


def _events(store, run_id: str, event_type: str) -> list:
    return [
        event
        for event in store.list_events(run_id)
        if event.event_type == event_type
    ]


# ---------------------------------------------------------------------------
# The contradiction: profile wallclock 60 vs contract wallclock 30
# ---------------------------------------------------------------------------


def test_profile_wallclock_above_contract_parks_once_without_revision(
    tmp_path: Path,
    orchestrator_bundle,
    monkeypatch,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Reject a contract that cannot fit the task profile.'
        )
    )
    run, _ = _bind_task_profile_and_contract(
        tmp_path,
        store,
        engine,
        run,
        resource_constraints=CONTRACT_CONSTRAINTS,
    )
    current = store.get_run(run.run_id)
    store.replace_run(
        current.model_copy(update={'state': RunState.HONEYDEW_REVIEWING}),
        expected_version=current.version,
    )
    action = _save_pending_matrix(
        store,
        run_id=run.run_id,
        resources=CPU_PROFILE,
        ordinal='1',
    )
    revised: list[str] = []
    monkeypatch.setattr(
        engine,
        '_beaker_revise',
        lambda run_id, *, feedback: revised.append(feedback),
    )
    turn_before = store.get_run(run.run_id).turn_number

    engine._honeydew_review(run.run_id, implementation_turn_id='turn-1')

    parked = store.get_run(run.run_id)
    assert parked.state == RunState.PAUSED
    # The contradiction is a configuration problem, not a model failure: it
    # consumes no revision, no agent turn, and no retry.
    assert revised == []
    assert parked.methodology_revision_count == 0
    assert parked.turn_number == turn_before
    assert _events(store, run.run_id, 'methodology.revision_requested') == []

    rejections = [
        event
        for event in _events(store, run.run_id, 'action.rejected')
        if event.payload.get('action_id') == action.action_id
    ]
    assert len(rejections) == 1
    reason = str(rejections[0].payload['reason'])
    assert 'wallclock_minutes' in reason
    assert '60' in reason
    assert '30' in reason

    conflicts = _events(store, run.run_id, 'methodology.resource_authority_conflict')
    assert len(conflicts) == 1
    pause = _events(store, run.run_id, 'run.paused')
    assert len(pause) == 1
    pause_reason = str(pause[0].payload['reason'])
    assert 'wallclock_minutes' in pause_reason
    assert '60' in pause_reason
    assert '30' in pause_reason
    assert 'human resolution' in pause_reason.lower()


def test_contract_matching_guess_does_not_restart_the_loop(
    tmp_path: Path,
    orchestrator_bundle,
    monkeypatch,
) -> None:
    # The live oscillation: the model answers the contract-ceiling rejection
    # with wallclock 30, which then violates the exact-profile-match rule.
    # Once the authority contradiction is detected, a different matrix guess
    # must not produce a revision either.
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Never alternate between the two rejections.')
    )
    run, _ = _bind_task_profile_and_contract(
        tmp_path,
        store,
        engine,
        run,
        resource_constraints=CONTRACT_CONSTRAINTS,
    )
    current = store.get_run(run.run_id)
    store.replace_run(
        current.model_copy(update={'state': RunState.HONEYDEW_REVIEWING}),
        expected_version=current.version,
    )
    _save_pending_matrix(
        store,
        run_id=run.run_id,
        resources=CPU_PROFILE,
        ordinal='1',
    )
    revised: list[str] = []
    monkeypatch.setattr(
        engine,
        '_beaker_revise',
        lambda run_id, *, feedback: revised.append(feedback),
    )
    engine._honeydew_review(run.run_id, implementation_turn_id='turn-1')

    # The model's next guess matches the contract ceiling instead of the
    # profile; the deterministic preflight still parks without a revision.
    _save_pending_matrix(
        store,
        run_id=run.run_id,
        resources={**CPU_PROFILE, 'wallclock_minutes': 30},
        ordinal='2',
    )
    engine._honeydew_review(run.run_id, implementation_turn_id='turn-2')

    parked = store.get_run(run.run_id)
    assert parked.state == RunState.PAUSED
    assert revised == []
    assert parked.methodology_revision_count == 0
    assert _events(store, run.run_id, 'methodology.revision_requested') == []


def test_contract_accommodating_profile_converges_on_first_proposal(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Converge when the contract fits the profile.')
    )
    workspace = Path(run.beaker_workspace)
    (workspace / 'configs').mkdir(parents=True, exist_ok=True)
    (workspace / 'configs' / 'candidate.yaml').write_text(
        'model: [logistic_regression]\n'
    )
    (workspace / 'implementation-plan.md').write_text('# Plan\n')
    source = workspace / 'benchmark-workspace' / 'titanic'
    source.mkdir(parents=True)
    (source / 'run.py').write_text(
        'import json\n'
        'with open("metrics.json", "w") as handle:\n'
        '    json.dump({"score": 1.0}, handle)\n'
    )
    run, _ = _bind_task_profile_and_contract(
        tmp_path,
        store,
        engine,
        run,
        resource_constraints=ACCOMMODATING_CONSTRAINTS,
    )
    current = store.get_run(run.run_id)
    store.replace_run(
        current.model_copy(update={'state': RunState.HONEYDEW_REVIEWING}),
        expected_version=current.version,
    )
    action = _save_pending_matrix(
        store,
        run_id=run.run_id,
        resources=CPU_PROFILE,
        ordinal='1',
    )

    engine._honeydew_review(run.run_id, implementation_turn_id='turn-1')

    approved = store.get_run(run.run_id)
    assert approved.state == RunState.AWAITING_EXECUTION_APPROVAL
    assert store.get_action(action.action_id).honeydew_approved is True
    assert _events(store, run.run_id, 'action.rejected') == []
    assert _events(store, run.run_id, 'methodology.revision_requested') == []
    assert approved.methodology_revision_count == 0


def test_matrix_template_carries_profile_exact_resources(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Copy the preselected profile exactly.')
    )
    template = engine._matrix_action_template(
        run.model_copy(update={'task_definition': CPU_PROFILE_TASK_DEFINITION})
    )
    arguments = template['arguments']
    assert arguments['resources'] == CPU_PROFILE
    assert arguments['runner_image'] == RUNNER_IMAGE
    assert arguments['required_artifacts'] == ['metrics.json']


# ---------------------------------------------------------------------------
# Unit coverage for the pure conflict detector
# ---------------------------------------------------------------------------


def test_profile_contract_conflicts_reports_each_offending_dimension() -> None:
    conflicts = profile_contract_resource_conflicts(
        profile={
            'cpu': 4,
            'memory_gib': 8,
            'gpus': 0,
            'wallclock_minutes': 60,
        },
        constraints=ResourceRequest(
            cpu=8.0,
            memory_gib=32.0,
            gpus=0,
            wallclock_minutes=30,
        ),
    )
    assert [item.dimension for item in conflicts] == ['wallclock_minutes']
    assert float(conflicts[0].profile_value) == 60.0
    assert float(conflicts[0].contract_value) == 30.0


def test_profile_within_contract_has_no_conflicts() -> None:
    assert (
        profile_contract_resource_conflicts(
            profile={
                'cpu': 4,
                'memory_gib': 8,
                'gpus': 0,
                'wallclock_minutes': 60,
            },
            constraints=ResourceRequest(
                cpu=8.0,
                memory_gib=32.0,
                gpus=0,
                wallclock_minutes=60,
            ),
        )
        == []
    )


def test_profile_conflicts_ignore_undeclared_dimensions() -> None:
    assert (
        profile_contract_resource_conflicts(
            profile={'wallclock_minutes': 30},
            constraints=ResourceRequest(
                cpu=1.0,
                memory_gib=1.0,
                gpus=0,
                wallclock_minutes=30,
            ),
        )
        == []
    )


# ---------------------------------------------------------------------------
# Fail closed at contract-seal time
# ---------------------------------------------------------------------------


class _SealConflictRuntime(ScriptedMockRuntime):
    """Drafts a contract candidate that cannot fit the preselected profile."""

    def run_turn(self, **kwargs):
        prompt = kwargs['prompt']
        agent = kwargs['agent'].value
        if agent == 'honeydew' and 'Draft a concrete program.md' in prompt:
            result, message_id = super().run_turn(**kwargs)
            assert result.evaluation_contract_proposal is not None
            result.evaluation_contract_proposal.evaluator_type = 'candidate-v1'
            return result, message_id
        if (
            agent == 'beaker'
            and 'Draft an immutable evaluation-contract' in prompt
        ):
            root = kwargs['workspace'] / 'contract-candidate/candidate-v1/1.0.0'
            root.mkdir(parents=True, exist_ok=True)
            descriptor = {
                'contract_id': 'candidate-v1',
                'version': '1.0.0',
                'manifest': {
                    'primary_metric': 'score',
                    'primary_metric_direction': 'maximize',
                },
                'execution_wrapper': 'run_contract.py',
                'evaluation_entry_point': 'evaluator.py',
                'expected_input_schema': 'input.schema.json',
                'expected_output_schema': 'output.schema.json',
                'required_artifacts': ['metrics.json', 'evaluation.json'],
                'resource_constraints': CONTRACT_CONSTRAINTS,
                'container_image_digest': None,
            }
            (root / 'contract.json').write_text(json.dumps(descriptor))
            (root / 'run_contract.py').write_text('# wrapper\n')
            (root / 'evaluator.py').write_text('# evaluator\n')
            (root / 'input.schema.json').write_text('{"type": "object"}\n')
            (root / 'output.schema.json').write_text('{"type": "object"}\n')
            return (
                AgentTurnResult(
                    kind=TurnKind.CONTRACT_CANDIDATE,
                    summary='Drafted and locally checked the candidate.',
                    requested_actions=[
                        RequestedAction(
                            type='propose_evaluation_contract',
                            arguments={
                                'contract_id': 'candidate-v1',
                                'version': '1.0.0',
                                'candidate_path': (
                                    'contract-candidate/candidate-v1/1.0.0'
                                ),
                                'rationale': 'Implement the approved evaluator.',
                            },
                            reason='Review and promote the sealed candidate.',
                        )
                    ],
                    done=True,
                ),
                'mock-contract-message',
            )
        if agent == 'honeydew' and 'Review the sealed evaluation-contract' in prompt:
            return (
                AgentTurnResult(
                    kind=TurnKind.METHODOLOGY_REVIEW,
                    summary='The sealed candidate implements the protocol.',
                    done=True,
                ),
                'mock-contract-review',
            )
        return super().run_turn(**kwargs)


def test_contract_seal_below_profile_parks_without_redraft(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, _, original = orchestrator_bundle
    engine = ResearchOrchestrator(
        settings=settings,
        store=store,
        runtime=_SealConflictRuntime(runner_image=RUNNER_IMAGE),
        workspaces=original.workspaces,
        contracts=original.contracts,
        contract_candidates=original.contract_candidates,
        policy=original.policy,
        cluster=cluster,
        discord=DisabledDiscordAdapter(),
    )
    run = engine.create_run(
        RunCreateRequest(objective='Park a conflicting contract candidate.')
    )
    current = store.get_run(run.run_id)
    store.replace_run(
        current.model_copy(
            update={'task_definition': dict(CPU_PROFILE_TASK_DEFINITION)}
        ),
        expected_version=current.version,
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')

    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )

    parked = store.get_run(run.run_id)
    assert parked.state == RunState.PAUSED
    assert _events(store, run.run_id, 'contract.candidate_rejected') == []
    conflicts = _events(
        store,
        run.run_id,
        'methodology.resource_authority_conflict',
    )
    assert len(conflicts) == 1
    reason = str(conflicts[0].payload['reason'])
    assert 'wallclock_minutes' in reason
    assert '60' in reason
    assert '30' in reason
    pause = _events(store, run.run_id, 'run.paused')
    assert len(pause) == 1
    assert 'wallclock_minutes' in str(pause[0].payload['reason'])
