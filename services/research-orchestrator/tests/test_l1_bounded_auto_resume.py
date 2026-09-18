"""Bounded automatic resume of wall-clock turn aborts (L1).

Live evidence (run ``df9995aa``, 2026-09-18): turn 3 of 20 hit the OpenCode
hard wall-clock limit (``OpenCode turn exceeded the hard wall-clock limit of
1800 seconds``, ``failure_class='turn_timeout'``). ``turn_timeout`` is
deliberately non-retryable (``engine.NON_RETRYABLE_TURN_FAILURE_CLASSES``):
the engine rotates the session and raises, the just-incremented
``turn_number`` is NOT rolled back, and the run parks in ``PAUSED`` awaiting a
human resume. The retry only completed because a human/driver resumed it,
permanently spending one of ``maximum_turns`` (``run.auto_resumed`` count = 0
live).

L1 makes that resume bounded and automatic:

* a ``turn_timeout`` auto-resumes the turn (fresh rotated session, same
  prompt) while the run is inside ``agent_turn_timeout_auto_resume_limit``;
* every auto-resume emits ``run.auto_resumed`` carrying
  ``failure_class='turn_timeout'``;
* the failed attempt's ``turn_number`` is rolled back so an auto-resumed turn
  never consumes ``maximum_turns``;
* once the bound is exhausted the original failure propagates and the run is
  parked in a clean, resumable ``PAUSED`` -- not a strand and not a loop;
* a ``methodology.human_resolution_requested`` pause is NEVER auto-resumed
  (runbook ``drive-a-real-run.md`` section 5.3): resuming it re-enters the
  non-converging loop and burns the turn budget to ``TIMED_OUT``.

Tests 4 and 5 are the L1 rotation-accounting half: when a session rotates, the
measure must be the real context size the runtime observed, not a whitespace
word count over stored turns (issue #517: the live 60,333-token prompt was
estimated at 2,014 words). When no usage is observable the fallback is
deterministic and is a floor -- never a host-safe bound.

These tests are the T7 specification. Tests 4 and 5 are live against the
merged real-token rotation (PR #518). T7 implemented the bounded auto-resume
in the engine's turn-failure handler and removed the remaining
``xfail(strict=True)`` markers; the assertions are unchanged.
"""

from __future__ import annotations

import json

import pytest

from app.engine import ResearchOrchestrator
from app.opencode_runtime import AgentRuntime, OpenCodeRuntimeError
from app.schemas import AgentName, RunCreateRequest, RunState, TurnKind


# T7 adds this bound to Settings (default 2, 0 disables). Until the field
# exists, set it through the instance mapping so the RED run fails on the
# engine behavior under test, not on Settings field validation.
AUTO_RESUME_LIMIT_FIELD = 'agent_turn_timeout_auto_resume_limit'


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


def _set_auto_resume_limit(
    settings: object,
    limit: int,
) -> None:
    object.__setattr__(settings, AUTO_RESUME_LIMIT_FIELD, limit)


def _auto_resumed_events(store, run_id: str):
    return [
        event
        for event in store.list_events(run_id)
        if event.event_type == 'run.auto_resumed'
    ]


def _make_run(
    engine: ResearchOrchestrator,
    objective: str,
    **request_kwargs,
):
    return engine.create_run(
        RunCreateRequest(objective=objective, **request_kwargs)
    )


def _draft_prompt() -> str:
    return (
        "Draft a concrete program.md for this objective: auto-resume test\n\n"
        "Evaluation contract: contract://generic-task-integrity-v1/1.0.0"
    )


def _pending_action(store, run_id: str, action_type: str):
    return next(
        action
        for action in store.list_actions(run_id)
        if action.type == action_type
        and action.approval_status.value == 'pending'
    )


def _dense_blob(entries: int) -> str:
    # Compact JSON collapses to a handful of whitespace words; a token-like
    # estimate sees thousands.
    return json.dumps(
        {
            f'k{index}': [index, index + 1, {'nested': index * 2}]
            for index in range(entries)
        },
        separators=(',', ':'),
    )


def test_turn_timeout_auto_resumes_within_bound(orchestrator_bundle) -> None:
    """A wall-clock abort resumes automatically and advances the run."""
    settings, store, cluster, runtime, engine = orchestrator_bundle
    _set_auto_resume_limit(settings, 2)
    run = _make_run(engine, 'Bounded wall-clock auto-resume objective.')
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=1, failure_class='turn_timeout'
    )
    _, result = engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'auto-resume test'},
    )
    # The auto-resumed attempt completed the turn instead of raising.
    assert result.kind == TurnKind.PROTOCOL_DRAFT
    assert engine.runtime.attempts == 2

    resumes = _auto_resumed_events(store, run.run_id)
    assert len(resumes) == 1
    assert resumes[0].payload.get('failure_class') == 'turn_timeout'

    final = store.get_run(run.run_id)
    # The failed attempt's just-incremented turn number was rolled back:
    # draft (1) + the successful auto-resumed turn (2).
    assert final.turn_number == 2
    assert final.state != RunState.PAUSED
    assert final.state != RunState.TIMED_OUT
    assert not any(
        event.event_type == 'run.paused'
        for event in store.list_events(run.run_id)
    )


def test_turn_timeout_pauses_after_bound_exhausted(orchestrator_bundle) -> None:
    """After the bound the run parks in a clean, resumable PAUSED."""
    settings, store, cluster, runtime, engine = orchestrator_bundle
    _set_auto_resume_limit(settings, 2)
    run = _make_run(engine, 'Exhausted wall-clock auto-resume objective.')
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=100, failure_class='turn_timeout'
    )
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    with pytest.raises(OpenCodeRuntimeError):
        engine.approve_action(
            protocol.action_id,
            reviewer='test-human',
            reason='Protocol accepted.',
        )
    # Exactly the initial attempt plus the bounded auto-resumes: no loop.
    assert engine.runtime.attempts == 3
    resumes = _auto_resumed_events(store, run.run_id)
    assert len(resumes) == 2

    final = store.get_run(run.run_id)
    assert final.state == RunState.PAUSED
    assert final.resume_state is not None


def test_auto_resumed_turn_does_not_consume_budget(orchestrator_bundle) -> None:
    """An auto-resumed turn must not consume maximum_turns."""
    settings, store, cluster, runtime, engine = orchestrator_bundle
    _set_auto_resume_limit(settings, 2)
    run = _make_run(
        engine,
        'Auto-resumed turns must not consume the turn budget.',
        maximum_turns=3,
    )
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=2, failure_class='turn_timeout'
    )
    _, result = engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'budget test'},
    )
    assert result.kind == TurnKind.PROTOCOL_DRAFT
    assert engine.runtime.attempts == 3
    assert len(_auto_resumed_events(store, run.run_id)) == 2

    final = store.get_run(run.run_id)
    # Two real completed turns (draft + this one); the two rolled-back failed
    # attempts did not spend budget.
    assert final.turn_number == 2
    assert final.maximum_turns == 3
    assert final.state != RunState.TIMED_OUT
    assert not any(
        event.event_type == 'run.state_changed'
        and event.payload.get('to') == RunState.TIMED_OUT.value
        for event in store.list_events(run.run_id)
    )


def test_rotation_uses_runtime_reported_tokens(orchestrator_bundle) -> None:
    """Rotation consumes the real observed context, not a word count."""
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, 'Rotation must use the runtime-reported context.')
    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'real token rotation'},
    )
    session_before = store.get_run(run.run_id).honeydew_session_id
    assert session_before is not None

    # The stored-turn estimate stays tiny; only the real context OpenCode
    # reports can reach the ceiling and fire rotation.
    runtime.session_context_tokens_override = 24_000
    settings.turn_history_rotation_token_threshold = 24_000
    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'real token rotation'},
    )
    session_after = store.get_run(run.run_id).honeydew_session_id
    assert session_after != session_before

    rotations = [
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'agent.session_rotated'
    ]
    assert rotations
    reason = rotations[-1].payload['reason']
    assert 'threshold' in reason
    assert '24000' in reason


def test_rotation_falls_back_to_turn_count_when_usage_absent(
    orchestrator_bundle,
) -> None:
    """Without runtime usage the fallback is deterministic and a floor."""
    settings, store, cluster, runtime, engine = orchestrator_bundle
    runtime.session_context_tokens_override = None
    run = _make_run(engine, 'No runtime usage falls back deterministically.')
    session = store.get_run(run.run_id).honeydew_session_id
    assert session is not None

    first = engine._session_context_tokens(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        session_id=session,
    )
    second = engine._session_context_tokens(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        session_id=session,
    )
    assert first == second
    assert first > 0
    # The fallback sees only orchestrator-stored turn data. It is a floor,
    # not a host-safe bound: it stays strictly below the configured rotation
    # ceiling, so it can never be read as "this session fits the host".
    assert first < settings.turn_history_rotation_token_threshold

    # The floor still accumulates real stored size: a dense JSON payload
    # whose whitespace count is ~1 crosses a token-like threshold.
    blob = _dense_blob(6_000)
    settings.turn_history_rotation_token_threshold = 10_000
    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'blob': blob},
    )
    session_before = store.get_run(run.run_id).honeydew_session_id
    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'blob': blob},
    )
    session_after = store.get_run(run.run_id).honeydew_session_id
    assert session_after != session_before
    assert any(
        event.event_type == 'agent.session_rotated'
        for event in store.list_events(run.run_id)
    )


def test_human_resolution_pause_is_not_auto_resumed(
    orchestrator_bundle,
) -> None:
    """A human-resolution pause must never be resumed automatically."""
    settings, store, cluster, runtime, engine = orchestrator_bundle
    _set_auto_resume_limit(settings, 2)

    # Control: the bounded auto-resume IS active for a wall-clock abort on
    # the approval path -- the run advances instead of parking.
    control = _make_run(engine, 'Control run for the auto-resume guard.')
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=1, failure_class='turn_timeout'
    )
    protocol = _pending_action(store, control.run_id, 'approve_protocol')
    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )
    assert store.get_run(control.run_id).state != RunState.PAUSED

    # Guard: the documented human-resolution pause is not transient.
    guard = _make_run(engine, 'Human resolution must never be auto-resumed.')
    current = store.get_run(guard.run_id)
    store.replace_run(
        current.model_copy(update={'state': RunState.HONEYDEW_REVIEWING}),
        expected_version=current.version,
    )
    settings.maximum_methodology_revisions = 0
    engine._request_methodology_revision(
        guard.run_id,
        feedback='test human resolution',
    )
    paused = store.get_run(guard.run_id)
    assert paused.state == RunState.PAUSED
    assert any(
        event.event_type == 'methodology.human_resolution_requested'
        for event in store.list_events(guard.run_id)
    )
    turn_number_before = paused.turn_number

    # The automatic recovery entrypoint must not touch this pause.
    engine.recover()
    after = store.get_run(guard.run_id)
    assert after.state == RunState.PAUSED
    assert after.turn_number == turn_number_before
    assert not _auto_resumed_events(store, guard.run_id)
    assert not any(
        event.event_type == 'run.resumed'
        for event in store.list_events(guard.run_id)
    )
