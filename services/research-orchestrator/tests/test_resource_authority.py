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
from app.engine import ResearchOrchestrator, WorkflowError
from app.mock_runtime import ScriptedMockRuntime
from app.preflight import (
    declared_budget_conflicts,
    profile_contract_resource_conflicts,
)
from app.schemas import (
    ActionRecord,
    AgentName,
    AgentTurnResult,
    ApprovalStatus,
    ArtifactRecord,
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
    manifest_budget: dict[str, object] | None = None,
    contract_id: str = 'example-research-v1',
    version: str = '9.9.9',
) -> object:
    # The engine resolver checks settings.promoted_contract_root first, so the
    # synthetic descriptor is installed there. Its identity matches the mock
    # protocol proposal's evaluator_type so the binding stays compatible.
    root = tmp_path / 'trusted-contracts' / contract_id / version
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        'primary_metric': 'score',
        'primary_metric_direction': 'maximize',
    }
    if manifest_budget is not None:
        manifest['budget'] = manifest_budget
    descriptor = {
        'contract_id': contract_id,
        'version': version,
        'manifest': manifest,
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
    manifest_budget: dict[str, object] | None = None,
    task_definition: dict[str, object] | None = None,
):
    contract = _install_contract(
        tmp_path,
        engine,
        resource_constraints=resource_constraints,
        manifest_budget=manifest_budget,
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


def test_contract_declared_budget_above_profile_parks_without_revision(
    tmp_path: Path,
    orchestrator_bundle,
    monkeypatch,
) -> None:
    # A declared manifest.budget is informational, but it must not claim more
    # wall-clock than the authoritative profile grants: 90 minutes declared
    # inside a 120-minute contract envelope still contradicts the 60-minute
    # task profile, so the contradiction is caught before approval.
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Reject a declared budget over the profile.')
    )
    run, _ = _bind_task_profile_and_contract(
        tmp_path,
        store,
        engine,
        run,
        resource_constraints=ACCOMMODATING_CONSTRAINTS,
        manifest_budget={'wallclock_minutes': 90},
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
        ordinal='budget-1',
    )
    revised: list[str] = []
    monkeypatch.setattr(
        engine,
        '_beaker_revise',
        lambda run_id, *, feedback: revised.append(feedback),
    )

    engine._honeydew_review(run.run_id, implementation_turn_id='turn-1')

    parked = store.get_run(run.run_id)
    assert parked.state == RunState.PAUSED
    assert revised == []
    assert parked.methodology_revision_count == 0
    assert _events(store, run.run_id, 'methodology.revision_requested') == []
    conflicts = _events(
        store,
        run.run_id,
        'methodology.resource_authority_conflict',
    )
    assert len(conflicts) == 1
    reason = str(conflicts[0].payload['reason'])
    assert 'manifest.budget' in reason
    assert '90' in reason
    assert '60' in reason


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
# Declared manifest.budget contradictions (issue #500)
# ---------------------------------------------------------------------------


def test_declared_budget_above_contract_constraints_is_a_conflict() -> None:
    conflicts = declared_budget_conflicts(
        manifest={'budget': {'wallclock_minutes': 240}},
        constraints=ResourceRequest(
            cpu=4.0,
            memory_gib=8.0,
            gpus=0,
            wallclock_minutes=60,
        ),
    )

    assert [item.scope for item in conflicts] == ['contract resource_constraints']
    described = conflicts[0].describe()
    assert '240' in described
    assert '60' in described


def test_declared_budget_above_task_profile_is_a_conflict() -> None:
    conflicts = declared_budget_conflicts(
        manifest={'budget': {'wallclock_minutes': 90}},
        constraints=ResourceRequest(
            cpu=8.0,
            memory_gib=32.0,
            gpus=0,
            wallclock_minutes=120,
        ),
        profile={'wallclock_minutes': 60},
    )

    assert [item.scope for item in conflicts] == ['task resource profile']


def test_declared_budget_within_envelope_has_no_conflicts() -> None:
    assert (
        declared_budget_conflicts(
            manifest={'budget': {'wallclock_minutes': 60}},
            constraints=ResourceRequest(
                cpu=8.0,
                memory_gib=32.0,
                gpus=0,
                wallclock_minutes=120,
            ),
            profile={'wallclock_minutes': 60},
        )
        == []
    )


def test_declared_budget_absent_has_no_conflicts() -> None:
    assert (
        declared_budget_conflicts(
            manifest={'primary_metric': 'score'},
            constraints=ResourceRequest(
                cpu=8.0,
                memory_gib=32.0,
                gpus=0,
                wallclock_minutes=120,
            ),
        )
        == []
    )


def test_declared_budget_malformed_shape_raises() -> None:
    constraints = ResourceRequest(
        cpu=8.0,
        memory_gib=32.0,
        gpus=0,
        wallclock_minutes=120,
    )

    with pytest.raises(ValueError, match='wallclock_minutes'):
        declared_budget_conflicts(
            manifest={'budget': {'wallclock_minutes': 0}},
            constraints=constraints,
        )
    with pytest.raises(ValueError, match='wallclock_minutes'):
        declared_budget_conflicts(
            manifest={'budget': {'wallclock_minutes': 60.5}},
            constraints=constraints,
        )
    with pytest.raises(ValueError, match='JSON object'):
        declared_budget_conflicts(
            manifest={'budget': 'sixty minutes'},
            constraints=constraints,
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


# ---------------------------------------------------------------------------
# Installed-contract binding path: stale promoted contract (issue #490)
#
# A run that binds an already-promoted contract used to park at
# AWAITING_CONTRACT_PROMOTION and re-enter the same failing binding call on
# every resume (HTTP 409 "installed contract remains incompatible with the
# protocol"), forever. These tests pin the replacement behavior: the stale
# contract is never bound, the run fails closed exactly once with the
# #484-style actionable message, and a second resume is refused before any
# workflow recovery runs.
# ---------------------------------------------------------------------------


class _ProfileProtocolRuntime(ScriptedMockRuntime):
    """Drafts the protocol proposal with the authoritative task profile.

    Post-#484 the stored protocol proposal carries the compiled task's
    resource profile in ``resource_constraints``; that is what makes
    ``_contract_binding_compatible`` reject a stale installed contract.
    """

    def run_turn(self, **kwargs):
        if (
            kwargs['agent'] == AgentName.HONEYDEW
            and 'Draft a concrete program.md' in kwargs['prompt']
        ):
            result, message_id = super().run_turn(**kwargs)
            assert result.evaluation_contract_proposal is not None
            result.evaluation_contract_proposal.resource_constraints = (
                ResourceRequest(**CPU_PROFILE)
            )
            return result, message_id
        return super().run_turn(**kwargs)


def _bind_profile_task_and_park_for_promotion(
    tmp_path: Path,
    store,
    engine: ResearchOrchestrator,
    run,
    *,
    resource_constraints: dict[str, object],
):
    """Rebind a run to an installed contract and park it awaiting promotion.

    Mirrors the live run's durable state: a compiled task profile, a binding
    to the (stale) already-promoted contract, and an approved promotion
    action waiting at AWAITING_CONTRACT_PROMOTION.
    """

    contract = _install_contract(
        tmp_path,
        engine,
        resource_constraints=resource_constraints,
    )
    current = store.get_run(run.run_id)
    store.replace_run(
        current.model_copy(
            update={
                'task_definition': dict(CPU_PROFILE_TASK_DEFINITION),
                'evaluation_contract_id': contract.descriptor.contract_id,
                'evaluation_contract_version': contract.descriptor.version,
                'evaluation_contract_digest': contract.digest,
                'state': RunState.PAUSED,
                'resume_state': RunState.AWAITING_CONTRACT_PROMOTION,
                'active_since': None,
            }
        ),
        expected_version=current.version,
    )
    return contract


def _save_approved_promotion_action(
    store,
    *,
    run_id: str,
    descriptor: dict[str, object],
    digest: str,
    sealed_path: str = '/sealed/not-used',
) -> ActionRecord:
    # A human-approved promotion action plus its sealed candidate artifact,
    # exactly what _promote_contract_candidate consumes when the destination
    # contract turns out to be already installed.
    action = store.save_action(
        ActionRecord(
            run_id=run_id,
            proposed_by=AgentName.BEAKER,
            type='propose_evaluation_contract',
            arguments={
                'contract_id': str(descriptor['contract_id']),
                'version': str(descriptor['version']),
                'candidate_path': (
                    f"contract-candidate/{descriptor['contract_id']}/"
                    f"{descriptor['version']}"
                ),
                'rationale': 'Promote the reviewed contract.',
            },
            policy_classification=(
                PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL
            ),
            approval_status=ApprovalStatus.APPROVED,
            honeydew_approved=True,
            reviewer='test-human',
            reason='Promote the reviewed contract.',
            idempotency_key=f'promote-contract-{run_id}',
        )
    )
    store.save_artifact(
        ArtifactRecord(
            run_id=run_id,
            type='evaluation_contract_candidate',
            uri=(
                f"artifact://{run_id}/contract-candidate/"
                f"{descriptor['contract_id']}/{descriptor['version']}"
            ),
            sha256=digest,
            metadata={
                'action_id': action.action_id,
                'descriptor': descriptor,
                'sealed_path': sealed_path,
            },
        )
    )
    return action


def _write_sealed_candidate_source(
    root: Path,
    *,
    contract_id: str,
    version: str,
    resource_constraints: dict[str, object],
) -> None:
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


def test_installed_contract_below_profile_fails_closed_once(
    tmp_path: Path,
    orchestrator_bundle,
    monkeypatch,
) -> None:
    settings, store, cluster, _, original = orchestrator_bundle
    engine = ResearchOrchestrator(
        settings=settings,
        store=store,
        runtime=_ProfileProtocolRuntime(runner_image=RUNNER_IMAGE),
        workspaces=original.workspaces,
        contracts=original.contracts,
        contract_candidates=original.contract_candidates,
        policy=original.policy,
        cluster=cluster,
        discord=DisabledDiscordAdapter(),
    )
    run = engine.create_run(
        RunCreateRequest(
            objective='Never loop on a stale installed contract (#490).'
        )
    )
    contract = _bind_profile_task_and_park_for_promotion(
        tmp_path,
        store,
        engine,
        run,
        resource_constraints=CONTRACT_CONSTRAINTS,
    )
    action = _save_approved_promotion_action(
        store,
        run_id=run.run_id,
        descriptor=contract.descriptor.model_dump(mode='json'),
        digest=contract.digest,
    )
    promotion_calls: list[str] = []
    original_promote = engine._promote_contract_candidate

    def spy_promote(action_record):
        promotion_calls.append(action_record.action_id)
        return original_promote(action_record)

    monkeypatch.setattr(engine, '_promote_contract_candidate', spy_promote)
    turn_before = store.get_run(run.run_id).turn_number

    engine.resume_run(run.run_id, requested_by='test-human')

    failed = store.get_run(run.run_id)
    assert failed.state == RunState.FAILED
    assert promotion_calls == [action.action_id]
    # Configuration contradiction, not a model failure: no agent turn, no
    # methodology revision, no retry is consumed.
    assert failed.turn_number == turn_before
    assert failed.methodology_revision_count == 0
    # The stale contract is never bound and never silently reused.
    assert _events(store, run.run_id, 'contract.bound_installed') == []
    pause_reasons = [
        str(event.payload.get('reason'))
        for event in _events(store, run.run_id, 'run.paused')
    ]
    assert not any(
        'installed contract remains incompatible' in reason
        for reason in pause_reasons
    )

    conflicts = _events(
        store,
        run.run_id,
        'methodology.resource_authority_conflict',
    )
    assert len(conflicts) == 1
    assert conflicts[0].payload['origin'] == 'installed_contract_binding'
    reason = str(conflicts[0].payload['reason'])
    assert 'wallclock_minutes' in reason
    assert '60' in reason
    assert '30' in reason

    failures = _events(store, run.run_id, 'run.failed')
    assert len(failures) == 1
    failure = str(failures[0].payload['error'])
    assert 'wallclock_minutes' in failure
    assert '60' in failure
    assert '30' in failure

    # A second resume must not re-enter the failing binding call: the run is
    # terminal, so resume is refused before any workflow recovery starts and
    # the one-shot rejection is not re-emitted.
    with pytest.raises(WorkflowError, match='not resumable'):
        engine.resume_run(run.run_id, requested_by='test-human')
    assert promotion_calls == [action.action_id]
    assert (
        len(
            _events(
                store,
                run.run_id,
                'methodology.resource_authority_conflict',
            )
        )
        == 1
    )
    assert len(_events(store, run.run_id, 'run.failed')) == 1


def test_installed_contract_accommodating_profile_binds_as_before(
    tmp_path: Path,
    orchestrator_bundle,
    monkeypatch,
) -> None:
    settings, store, cluster, _, original = orchestrator_bundle
    engine = ResearchOrchestrator(
        settings=settings,
        store=store,
        runtime=_ProfileProtocolRuntime(runner_image=RUNNER_IMAGE),
        workspaces=original.workspaces,
        contracts=original.contracts,
        contract_candidates=original.contract_candidates,
        policy=original.policy,
        cluster=cluster,
        discord=DisabledDiscordAdapter(),
    )
    run = engine.create_run(
        RunCreateRequest(objective='Bind a contract that fits the profile.')
    )
    contract = _bind_profile_task_and_park_for_promotion(
        tmp_path,
        store,
        engine,
        run,
        resource_constraints=ACCOMMODATING_CONSTRAINTS,
    )
    _save_approved_promotion_action(
        store,
        run_id=run.run_id,
        descriptor=contract.descriptor.model_dump(mode='json'),
        digest=contract.digest,
    )
    planned: list[str] = []
    monkeypatch.setattr(
        engine,
        '_beaker_plan',
        lambda run_id: planned.append(run_id),
    )

    engine.resume_run(run.run_id, requested_by='test-human')

    bound = store.get_run(run.run_id)
    assert bound.state == RunState.BEAKER_PLANNING
    assert bound.evaluation_contract_id == contract.descriptor.contract_id
    assert bound.evaluation_contract_digest == contract.digest
    assert planned == [run.run_id]
    bound_events = _events(store, run.run_id, 'contract.bound_installed')
    assert len(bound_events) == 1
    assert bound_events[0].payload['digest'] == contract.digest
    assert _events(
        store, run.run_id, 'methodology.resource_authority_conflict'
    ) == []
    assert _events(store, run.run_id, 'run.failed') == []


def test_stale_sealed_candidate_below_profile_fails_closed_once(
    tmp_path: Path,
    orchestrator_bundle,
) -> None:
    # The other half of the stale-promotion lifecycle (issue #490 direction
    # 3): a sealed candidate drafted before the profile became authoritative
    # must not be promoted at all. Promoting it would install an unusable
    # immutable contract and then loop on "promoted contract remains
    # incompatible with the protocol", so the run fails closed instead.
    settings, store, cluster, _, original = orchestrator_bundle
    engine = ResearchOrchestrator(
        settings=settings,
        store=store,
        runtime=_ProfileProtocolRuntime(runner_image=RUNNER_IMAGE),
        workspaces=original.workspaces,
        contracts=original.contracts,
        contract_candidates=original.contract_candidates,
        policy=original.policy,
        cluster=cluster,
        discord=DisabledDiscordAdapter(),
    )
    run = engine.create_run(
        RunCreateRequest(objective='Fail closed on a stale sealed candidate.')
    )
    source = tmp_path / 'candidate-source'
    _write_sealed_candidate_source(
        source,
        contract_id='example-research-v1',
        version='2.0.0',
        resource_constraints=CONTRACT_CONSTRAINTS,
    )
    sealed = engine.contract_candidates.seal(
        source=source,
        contract_id='example-research-v1',
        version='2.0.0',
    )
    current = store.get_run(run.run_id)
    store.replace_run(
        current.model_copy(
            update={
                'task_definition': dict(CPU_PROFILE_TASK_DEFINITION),
                'state': RunState.PAUSED,
                'resume_state': RunState.AWAITING_CONTRACT_PROMOTION,
                'active_since': None,
            }
        ),
        expected_version=current.version,
    )
    _save_approved_promotion_action(
        store,
        run_id=run.run_id,
        descriptor=sealed.descriptor.model_dump(mode='json'),
        digest=sealed.digest,
        sealed_path=str(sealed.sealed_path),
    )
    turn_before = store.get_run(run.run_id).turn_number

    engine.resume_run(run.run_id, requested_by='test-human')

    failed = store.get_run(run.run_id)
    assert failed.state == RunState.FAILED
    assert failed.turn_number == turn_before
    promotion_pauses = [
        str(event.payload.get('reason'))
        for event in _events(store, run.run_id, 'run.paused')
        if 'promoted contract remains incompatible'
        in str(event.payload.get('reason'))
    ]
    assert promotion_pauses == []
    conflicts = _events(
        store,
        run.run_id,
        'methodology.resource_authority_conflict',
    )
    assert len(conflicts) == 1
    assert conflicts[0].payload['origin'] == 'candidate_promotion'
    reason = str(conflicts[0].payload['reason'])
    assert 'wallclock_minutes' in reason
    assert '60' in reason
    assert '30' in reason
    # The stale candidate was rejected before promotion: nothing unusable was
    # installed into the trusted contract root.
    assert not (
        tmp_path / 'trusted-contracts' / 'example-research-v1' / '2.0.0'
    ).exists()
    with pytest.raises(WorkflowError, match='not resumable'):
        engine.resume_run(run.run_id, requested_by='test-human')
    assert (
        len(
            _events(
                store,
                run.run_id,
                'methodology.resource_authority_conflict',
            )
        )
        == 1
    )
