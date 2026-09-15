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
