"""Real-token context rotation (run 295bc0ce regression).

Run 295bc0ce deadlocked the ``.17`` MLX Coder host on a 60,333-token prompt
while the old estimator summed the run's stored turns to 2,014 tokens -- 1.6%
of the then-128,000 threshold -- because it counted whitespace words and never
saw OpenCode's own session contents. Rotation is now driven by the real
per-message token usage OpenCode reports, with a conservative character-based
floor when a runtime cannot report usage.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.config import SAFE_SESSION_CONTEXT_TOKEN_CEILING, Settings
from app.engine import WorkflowError
from app.opencode_runtime import (
    OpenCodeProcessRuntime,
    message_context_tokens,
)
from app.prompt_tokens import estimate_prompt_tokens
from app.schemas import (
    AgentName,
    RunCreateRequest,
    RunState,
    TurnKind,
    TurnRecord,
)


def _draft_prompt() -> str:
    return (
        'Draft a concrete program.md for this objective: real token test\n\n'
        'Evaluation contract: contract://generic-task-integrity-v1/1.0.0'
    )


def _dense_context(entries: int) -> dict[str, str]:
    # Compact JSON has almost no whitespace, so a whitespace word count
    # collapses it to a single "token".
    return {
        'blob': json.dumps(
            {
                f'k{index}': [index, index + 1, {'nested': index * 2}]
                for index in range(entries)
            },
            separators=(',', ':'),
        )
    }


def test_estimate_prompt_tokens_counts_dense_json_not_whitespace_words() -> None:
    payload = _dense_context(6_000)['blob']
    assert len(payload.split()) == 1
    assert estimate_prompt_tokens(payload) > 1_000
    assert estimate_prompt_tokens(payload) >= len(payload) // 4
    assert estimate_prompt_tokens('') == 1


def test_message_context_tokens_counts_prompt_cache_and_generation() -> None:
    body = {
        'info': {
            'tokens': {
                'input': 100,
                'output': 40,
                'reasoning': 10,
                'cache': {'read': 5, 'write': 2},
            }
        }
    }
    assert message_context_tokens(body) == 157


def test_message_context_tokens_ignores_bodies_without_usage() -> None:
    assert message_context_tokens({}) is None
    assert message_context_tokens({'info': {}}) is None
    assert message_context_tokens({'info': {'tokens': {}}}) is None
    assert (
        message_context_tokens(
            {
                'info': {
                    'tokens': {
                        'input': 0,
                        'output': 0,
                        'reasoning': 0,
                        'cache': {'read': 0, 'write': 0},
                    }
                }
            }
        )
        is None
    )


def test_session_context_tokens_reports_the_observed_real_usage() -> None:
    runtime = OpenCodeProcessRuntime(Settings())
    runtime._observed_session_tokens[('run-1', AgentName.HONEYDEW)] = (
        's-1',
        157,
    )
    assert (
        runtime.session_context_tokens(
            run_id='run-1', agent=AgentName.HONEYDEW, session_id='s-1'
        )
        == 157
    )
    assert (
        runtime.session_context_tokens(
            run_id='run-1', agent=AgentName.HONEYDEW, session_id='s-other'
        )
        is None
    )


def test_rotation_floor_defers_immediate_re_rotation(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    settings.turn_history_rotation_token_threshold = 24_000
    run = engine.create_run(
        RunCreateRequest(objective='Do not rotate on every turn.')
    )
    store.replace_run(
        run.model_copy(
            update={
                'honeydew_session_id': 'session-thrash',
                'session_rotation_count': 1,
                'last_rotation_turn': 5,
                'turn_number': 5,
            }
        ),
        expected_version=run.version,
    )
    runtime.session_context_tokens_override = 50_000

    updated = engine._maybe_rotate_turn_history(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        run=store.get_run(run.run_id),
    )

    assert updated.honeydew_session_id == 'session-thrash'
    assert not any(
        event.event_type == 'agent.session_rotated'
        for event in store.list_events(run.run_id)
    )


def test_rotation_bookkeeping_increments_after_the_floor(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    settings.turn_history_rotation_token_threshold = 24_000
    run = engine.create_run(
        RunCreateRequest(objective='Record each threshold rotation.')
    )
    store.replace_run(
        run.model_copy(
            update={
                'honeydew_session_id': 'session-1',
                'session_rotation_count': 1,
                'last_rotation_turn': 1,
                'turn_number': 3,
            }
        ),
        expected_version=run.version,
    )
    runtime.session_context_tokens_override = 50_000

    updated = engine._maybe_rotate_turn_history(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        run=store.get_run(run.run_id),
    )

    assert updated.honeydew_session_id is None
    assert updated.session_rotation_count == 2
    assert updated.last_rotation_turn == 3


def test_rotation_cap_pauses_the_run(orchestrator_bundle) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    settings.turn_history_rotation_token_threshold = 24_000
    settings.maximum_session_rotations = 4
    run = engine.create_run(
        RunCreateRequest(objective='Stop rotating after the cap.')
    )
    store.replace_run(
        run.model_copy(
            update={
                'honeydew_session_id': 'session-thrash',
                'session_rotation_count': 4,
                'last_rotation_turn': 0,
                'turn_number': 5,
            }
        ),
        expected_version=run.version,
    )
    runtime.session_context_tokens_override = 50_000

    with pytest.raises(WorkflowError):
        engine._maybe_rotate_turn_history(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            run=store.get_run(run.run_id),
        )

    assert store.get_run(run.run_id).state == RunState.PAUSED


def test_rotation_cap_does_not_pause_below_threshold(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    settings.turn_history_rotation_token_threshold = 24_000
    settings.maximum_session_rotations = 4
    run = engine.create_run(
        RunCreateRequest(objective='A fresh session still makes progress.')
    )
    store.replace_run(
        run.model_copy(
            update={
                'honeydew_session_id': 'session-fresh',
                'session_rotation_count': 4,
                'last_rotation_turn': 0,
                'turn_number': 5,
            }
        ),
        expected_version=run.version,
    )
    runtime.session_context_tokens_override = 1_000

    updated = engine._maybe_rotate_turn_history(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        run=store.get_run(run.run_id),
    )

    assert updated.state != RunState.PAUSED
    assert updated.honeydew_session_id == 'session-fresh'


def test_real_context_rotates_even_when_prior_turns_failed(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    settings.turn_history_rotation_token_threshold = 24_000
    run = engine.create_run(
        RunCreateRequest(objective='Failed turns still carry real context.')
    )
    session_id = 'session-failed-turns'
    store.replace_run(
        run.model_copy(update={'honeydew_session_id': session_id}),
        expected_version=run.version,
    )
    # The only turn on this session failed, so it is excluded from the
    # stored-data estimate; only the real runtime signal can see its context.
    store.save_turn(
        TurnRecord(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            opencode_session_id=session_id,
            status='failed',
        )
    )
    runtime.session_context_tokens_override = 50_000

    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'failed turns'},
    )

    rotations = [
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'agent.session_rotated'
    ]
    assert rotations
    assert '50000' in rotations[-1].payload['reason']


def test_run_turn_captures_real_token_usage(tmp_path, monkeypatch) -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'info': {
                    'id': 'message-1',
                    'tokens': {
                        'input': 100,
                        'output': 40,
                        'reasoning': 10,
                        'cache': {'read': 5, 'write': 2},
                    },
                },
                'parts': [
                    {
                        'type': 'text',
                        'text': json.dumps(
                            {'kind': 'verification', 'summary': 'ok'}
                        ),
                    }
                ],
            },
        )

    runtime = OpenCodeProcessRuntime(Settings())
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    handle = SimpleNamespace(
        run_id='run-1',
        base_url='http://opencode.test',
        password='password',
    )
    monkeypatch.setattr(runtime, '_start_process', lambda **_: handle)
    monkeypatch.setattr(
        runtime,
        '_client',
        lambda _: httpx.Client(
            base_url=handle.base_url,
            transport=httpx.MockTransport(respond),
        ),
    )

    runtime.run_turn(
        run_id='run-1',
        agent=AgentName.BEAKER,
        workspace=workspace,
        session_id='session-1',
        prompt='Verify the evidence.',
    )

    assert (
        runtime.session_context_tokens(
            run_id='run-1', agent=AgentName.BEAKER, session_id='session-1'
        )
        == 157
    )


def test_default_real_token_ceiling_is_below_the_fatal_prompt() -> None:
    settings = Settings()
    assert settings.effective_turn_history_rotation_token_threshold == 24_000
    assert 24_000 < 60_333


def test_rotation_threshold_is_clamped_to_the_host_safety_ceiling() -> None:
    settings = Settings(turn_history_rotation_token_threshold=128_000)
    assert (
        settings.effective_turn_history_rotation_token_threshold
        == SAFE_SESSION_CONTEXT_TOKEN_CEILING
    )
    assert SAFE_SESSION_CONTEXT_TOKEN_CEILING < 60_333
    settings.turn_history_rotation_token_threshold = 0
    assert settings.effective_turn_history_rotation_token_threshold == 0


def test_rotation_uses_the_real_reported_token_count(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    settings.turn_history_rotation_token_threshold = 24_000
    run = engine.create_run(
        RunCreateRequest(objective='Bound the real reported prompt.')
    )
    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'real token rotation'},
    )
    session_before = store.get_run(run.run_id).honeydew_session_id
    assert session_before is not None

    # The stored-data estimate stays tiny; only the real OpenCode-reported
    # context can reach the ceiling and fire rotation.
    runtime.session_context_tokens_override = 24_000
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
    assert '24000' in reason
    assert 'threshold' in reason


def test_dense_json_context_rotates_on_the_token_estimate(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    # The runtime reports no usage, so the fallback must still count dense JSON
    # as many tokens -- a whitespace estimate would see a single word.
    runtime.session_context_tokens_override = None
    blob = _dense_context(6_000)['blob']
    settings.turn_history_rotation_token_threshold = 10_000
    run = engine.create_run(
        RunCreateRequest(objective='Rotate on the token estimate.')
    )
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


def test_accumulated_session_context_triggers_rotation(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    runtime.session_context_tokens_override = None
    per_turn = _dense_context(1_000)
    per_turn_tokens = estimate_prompt_tokens(
        json.dumps(per_turn, sort_keys=True, default=str)
    )
    # Two turns' worth crosses the ceiling, so rotation fires before turn three.
    settings.turn_history_rotation_token_threshold = per_turn_tokens * 2
    run = engine.create_run(
        RunCreateRequest(objective='Rotate a long accumulating session.')
    )
    for _ in range(2):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={'blob': per_turn['blob']},
        )
    session_before = store.get_run(run.run_id).honeydew_session_id

    engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'blob': per_turn['blob']},
    )
    session_after = store.get_run(run.run_id).honeydew_session_id
    assert session_after != session_before
    assert any(
        event.event_type == 'agent.session_rotated'
        for event in store.list_events(run.run_id)
    )
