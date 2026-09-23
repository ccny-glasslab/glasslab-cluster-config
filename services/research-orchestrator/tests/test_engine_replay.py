"""Engine-level deterministic replay over recorded agent turns.

Increment 1 of the L2 engine replay: a ``ReplayAgentRuntime`` feeds
pre-recorded ``AgentTurnResult`` objects to the real ``ResearchOrchestrator``
state machine, so a full multi-gate workflow runs in-process in well under a
second instead of spending hours on live model turns. No OpenCode subprocess
and no network are touched.

The fixture is derived from the frozen run evidence corpus
(``evidence-freeze/20260923``): the ``beaker:contract_candidate`` turn is the
verbatim recorded ``agent_turn_result.json``; the standard-flow turns are
hand-authored to the same ``AgentTurnResult`` schema (documented in the
fixture's ``provenance`` block).
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

import pytest

from app.replay_fixture import RecordingRuntime
from app.replay_runtime import (
    ReplayAgentRuntime,
    ReplayRecorder,
    ReplayRuntimeError,
)
from app.schemas import (
    AgentName,
    AgentTurnResult,
    ApprovalStatus,
    RunCreateRequest,
    RunState,
    TurnKind,
)


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / 'fixtures'
    / 'replay'
    / 'engine_flow.json'
)


def _pending_action(store, run_id: str, action_type: str):
    return next(
        action
        for action in store.list_actions(run_id)
        if action.type == action_type
        and action.approval_status == ApprovalStatus.PENDING
    )


def _advance_to_jobs(engine, store):
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
    matrix = _pending_action(store, run.run_id, 'submit_experiment_matrix')
    assert matrix.honeydew_approved is True
    engine.approve_action(
        matrix.action_id,
        reviewer='test-human',
        reason='Fake execution accepted.',
    )
    return store.get_run(run.run_id)


def _complete_jobs(engine, store, cluster, run_id: str):
    for job in store.list_jobs(run_id):
        assert job.external_run_id
        cluster.complete(job.external_run_id, metrics={'score': 0.75})
    return engine.reconcile_run(run_id)


def _forbid_opencode_subprocess(monkeypatch) -> list[str]:
    # subprocess.run (the git workspace manager) routes through Popen, so
    # guarding Popen intercepts both direct launches and the wrappers while
    # letting non-OpenCode commands reach the real Popen.
    real_popen = subprocess.Popen
    commands: list[str] = []

    def guarded_popen(args, *pargs, **kwargs):
        parts = args if isinstance(args, (list, tuple)) else [args]
        rendered = ' '.join(str(part) for part in parts)
        commands.append(rendered)
        if 'opencode' in rendered.lower():
            raise AssertionError(
                f'opencode subprocess invoked during replay: {rendered[:200]}'
            )
        return real_popen(args, *pargs, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', guarded_popen)
    return commands


def _freeze_store_durability(monkeypatch, store) -> None:
    # This is a deterministic logic test: crash durability is irrelevant, and
    # per-commit fsync latency is an unrelated disk variable that can push the
    # measurement past 1s. Disabling it keeps the bound about replay speed.
    real_connect = store._connect

    def connect_without_fsync():
        connection = real_connect()
        connection.execute('PRAGMA synchronous=OFF')
        return connection

    monkeypatch.setattr(store, '_connect', connect_without_fsync)


def _contract_prompt(turn_kind: TurnKind) -> str:
    # Mirrors the engine's authoritative structured-output contract suffix.
    return (
        'Recorded replay prompt.\n\n'
        'AUTHORITATIVE STRUCTURED OUTPUT CONTRACT:\n'
        f'- Set the JSON `kind` field to exactly `{turn_kind.value}`.\n'
        '- No other `kind` is acceptable for this turn.\n'
    )


def test_replay_runtime_drives_multi_gate_flow_under_one_second(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    # Bounded two-gate checkpoint: the live equivalent costs hours of model
    # turns, while the full lifecycle already exceeds 1s in engine/git overhead.
    _, store, _, _, engine = orchestrator_bundle
    runtime = ReplayAgentRuntime(fixture_path=FIXTURE_PATH)
    engine.runtime = runtime
    _freeze_store_durability(monkeypatch, store)
    commands = _forbid_opencode_subprocess(monkeypatch)

    started = time.perf_counter()
    run = engine.create_run(
        RunCreateRequest(
            objective='Test the bounded orchestrator workflow with fake evidence.'
        )
    )
    assert run.state == RunState.AWAITING_PROTOCOL_APPROVAL
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    engine.approve_action(
        protocol.action_id,
        reviewer='test-human',
        reason='Protocol accepted.',
    )
    run = store.get_run(run.run_id)
    elapsed = time.perf_counter() - started

    assert run.state == RunState.AWAITING_EXECUTION_APPROVAL
    assert elapsed < 1.0, f'engine replay gate-check took {elapsed:.3f}s'
    # The guard must have been exercised (git workspace setup runs), and none
    # of the observed commands may be an OpenCode model subprocess.
    assert commands, 'subprocess guard saw no commands; it was not active'
    assert not [c for c in commands if 'opencode' in c.lower()]
    assert runtime.turn_counts[AgentName.HONEYDEW] >= 2
    assert runtime.turn_counts[AgentName.BEAKER] >= 2
    assert all(turn.status == 'completed' for turn in store.list_turns(run.run_id))


def test_replay_runtime_drives_full_lifecycle_to_complete(
    orchestrator_bundle,
    monkeypatch,
) -> None:
    # Full-lifecycle coverage for every recorded turn kind.
    _, store, cluster, _, engine = orchestrator_bundle
    runtime = ReplayAgentRuntime(fixture_path=FIXTURE_PATH)
    engine.runtime = runtime
    commands = _forbid_opencode_subprocess(monkeypatch)

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
    assert not [c for c in commands if 'opencode' in c.lower()]
    assert runtime.turn_counts[AgentName.HONEYDEW] >= 4
    assert runtime.turn_counts[AgentName.BEAKER] >= 3
    assert all(turn.status == 'completed' for turn in store.list_turns(run.run_id))


def test_replay_runtime_replays_verbatim_corpus_turn(tmp_path) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text())
    recorded = fixture['turns']['beaker:contract_candidate'][0]['result']

    runtime = ReplayAgentRuntime(fixture_path=FIXTURE_PATH)
    session = runtime.ensure_session(
        run_id='corpus-run-0001',
        agent=AgentName.BEAKER,
        workspace=tmp_path,
        existing_session_id=None,
    )
    result, message_id = runtime.run_turn(
        run_id='corpus-run-0001',
        agent=AgentName.BEAKER,
        workspace=tmp_path,
        session_id=session.session_id,
        prompt=_contract_prompt(TurnKind.CONTRACT_CANDIDATE),
    )

    assert result.kind == TurnKind.CONTRACT_CANDIDATE
    assert result.model_dump(mode='json', exclude_defaults=True) == recorded
    assert message_id


def test_replay_runtime_is_deterministic_across_fresh_instances(tmp_path) -> None:
    def play():
        runtime = ReplayAgentRuntime(fixture_path=FIXTURE_PATH)
        session = runtime.ensure_session(
            run_id='corpus-run-0001',
            agent=AgentName.BEAKER,
            workspace=tmp_path,
            existing_session_id=None,
        )
        result, message_id = runtime.run_turn(
            run_id='corpus-run-0001',
            agent=AgentName.BEAKER,
            workspace=tmp_path,
            session_id=session.session_id,
            prompt=_contract_prompt(TurnKind.CONTRACT_CANDIDATE),
        )
        return session, result, message_id

    first_session, first_result, first_message = play()
    second_session, second_result, second_message = play()

    assert first_session == second_session
    assert first_message == second_message
    assert first_result.model_dump(mode='json') == second_result.model_dump(
        mode='json'
    )


def test_replay_runtime_rejects_unrecorded_turn(tmp_path) -> None:
    runtime = ReplayAgentRuntime(fixture_path=FIXTURE_PATH)
    session = runtime.ensure_session(
        run_id='corpus-run-0002',
        agent=AgentName.HONEYDEW,
        workspace=tmp_path,
        existing_session_id=None,
    )
    with pytest.raises(ReplayRuntimeError):
        runtime.run_turn(
            run_id='corpus-run-0002',
            agent=AgentName.HONEYDEW,
            workspace=tmp_path,
            session_id=session.session_id,
            prompt=_contract_prompt(TurnKind.TASK_SPEC),
        )


def test_recording_runtime_captures_a_real_turn(orchestrator_bundle, tmp_path) -> None:
    _, _, _, mock_runtime, engine = orchestrator_bundle
    recorder = ReplayRecorder()
    engine.runtime = RecordingRuntime(inner=mock_runtime, recorder=recorder)

    engine.create_run(
        RunCreateRequest(objective='Record one real scripted protocol turn.')
    )

    recorded = recorder.to_fixture()['turns']['honeydew:protocol_draft'][0]
    assert recorded['result']['kind'] == 'protocol_draft'
    assert recorded['files']['program.md']

    target = tmp_path / 'recording.json'
    recorder.write(target)
    replay = ReplayAgentRuntime(fixture_path=target)
    session = replay.ensure_session(
        run_id='recording-run',
        agent=AgentName.HONEYDEW,
        workspace=tmp_path,
        existing_session_id=None,
    )
    result, _ = replay.run_turn(
        run_id='recording-run',
        agent=AgentName.HONEYDEW,
        workspace=tmp_path,
        session_id=session.session_id,
        prompt=_contract_prompt(TurnKind.PROTOCOL_DRAFT),
    )
    assert result.kind == TurnKind.PROTOCOL_DRAFT
    assert (tmp_path / 'program.md').is_file()


def test_replay_recorder_round_trips_fixture(tmp_path) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text())
    recorder = ReplayRecorder()
    for key, records in fixture['turns'].items():
        agent_value, kind_value = key.split(':', 1)
        for record in records:
            recorder.capture(
                agent=AgentName(agent_value),
                turn_kind=TurnKind(kind_value),
                result=AgentTurnResult.model_validate(record['result']),
                files=record.get('files'),
            )

    target = tmp_path / 'recorded.json'
    recorder.write(target)
    reloaded = json.loads(target.read_text())

    assert reloaded['schema_version'] == 'glasslab-runtime-replay-v1'
    for key, records in fixture['turns'].items():
        expected = AgentTurnResult.model_validate(records[0]['result'])
        actual = AgentTurnResult.model_validate(reloaded['turns'][key][0]['result'])
        assert actual == expected
        if 'files' in records[0]:
            assert reloaded['turns'][key][0]['files'] == records[0]['files']
    # The reloaded fixture is directly loadable by the replay runtime.
    ReplayAgentRuntime(fixture_path=target)
