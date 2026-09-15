"""Bounded retry of retryable agent-turn failures (issue #229).

A protocol-draft turn that burns the full wall-clock budget (or a transient
provider/startup failure) must not terminally fail the run: the engine should
retry the turn with a rotated fresh session, bounded by a configured retry
count. Deterministic failures (structured-output validation, kind mismatch)
must never be retried.
"""

from __future__ import annotations

import json

import pytest

from app.engine import ResearchOrchestrator, WorkflowError
from app.mock_runtime import ScriptedMockRuntime
from app.opencode_runtime import AgentRuntime, OpenCodeRuntimeError
from app.schemas import AgentName, RunCreateRequest, RunState, TurnKind


class FlakyTurnRuntime:
    """Delegates to a real mock runtime but fails the first N run_turn calls."""

    def __init__(
        self,
        inner: AgentRuntime,
        *,
        fail_calls: int,
        failure_class: str,
        details: dict | None = None,
    ) -> None:
        self.inner = inner
        self.fail_calls = fail_calls
        self.failure_class = failure_class
        self.details = details
        self.attempts = 0

    def ensure_session(self, **kwargs):
        return self.inner.ensure_session(**kwargs)

    def run_turn(self, **kwargs):
        self.attempts += 1
        if self.attempts <= self.fail_calls:
            raise OpenCodeRuntimeError(
                "OpenCode turn exceeded the hard wall-clock limit of 1800 seconds",
                failure_class=self.failure_class,
                details=self.details,
            )
        return self.inner.run_turn(**kwargs)

    def abort(self, **kwargs):
        return self.inner.abort(**kwargs)

    def close(self):
        return self.inner.close()

    def release(self, **kwargs):
        return self.inner.release(**kwargs)


class PauseOrCancelBeforeRetryRuntime(ScriptedMockRuntime):
    """Fails the first turn with a retryable error after pausing/cancelling.

    Reproduces issue #239 Mechanism B: pause/cancel abort an in-flight turn,
    the abort surfaces inside ``_run_agent_turn`` as a retryable provider
    error, and the retry path must refuse to launch a fresh model turn on a
    run that is no longer advanceable.
    """

    def __init__(
        self,
        *,
        runner_image: str,
        engine: ResearchOrchestrator,
        transition: str,
    ) -> None:
        super().__init__(runner_image=runner_image)
        self.engine = engine
        self.transition = transition
        self.attempts = 0

    def run_turn(self, **kwargs):
        self.attempts += 1
        if self.attempts == 1:
            if self.transition == 'pause':
                self.engine.pause_run(kwargs['run_id'], requested_by='test')
            else:
                self.engine.cancel_run(kwargs['run_id'], requested_by='test')
            raise OpenCodeRuntimeError(
                "OpenCode turn exceeded the hard wall-clock limit of 1800 seconds",
                failure_class="network",
            )
        return super().run_turn(**kwargs)


def _make_run(engine: ResearchOrchestrator, objective: str):
    return engine.create_run(RunCreateRequest(objective=objective))


def _draft_prompt() -> str:
    return (
        "Draft a concrete program.md for this objective: retry test\n\n"
        "Evaluation contract: contract://generic-task-integrity-v1/1.0.0"
    )


def test_retryable_turn_failure_is_retried_with_fresh_session(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Retry test objective for the agent-turn retry path.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=1, failure_class="network"
    )
    _, result = engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={"objective": "retry test"},
    )
    assert result.kind == TurnKind.PROTOCOL_DRAFT
    assert engine.runtime.attempts == 2
    events = store.list_events(run.run_id)
    assert any(e.event_type == "agent.session_rotated" for e in events)
    final = store.get_run(run.run_id)
    assert final.honeydew_session_id is not None


def test_non_retryable_turn_failure_is_not_retried(orchestrator_bundle) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Non-retryable failure objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=10, failure_class="validation"
    )
    with pytest.raises(OpenCodeRuntimeError):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={"objective": "retry test"},
        )
    assert engine.runtime.attempts == 1


def test_retry_bound_is_respected(orchestrator_bundle) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Bounded retry objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=10, failure_class="network"
    )
    with pytest.raises(OpenCodeRuntimeError):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={"objective": "retry test"},
        )
    assert engine.runtime.attempts == 1 + settings.agent_turn_max_retries


def test_turn_timeout_failure_is_not_retried(orchestrator_bundle) -> None:
    """A wall-clock abort pauses instead of burning retry budget.

    A fresh session with the same prompt re-enters the same work and the same
    wall; the run pauses and resumes with the worktree intact via resume_run()
    (see engine._should_retry_turn classification).
    """
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Turn timeout objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=10, failure_class="turn_timeout"
    )
    with pytest.raises(OpenCodeRuntimeError):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={"objective": "retry test"},
        )
    assert engine.runtime.attempts == 1


def test_repeated_tool_loop_failure_is_not_retried(orchestrator_bundle) -> None:
    """A stuck-tool-loop abort is deterministic, not transient."""
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Repeated tool loop objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=10, failure_class="repeated_tool_loop"
    )
    with pytest.raises(OpenCodeRuntimeError):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={"objective": "retry test"},
        )
    assert engine.runtime.attempts == 1


def test_doom_loop_failure_records_last_failure_and_corrective_context(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Doom loop corrective recovery objective.")
    details = {
        'tool': 'bash',
        'count': 6,
        'input_digest': 'a1b2c3d4e5f60718',
    }
    engine.runtime = FlakyTurnRuntime(
        runtime,
        fail_calls=10,
        failure_class='repeated_tool_loop',
        details=details,
    )
    with pytest.raises(OpenCodeRuntimeError):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={"objective": "doom loop test"},
        )

    checkpoint_path = (
        engine.workspaces.paths(run.run_id).events
        / 'honeydew-recovery-checkpoint.json'
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
    assert checkpoint['schema_version'] == 'glasslab-recovery-checkpoint-v1'
    assert checkpoint['last_failure']['failure_class'] == 'repeated_tool_loop'
    assert checkpoint['last_failure']['repeated_tool'] == 'bash'
    assert checkpoint['last_failure']['repeated_count'] == 6
    assert checkpoint['last_failure']['input_digest'] == 'a1b2c3d4e5f60718'

    doom_events = [
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'agent.doom_loop_detected'
    ]
    assert len(doom_events) == 1
    assert doom_events[0].payload['repeated_tool'] == 'bash'
    assert doom_events[0].payload['repeated_count'] == 6
    assert doom_events[0].payload['input_digest'] == 'a1b2c3d4e5f60718'

    context = engine._recovery_context(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
    )
    assert 'bash' in context
    assert 'byte-identical' in context
    assert 'Do NOT repeat that identical call' in context
    assert 'workspace_status' in context


def test_non_doom_loop_failure_has_no_corrective_instruction(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Turn timeout recovery objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=10, failure_class='turn_timeout'
    )
    with pytest.raises(OpenCodeRuntimeError):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={"objective": "timeout test"},
        )

    checkpoint_path = (
        engine.workspaces.paths(run.run_id).events
        / 'honeydew-recovery-checkpoint.json'
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
    assert checkpoint['last_failure']['failure_class'] == 'turn_timeout'
    assert 'repeated_tool' not in checkpoint['last_failure']

    context = engine._recovery_context(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
    )
    assert 'Do NOT repeat that identical call' not in context
    assert 'byte-identical' not in context
    assert 'workspace_status' in context
    assert not any(
        event.event_type == 'agent.doom_loop_detected'
        for event in store.list_events(run.run_id)
    )


def test_step_budget_failure_is_not_retried(orchestrator_bundle) -> None:
    """A step-budget abort is deterministic, not transient."""
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Step budget objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime,
        fail_calls=10,
        failure_class='step_budget_exceeded',
        details={'step_count': 300, 'step_limit': 250},
    )
    with pytest.raises(OpenCodeRuntimeError):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={"objective": "step budget test"},
        )
    assert engine.runtime.attempts == 1


def test_step_budget_failure_records_last_failure_and_corrective_context(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Step budget corrective recovery objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime,
        fail_calls=10,
        failure_class='step_budget_exceeded',
        details={'step_count': 300, 'step_limit': 250},
    )
    with pytest.raises(OpenCodeRuntimeError):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={"objective": "step budget test"},
        )

    checkpoint_path = (
        engine.workspaces.paths(run.run_id).events
        / 'honeydew-recovery-checkpoint.json'
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
    assert checkpoint['last_failure']['failure_class'] == 'step_budget_exceeded'
    assert checkpoint['last_failure']['step_count'] == 300
    assert checkpoint['last_failure']['step_limit'] == 250

    budget_events = [
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'agent.turn_step_budget_exceeded'
    ]
    assert len(budget_events) == 1
    assert budget_events[0].payload['step_count'] == 300
    assert budget_events[0].payload['step_limit'] == 250

    context = engine._recovery_context(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
    )
    assert 'exceeded the step budget' in context
    assert '300' in context
    assert 'Stop exploring' in context
    assert 'workspace_status' in context
    # A step-budget failure must never carry the doom-loop correction.
    assert 'byte-identical' not in context
    assert 'Do NOT repeat that identical call' not in context


def test_doom_loop_corrective_context_omits_step_budget_instruction(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Doom loop only corrective recovery objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime,
        fail_calls=10,
        failure_class='repeated_tool_loop',
        details={
            'tool': 'bash',
            'count': 6,
            'input_digest': 'a1b2c3d4e5f60718',
        },
    )
    with pytest.raises(OpenCodeRuntimeError):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={"objective": "doom loop test"},
        )

    context = engine._recovery_context(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
    )
    assert 'byte-identical' in context
    assert 'Do NOT repeat that identical call' in context
    # The step-budget correction must not leak into a doom-loop recovery.
    assert 'exceeded the step budget' not in context
    assert 'Stop exploring' not in context


def test_provider_failure_is_retryable(orchestrator_bundle) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Provider failure objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=1, failure_class="provider"
    )
    _, result = engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={"objective": "retry test"},
    )
    assert result.kind == TurnKind.PROTOCOL_DRAFT
    assert engine.runtime.attempts == 2


def test_startup_failure_is_retryable(orchestrator_bundle) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Startup failure objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=1, failure_class="startup"
    )
    _, result = engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={"objective": "retry test"},
    )
    assert result.kind == TurnKind.PROTOCOL_DRAFT
    assert engine.runtime.attempts == 2


@pytest.mark.parametrize('transition', ['pause', 'cancel'])
def test_retry_refused_after_pause_or_cancel(
    orchestrator_bundle,
    transition,
) -> None:
    """A retryable failure must not start a fresh turn on a paused/terminal run.

    Pause/cancel abort an in-flight turn; the abort surfaces as a retryable
    provider/network error. The retry path must re-read the run state and
    refuse to launch a brand-new model turn on a run that is no longer
    advanceable (issue #239).
    """
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Retry refusal after pause/cancel objective.")
    engine.runtime = PauseOrCancelBeforeRetryRuntime(
        runner_image=runtime.runner_image,
        engine=engine,
        transition=transition,
    )
    with pytest.raises(WorkflowError, match='workflow advancement stopped'):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={"objective": "retry test"},
        )
    # Only the aborted attempt ran; the retry was refused.
    assert engine.runtime.attempts == 1
    final = store.get_run(run.run_id)
    assert final.state == (
        RunState.PAUSED if transition == 'pause' else RunState.CANCELLED
    )