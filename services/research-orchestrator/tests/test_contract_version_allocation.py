"""A second run on the same objective must not collide at contract promotion.

The protocol prompt pins the evaluation-contract id to the objective, so a
second run re-drafts the same contract_id@version with new bytes. The
orchestrator allocates a free version before drafting, so the candidate targets
an unoccupied id@version and promotes alongside the installed contract. The
fail-closed guard remains for a genuine namespace violation that bypasses the
reservation (issue #490 follow-up).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.discord_adapter import DisabledDiscordAdapter
from app.engine import ResearchOrchestrator
from app.mock_runtime import ScriptedMockRuntime
from app.schemas import (
    AgentName,
    AgentTurnResult,
    ApprovalStatus,
    RequestedAction,
    RunCreateRequest,
    RunState,
    TurnKind,
)

from conftest import RUNNER_IMAGE


def _seed_promoted_contract(
    manager,
    root: Path,
    *,
    version: str,
    marker: str,
    metric: str,
) -> str:
    source = root / f'seed-{marker}'
    source.mkdir(parents=True)
    descriptor = {
        'contract_id': 'candidate-v1',
        'version': version,
        'manifest': {
            'primary_metric': metric,
            'primary_metric_direction': 'maximize',
        },
        'execution_wrapper': 'run_contract.py',
        'evaluation_entry_point': 'evaluator.py',
        'expected_input_schema': 'input.schema.json',
        'expected_output_schema': 'output.schema.json',
        'required_artifacts': ['metrics.json', 'evaluation.json'],
        'resource_constraints': {
            'cpu': 1,
            'memory_gib': 1,
            'gpus': 0,
            'wallclock_minutes': 5,
        },
        'container_image_digest': None,
    }
    (source / 'contract.json').write_text(json.dumps(descriptor))
    (source / 'run_contract.py').write_text('print("wrapper")\n')
    (source / 'evaluator.py').write_text(f'print("evaluate {marker}")\n')
    (source / 'input.schema.json').write_text('{"type": "object"}\n')
    (source / 'output.schema.json').write_text('{"type": "object"}\n')
    sealed = manager.seal(
        source=source,
        contract_id='candidate-v1',
        version=version,
    )
    manager.promote(
        sealed_path=sealed.sealed_path,
        expected_digest=sealed.digest,
    )
    return sealed.digest


class AllocatingContractRuntime(ScriptedMockRuntime):
    def __init__(self, *, runner_image: str) -> None:
        super().__init__(runner_image=runner_image)
        self.drafted_version: str | None = None

    def run_turn(self, **kwargs):
        prompt = kwargs['prompt']
        agent = kwargs['agent']
        if agent == AgentName.HONEYDEW and 'Draft a concrete program.md' in prompt:
            result, message_id = super().run_turn(**kwargs)
            assert result.evaluation_contract_proposal is not None
            result.evaluation_contract_proposal.evaluator_type = 'candidate-v1'
            return result, message_id
        if (
            agent == AgentName.BEAKER
            and 'Draft an immutable evaluation-contract' in prompt
        ):
            match = re.search(r'must be EXACTLY "([^"]+)"', prompt)
            assert match is not None, 'allocated version missing from prompt'
            version = match.group(1)
            self.drafted_version = version
            root = (
                kwargs['workspace']
                / f'contract-candidate/candidate-v1/{version}'
            )
            root.mkdir(parents=True, exist_ok=True)
            descriptor = {
                'contract_id': 'candidate-v1',
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
                'resource_constraints': {
                    'cpu': 1,
                    'memory_gib': 1,
                    'gpus': 0,
                    'wallclock_minutes': 5,
                },
                'container_image_digest': None,
            }
            (root / 'contract.json').write_text(json.dumps(descriptor))
            (root / 'run_contract.py').write_text('print("wrapper")\n')
            (root / 'evaluator.py').write_text('print("evaluate allocated")\n')
            (root / 'input.schema.json').write_text('{"type": "object"}\n')
            (root / 'output.schema.json').write_text('{"type": "object"}\n')
            return (
                AgentTurnResult(
                    kind=TurnKind.CONTRACT_CANDIDATE,
                    summary='Drafted the candidate at the allocated version.',
                    requested_actions=[
                        RequestedAction(
                            type='propose_evaluation_contract',
                            arguments={
                                'contract_id': 'candidate-v1',
                                'version': version,
                                'candidate_path': (
                                    'contract-candidate/candidate-v1/'
                                    f'{version}'
                                ),
                                'rationale': 'Implement the approved evaluator.',
                            },
                            reason='Review and promote the sealed candidate.',
                        )
                    ],
                    done=True,
                ),
                'mock-allocated-contract',
            )
        if (
            agent == AgentName.HONEYDEW
            and 'Review the sealed evaluation-contract' in prompt
        ):
            return (
                AgentTurnResult(
                    kind=TurnKind.METHODOLOGY_REVIEW,
                    summary='The sealed candidate implements the protocol.',
                    done=True,
                ),
                'mock-contract-review',
            )
        return super().run_turn(**kwargs)


def _build_engine(settings, store, cluster, original, runtime):
    return ResearchOrchestrator(
        settings=settings,
        store=store,
        runtime=runtime,
        workspaces=original.workspaces,
        contracts=original.contracts,
        contract_candidates=original.contract_candidates,
        policy=original.policy,
        cluster=cluster,
        discord=DisabledDiscordAdapter(),
    )


def _pending_action(store, run_id: str, action_type: str):
    return next(
        action
        for action in store.list_actions(run_id)
        if action.type == action_type
        and action.approval_status == ApprovalStatus.PENDING
    )


def _drive_to_contract_approval(engine, store, run):
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )


def test_second_run_promotes_alongside_the_installed_contract(
    orchestrator_bundle,
    tmp_path: Path,
) -> None:
    settings, store, cluster, _, original = orchestrator_bundle
    _seed_promoted_contract(
        original.contract_candidates,
        tmp_path,
        version='1.0.0',
        marker='installed',
        metric='accuracy',
    )
    runtime = AllocatingContractRuntime(runner_image=RUNNER_IMAGE)
    engine = _build_engine(settings, store, cluster, original, runtime)
    run = engine.create_run(
        RunCreateRequest(objective='Re-run the same objective.')
    )
    _drive_to_contract_approval(engine, store, run)

    awaiting = store.get_run(run.run_id)
    assert awaiting.state == RunState.AWAITING_CONTRACT_PROMOTION
    assert runtime.drafted_version == '1.0.1'
    allocated = [
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'contract.version_allocated'
    ]
    assert allocated, 'expected a contract.version_allocated event'
    assert allocated[0].payload == {
        'contract_id': 'candidate-v1',
        'requested_version': '1.0.0',
        'allocated_version': '1.0.1',
    }

    candidate = _pending_action(
        store,
        run.run_id,
        'propose_evaluation_contract',
    )
    engine.approve_action(
        candidate.action_id,
        reviewer='test-admin',
        reason='Promote the reviewed harness.',
    )

    rebound = store.get_run(run.run_id)
    assert rebound.state != RunState.FAILED
    assert rebound.evaluation_contract_id == 'candidate-v1'
    assert rebound.evaluation_contract_version == '1.0.1'
    catalog = json.loads(
        Path(settings.trusted_contract_catalog_path).read_text()
    )
    assert 'candidate-v1@1.0.0' in catalog
    assert 'candidate-v1@1.0.1' in catalog


def test_version_conflict_still_fails_closed_when_allocation_is_bypassed(
    orchestrator_bundle,
    tmp_path: Path,
) -> None:
    settings, store, cluster, _, original = orchestrator_bundle
    _seed_promoted_contract(
        original.contract_candidates,
        tmp_path,
        version='1.0.0',
        marker='installed',
        metric='accuracy',
    )
    runtime = AllocatingContractRuntime(runner_image=RUNNER_IMAGE)
    engine = _build_engine(settings, store, cluster, original, runtime)
    run = engine.create_run(
        RunCreateRequest(objective='Re-run the same objective.')
    )
    _drive_to_contract_approval(engine, store, run)
    assert runtime.drafted_version == '1.0.1'

    _seed_promoted_contract(
        original.contract_candidates,
        tmp_path,
        version='1.0.1',
        marker='external',
        metric='accuracy',
    )
    candidate = _pending_action(
        store,
        run.run_id,
        'propose_evaluation_contract',
    )
    engine.approve_action(
        candidate.action_id,
        reviewer='test-admin',
        reason='Promote the reviewed harness.',
    )

    failed = store.get_run(run.run_id)
    assert failed.state == RunState.FAILED
    assert any(
        event.event_type == 'contract.version_conflict'
        for event in store.list_events(run.run_id)
    )
