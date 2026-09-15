"""Snapshot/segment driving of the research rehearsal harness.

Covers the dev/ops affordance in app.rehearse_research_flow: taking a whole
root snapshot, restoring it, reading snapshot metadata, and advancing a run
one segment (one human-wait gate approval, plus any intervening job states) at
a time. ScriptedMockRuntime is injected via runtime_factory so no model server
or Kubernetes cluster is ever contacted.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import rehearse_research_flow as rehearsal
from app.mock_runtime import ScriptedMockRuntime
from app.schemas import ApprovalStatus, RunCreateRequest, RunState


def _mock_factory(settings):
    return ScriptedMockRuntime(runner_image=rehearsal.RUNNER_IMAGE)


def _build(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    return rehearsal._build_engine(root, runtime_factory=_mock_factory)


def _run_state(root: Path) -> RunState:
    # Reopen the store from disk so the assertion reflects the durable state a
    # fresh process would see after a restore, not a cached engine connection.
    _, state_value = rehearsal._current_run_state(root)
    assert state_value is not None
    return RunState(state_value)


def _pending_action(store, run_id: str, action_type: str):
    return next(
        action
        for action in store.list_actions(run_id)
        if action.type == action_type
        and action.approval_status == ApprovalStatus.PENDING
    )


def test_snapshot_restore_round_trip(tmp_path: Path) -> None:
    root = tmp_path / 'root'
    snapshots = tmp_path / 'snapshots'
    _, store, _, engine = _build(root)
    run = engine.create_run(
        RunCreateRequest(objective='Snapshot and restore the rehearsal root.')
    )
    assert run.state is RunState.AWAITING_PROTOCOL_APPROVAL

    # Given: a snapshot of the root at AWAITING_PROTOCOL_APPROVAL.
    dest = rehearsal.create_snapshot(root, snapshots, 'base')
    assert (dest / 'state' / 'orchestrator.db').is_file()
    meta = rehearsal.read_snapshot_meta(snapshots, 'base')
    assert meta is not None
    assert meta['state'] == 'AWAITING_PROTOCOL_APPROVAL'
    assert meta['run_id'] == run.run_id

    # When: the root is mutated after the snapshot (run advances + marker file).
    protocol = _pending_action(store, run.run_id, 'approve_protocol')
    engine.approve_action(
        protocol.action_id,
        reviewer='test',
        reason='Advance past the snapshot.',
    )
    (root / 'marker.txt').write_text('mutated after snapshot')
    assert _run_state(root) is RunState.AWAITING_EXECUTION_APPROVAL

    rehearsal.restore_snapshot(root, snapshots, 'base')

    # Then: the root is back at the captured state and the marker is gone.
    assert not (root / 'marker.txt').exists()
    assert (tmp_path / 'root.prev').is_dir()
    assert _run_state(root) is RunState.AWAITING_PROTOCOL_APPROVAL


def test_snapshot_list_metadata_and_cli(tmp_path: Path, capsys) -> None:
    root = tmp_path / 'root'
    snapshots = tmp_path / 'snapshots'
    _, _, _, engine = _build(root)
    run = engine.create_run(
        RunCreateRequest(objective='List snapshot metadata over the CLI.')
    )
    assert rehearsal.list_snapshots(snapshots) == []

    # Drain the git-init chatter emitted while building the root.
    capsys.readouterr()

    # When: --snapshot (no NAME) records a snapshot under the current state.
    assert (
        rehearsal.main(
            [
                '--root',
                str(root),
                '--snapshot-root',
                str(snapshots),
                '--snapshot',
            ]
        )
        == 0
    )
    captured = json.loads(capsys.readouterr().out)
    assert captured['snapshot'] == 'AWAITING_PROTOCOL_APPROVAL'
    assert captured['meta']['run_id'] == run.run_id

    # Then: --list projects every snapshot's metadata record.
    assert (
        rehearsal.main(
            [
                '--root',
                str(root),
                '--snapshot-root',
                str(snapshots),
                '--list',
            ]
        )
        == 0
    )
    listed = json.loads(capsys.readouterr().out)
    assert [entry['name'] for entry in listed] == ['AWAITING_PROTOCOL_APPROVAL']
    assert set(listed[0]) >= {
        'name',
        'state',
        'run_id',
        'created_at_utc',
        'git_commit',
    }


def test_one_segment_stops_at_next_human_wait_gate(tmp_path: Path) -> None:
    root = tmp_path / 'root'
    snapshots = tmp_path / 'snapshots'
    _, _, _, engine = _build(root)
    run = engine.create_run(
        RunCreateRequest(objective='Run exactly one rehearsal segment.')
    )
    assert run.state is RunState.AWAITING_PROTOCOL_APPROVAL
    rehearsal.create_snapshot(root, snapshots, 'start')

    # Given: the snapshot restored and exactly one gate approved.
    rehearsal.restore_snapshot(root, snapshots, 'start')
    summary = rehearsal.run_rehearsal(
        root=root,
        snapshot_root=snapshots,
        max_gates=1,
        runtime_factory=_mock_factory,
    )

    # Then: the run stopped at the next human-wait state after one gate.
    assert summary['result'] == 'SEGMENT_DONE'
    assert summary['entry_state'] == 'AWAITING_PROTOCOL_APPROVAL'
    assert summary['reached_state'] == 'AWAITING_EXECUTION_APPROVAL'
    assert summary['gates_consumed'] == 1
    assert summary['approved_gates'] == ['approve_protocol']
    assert _run_state(root) is RunState.AWAITING_EXECUTION_APPROVAL

    # A second segment consumes the execution gate, then advances through the
    # JOB states without counting them, stopping at final acceptance.
    second = rehearsal.run_rehearsal(
        root=root,
        snapshot_root=snapshots,
        max_gates=1,
        runtime_factory=_mock_factory,
    )
    assert second['result'] == 'SEGMENT_DONE'
    assert second['gates_consumed'] == 1
    assert second['reached_state'] == 'AWAITING_FINAL_ACCEPTANCE'
    assert _run_state(root) is RunState.AWAITING_FINAL_ACCEPTANCE


def test_full_run_with_mock_writes_gate_snapshots(tmp_path: Path) -> None:
    root = tmp_path / 'root'
    snapshots = tmp_path / 'snapshots'

    summary = rehearsal.run_rehearsal(
        root=root,
        snapshot_root=snapshots,
        runtime_factory=_mock_factory,
        auto_snapshot=True,
    )

    assert summary['result'] == 'PASS'
    assert summary['final_state'] == 'COMPLETE'
    assert _run_state(root) is RunState.COMPLETE

    names = [entry['name'] for entry in rehearsal.list_snapshots(snapshots)]
    assert 'AWAITING_PROTOCOL_APPROVAL' in names
    assert 'AWAITING_FINAL_ACCEPTANCE' in names
    meta = rehearsal.read_snapshot_meta(
        snapshots,
        'AWAITING_PROTOCOL_APPROVAL',
    )
    assert meta is not None
    assert meta['state'] == 'AWAITING_PROTOCOL_APPROVAL'
    assert meta['git_commit']


def _pause_for_human_resolution(store, engine, run) -> None:
    """Drive the engine's own revision-cap path into a human-resolution pause.

    With the automatic revision budget set to 0, the very first
    _request_methodology_revision emits methodology.human_resolution_requested
    and calls pause_run, exactly as the live non-convergence loop does.
    """
    engine.settings.maximum_methodology_revisions = 0
    store.replace_run(
        run.model_copy(update={'state': RunState.HONEYDEW_REVIEWING}),
        expected_version=run.version,
    )
    engine._request_methodology_revision(
        run.run_id,
        feedback='Deterministic matrix preflight failed repeatedly.',
    )


def test_human_resolution_pause_returns_blocked_without_resuming(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / 'root'
    snapshots = tmp_path / 'snapshots'
    _, store, _, engine = _build(root)
    run = engine.create_run(
        RunCreateRequest(objective='Block on a human-resolution pause.')
    )

    # Given: the run paused itself for human resolution after the engine's
    # automatic revision budget was exhausted.
    _pause_for_human_resolution(store, engine, run)
    paused = store.get_run(run.run_id)
    assert paused.state is RunState.PAUSED
    assert paused.resume_state is RunState.BEAKER_REVISING
    assert any(
        event.event_type == 'methodology.human_resolution_requested'
        for event in store.list_events(run.run_id)
    )

    resumed: list[str] = []
    original_resume = rehearsal.ResearchOrchestrator.resume_run

    def _spy_resume(self, run_id, **kwargs):
        resumed.append(run_id)
        return original_resume(self, run_id, **kwargs)

    monkeypatch.setattr(
        rehearsal.ResearchOrchestrator, 'resume_run', _spy_resume
    )

    # When: the driver processes the paused run.
    summary = rehearsal.run_rehearsal(
        root=root,
        snapshot_root=snapshots,
        runtime_factory=_mock_factory,
    )

    # Then: it stops as BLOCKED (never RESUMABLE), the run stays paused, and
    # the driver never calls resume_run.
    assert summary['result'] == 'BLOCKED'
    assert summary['result'] != 'RESUMABLE'
    assert summary['entry_state'] == 'PAUSED'
    assert summary['reached_state'] == 'PAUSED'
    assert summary.get('blocked_reason') or summary.get('reason')
    assert resumed == []
    assert store.get_run(run.run_id).state is RunState.PAUSED
    assert not any(
        event.event_type == 'run.resumed'
        for event in store.list_events(run.run_id)
    )


def test_transient_turn_pause_still_resumes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / 'root'
    snapshots = tmp_path / 'snapshots'
    _, store, _, engine = _build(root)
    run = engine.create_run(
        RunCreateRequest(objective='Resume a transient turn-timeout pause.')
    )

    # Given: a run paused only by a transient turn wall (no human-resolution
    # event was ever emitted).
    engine.pause_run(
        run.run_id,
        requested_by='orchestrator',
        reason='Agent turn exceeded the wall-clock timeout.',
    )
    paused = store.get_run(run.run_id)
    assert paused.state is RunState.PAUSED
    assert paused.resume_state is RunState.AWAITING_PROTOCOL_APPROVAL
    assert not any(
        'human_resolution_requested' in event.event_type
        for event in store.list_events(run.run_id)
    )

    resumed: list[str] = []
    original_resume = rehearsal.ResearchOrchestrator.resume_run

    def _spy_resume(self, run_id, **kwargs):
        resumed.append(run_id)
        return original_resume(self, run_id, **kwargs)

    monkeypatch.setattr(
        rehearsal.ResearchOrchestrator, 'resume_run', _spy_resume
    )

    # When: the driver processes the paused run.
    summary = rehearsal.run_rehearsal(
        root=root,
        snapshot_root=snapshots,
        max_gates=1,
        runtime_factory=_mock_factory,
    )

    # Then: the transient pause is still auto-resumed and the segment reaches
    # the next human-wait gate (the pre-existing RESUMABLE/resume behavior).
    assert resumed == [run.run_id]
    assert summary['result'] == 'SEGMENT_DONE'
    assert summary['entry_state'] == 'PAUSED'
    assert summary['reached_state'] == 'AWAITING_EXECUTION_APPROVAL'
    assert store.get_run(run.run_id).state is RunState.AWAITING_EXECUTION_APPROVAL


def test_human_resolution_signal_clears_after_a_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / 'root'
    _, store, _, engine = _build(root)
    run = engine.create_run(
        RunCreateRequest(objective='Clear the human-resolution signal.')
    )
    _pause_for_human_resolution(store, engine, run)

    # Given: the current pause carries the human-resolution signal.
    event = rehearsal._human_resolution_pause(store, run.run_id)
    assert event is not None
    assert 'human_resolution_requested' in event.event_type

    # When: a human resumes the run (the durable run.resumed event) and it
    # later pauses for a transient turn wall.
    monkeypatch.setattr(engine, '_recover_run', lambda run_id: None)
    engine.resume_run(run.run_id, requested_by='human', reason='Resolved.')
    engine.pause_run(
        run.run_id,
        requested_by='orchestrator',
        reason='Agent turn exceeded the wall-clock timeout.',
    )

    # Then: the earlier human-resolution event no longer marks this pause.
    assert rehearsal._human_resolution_pause(store, run.run_id) is None
