"""End-to-end orchestrator workflow over scripted and fault-injecting runtimes.

Exercises the full run lifecycle (protocol -> contract -> implementation ->
matrix -> jobs -> report) with mock agent runtimes that deny, fail once,
pause mid-turn, lose files, or return invalid contracts, plus the recovery,
resume, cancellation, idempotent-submission, and HTTP surfaces that make the
workflow safe to restart. Cluster execution runs against FakeClusterExecutor.
"""

from __future__ import annotations

import json
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
import pytest

from app.contracts import EvaluationContractResolver
from app.contract_candidates import ContractCandidateManager
from app.discord_adapter import DisabledDiscordAdapter
from app.engine import ResearchOrchestrator, WorkflowError
from app.main import create_app
from app.mock_runtime import ScriptedMockRuntime
from app.policy import ActionPolicy
from app.schemas import (
    AgentName,
    AgentTurnResult,
    ActionRecord,
    ApprovalStatus,
    ArtifactRecord,
    ExperimentMatrix,
    JobStatus,
    RequestedAction,
    PolicyClassification,
    RunCreateRequest,
    RunState,
    TurnKind,
    TurnRecord,
    utc_now,
)
from app.storage import SqliteStore
from app.workspaces import WorkspaceManager

from conftest import RUNNER_IMAGE


def test_human_approval_states_stop_active_runtime_clock(
    orchestrator_bundle,
) -> None:
    # Waiting on a human is not "active" run time: entering an awaiting state
    # freezes active_runtime_seconds and clears active_since, and leaving it
    # restarts the clock without re-adding the waiting window.
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Exclude human review from active runtime.')
    )
    assert run.state == RunState.AWAITING_PROTOCOL_APPROVAL
    assert run.active_since is None

    writing = store.replace_run(
        run.model_copy(
            update={
                'state': RunState.HONEYDEW_WRITING_REPORT,
                'active_runtime_seconds': 5.0,
                'active_since': utc_now() - timedelta(seconds=100),
            }
        ),
        expected_version=run.version,
    )
    waiting = store.transition_run(
        writing.run_id,
        RunState.AWAITING_FINAL_ACCEPTANCE,
    )
    assert 104 <= waiting.active_runtime_seconds <= 107
    assert waiting.active_since is None

    revising = store.transition_run(
        waiting.run_id,
        RunState.HONEYDEW_WRITING_REPORT,
    )
    assert revising.active_since is not None
    assert revising.active_runtime_seconds == waiting.active_runtime_seconds


def test_failed_result_starts_a_fresh_methodology_revision_budget(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    # A post-execution failure resets the revision counter (a new budget for
    # repairing an executed experiment), distinct from pre-execution review
    # revisions which share the existing budget.
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Repair a failed executed experiment.')
    )
    verifying = store.replace_run(
        run.model_copy(
            update={
                'state': RunState.HONEYDEW_VERIFYING,
                'methodology_revision_count': 2,
            }
        ),
        expected_version=run.version,
    )
    monkeypatch.setattr(
        engine,
        '_run_agent_turn',
        lambda **_kwargs: (
            None,
            AgentTurnResult(
                kind=TurnKind.VERIFICATION,
                summary='The executed candidate needs a code repair.',
                done=False,
            ),
        ),
    )
    monkeypatch.setattr(engine, '_beaker_revise', lambda *_args, **_kwargs: None)

    engine._verify_results(verifying.run_id)

    revising = store.get_run(verifying.run_id)
    assert revising.state == RunState.BEAKER_REVISING
    assert revising.methodology_revision_count == 0
    reset = next(
        event
        for event in store.list_events(verifying.run_id)
        if event.event_type == 'methodology.revision_budget_reset'
    )
    assert reset.payload['previous_revision_count'] == 2
    assert reset.payload['revision_count'] == 0

    engine._request_methodology_revision(
        verifying.run_id,
        feedback='The first post-execution repair remains incomplete.',
    )
    assert store.get_run(verifying.run_id).methodology_revision_count == 1


def test_evidence_snapshot_projects_bounded_verified_artifact_contents(
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Diagnose an authoritative job failure.')
    )
    log_path = Path(settings.shared_mount_root) / 'artifacts/job-1/runner.log'
    log_path.parent.mkdir(parents=True)
    log_content = (
        'unimportant prefix that should be truncated\n'
        'ValueError: feature names should match fit\n'
    ).encode()
    log_path.write_bytes(log_content)
    store.save_artifact(
        ArtifactRecord(
            run_id=run.run_id,
            type='runner_log',
            uri='artifacts/job-1/runner.log',
            sha256=sha256(log_content).hexdigest(),
        )
    )
    status_path = Path(settings.shared_mount_root) / 'artifacts/job-1/status.json'
    status_content = b'{"status":"failed","exit_code":1}'
    status_path.write_bytes(status_content)
    store.save_artifact(
        ArtifactRecord(
            run_id=run.run_id,
            type='status',
            uri='artifacts/job-1/status.json',
            sha256=sha256(status_content).hexdigest(),
        )
    )
    engine.settings.evidence_excerpt_max_bytes = 48

    contents = engine._evidence_snapshot(run.run_id)['artifact_contents']

    log = next(item for item in contents if item['type'] == 'runner_log')
    assert log['digest_verified'] is True
    assert log['truncated'] is True
    assert log['excerpt_position'] == 'tail'
    assert 'ValueError: feature names should match fit' in log['content']
    status = next(item for item in contents if item['type'] == 'status')
    assert status['content'] == {'status': 'failed', 'exit_code': 1}

    fairness_path = (
        Path(settings.shared_mount_root) / 'artifacts/job-1/fairness.csv'
    )
    fairness_content = b'group,accuracy\nA,0.75\n'
    fairness_path.write_bytes(fairness_content)
    store.save_artifact(
        ArtifactRecord(
            run_id=run.run_id,
            type='fairness_table',
            uri='artifacts/job-1/fairness.csv',
            sha256=sha256(fairness_content).hexdigest(),
        )
    )
    contents = engine._evidence_snapshot(run.run_id)['artifact_contents']
    fairness = next(
        item for item in contents if item['type'] == 'fairness_table'
    )
    assert fairness['digest_verified'] is True
    assert fairness['content'] == 'group,accuracy\nA,0.75\n'


def test_evidence_snapshot_rejects_mismatched_or_escaping_artifacts(
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Reject untrusted artifact evidence.')
    )
    status_path = Path(settings.shared_mount_root) / 'status.json'
    status_path.write_text('{"status":"complete"}')
    store.save_artifact(
        ArtifactRecord(
            run_id=run.run_id,
            type='status',
            uri='status.json',
            sha256='0' * 64,
        )
    )
    store.save_artifact(
        ArtifactRecord(
            run_id=run.run_id,
            type='status',
            uri='../status.json',
            sha256='0' * 64,
        )
    )

    contents = engine._evidence_snapshot(run.run_id)['artifact_contents']

    assert contents == [
        {
            'uri': 'artifact://status.json',
            'type': 'status',
            'sha256': '0' * 64,
            'content_unavailable': 'artifact digest mismatch',
        }
    ]


class DeniedThenValidRuntime(ScriptedMockRuntime):
    # First Beaker matrix proposal carries an unpermitted image (policy DENY),
    # then the retry uses the allowed runner image to succeed.
    def run_turn(self, **kwargs):
        if (
            kwargs['agent'].value == 'beaker'
            and 'Execute the task-specific plan' in kwargs['prompt']
        ):
            allowed_image = self.runner_image
            self.runner_image = 'example.invalid/untrusted:latest'
            try:
                return super().run_turn(**kwargs)
            finally:
                self.runner_image = allowed_image
        return super().run_turn(**kwargs)


class ContractOversizedThenValidRuntime(ScriptedMockRuntime):
    # First matrix proposal requests more memory than the evaluation contract
    # allows (preflight REJECT); the retry is within bounds and succeeds.
    def __init__(self, *, runner_image: str) -> None:
        super().__init__(runner_image=runner_image)
        self._oversized_once = True

    def run_turn(self, **kwargs):
        result, message_id = super().run_turn(**kwargs)
        if (
            self._oversized_once
            and kwargs['agent'].value == 'beaker'
            and 'Execute the task-specific plan' in kwargs['prompt']
        ):
            result.requested_actions[0].arguments['resources']['memory_gib'] = 2
            self._oversized_once = False
        return result, message_id


class FailOnceImplementationRuntime(ScriptedMockRuntime):
    # The first Beaker implementation turn raises like a real timeout; the
    # retry succeeds. The failed session id is remembered so the test can
    # prove recovery starts a fresh session.
    def __init__(self, *, runner_image: str) -> None:
        super().__init__(runner_image=runner_image)
        self.failed_session_id: str | None = None
        self.recovery_prompt: str | None = None

    def run_turn(self, **kwargs):
        if (
            kwargs['agent'] == AgentName.BEAKER
            and 'Execute the task-specific plan' in kwargs['prompt']
        ):
            if self.failed_session_id is None:
                self.failed_session_id = kwargs['session_id']
                raise RuntimeError('mock implementation timeout')
            self.recovery_prompt = kwargs['prompt']
        return super().run_turn(**kwargs)


class MissingPlanThenRepairRuntime(ScriptedMockRuntime):
    # Beaker claims the plan was written but the authoritative file is absent
    # on the first try, forcing the focused one-shot file repair path.
    def __init__(self, *, runner_image: str) -> None:
        super().__init__(runner_image=runner_image)
        self.removed_first_plan = False

    def run_turn(self, **kwargs):
        result = super().run_turn(**kwargs)
        if (
            kwargs['agent'] == AgentName.BEAKER
            and 'Write implementation-plan.md' in kwargs['prompt']
            and not self.removed_first_plan
        ):
            (kwargs['workspace'] / 'implementation-plan.md').unlink()
            self.removed_first_plan = True
        return result


class MissingProtocolThenRepairRuntime(ScriptedMockRuntime):
    # Honeydew's program.md is missing on the first draft; the focused repair
    # turn is redirected to a concrete re-draft prompt before it can run.
    def __init__(self, *, runner_image: str) -> None:
        super().__init__(runner_image=runner_image)
        self.removed_first_protocol = False

    def run_turn(self, **kwargs):
        if (
            kwargs['agent'] == AgentName.HONEYDEW
            and kwargs['prompt'].startswith('Focused workspace repair.')
        ):
            return super().run_turn(
                **{
                    **kwargs,
                    'prompt': 'Draft a concrete program.md focused repair.',
                }
            )
        result = super().run_turn(**kwargs)
        if (
            kwargs['agent'] == AgentName.HONEYDEW
            and 'Draft a concrete program.md' in kwargs['prompt']
            and not self.removed_first_protocol
        ):
            (kwargs['workspace'] / 'program.md').unlink()
            self.removed_first_protocol = True
        return result


class MissingContractProposalThenRepairRuntime(ScriptedMockRuntime):
    # Honeydew returns no evaluation_contract_proposal on the first turn; the
    # structured-output repair path must recover the metadata.
    def __init__(self, *, runner_image: str) -> None:
        super().__init__(runner_image=runner_image)
        self.removed_first_proposal = False

    def run_turn(self, **kwargs):
        prompt = kwargs['prompt']
        if 'Focused structured-output repair' in prompt:
            return super().run_turn(
                **{
                    **kwargs,
                    'prompt': 'Draft a concrete program.md',
                }
            )
        result, message_id = super().run_turn(**kwargs)
        if (
            kwargs['agent'] == AgentName.HONEYDEW
            and 'Draft a concrete program.md' in prompt
            and not self.removed_first_proposal
        ):
            result.evaluation_contract_proposal = None
            self.removed_first_proposal = True
        return result, message_id


class MissingProtocolDeclarationThenRepairRuntime(ScriptedMockRuntime):
    # Honeydew's first protocol_draft turn declares no produced protocol file
    # (and no program.md exists on disk), reproducing the live contract
    # violation where a formally valid turn skipped the required workspace
    # action. The focused repair turn must name program.md and produce it.
    def __init__(self, *, runner_image: str) -> None:
        super().__init__(runner_image=runner_image)
        self.omitted_protocol_once = False

    def run_turn(self, **kwargs):
        prompt = kwargs['prompt']
        if 'Focused protocol-file repair' in prompt:
            return super().run_turn(
                **{
                    **kwargs,
                    'prompt': 'Draft a concrete program.md',
                }
            )
        result, message_id = super().run_turn(**kwargs)
        if (
            kwargs['agent'] == AgentName.HONEYDEW
            and 'Draft a concrete program.md' in prompt
            and not self.omitted_protocol_once
        ):
            (kwargs['workspace'] / 'program.md').unlink(missing_ok=True)
            result.produced_files = []
            self.omitted_protocol_once = True
        return result, message_id


class RepeatedWrongKindRuntime(ScriptedMockRuntime):
    # Honeydew returns a valid-but-wrong kind on every protocol-draft attempt,
    # exhausting the single focused repair and proving the run fails safely
    # without advancing state or requesting any action/job.
    def run_turn(self, **kwargs):
        self.turn_counts[kwargs['agent']] += 1
        if (
            kwargs['agent'] == AgentName.HONEYDEW
            and 'Draft a concrete program.md' in kwargs['prompt']
        ):
            return (
                AgentTurnResult(
                    kind=TurnKind.VERIFICATION,
                    summary='Persistently wrong kind.',
                    done=True,
                ),
                'mock-wrong-kind',
            )
        return super().run_turn(**kwargs)


class PauseDuringImplementationRuntime(ScriptedMockRuntime):
    # Injects a pause from inside the running Beaker turn, exercising the
    # pause-during-turn path where pause bypasses the advancement lock.
    def __init__(self, *, runner_image: str) -> None:
        super().__init__(runner_image=runner_image)
        self.engine: ResearchOrchestrator | None = None

    def run_turn(self, **kwargs):
        result = super().run_turn(**kwargs)
        if (
            self.engine is not None
            and kwargs['agent'] == AgentName.BEAKER
            and (
                'Implement the bounded' in kwargs['prompt']
                or 'Execute the task-specific plan' in kwargs['prompt']
            )
        ):
            self.engine.pause_run(
                kwargs['run_id'],
                requested_by='test-mid-turn',
            )
        return result


class NewContractRuntime(ScriptedMockRuntime):
    # Drives the full contract-candidate flow: Honeydew proposes a new
    # evaluator, Beaker drafts and seals the candidate (wrong phase kind once
    # to force a retry), then Honeydew reviews the sealed contract.
    def __init__(self, *, runner_image: str) -> None:
        super().__init__(runner_image=runner_image)
        self.returned_wrong_contract_kind = False

    def run_turn(self, **kwargs):
        prompt = kwargs['prompt']
        agent = kwargs['agent'].value
        if agent == 'honeydew' and 'Draft a concrete program.md' in prompt:
            result, message_id = super().run_turn(**kwargs)
            assert result.evaluation_contract_proposal is not None
            result.evaluation_contract_proposal.evaluator_type = 'candidate-v1'
            return result, message_id
        if agent == 'beaker' and 'Draft an immutable evaluation-contract' in prompt:
            if not self.returned_wrong_contract_kind:
                self.returned_wrong_contract_kind = True
                return (
                    AgentTurnResult(
                        kind=TurnKind.PROTOCOL_DRAFT,
                        summary='Returned a stale phase label.',
                        done=True,
                    ),
                    'mock-wrong-kind',
                )
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
            (root / 'evaluator.py').write_text('print("evaluate")\n')
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


class InvalidThenValidContractRuntime(NewContractRuntime):
    # First sealed candidate is tampered with on disk so its digest check
    # fails; the retry produces a valid candidate that promotes.
    def __init__(self, *, runner_image: str) -> None:
        super().__init__(runner_image=runner_image)
        self.returned_invalid_candidate = False

    def run_turn(self, **kwargs):
        result, message_id = super().run_turn(**kwargs)
        if (
            kwargs['agent'].value == 'beaker'
            and result.kind == TurnKind.CONTRACT_CANDIDATE
            and not self.returned_invalid_candidate
        ):
            self.returned_invalid_candidate = True
            root = (
                kwargs['workspace']
                / 'contract-candidate/candidate-v1/1.0.0'
            )
            descriptor = json.loads((root / 'contract.json').read_text())
            descriptor['unexpected'] = True
            (root / 'contract.json').write_text(json.dumps(descriptor))
        return result, message_id


class FailingContractReviewRuntime(NewContractRuntime):
    # Honeydew's contract review turn raises, which must pause the run for
    # recovery instead of terminalizing it.
    def run_turn(self, **kwargs):
        if (
            kwargs['agent'].value == 'honeydew'
            and 'Review the sealed evaluation-contract' in kwargs['prompt']
        ):
            raise RuntimeError('mock malformed Honeydew output')
        return super().run_turn(**kwargs)


def _pending_action(store, run_id: str, action_type: str):
    return next(
        action
        for action in store.list_actions(run_id)
        if action.type == action_type
        and action.approval_status == ApprovalStatus.PENDING
    )


def _advance_to_jobs(engine, store):
    # Drives a fresh run through protocol approval and matrix approval to the
    # JOB_RUNNING state, the shared entry point for the job-phase tests.
    run = engine.create_run(
        RunCreateRequest(
            objective='Test the bounded orchestrator workflow with fake evidence.'
        )
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )
    matrix = _pending_action(
        store,
        run.run_id,
        'submit_experiment_matrix',
    )
    assert matrix.honeydew_approved is True
    engine.approve_action(
        matrix.action_id,
        reviewer='test-human',
        reason='Fake execution accepted.',
    )
    return store.get_run(run.run_id)


def _complete_jobs(engine, store, cluster, run_id: str):
    # FakeClusterExecutor.complete marks each external run done, then
    # reconcile_run advances the run to AWAITING_FINAL_ACCEPTANCE.
    for job in store.list_jobs(run_id):
        assert job.external_run_id
        cluster.complete(
            job.external_run_id,
            metrics={'score': 0.75},
        )
    return engine.reconcile_run(run_id)


def test_protocol_rejection_redrafts_with_feedback(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Compare two bounded methods.')
    )
    original = _pending_action(store, run.run_id, 'approve_protocol')

    engine.reject_action(
        original.action_id,
        reviewer='test-human',
        reason='Use the fixed 80/20 split.',
    )

    revised = store.get_run(run.run_id)
    assert revised.state == RunState.AWAITING_PROTOCOL_APPROVAL
    assert revised.protocol_version == 2
    assert store.get_action(original.action_id).approval_status == (
        ApprovalStatus.REJECTED
    )
    replacement = _pending_action(
        store,
        run.run_id,
        'approve_protocol',
    )
    assert replacement.action_id != original.action_id


def test_new_contract_is_reviewed_promoted_and_bound(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, _, original = orchestrator_bundle
    engine = ResearchOrchestrator(
        settings=settings,
        store=store,
        runtime=NewContractRuntime(runner_image=RUNNER_IMAGE),
        workspaces=original.workspaces,
        contracts=original.contracts,
        contract_candidates=original.contract_candidates,
        policy=original.policy,
        cluster=cluster,
        discord=DisabledDiscordAdapter(),
    )
    run = engine.create_run(
        RunCreateRequest(objective='Exercise contract candidate promotion.')
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )

    awaiting = store.get_run(run.run_id)
    assert awaiting.state == RunState.AWAITING_CONTRACT_PROMOTION
    candidate = _pending_action(
        store,
        run.run_id,
        'propose_evaluation_contract',
    )
    assert candidate.honeydew_approved is True
    engine.approve_action(
        candidate.action_id,
        reviewer='test-admin',
        reason='Promote the reviewed harness.',
    )

    rebound = store.get_run(run.run_id)
    assert rebound.evaluation_contract_id == 'candidate-v1'
    assert rebound.state == RunState.AWAITING_EXECUTION_APPROVAL
    assert Path(settings.trusted_contract_catalog_path).is_file()
    assert any(
        event.event_type == 'agent.output_rejected'
        for event in store.list_events(run.run_id)
    )


def test_invalid_contract_candidate_is_rejected_and_retried(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, _, original = orchestrator_bundle
    engine = ResearchOrchestrator(
        settings=settings,
        store=store,
        runtime=InvalidThenValidContractRuntime(runner_image=RUNNER_IMAGE),
        workspaces=original.workspaces,
        contracts=original.contracts,
        contract_candidates=original.contract_candidates,
        policy=original.policy,
        cluster=cluster,
        discord=DisabledDiscordAdapter(),
    )
    run = engine.create_run(
        RunCreateRequest(objective='Retry an invalid contract candidate.')
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )

    assert store.get_run(run.run_id).state == (
        RunState.AWAITING_CONTRACT_PROMOTION
    )
    assert any(
        event.event_type == 'contract.candidate_rejected'
        for event in store.list_events(run.run_id)
    )


def test_recovery_agent_failure_pauses_instead_of_terminalizing(
    orchestrator_bundle,
) -> None:
    # An agent turn that raises mid-approval must park the run in PAUSED with
    # a fresh session, a recovery checkpoint, and a session-rotated event, so
    # a later recover() resumes at the exact interrupted phase.
    settings, store, cluster, _, original = orchestrator_bundle
    runtime = FailingContractReviewRuntime(runner_image=RUNNER_IMAGE)
    engine = ResearchOrchestrator(
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
    run = engine.create_run(
        RunCreateRequest(objective='Pause a failed recovered agent turn.')
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    with pytest.raises(RuntimeError, match='malformed Honeydew'):
        engine.approve_action(
            protocol.action_id,
            reviewer='test-human',
            reason='Protocol accepted.',
        )
    current = store.get_run(run.run_id)
    assert current.state == RunState.PAUSED
    assert current.resume_state == RunState.HONEYDEW_REVIEWING_CONTRACT
    assert current.honeydew_session_id is None
    checkpoint = (
        original.workspaces.paths(run.run_id).events
        / 'honeydew-recovery-checkpoint.json'
    )
    assert checkpoint.is_file()
    checkpoint_payload = json.loads(checkpoint.read_text())
    assert checkpoint_payload['agent'] == 'honeydew'
    assert checkpoint_payload['expected_turn_kind'] == 'methodology_review'
    assert (run.run_id, AgentName.HONEYDEW) in runtime.released
    assert any(
        event.event_type == 'agent.session_rotated'
        for event in store.list_events(run.run_id)
    )

    failed = current.model_copy(
        update={
            'state': RunState.HONEYDEW_REVIEWING_CONTRACT,
            'resume_state': None,
        }
    )
    store.replace_run(failed, expected_version=current.version)
    engine.recover()

    recovered = store.get_run(run.run_id)
    assert recovered.state == RunState.PAUSED
    assert recovered.resume_state == RunState.HONEYDEW_REVIEWING_CONTRACT
    assert any(
        event.event_type == 'run.recovery_failed'
        for event in store.list_events(run.run_id)
    )


def test_rejected_protocol_action_resumes_after_partial_failure(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Recover a partially rejected protocol.')
    )
    original = _pending_action(store, run.run_id, 'approve_protocol')
    store.update_action(
        original.action_id,
        approval_status=ApprovalStatus.REJECTED,
        reviewer='test-human',
        reason='Initial rejection was recorded.',
    )

    engine.reject_action(
        original.action_id,
        reviewer='test-human',
        reason='Use the available hardware and fixed split.',
    )

    recovered = store.get_run(run.run_id)
    assert recovered.state == RunState.AWAITING_PROTOCOL_APPROVAL
    assert recovered.protocol_version == 2
    resumed = next(
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'action.rejection_resumed'
    )
    assert resumed.payload['reason'] == (
        'Use the available hardware and fixed split.'
    )
    assert _pending_action(
        store,
        run.run_id,
        'approve_protocol',
    ).action_id != original.action_id


def test_mocked_complete_workflow_and_agent_isolation(orchestrator_bundle) -> None:
    _, store, cluster, runtime, engine = orchestrator_bundle
    run = _advance_to_jobs(engine, store)
    assert run.state == RunState.JOB_RUNNING
    run = _complete_jobs(engine, store, cluster, run.run_id)
    assert run.state == RunState.AWAITING_FINAL_ACCEPTANCE
    final_action = _pending_action(store, run.run_id, 'accept_final_report')
    engine.approve_action(
        final_action.action_id,
        reviewer='test-human',
        reason='Report accepted.',
    )
    run = store.get_run(run.run_id)
    assert run.state == RunState.COMPLETE
    assert run.beaker_session_id != run.honeydew_session_id
    assert run.beaker_workspace != run.honeydew_workspace
    assert Path(run.protocol_path or '').is_file()
    assert (Path(run.reports_path) / 'report.md').is_file()
    assert {
        artifact.type for artifact in store.list_artifacts(run.run_id)
    } == {
        'protocol',
        'evaluation_contract_proposal',
        'metrics',
        'report',
    }
    assert len(store.list_artifacts(run.run_id)) == 5
    turns = store.list_turns(run.run_id)
    assert len(turns) == 7
    assert all(turn.status == 'completed' for turn in turns)
    assert runtime.turn_counts


def test_missing_beaker_plan_file_gets_one_focused_repair(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    runtime = MissingPlanThenRepairRuntime(runner_image=RUNNER_IMAGE)
    engine.runtime = runtime
    run = engine.create_run(
        RunCreateRequest(objective='Repair a missing authoritative plan file.')
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')

    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )

    current = store.get_run(run.run_id)
    assert current.state == RunState.AWAITING_EXECUTION_APPROVAL
    assert (Path(current.beaker_workspace) / 'implementation-plan.md').is_file()
    event_types = {
        event.event_type for event in store.list_events(run.run_id)
    }
    assert 'agent.file_repair_requested' in event_types
    assert 'agent.file_repair_completed' in event_types
    assert runtime.turn_counts[AgentName.BEAKER] == 3


def test_missing_honeydew_protocol_file_gets_one_focused_repair(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    runtime = MissingProtocolThenRepairRuntime(runner_image=RUNNER_IMAGE)
    engine.runtime = runtime

    run = engine.create_run(
        RunCreateRequest(objective='Repair a missing authoritative protocol file.')
    )

    current = store.get_run(run.run_id)
    assert current.state == RunState.AWAITING_PROTOCOL_APPROVAL
    assert Path(current.protocol_path or '').is_file()
    event_types = {
        event.event_type for event in store.list_events(run.run_id)
    }
    assert 'agent.file_repair_requested' in event_types
    assert 'agent.file_repair_completed' in event_types
    assert runtime.turn_counts[AgentName.HONEYDEW] == 2


def test_missing_protocol_declaration_gets_one_focused_repair(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    runtime = MissingProtocolDeclarationThenRepairRuntime(
        runner_image=RUNNER_IMAGE
    )
    engine.runtime = runtime

    run = engine.create_run(
        RunCreateRequest(objective='Repair a missing protocol declaration.')
    )

    current = store.get_run(run.run_id)
    assert current.state == RunState.AWAITING_PROTOCOL_APPROVAL
    assert Path(current.protocol_path or '').is_file()
    assert runtime.omitted_protocol_once
    event_types = {
        event.event_type for event in store.list_events(run.run_id)
    }
    assert 'agent.output_rejected' in event_types
    assert runtime.turn_counts[AgentName.HONEYDEW] == 2


def test_repeated_wrong_kind_fails_without_advancing(
    orchestrator_bundle,
) -> None:
    _, store, cluster, _, engine = orchestrator_bundle
    runtime = RepeatedWrongKindRuntime(runner_image=RUNNER_IMAGE)
    engine.runtime = runtime

    with pytest.raises(WorkflowError):
        engine.create_run(
            RunCreateRequest(objective='Wrong kind on every draft attempt.')
        )

    run = next(iter(store.list_runs()))
    assert run.state == RunState.FAILED
    assert runtime.turn_counts[AgentName.HONEYDEW] == 2
    # The failed run must not have advanced: no approval action, no submitted
    # job, and no duplicated event stream beyond the two attempted turns.
    actions = store.list_actions(run.run_id)
    assert all(action.type != 'approve_protocol' for action in actions)
    assert not store.list_jobs(run.run_id)
    assert not cluster.submissions


def test_missing_contract_proposal_gets_one_focused_repair(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    runtime = MissingContractProposalThenRepairRuntime(runner_image=RUNNER_IMAGE)
    engine.runtime = runtime

    run = engine.create_run(
        RunCreateRequest(objective='Repair missing contract proposal metadata.')
    )

    assert run.state == RunState.AWAITING_PROTOCOL_APPROVAL
    proposal = next(
        artifact
        for artifact in store.list_artifacts(run.run_id)
        if artifact.type == 'evaluation_contract_proposal'
    )
    assert proposal.metadata['origin'] == 'honeydew'
    event_types = {
        event.event_type for event in store.list_events(run.run_id)
    }
    assert 'agent.output_rejected' in event_types
    assert 'agent.output_repaired' in event_types
    assert runtime.turn_counts[AgentName.HONEYDEW] == 2


def test_bound_legacy_contract_can_supply_missing_proposal(
    orchestrator_bundle,
) -> None:
    _, _, _, _, engine = orchestrator_bundle
    descriptor = engine.contracts.resolve(
        'ml-benchmark-adult-income-v1',
        '1.1.0',
    ).descriptor

    proposal = engine._proposal_from_bound_contract(descriptor)

    assert proposal.evaluator_type == descriptor.contract_id
    assert proposal.primary_metric.name == 'rubric_score'
    assert proposal.primary_metric.direction == 'maximize'
    assert proposal.required_artifacts == descriptor.required_artifacts
    assert proposal.max_wallclock_minutes == 60
    assert proposal.resource_constraints.model_dump() == (
        descriptor.resource_constraints.model_dump()
    )


def test_idempotent_job_submission(orchestrator_bundle) -> None:
    # Resubmitting the same spec must be a no-op in the fake cluster and the
    # store, guaranteeing recovery never duplicates external cluster jobs.
    _, store, cluster, _, engine = orchestrator_bundle
    run = _advance_to_jobs(engine, store)
    job = store.list_jobs(run.run_id)[0]
    first = cluster.submit(job.spec)
    second = cluster.submit(job.spec)
    assert first == second
    assert len(cluster.submissions) == len(store.list_jobs(run.run_id))
    stored, created = store.create_job_if_absent(job)
    assert created is False
    assert stored.job_id == job.job_id


def test_submitted_queued_job_is_inspected_without_resubmission(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    # A submitted-but-queued job must be inspected, never resubmitted: each
    # reconcile emits exactly one job.submitted event per job.
    _, store, cluster, _, engine = orchestrator_bundle
    original_submit = cluster.submit
    submission_calls = []

    def submit_as_accepted(spec):
        submission_calls.append(spec.idempotency_key)
        submission = original_submit(spec)
        accepted = submission.__class__(
            external_run_id=submission.external_run_id,
            job_name=submission.job_name,
            kubernetes_uid=submission.kubernetes_uid,
            status=JobStatus.QUEUED,
        )
        cluster.submissions[spec.idempotency_key] = accepted
        cluster.snapshots[accepted.external_run_id] = (
            cluster.snapshots[accepted.external_run_id].__class__(
                status=JobStatus.QUEUED
            )
        )
        return accepted

    monkeypatch.setattr(cluster, 'submit', submit_as_accepted)
    run = _advance_to_jobs(engine, store)
    jobs = store.list_jobs(run.run_id)
    assert all(job.status == JobStatus.QUEUED for job in jobs)
    assert all(job.external_run_id is not None for job in jobs)

    engine.reconcile_run(run.run_id)
    engine.reconcile_run(run.run_id)

    assert submission_calls == [job.idempotency_key for job in jobs]
    assert len(
        [
            event
            for event in store.list_events(run.run_id)
            if event.event_type == 'job.submitted'
        ]
    ) == len(jobs)


def test_policy_denial_returns_beaker_to_revision(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    engine.runtime = DeniedThenValidRuntime(runner_image=RUNNER_IMAGE)
    run = engine.create_run(
        RunCreateRequest(
            objective='Revise a policy-denied matrix without failing the run.'
        )
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )
    run = store.get_run(run.run_id)
    assert run.state == RunState.AWAITING_EXECUTION_APPROVAL
    denied = [
        action
        for action in store.list_actions(run.run_id)
        if action.approval_status == ApprovalStatus.DENIED
    ]
    assert len(denied) == 1
    assert 'not permitted' in denied[0].reason
    assert any(
        event.event_type == 'run.state_changed'
        and event.payload.get('to') == RunState.BEAKER_REVISING.value
        for event in store.list_events(run.run_id)
    )


def test_imported_task_prompt_uses_exact_dataset_binding_names(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Use exact imported dataset binding names.')
    )
    current = store.get_run(run.run_id)
    task = {
        'source_subdirectory': 'research-workspace/task',
        'runner_image': RUNNER_IMAGE,
        'resources': {
            'cpu': 1,
            'memory_gib': 1,
            'gpus': 0,
            'wallclock_minutes': 5,
        },
        'required_artifacts': ['metrics.json'],
        'datasets': [
            {
                'name': 'adult_train',
                'role': 'train',
                'uri': 's3://datasets/adult.data',
                'sha256': 'a' * 64,
                'contains_labels': True,
            }
        ],
    }
    store.replace_run(
        current.model_copy(update={'task_definition': task}),
        expected_version=current.version,
    )
    plan = Path(current.beaker_workspace) / 'implementation-plan.md'
    plan.write_text('# Plan\n')
    captured: dict[str, str] = {}

    def capture_turn(**kwargs):
        captured['prompt'] = kwargs['prompt']
        raise RuntimeError('prompt captured')

    monkeypatch.setattr(engine, '_run_agent_turn', capture_turn)

    with pytest.raises(RuntimeError, match='prompt captured'):
        engine._beaker_implement(run.run_id)

    implementation_prompt = captured['prompt']
    assert 'keyed by each exact declared dataset `name`' in implementation_prompt
    assert 'Do not assume generic keys such as `train` or `test`' in implementation_prompt
    assert '"adult_train": "/mnt/datasets/adult_train"' in implementation_prompt
    assert 'Run a loader-only smoke check' in implementation_prompt
    assert 'Do not run the full benchmark' in implementation_prompt
    assert 'Do not install Python or system dependencies' in implementation_prompt
    assert 'preselected runner image owns experiment dependencies' in implementation_prompt
    assert '`metrics.json` document root' in implementation_prompt


def test_imported_task_revision_does_not_retry_missing_dependencies(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Revise an imported benchmark safely.')
    )
    current = store.get_run(run.run_id)
    store.replace_run(
        current.model_copy(
            update={
                'task_definition': {
                    'source_subdirectory': 'benchmark-workspace/task',
                    'runner_image': RUNNER_IMAGE,
                    'resources': {
                        'cpu': 1,
                        'memory_gib': 1,
                        'gpus': 0,
                        'wallclock_minutes': 5,
                    },
                    'required_artifacts': ['metrics.json'],
                    'datasets': [],
                }
            }
        ),
        expected_version=current.version,
    )
    captured: dict[str, str] = {}

    def capture_turn(**kwargs):
        captured['prompt'] = kwargs['prompt']
        raise RuntimeError('prompt captured')

    monkeypatch.setattr(engine, '_run_agent_turn', capture_turn)

    with pytest.raises(RuntimeError, match='prompt captured'):
        engine._beaker_revise(run.run_id, feedback='Fix the smoke check.')

    revision_prompt = captured['prompt']
    assert 'Attempt each local command only once' in revision_prompt
    assert 'If a check fails with ModuleNotFoundError' in revision_prompt
    assert 'do not install packages, repeat the command' in revision_prompt
    assert 'artifact://, git://, event://, job://, or contract://' in revision_prompt


def test_contract_preflight_returns_beaker_to_revision(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    runtime = ContractOversizedThenValidRuntime(
        runner_image=RUNNER_IMAGE
    )
    engine.runtime = runtime
    run = engine.create_run(
        RunCreateRequest(
            objective='Reject a matrix that exceeds the evaluation contract.'
        )
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )

    run = store.get_run(run.run_id)
    assert run.state == RunState.AWAITING_EXECUTION_APPROVAL
    rejected = [
        action
        for action in store.list_actions(run.run_id)
        if action.type == 'submit_experiment_matrix'
        and action.approval_status == ApprovalStatus.REJECTED
    ]
    assert len(rejected) == 1
    assert 'evaluation-contract resource constraints' in rejected[0].reason
    pending = _pending_action(
        store,
        run.run_id,
        'submit_experiment_matrix',
    )
    assert pending.honeydew_approved is True
    revision_prompt = next(
        prompt
        for agent, prompt in runtime.prompts
        if agent == AgentName.BEAKER
        and 'focused deterministic-preflight correction' in prompt
    )
    assert 'Do not browse unrelated repository files' in revision_prompt
    assert '`configs/baseline.yaml`' in revision_prompt
    assert 'exact dotted path from the root' in revision_prompt
    assert 'not beneath `methodology`' in revision_prompt
    assert 'do not add `description` or `values` metadata wrappers' in revision_prompt
    assert any(
        agent == AgentName.HONEYDEW
        and 'Required root metric keys:' in prompt
        and 'nested copies do not satisfy the contract' in prompt
        for agent, prompt in runtime.prompts
    )


def test_transient_approved_action_failure_is_persisted_and_pauses_run(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Pause safely when approved action execution fails.'
        )
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')

    def fail_execution(_action):
        raise RuntimeError('temporary OpenCode outage')

    monkeypatch.setattr(engine, '_resume_approved_action', fail_execution)
    with pytest.raises(RuntimeError, match='temporary OpenCode outage'):
        engine.approve_action(
            protocol.action_id,
            reviewer='test-human',
            reason='Protocol accepted.',
        )

    assert store.get_run(run.run_id).state == RunState.PAUSED
    assert (
        store.get_action(protocol.action_id).approval_status
        == ApprovalStatus.APPROVED
    )
    failure = next(
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'action.execution_failed'
    )
    assert failure.payload['retryable'] is True
    assert failure.payload['jobs_created'] == 0
    assert failure.payload['resulting_state'] == RunState.PAUSED.value


def test_failed_resume_returns_run_to_paused_state(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Verify a failed resumed turn returns to paused state.'
        )
    )
    engine.pause_run(run.run_id, requested_by='test')

    def fail_recovery(_: str) -> None:
        raise RuntimeError('mock resumed turn timeout')

    monkeypatch.setattr(engine, '_recover_run', fail_recovery)
    with pytest.raises(RuntimeError, match='mock resumed turn timeout'):
        engine.resume_run(run.run_id, requested_by='test')

    paused = store.get_run(run.run_id)
    assert paused.state == RunState.PAUSED
    assert paused.resume_state == RunState.AWAITING_PROTOCOL_APPROVAL
    event = store.list_events(run.run_id)[-1]
    assert event.event_type == 'run.paused'
    assert event.payload['requested_by'] == 'orchestrator'
    assert 'mock resumed turn timeout' in event.payload['reason']


def test_pause_during_agent_turn_stops_workflow_advancement(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    runtime = PauseDuringImplementationRuntime(runner_image=RUNNER_IMAGE)
    runtime.engine = engine
    engine.runtime = runtime
    run = engine.create_run(
        RunCreateRequest(
            objective='Stop advancement when paused during implementation.'
        )
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')

    with pytest.raises(
        WorkflowError,
        match='workflow advancement stopped after agent turn',
    ):
        engine.approve_action(
            protocol.action_id,
            reviewer='test-human',
            reason='Protocol accepted.',
        )

    paused = store.get_run(run.run_id)
    assert paused.state == RunState.PAUSED
    assert paused.resume_state == RunState.BEAKER_IMPLEMENTING
    assert not any(
        action.type == 'submit_experiment_matrix'
        for action in store.list_actions(run.run_id)
    )
    assert runtime.turn_counts[AgentName.HONEYDEW] == 1


def test_honeydew_receives_read_only_beaker_review_snapshot(
    orchestrator_bundle,
) -> None:
    _, store, _, runtime, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Review the actual implementation before execution.'
        )
    )
    cache = Path(run.beaker_workspace) / '__pycache__'
    cache.mkdir()
    (cache / 'generated.pyc').write_bytes(b'generated bytecode')
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )

    review_root = Path(run.honeydew_workspace) / '.glasslab-review'
    manifest = json.loads((review_root / 'manifest.json').read_text())
    reviewed_paths = {item['path'] for item in manifest['files']}
    assert 'configs/baseline.yaml' in reviewed_paths
    assert 'experiment.py' in reviewed_paths
    assert 'implementation-plan.md' in reviewed_paths
    assert '__pycache__/generated.pyc' not in reviewed_paths
    assert (review_root / 'experiment.py').read_text() == (
        'print("bounded mock experiment")\n'
    )
    assert (review_root / 'experiment.py').stat().st_mode & 0o222 == 0
    honeydew_prompt = next(
        prompt
        for agent, prompt in reversed(runtime.prompts)
        if agent == AgentName.HONEYDEW and 'Review Beaker' in prompt
    )
    assert '.glasslab-review/manifest.json' in honeydew_prompt
    assert any(
        event.event_type == 'agent.review_snapshot_created'
        for event in store.list_events(run.run_id)
    )


def test_failed_turn_resumes_with_fresh_session_and_checkpoint(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    runtime = FailOnceImplementationRuntime(runner_image=RUNNER_IMAGE)
    engine.runtime = runtime
    run = engine.create_run(
        RunCreateRequest(
            objective='Recover implementation without retaining failed history.'
        )
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    with pytest.raises(RuntimeError, match='mock implementation timeout'):
        engine.approve_action(
            protocol.action_id,
            reviewer='test-human',
            reason='Protocol accepted.',
        )

    paused = store.get_run(run.run_id)
    assert paused.state == RunState.PAUSED
    assert paused.resume_state == RunState.BEAKER_IMPLEMENTING
    assert paused.beaker_session_id is None
    assert runtime.failed_session_id is not None

    legacy_paused = paused.model_copy(
        update={
            'beaker_runtime_id': 'legacy-runtime',
            'beaker_session_id': runtime.failed_session_id,
        }
    )
    store.replace_run(legacy_paused, expected_version=paused.version)
    resumed = engine.resume_run(run.run_id, requested_by='test-human')
    assert resumed.state == RunState.AWAITING_EXECUTION_APPROVAL
    assert resumed.beaker_session_id != runtime.failed_session_id
    assert runtime.recovery_prompt is not None
    assert runtime.recovery_prompt.startswith(
        'This is a fresh OpenCode session after an interrupted or failed turn.'
    )
    assert 'implementation-plan.md' in runtime.recovery_prompt


def test_imported_task_resume_finalizes_existing_runner(
    orchestrator_bundle,
) -> None:
    _, store, _, runtime, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Resume an imported task from its completed runner.'
        )
    )
    source_subdirectory = 'benchmark-workspace/adult-income'
    source = Path(run.beaker_workspace) / source_subdirectory
    source.mkdir(parents=True)
    (source / 'run.py').write_text(
        'import json\n'
        'with open("metrics.json", "w") as handle:\n'
        '    json.dump({"score": 1.0}, handle)\n'
    )
    config = Path(run.beaker_workspace) / 'configs' / 'candidate.yaml'
    config.parent.mkdir(exist_ok=True)
    config.write_text('candidate: true\n')
    (Path(run.beaker_workspace) / 'implementation-plan.md').write_text(
        '# Existing implementation plan\n'
    )
    task_definition = {
        'source_subdirectory': source_subdirectory,
        'runner_image': RUNNER_IMAGE,
        'resources': {
            'cpu': 1,
            'memory_gib': 1,
            'gpus': 0,
            'wallclock_minutes': 5,
        },
        'required_artifacts': ['metrics.json'],
        'datasets': [],
    }
    template = engine._matrix_action_template(
        run.model_copy(update={'task_definition': task_definition})
    )
    assert template['reason']
    ExperimentMatrix.model_validate(template['arguments'])
    paused = run.model_copy(
        update={
            'state': RunState.PAUSED,
            'resume_state': RunState.BEAKER_IMPLEMENTING,
            'task_definition': task_definition,
            'maximum_runtime_seconds': 60,
            'active_runtime_seconds': 1.0,
            'active_since': None,
        }
    )
    store.replace_run(paused, expected_version=run.version)

    resumed = engine.resume_run(run.run_id, requested_by='test-human')

    assert resumed.state == RunState.AWAITING_EXECUTION_APPROVAL
    finalizing_event = next(
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'run.state_changed'
        and event.payload['to'] == RunState.BEAKER_FINALIZING.value
    )
    assert finalizing_event.payload['from'] == RunState.BEAKER_IMPLEMENTING.value
    assert runtime.turn_counts[AgentName.BEAKER] == 1
    beaker_prompt = next(
        prompt
        for agent, prompt in runtime.prompts
        if agent == AgentName.BEAKER
    )
    assert 'Do not use any tools' in beaker_prompt
    assert resumed.active_runtime_seconds < resumed.maximum_runtime_seconds
    matrix = _pending_action(
        store,
        run.run_id,
        'submit_experiment_matrix',
    )
    assert matrix.honeydew_approved is True


def test_deterministic_matrix_execution_failure_requests_revision(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    # A deterministic expansion failure (ValueError, no jobs created) is the
    # one recovery a human is not needed for: Beaker re-drafts the matrix.
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Revise after deterministic matrix execution failure.'
        )
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )
    matrix = _pending_action(
        store,
        run.run_id,
        'submit_experiment_matrix',
    )

    def fail_submission(_action):
        raise ValueError('contract changed before submission')

    monkeypatch.setattr(engine, '_submit_matrix', fail_submission)
    with pytest.raises(ValueError, match='contract changed'):
        engine.approve_action(
            matrix.action_id,
            reviewer='test-human',
            reason='Matrix accepted.',
        )

    assert (
        store.get_action(matrix.action_id).approval_status
        == ApprovalStatus.EXECUTION_FAILED
    )
    assert store.get_run(run.run_id).state == RunState.AWAITING_EXECUTION_APPROVAL
    replacement = _pending_action(
        store,
        run.run_id,
        'submit_experiment_matrix',
    )
    assert replacement.action_id != matrix.action_id
    failure = next(
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'action.execution_failed'
    )
    assert failure.payload['retryable'] is False
    assert failure.payload['jobs_created'] == 0
    assert failure.payload['resulting_state'] == RunState.BEAKER_REVISING.value


def test_restart_recovery_from_job_running(orchestrator_bundle) -> None:
    # Rebuilds the engine from the same SqliteStore file after jobs completed,
    # simulating a process restart; recover() must finish the run from durable
    # state alone, without any in-memory knowledge.
    settings, store, cluster, _, engine = orchestrator_bundle
    run = _advance_to_jobs(engine, store)
    for job in store.list_jobs(run.run_id):
        assert job.external_run_id
        cluster.complete(job.external_run_id, metrics={'score': 0.9})

    restarted_store = SqliteStore(settings.database_path)
    restarted = ResearchOrchestrator(
        settings=settings,
        store=restarted_store,
        runtime=ScriptedMockRuntime(runner_image=RUNNER_IMAGE),
        workspaces=WorkspaceManager(
            workspace_root=settings.workspace_root,
            approved_repo_path=settings.approved_repo_path,
            approved_repo_ref=settings.approved_repo_ref,
        ),
        contracts=EvaluationContractResolver(
            settings.promoted_contract_root,
            fallback_roots=[settings.evaluation_contract_root],
        ),
        contract_candidates=ContractCandidateManager(
            sealed_root=settings.sealed_contract_candidate_root,
            promoted_root=settings.promoted_contract_root,
            catalog_path=settings.trusted_contract_catalog_path,
            shared_mount_root=settings.shared_mount_root,
        ),
        policy=ActionPolicy(
            permitted_images=settings.permitted_job_images,
            maximum_cpu=settings.maximum_cpu,
            maximum_memory_gib=settings.maximum_memory_gib,
            maximum_gpus=settings.maximum_gpus,
            maximum_parallel_jobs=settings.maximum_parallel_jobs,
        ),
        cluster=cluster,
        discord=DisabledDiscordAdapter(),
    )
    assert restarted.recover() == [run.run_id]
    recovered = restarted_store.get_run(run.run_id)
    assert recovered.state == RunState.AWAITING_FINAL_ACCEPTANCE
    assert len(restarted_store.list_artifacts(run.run_id)) == 5


def test_recovery_does_not_replay_stale_approved_matrix(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    # An older APPROVED matrix must never be replayed when a newer PENDING
    # replacement exists; only the latest proposed action drives recovery.
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Do not replay an obsolete matrix approval.')
    )
    run = store.replace_run(
        run.model_copy(update={'state': RunState.AWAITING_EXECUTION_APPROVAL}),
        expected_version=run.version,
    )
    old = store.save_action(
        ActionRecord(
            run_id=run.run_id,
            proposed_by=AgentName.BEAKER,
            type='submit_experiment_matrix',
            arguments={},
            policy_classification=(
                PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL
            ),
            approval_status=ApprovalStatus.APPROVED,
            reason='Old approved matrix.',
            idempotency_key='old-matrix',
        )
    )
    latest = store.save_action(
        ActionRecord(
            run_id=run.run_id,
            proposed_by=AgentName.BEAKER,
            type='submit_experiment_matrix',
            arguments={},
            policy_classification=(
                PolicyClassification.HONEYDEW_AND_HUMAN_APPROVAL
            ),
            approval_status=ApprovalStatus.PENDING,
            reason='Replacement matrix awaiting approval.',
            idempotency_key='latest-matrix',
        )
    )
    submitted: list[str] = []
    monkeypatch.setattr(
        engine,
        '_submit_matrix',
        lambda action: submitted.append(action.action_id),
    )

    engine._recover_run(run.run_id)

    assert old.action_id != latest.action_id
    assert submitted == []
    assert store.get_run(run.run_id).state == RunState.AWAITING_EXECUTION_APPROVAL


def test_recovery_backfills_protocol_artifact_from_event(
    orchestrator_bundle,
) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Backfill a protocol artifact from its durable event.'
        )
    )
    protocol = next(
        artifact
        for artifact in store.list_artifacts(run.run_id)
        if artifact.type == 'protocol'
    )
    with store._connect() as connection:
        connection.execute(
            'DELETE FROM artifacts WHERE artifact_id = ?',
            (protocol.artifact_id,),
        )

    assert {
        artifact.type for artifact in store.list_artifacts(run.run_id)
    } == {'evaluation_contract_proposal'}
    engine.recover()

    restored = [
        artifact
        for artifact in store.list_artifacts(run.run_id)
        if artifact.type == 'protocol'
    ]
    assert len(restored) == 1
    assert restored[0].sha256 == protocol.sha256


def test_restart_recovers_submission_without_external_id(
    orchestrator_bundle,
) -> None:
    _, store, cluster, _, engine = orchestrator_bundle
    run = _advance_to_jobs(engine, store)
    job = store.list_jobs(run.run_id)[0]
    store.update_job(
        job.model_copy(
            update={
                'status': JobStatus.SUBMITTING,
                'external_run_id': None,
                'job_name': None,
                'kubernetes_uid': None,
            }
        )
    )
    engine.recover()
    recovered = store.get_job(job.job_id)
    assert recovered.status == JobStatus.RUNNING
    assert recovered.external_run_id is not None


def test_transient_inspection_error_does_not_finish_run(
    orchestrator_bundle,
) -> None:
    _, store, cluster, _, engine = orchestrator_bundle
    run = _advance_to_jobs(engine, store)
    job = store.list_jobs(run.run_id)[0]
    snapshot = cluster.snapshots.pop(job.external_run_id)
    engine.reconcile_run(run.run_id)
    assert store.get_run(run.run_id).state == RunState.JOB_RUNNING
    assert store.get_job(job.job_id).status == JobStatus.UNKNOWN
    cluster.snapshots[job.external_run_id] = snapshot
    engine.reconcile_run(run.run_id)
    assert store.get_job(job.job_id).status == JobStatus.RUNNING


def test_cancellation_aborts_sessions_and_jobs(orchestrator_bundle) -> None:
    _, store, cluster, runtime, engine = orchestrator_bundle
    run = _advance_to_jobs(engine, store)
    cancelled = engine.cancel_run(run.run_id)
    assert cancelled.state == RunState.CANCELLED
    assert len(runtime.aborted) == 2
    assert all(
        job.status.value == 'cancelled'
        for job in store.list_jobs(run.run_id)
    )
    assert all(
        cluster.inspect(job.external_run_id).status.value == 'cancelled'
        for job in store.list_jobs(run.run_id)
        if job.external_run_id
    )


def test_cancellation_failure_does_not_claim_external_job_or_run_cancelled(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    _, store, cluster, runtime, engine = orchestrator_bundle
    run = _advance_to_jobs(engine, store)
    job = store.list_jobs(run.run_id)[0]

    def fail_cancel(_external_run_id: str) -> None:
        raise RuntimeError('workflow-api cancellation unavailable')

    monkeypatch.setattr(cluster, 'cancel', fail_cancel)

    with pytest.raises(WorkflowError, match='could not be confirmed'):
        engine.cancel_run(run.run_id)

    assert store.get_run(run.run_id).state != RunState.CANCELLED
    assert store.get_job(job.job_id).status != JobStatus.CANCELLED
    assert runtime.aborted
    event = store.list_events(run.run_id)[-1]
    assert event.event_type == 'run.cancellation_failed'
    assert event.payload['cancellation_errors']


def test_cancellation_discards_paused_run_without_resuming(orchestrator_bundle) -> None:
    _, store, _, runtime, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Discard a paused research run.')
    )
    engine.pause_run(run.run_id, requested_by='test')

    cancelled = engine.cancel_run(run.run_id, requested_by='test')

    assert cancelled.state == RunState.CANCELLED
    assert store.get_run(run.run_id).state == RunState.CANCELLED
    assert runtime.aborted
    assert store.list_events(run.run_id)[-1].event_type == 'run.cancelled'


def test_event_sequence_is_append_only_and_ordered(orchestrator_bundle) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Verify append-only event sequence ordering for a run.'
        )
    )
    events = store.list_events(run.run_id)
    assert [event.sequence_number for event in events] == list(
        range(1, len(events) + 1)
    )
    assert events[0].event_type == 'run.created'
    assert any(event.event_type == 'action.proposed' for event in events)


def test_http_turns_endpoint_redacts_and_bounds_history(
    orchestrator_bundle,
) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='Expose structured turn history through a read-only API.'
        )
    )
    # The scripted runtime already recorded a real Honeydew protocol_draft
    # turn; add a synthetic turn carrying credential-shaped content in both
    # input_event and error so redaction is exercised end to end, not just
    # unit-tested in isolation (see test_redaction.py).
    store.save_turn(
        TurnRecord(
            run_id=run.run_id,
            agent=AgentName.BEAKER,
            input_event={
                'objective': run.objective,
                'discord_bot_token': 'not-a-real-token',
            },
            status='failed',
            error='provider rejected request: Bearer abcdefghijklmnop0123456789',
        )
    )

    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        assert client.get('/runs/not-a-real-run/turns').status_code == 404

        response = client.get(f'/runs/{run.run_id}/turns')
        assert response.status_code == 200
        turns = response.json()['turns']
        assert len(turns) == 2

        protocol_turn = next(
            item for item in turns if item['agent'] == 'honeydew'
        )
        assert protocol_turn['status'] == 'completed'
        assert protocol_turn['output']['kind'] == 'protocol_draft'
        assert protocol_turn['started_at'] is not None
        assert protocol_turn['ended_at'] is not None

        failed_turn = next(item for item in turns if item['agent'] == 'beaker')
        assert failed_turn['status'] == 'failed'
        assert failed_turn['ended_at'] is not None
        assert 'not-a-real-token' not in json.dumps(failed_turn)
        assert failed_turn['input']['discord_bot_token'] == '[REDACTED]'
        assert 'abcdefghijklmnop0123456789' not in failed_turn['error']
        assert failed_turn['error'] == '[REDACTED]'

        bounded = client.get(f'/runs/{run.run_id}/turns', params={'limit': 1})
        assert bounded.status_code == 200
        bounded_turns = bounded.json()['turns']
        assert len(bounded_turns) == 1
        # limit keeps the most recent turn(s), in original chronological
        # order, matching summarize_turns' documented behavior.
        assert bounded_turns[0]['turn_id'] == failed_turn['turn_id']

        assert client.get(
            f'/runs/{run.run_id}/turns',
            params={'limit': 0},
        ).status_code == 422
        assert client.get(
            f'/runs/{run.run_id}/turns',
            params={'limit': 10000},
        ).status_code == 422


def test_http_api_with_mock_runtime(orchestrator_bundle) -> None:
    settings, store, _, _, engine = orchestrator_bundle
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        assert client.get('/ready').status_code == 200
        response = client.post(
            '/runs',
            json={
                'objective': (
                    'Create an API-driven protocol with a mocked OpenCode turn.'
                )
            },
        )
        assert response.status_code == 201
        run_id = response.json()['run_id']
        assert response.json()['state'] == 'AWAITING_PROTOCOL_APPROVAL'
        assert client.get(f'/runs/{run_id}').status_code == 200
        paused = client.post(f'/runs/{run_id}/pause')
        assert paused.status_code == 200
        assert paused.json()['state'] == 'PAUSED'
        resumed = client.post(f'/runs/{run_id}/resume')
        assert resumed.status_code == 200
        assert resumed.json()['state'] == 'AWAITING_PROTOCOL_APPROVAL'
        events = client.get(f'/runs/{run_id}/events').json()['events']
        assert events
        assert any(event['event_type'] == 'run.paused' for event in events)
        assert any(event['event_type'] == 'run.resumed' for event in events)
        dataset = client.post(
            '/datasets/import',
            files={'dataset': ('train.csv', b'feature,label\n1,0\n', 'text/csv')},
            data={
                'name': 'training_data',
                'role': 'train',
                'contains_labels': 'true',
                'uploaded_by': 'api-test',
            },
        )
        assert dataset.status_code == 201
        assert dataset.json()['reference_uri'].startswith(
            'glasslab-dataset://'
        )
        dataset_id = dataset.json()['dataset_id']
        assert client.get(f'/datasets/{dataset_id}').status_code == 200
        assert len(client.get('/datasets').json()) == 1
        action = _pending_action(store, run_id, 'approve_protocol')
        approval = client.post(
            f'/actions/{action.action_id}/approve',
            json={'reviewer': 'api-human', 'reason': 'Approved by API test.'},
        )
        assert approval.status_code == 200
        assert client.get(f'/actions/{action.action_id}').status_code == 200


def test_http_startup_does_not_wait_for_recovery(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    recovery_started = Event()
    release_recovery = Event()

    def blocking_recovery() -> list[str]:
        recovery_started.set()
        release_recovery.wait(timeout=5)
        return []

    monkeypatch.setattr(engine, 'recover', blocking_recovery)
    app = create_app(settings, engine=engine, start_watcher=False)
    with TestClient(app) as client:
        assert recovery_started.wait(timeout=1)
        assert client.get('/health').status_code == 200
        release_recovery.set()


def test_http_mutations_require_operator_token(orchestrator_bundle) -> None:
    settings, _, _, _, engine = orchestrator_bundle
    secured = settings.model_copy(
        update={
            'require_operator_auth': True,
            'operator_api_token': 'test-operator-token',
        }
    )
    app = create_app(secured, engine=engine, start_watcher=False)
    payload = {
        'objective': 'Create a secured API-driven research protocol.'
    }
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        assert client.post('/runs', json=payload).status_code == 401
        response = client.post(
            '/runs',
            json=payload,
            headers={
                'X-Glasslab-Operator-Token': 'test-operator-token'
            },
        )
        assert response.status_code == 201
