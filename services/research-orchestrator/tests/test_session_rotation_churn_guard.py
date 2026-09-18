"""The session-rotation cap bounds churn, not a healthy run's lifetime.

PR #518 added ``maximum_session_rotations`` as a per-run cap on threshold
session rotations. As a *lifetime* cap it is wrong: on a heavy research session
the real context crosses the 24,000-token ceiling about every two turns, so a
legitimate 20-turn run would pause around turn 8-10 -- and because the counter
was never reset, every operator resume bought about one turn before the next
crossing paused it again.

The cap must bound *consecutive, unproductive* rotation churn. The counter
resets when a session is observed below the threshold (the previous rotation
demonstrably bought headroom, so the run is making progress) and when an
operator resumes a paused run (an explicit fresh churn budget). A run whose
fresh sessions immediately re-reach the ceiling -- rotation never helps --
still escalates to a clean, resumable ``PAUSED`` after the cap.
"""

from __future__ import annotations

import pytest

from app.engine import WorkflowError
from app.schemas import AgentName, RunCreateRequest, RunState, TurnKind


THRESHOLD = 24_000
ABOVE = 50_000
BELOW = 1_000
MAXIMUM = 4


def _seed(store, run_id: str, **updates):
    current = store.get_run(run_id)
    return store.replace_run(
        current.model_copy(update=updates),
        expected_version=current.version,
    )


def _crossing(engine, store, run_id: str):
    return engine._maybe_rotate_turn_history(
        run_id=run_id,
        agent=AgentName.HONEYDEW,
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        run=store.get_run(run_id),
    )


def _rotations(store, run_id: str):
    return [
        event
        for event in store.list_events(run_id)
        if event.event_type == 'agent.session_rotated'
    ]


def test_progress_between_rotations_does_not_exhaust_the_cap(
    orchestrator_bundle,
) -> None:
    """A productive run may rotate far more than ``maximum_session_rotations``."""
    settings, store, cluster, runtime, engine = orchestrator_bundle
    settings.turn_history_rotation_token_threshold = THRESHOLD
    settings.maximum_session_rotations = MAXIMUM
    run = engine.create_run(
        RunCreateRequest(objective='A long but productive rotation run.')
    )

    turn = 2
    for cycle in range(MAXIMUM + 2):
        # Progress: the fresh session is observed with real headroom below the
        # ceiling, so the previous rotation demonstrably worked.
        _seed(
            store,
            run.run_id,
            honeydew_session_id=f'productive-{cycle}',
            turn_number=turn,
        )
        runtime.session_context_tokens_override = BELOW
        progressed = _crossing(engine, store, run.run_id)
        assert progressed.session_rotation_count == 0

        # The same session later crosses the ceiling: one rotation, no pause.
        runtime.session_context_tokens_override = ABOVE
        _seed(store, run.run_id, turn_number=turn + 2)
        rotated = _crossing(engine, store, run.run_id)
        assert rotated.session_rotation_count == 1
        assert rotated.state != RunState.PAUSED
        turn += 2

    # Six lifetime rotations, double the cap: the run never paused.
    assert len(_rotations(store, run.run_id)) == MAXIMUM + 2
    assert store.get_run(run.run_id).state != RunState.PAUSED


def test_unproductive_churn_still_escalates_to_paused(
    orchestrator_bundle,
) -> None:
    """Without progress the cap still parks the run in a clean, resumable pause."""
    settings, store, cluster, runtime, engine = orchestrator_bundle
    settings.turn_history_rotation_token_threshold = THRESHOLD
    settings.maximum_session_rotations = MAXIMUM
    run = engine.create_run(
        RunCreateRequest(objective='Fresh sessions keep re-reaching the ceiling.')
    )

    # Earlier lifetime rotations must not, by themselves, pause the run: one
    # below-threshold observation is progress and resets the churn counter.
    _seed(
        store,
        run.run_id,
        honeydew_session_id='progress',
        turn_number=10,
        last_rotation_turn=8,
        session_rotation_count=MAXIMUM,
    )
    runtime.session_context_tokens_override = BELOW
    assert _crossing(engine, store, run.run_id).session_rotation_count == 0

    # Consecutive unproductive rotations (each fresh session immediately over
    # the ceiling again) do not pause until the cap is actually reached.
    runtime.session_context_tokens_override = ABOVE
    turn = 12
    for index in range(MAXIMUM):
        _seed(
            store,
            run.run_id,
            honeydew_session_id=f'churn-{index}',
            turn_number=turn,
        )
        churning = _crossing(engine, store, run.run_id)
        assert churning.session_rotation_count == index + 1
        assert churning.state != RunState.PAUSED
        turn += 2

    # The next crossing exhausts the cap: a clean, resumable PAUSED.
    _seed(
        store, run.run_id, honeydew_session_id='churn-final', turn_number=turn
    )
    with pytest.raises(WorkflowError):
        _crossing(engine, store, run.run_id)
    paused = store.get_run(run.run_id)
    assert paused.state == RunState.PAUSED
    assert paused.resume_state is not None
    assert paused.session_rotation_count == MAXIMUM


def test_resume_after_churn_pause_resets_and_makes_progress(
    orchestrator_bundle,
) -> None:
    """An operator resume grants a fresh churn budget and the run advances."""
    settings, store, cluster, runtime, engine = orchestrator_bundle
    settings.turn_history_rotation_token_threshold = THRESHOLD
    settings.maximum_session_rotations = MAXIMUM
    run = engine.create_run(
        RunCreateRequest(objective='Nurse a churn-paused run past the cap.')
    )

    runtime.session_context_tokens_override = ABOVE
    turn = 10
    for index in range(MAXIMUM):
        _seed(
            store,
            run.run_id,
            honeydew_session_id=f'churn-{index}',
            turn_number=turn,
            last_rotation_turn=turn - 2,
        )
        _crossing(engine, store, run.run_id)
        turn += 2
    _seed(store, run.run_id, honeydew_session_id='churn-cap', turn_number=turn)
    with pytest.raises(WorkflowError):
        _crossing(engine, store, run.run_id)
    paused = store.get_run(run.run_id)
    assert paused.state == RunState.PAUSED
    assert paused.session_rotation_count == MAXIMUM

    # The explicit operator action clears the churn budget.
    engine.resume_run(
        run.run_id, requested_by='test-human', reason='operator review'
    )
    resumed = store.get_run(run.run_id)
    assert resumed.state != RunState.PAUSED
    assert resumed.session_rotation_count == 0

    # The next crossing rotates once and the run keeps going: no immediate
    # re-pause on the first post-resume threshold crossing.
    runtime.session_context_tokens_override = ABOVE
    _seed(
        store,
        run.run_id,
        honeydew_session_id='after-resume',
        turn_number=resumed.turn_number + 2,
        last_rotation_turn=0,
    )
    after = _crossing(engine, store, run.run_id)
    assert after.state != RunState.PAUSED
    assert after.session_rotation_count == 1
