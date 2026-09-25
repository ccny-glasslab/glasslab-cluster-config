"""Threshold-triggered turn-history rotation (issue #431).

A continuing agent session carries its whole turn history; on the shared Coder
endpoint that unbounded history competes with page cache. When the accumulated
context for the live session crosses a configured token threshold, the engine
must rotate the session through the *same* recovery-checkpoint path it already
uses after a failed turn, and the run must continue from the compact checkpoint
instead of restarting. No second state format is introduced.
"""

from __future__ import annotations

import json

from app.config import Settings
from app.schemas import (
    AgentName,
    RunCreateRequest,
    RunState,
    TurnKind,
)
from app.storage import SqliteStore


RECOVERY_PREFIX = (
    'This is a fresh OpenCode session after an interrupted or failed turn.'
)


def _draft_prompt() -> str:
    return (
        'Draft a concrete program.md for this objective: rotation test\n\n'
        'Evaluation contract: contract://generic-task-integrity-v1/1.0.0'
    )


def _pending_action(store: SqliteStore, run_id: str, action_type: str):
    return next(
        action
        for action in store.list_actions(run_id)
        if action.type == action_type
        and action.approval_status.value == 'pending'
    )


def _drive_full_workflow(engine, store, cluster, objective: str):
    """Run the deterministic mock workflow from creation to COMPLETE."""
    run = engine.create_run(RunCreateRequest(objective=objective))
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )
    matrix = _pending_action(store, run.run_id, 'submit_experiment_matrix')
    engine.approve_action(
        matrix.action_id,
        reviewer='test-human',
        reason='Execution accepted.',
    )
    for job in store.list_jobs(run.run_id):
        cluster.complete(job.external_run_id, metrics={'score': 0.75})
    # The first reconcile records the terminal artifacts and defers analysis
    # one poll; the second advances to the report turn.
    engine.reconcile_run(run.run_id)
    engine.reconcile_run(run.run_id)
    final = _pending_action(store, run.run_id, 'accept_final_report')
    engine.approve_action(
        final.action_id,
        reviewer='test-human',
        reason='Report accepted.',
    )
    return store.get_run(run.run_id)


def test_rotation_threshold_default_is_configured() -> None:
    # The knob is enabled with a bounded default so long sessions rotate
    # without operator configuration; 0 is the documented off switch. The
    # default real-token ceiling sits well below the 60,333-token request that
    # deadlocked the .17 host (run 295bc0ce).
    settings = Settings()
    assert settings.turn_history_rotation_token_threshold == 24_000
    assert settings.effective_turn_history_rotation_token_threshold == 24_000


def test_context_threshold_rotates_session_and_continues(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Rotate the accumulated turn history.')
    )
    _, first = engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'rotation test'},
    )
    assert first.kind == TurnKind.PROTOCOL_DRAFT
    session_before = store.get_run(run.run_id).honeydew_session_id
    assert session_before is not None

    # The ceiling sits below the history the live session has already
    # accumulated, so the next turn must rotate through the recovery path.
    settings.turn_history_rotation_token_threshold = 1

    _, second = engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'rotation test'},
    )
    # The run continued: the turn produced the expected kind on a new session.
    assert second.kind == TurnKind.PROTOCOL_DRAFT
    session_after = store.get_run(run.run_id).honeydew_session_id
    assert session_after is not None
    assert session_after != session_before

    rotations = [
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'agent.session_rotated'
    ]
    assert rotations
    assert 'threshold' in rotations[-1].payload['reason']

    # The rotation reused the recovery checkpoint format, not a parallel one.
    checkpoint_path = (
        engine.workspaces.paths(run.run_id).events
        / 'honeydew-recovery-checkpoint.json'
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
    assert checkpoint['schema_version'] == 'glasslab-recovery-checkpoint-v1'
    assert checkpoint['agent'] == 'honeydew'
    # The checkpoint carries the prior session's context forward.
    assert checkpoint['recent_completed_turns']

    # The fresh session received the compact checkpoint as context.
    assert any(
        prompt.startswith(RECOVERY_PREFIX)
        for _, prompt in runtime.prompts
    )


def test_long_session_rotates_and_continues_to_completion(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    # Force the threshold to cross on every turn that has live history so a
    # real multi-turn run rotates repeatedly and still reaches the end.
    settings.turn_history_rotation_token_threshold = 1
    run = _drive_full_workflow(
        engine,
        store,
        cluster,
        objective='Complete a long synthetic session with rotation.',
    )
    assert run.state == RunState.COMPLETE
    turns = store.list_turns(run.run_id)
    assert len(turns) == 7
    assert all(turn.status == 'completed' for turn in turns)

    rotations = [
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'agent.session_rotated'
    ]
    # Both agents rotated at least once, and every rotation carried the
    # recovery-checkpoint reason.
    assert {event.payload['agent'] for event in rotations} == {
        'honeydew',
        'beaker',
    }
    assert all('threshold' in event.payload['reason'] for event in rotations)

    for agent in ('honeydew', 'beaker'):
        checkpoint_path = (
            engine.workspaces.paths(run.run_id).events
            / f'{agent}-recovery-checkpoint.json'
        )
        assert checkpoint_path.is_file()


def test_zero_threshold_disables_rotation(orchestrator_bundle) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    settings.turn_history_rotation_token_threshold = 0
    run = engine.create_run(
        RunCreateRequest(objective='Disable rotation for this run.')
    )
    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'rotation disabled'},
    )
    session_before = store.get_run(run.run_id).honeydew_session_id
    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'rotation disabled'},
    )
    session_after = store.get_run(run.run_id).honeydew_session_id
    assert session_after == session_before
    assert not any(
        event.event_type == 'agent.session_rotated'
        for event in store.list_events(run.run_id)
    )


def test_history_below_threshold_does_not_rotate(orchestrator_bundle) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    # A ceiling far above any short mock turn's accumulated history leaves the
    # session intact across turns.
    settings.turn_history_rotation_token_threshold = 10_000_000
    run = engine.create_run(
        RunCreateRequest(objective='Keep the session below the ceiling.')
    )
    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'below threshold'},
    )
    session_before = store.get_run(run.run_id).honeydew_session_id
    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'below threshold'},
    )
    session_after = store.get_run(run.run_id).honeydew_session_id
    assert session_after == session_before
    assert not any(
        event.event_type == 'agent.session_rotated'
        for event in store.list_events(run.run_id)
    )
