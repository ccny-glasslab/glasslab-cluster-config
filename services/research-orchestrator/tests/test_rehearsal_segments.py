"""Snapshot/segment driving of the research rehearsal harness.

Covers the dev/ops affordance in app.rehearse_research_flow: taking a whole
root snapshot, restoring it, reading snapshot metadata, and advancing a run
one segment (one human-wait gate approval, plus any intervening job states) at
a time. ScriptedMockRuntime is injected via runtime_factory so no model server
or Kubernetes cluster is ever contacted.

It also locks in that snapshots survive the read-only review surface the
engine materializes (files 0444, directories 0555): a snapshot must be
re-creatable and removable without a chmod walk (#473).
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from app import rehearse_research_flow as rehearsal
from app.mock_runtime import ScriptedMockRuntime
from app.schemas import ApprovalStatus, JobStatus, RunCreateRequest, RunState
from app.storage import SqliteStore


def _mock_factory(settings):
    return ScriptedMockRuntime(runner_image=rehearsal.RUNNER_IMAGE)


class _CapacityLimitedMockRuntime(ScriptedMockRuntime):
    """Scripted runtime whose matrix exceeds the run's parallel capacity.

    The stock scripted matrix is two jobs wide and the rehearsal runs with
    ``maximum_parallel_jobs=2``, so a single capacity pass submits every job.
    Real-model matrices routinely exceed the cap, leaving queued jobs with no
    ``external_run_id``; this runtime reproduces that shape deterministically
    so the driver's fill -> complete -> reconcile loop is exercised.
    """

    def run_turn(self, **kwargs):
        result, message_id = super().run_turn(**kwargs)
        matrix = next(
            (
                action
                for action in result.requested_actions
                if action.type == 'submit_experiment_matrix'
            ),
            None,
        )
        if matrix is None:
            return result, message_id
        arguments = dict(matrix.arguments)
        arguments['variants'] = [
            {'name': 'baseline', 'overrides': {'learning_rate': 0.0001}},
            {'name': 'candidate-a', 'overrides': {'learning_rate': 0.0002}},
            {'name': 'candidate-b', 'overrides': {'learning_rate': 0.0003}},
            {'name': 'candidate-c', 'overrides': {'learning_rate': 0.0004}},
        ]
        arguments['maximum_parallel_jobs'] = 2
        updated = [
            matrix.model_copy(update={'arguments': arguments})
            if action is matrix
            else action
            for action in result.requested_actions
        ]
        return result.model_copy(update={'requested_actions': updated}), message_id


def _capacity_limited_factory(settings):
    return _CapacityLimitedMockRuntime(runner_image=rehearsal.RUNNER_IMAGE)


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


def _materialize_readonly_review_copy(engine, run_id: str, staging: Path) -> Path:
    """Mirror the read-only contract-candidate review surface in the root.

    The engine copies a sealed candidate into the run's Honeydew workspace and
    makes the copy read-only (files 0444, directories 0555) before review; the
    mock runtime never reaches that step, so tests materialize it directly.
    """
    sealed = staging / 'sealed-candidate'
    (sealed / 'tests').mkdir(parents=True)
    (sealed / 'evaluator.py').write_text('# sealed evaluator\n')
    (sealed / 'tests' / 'test_vectors.py').write_text('# sealed vectors\n')
    return engine.workspaces.copy_contract_candidate_for_review(
        run_id=run_id,
        source=sealed,
        contract_id='wine-classification',
        version='1.0.0',
        digest='0' * 64,
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


def test_snapshot_taken_twice_over_readonly_review_copy(tmp_path: Path) -> None:
    root = tmp_path / 'root'
    snapshots = tmp_path / 'snapshots'
    _, _, _, engine = _build(root)
    run = engine.create_run(
        RunCreateRequest(objective='Snapshot a read-only review surface twice.')
    )
    review = _materialize_readonly_review_copy(engine, run.run_id, tmp_path)
    assert (review / 'tests').stat().st_mode & 0o777 == 0o555
    assert (review / 'evaluator.py').stat().st_mode & 0o777 == 0o444

    # Given: a snapshot of the state, then the same state snapshotted again.
    # Re-creating the snapshot used to abort with PermissionError while
    # deleting the read-only copy the first snapshot had preserved (#473).
    first = rehearsal.create_snapshot(
        root, snapshots, 'AWAITING_PROTOCOL_APPROVAL'
    )
    second = rehearsal.create_snapshot(
        root, snapshots, 'AWAITING_PROTOCOL_APPROVAL'
    )

    # Then: the destination was re-created and the content stayed faithful.
    assert second == first
    copied = second / 'state' / review.relative_to(root) / 'evaluator.py'
    assert copied.read_text() == '# sealed evaluator\n'
    meta = rehearsal.read_snapshot_meta(
        snapshots, 'AWAITING_PROTOCOL_APPROVAL'
    )
    assert meta is not None
    assert meta['run_id'] == run.run_id


def test_harness_snapshot_is_removable_without_chmod_walk(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'root'
    snapshots = tmp_path / 'snapshots'
    _, _, _, engine = _build(root)
    run = engine.create_run(
        RunCreateRequest(objective='Keep snapshots removable from the shell.')
    )
    review = _materialize_readonly_review_copy(engine, run.run_id, tmp_path)

    # Given: a snapshot of a root that contains the read-only review copy.
    dest = rehearsal.create_snapshot(root, snapshots, 'gate')

    # Then: the copy kept the review bytes but not the read-only modes, so a
    # plain tree removal (an operator's rm -rf) succeeds without a chmod walk.
    copied = dest / 'state' / review.relative_to(root)
    assert (copied / 'tests' / 'test_vectors.py').read_text() == '# sealed vectors\n'
    shutil.rmtree(dest)
    assert not dest.exists()


def test_snapshot_recreated_over_legacy_readonly_snapshot(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'root'
    snapshots = tmp_path / 'snapshots'
    _, _, _, engine = _build(root)
    engine.create_run(
        RunCreateRequest(objective='Re-create over a frozen snapshot.')
    )
    dest = rehearsal.create_snapshot(root, snapshots, 'gate')

    # Given: a snapshot frozen read-only, the on-disk shape every snapshot
    # taken by the older harness had.
    for path in dest.rglob('*'):
        if not path.is_symlink():
            path.chmod(0o555 if path.is_dir() else 0o444)
    dest.chmod(0o555)

    # When: the harness frees the destination itself and snapshots again.
    second = rehearsal.create_snapshot(root, snapshots, 'gate')

    # Then: the read-only destination was removed by the harness, not by a
    # PermissionError.
    assert second == dest
    assert (dest / 'meta.json').is_file()


def test_capacity_limited_matrix_advances_without_asserting(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'root'
    snapshots = tmp_path / 'snapshots'

    # Given: a matrix with four jobs but maximum_parallel_jobs=2, so the engine
    # can submit only two per capacity pass and leaves the rest QUEUED with no
    # external_run_id.
    summary = rehearsal.run_rehearsal(
        root=root,
        snapshot_root=snapshots,
        runtime_factory=_capacity_limited_factory,
    )

    # Then: the driver drove fill -> complete -> reconcile to COMPLETE instead
    # of asserting on a queued job that had no external_run_id yet.
    assert summary['result'] == 'PASS'
    assert summary['final_state'] == 'COMPLETE'
    assert _run_state(root) is RunState.COMPLETE

    # And every job actually finished, not just the first capacity batch.
    run_id, _ = rehearsal._current_run_state(root)
    assert run_id is not None
    jobs = SqliteStore(str(root / 'orchestrator.db')).list_jobs(run_id)
    assert len(jobs) == 4
    assert all(
        job.status
        in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
        for job in jobs
    )


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
