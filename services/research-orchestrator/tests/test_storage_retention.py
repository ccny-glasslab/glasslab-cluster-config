"""Storage usage reporting and terminal-run cleanup (issue #99).

Uses lightweight fake stores (not the full orchestrator_bundle fixture)
because these tests need exact control over RunRecord.updated_at to place
runs precisely inside/outside the retention window; the real store always
overwrites updated_at with the current time on every write (see
SqliteStore.replace_run), which makes backdating impossible through the
public API.
"""

from __future__ import annotations

from datetime import timedelta

from app.schemas import ArtifactRecord, RunRecord, RunState, utc_now
from app.storage import RecordNotFound
from app.storage_retention import (
    CLEANABLE_SUBDIRECTORIES,
    plan_cleanup,
    report_storage_usage,
    run_cleanup,
)


class _FakeStore:
    def __init__(self, runs, artifacts=None):
        self._runs = {run.run_id: run for run in runs}
        self._artifacts = artifacts or {}

    def list_runs(self):
        return list(self._runs.values())

    def list_artifacts(self, run_id):
        return self._artifacts.get(run_id, [])

    def get_run(self, run_id):
        try:
            return self._runs[run_id]
        except KeyError:
            raise RecordNotFound(run_id) from None


def _run_record(run_id: str, *, state: RunState, updated_at) -> RunRecord:
    now = utc_now()
    return RunRecord(
        run_id=run_id,
        objective='Exercise storage retention for issue #99.',
        state=state,
        evaluation_contract_id='contract-1',
        evaluation_contract_version='1.0.0',
        evaluation_contract_digest='a' * 64,
        beaker_workspace=f'/tmp/{run_id}/beaker-worktree',
        honeydew_workspace=f'/tmp/{run_id}/honeydew-worktree',
        shared_artifacts_path=f'/tmp/{run_id}/shared-artifacts',
        reports_path=f'/tmp/{run_id}/reports',
        maximum_turns=20,
        maximum_runtime_seconds=86400,
        maximum_parallel_jobs=1,
        created_at=now,
        updated_at=updated_at,
    )


def _artifact(run_id: str, *, path: str) -> ArtifactRecord:
    return ArtifactRecord(
        run_id=run_id,
        type='report',
        uri=f'artifact://{run_id}/reports/report.md',
        sha256='b' * 64,
        metadata={'path': path},
    )


def _write(path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * size_bytes)


def test_report_storage_usage_sums_subdirectories_per_run(tmp_path) -> None:
    run = _run_record('run-1', state=RunState.COMPLETE, updated_at=utc_now())
    _write(tmp_path / 'run-1' / 'runtime' / 'a.bin', 100)
    _write(tmp_path / 'run-1' / 'runtime' / 'nested' / 'b.bin', 50)
    _write(tmp_path / 'run-1' / 'protocol' / 'program.md', 25)

    report = report_storage_usage(
        store=_FakeStore([run]),
        workspace_root=tmp_path,
    )

    assert report.total_bytes == 175
    assert len(report.runs) == 1
    usage = report.runs[0]
    assert usage.run_id == 'run-1'
    assert usage.total_bytes == 175
    assert usage.subdirectory_bytes == {'runtime': 150, 'protocol': 25}


def test_report_storage_usage_skips_runs_without_a_workspace_directory(
    tmp_path,
) -> None:
    run = _run_record('run-missing', state=RunState.COMPLETE, updated_at=utc_now())

    report = report_storage_usage(store=_FakeStore([run]), workspace_root=tmp_path)

    assert report.total_bytes == 0
    assert report.runs == ()


def test_plan_cleanup_only_includes_terminal_runs_past_retention(
    tmp_path,
) -> None:
    now = utc_now()
    active = _run_record(
        'run-active', state=RunState.BEAKER_IMPLEMENTING, updated_at=now - timedelta(days=90)
    )
    recently_terminal = _run_record(
        'run-recent', state=RunState.COMPLETE, updated_at=now - timedelta(days=1)
    )
    expired_terminal = _run_record(
        'run-expired', state=RunState.FAILED, updated_at=now - timedelta(days=30)
    )
    for run_id in ('run-active', 'run-recent', 'run-expired'):
        _write(tmp_path / run_id / 'runtime' / 'cache.bin', 10)

    plans = plan_cleanup(
        store=_FakeStore([active, recently_terminal, expired_terminal]),
        workspace_root=tmp_path,
        retention_days=14,
        now=now,
    )

    assert [plan.run_id for plan in plans] == ['run-expired']


def test_plan_cleanup_never_proposes_durable_subdirectories(tmp_path) -> None:
    now = utc_now()
    run = _run_record('run-1', state=RunState.COMPLETE, updated_at=now - timedelta(days=30))
    for name in (
        'protocol',
        'reports',
        'shared-artifacts',
        'events',
        'beaker-worktree',
        'honeydew-worktree',
        'runtime',
    ):
        _write(tmp_path / 'run-1' / name / 'file.bin', 10)

    plans = plan_cleanup(
        store=_FakeStore([run]),
        workspace_root=tmp_path,
        retention_days=14,
        now=now,
    )

    assert len(plans) == 1
    proposed_names = {item.name for item in plans[0].subdirectories}
    assert proposed_names == set(CLEANABLE_SUBDIRECTORIES)
    assert 'protocol' not in proposed_names
    assert 'reports' not in proposed_names
    assert 'shared-artifacts' not in proposed_names
    assert 'events' not in proposed_names


def test_plan_cleanup_skips_subdirectory_referenced_by_an_artifact(
    tmp_path,
) -> None:
    now = utc_now()
    run = _run_record('run-1', state=RunState.COMPLETE, updated_at=now - timedelta(days=30))
    referenced_file = tmp_path / 'run-1' / 'runtime' / 'beaker' / 'unexpected.txt'
    _write(referenced_file, 10)
    _write(tmp_path / 'run-1' / 'beaker-worktree' / 'scratch.txt', 20)
    artifacts = {'run-1': [_artifact('run-1', path=str(referenced_file))]}

    plans = plan_cleanup(
        store=_FakeStore([run], artifacts),
        workspace_root=tmp_path,
        retention_days=14,
        now=now,
    )

    assert len(plans) == 1
    by_name = {item.name: item for item in plans[0].subdirectories}
    assert by_name['runtime'].eligible is False
    assert 'referenced by an artifact record' in by_name['runtime'].skip_reason
    # An unrelated subdirectory with no referenced artifact is unaffected.
    assert by_name['beaker-worktree'].eligible is True
    assert plans[0].eligible_bytes == by_name['beaker-worktree'].bytes_to_free


def test_run_cleanup_dry_run_deletes_nothing(tmp_path) -> None:
    now = utc_now()
    run = _run_record('run-1', state=RunState.COMPLETE, updated_at=now - timedelta(days=30))
    target = tmp_path / 'run-1' / 'runtime' / 'beaker' / 'cache.bin'
    _write(target, 100)

    report = run_cleanup(
        store=_FakeStore([run]),
        workspace_root=tmp_path,
        retention_days=14,
        dry_run=True,
        now=now,
    )

    assert report.dry_run is True
    assert report.usage_after is None
    assert report.bytes_freed == 100
    assert target.is_file()


def test_run_cleanup_apply_deletes_eligible_subdirectories_only(tmp_path) -> None:
    now = utc_now()
    run = _run_record('run-1', state=RunState.COMPLETE, updated_at=now - timedelta(days=30))
    runtime_file = tmp_path / 'run-1' / 'runtime' / 'beaker' / 'cache.bin'
    worktree_file = tmp_path / 'run-1' / 'beaker-worktree' / 'scratch.py'
    protocol_file = tmp_path / 'run-1' / 'protocol' / 'program.md'
    _write(runtime_file, 100)
    _write(worktree_file, 50)
    _write(protocol_file, 25)

    report = run_cleanup(
        store=_FakeStore([run]),
        workspace_root=tmp_path,
        retention_days=14,
        dry_run=False,
        now=now,
    )

    assert report.dry_run is False
    assert report.bytes_freed == 150
    assert not runtime_file.exists()
    assert not worktree_file.exists()
    assert protocol_file.is_file(), 'durable protocol/ must never be deleted'
    assert report.usage_after is not None
    assert report.usage_after.total_bytes == 25


def test_run_cleanup_apply_never_touches_active_run_storage(tmp_path) -> None:
    now = utc_now()
    active = _run_record(
        'run-active',
        state=RunState.HONEYDEW_VERIFYING,
        updated_at=now - timedelta(days=365),
    )
    runtime_file = tmp_path / 'run-active' / 'runtime' / 'honeydew' / 'cache.bin'
    _write(runtime_file, 100)

    report = run_cleanup(
        store=_FakeStore([active]),
        workspace_root=tmp_path,
        retention_days=14,
        dry_run=False,
        now=now,
    )

    assert report.plans == ()
    assert report.bytes_freed == 0
    assert runtime_file.is_file()


def test_run_cleanup_apply_skips_run_that_left_terminal_state_mid_scan(
    tmp_path,
) -> None:
    # Regression guard for the re-fetch-before-delete race check: plan_cleanup
    # sees the run as terminal, but by the time cleanup gets to deleting it
    # the store reports it resumed (e.g. a rare recovery path). The stored
    # storage must survive.
    now = utc_now()
    terminal_snapshot = _run_record(
        'run-1', state=RunState.FAILED, updated_at=now - timedelta(days=30)
    )
    resumed_snapshot = terminal_snapshot.model_copy(
        update={'state': RunState.BEAKER_REVISING}
    )
    runtime_file = tmp_path / 'run-1' / 'runtime' / 'beaker' / 'cache.bin'
    _write(runtime_file, 100)

    store = _FakeStore([terminal_snapshot])
    # list_runs() (used for planning) still sees the terminal snapshot;
    # get_run() (used for the pre-delete guard) reports the resumed state.
    store.get_run = lambda run_id: resumed_snapshot  # type: ignore[method-assign]

    report = run_cleanup(
        store=store,
        workspace_root=tmp_path,
        retention_days=14,
        dry_run=False,
        now=now,
    )

    assert len(report.plans) == 1, 'planning still reports it as a candidate'
    assert runtime_file.is_file(), 'the guard must have refused the deletion'


def test_run_cleanup_apply_skips_run_removed_mid_scan(tmp_path) -> None:
    now = utc_now()
    terminal_snapshot = _run_record(
        'run-1', state=RunState.FAILED, updated_at=now - timedelta(days=30)
    )
    runtime_file = tmp_path / 'run-1' / 'runtime' / 'beaker' / 'cache.bin'
    _write(runtime_file, 100)

    store = _FakeStore([terminal_snapshot])

    def _missing_get_run(run_id):
        raise RecordNotFound(run_id)

    store.get_run = _missing_get_run  # type: ignore[method-assign]

    report = run_cleanup(
        store=store,
        workspace_root=tmp_path,
        retention_days=14,
        dry_run=False,
        now=now,
    )

    assert runtime_file.is_file()


def test_plan_cleanup_conversation_run_becomes_eligible_after_conversation_retention_days(
    tmp_path,
) -> None:
    # Conversation runs (conversation=True) are inert and never reach a terminal state.
    # They become eligible for cleanup after conversation_run_retention_days based on
    # their created_at timestamp (the authoritative "conversation-end" timestamp for
    # inert conversations). The re-fetch guard re-checks both eligibility branches.
    now = utc_now()
    recent_conversation = _run_record(
        'conv-recent',
        state=RunState.CREATED,
        updated_at=now - timedelta(days=1),
    )
    recent_conversation = recent_conversation.model_copy(update={'conversation': True})
    expired_conversation = _run_record(
        'conv-expired',
        state=RunState.PAUSED,
        updated_at=now - timedelta(days=100),
    )
    expired_conversation = expired_conversation.model_copy(update={'conversation': True})
    terminal_expired = _run_record(
        'term-expired',
        state=RunState.COMPLETE,
        updated_at=now - timedelta(days=30),
    )
    for run_id in ('conv-recent', 'conv-expired', 'term-expired'):
        _write(tmp_path / run_id / 'runtime' / 'cache.bin', 10)

    # With conversation_run_retention_days=30, only conv-expired should be eligible
    plans = plan_cleanup(
        store=_FakeStore([recent_conversation, expired_conversation, terminal_expired]),
        workspace_root=tmp_path,
        retention_days=14,
        conversation_retention_days=30,
        now=now,
    )

    # conv-expired should be included (conversation + 100 days old)
    # term-expired should be included (terminal + 30 days old, past 14-day terminal threshold)
    # conv-recent should be excluded (conversation + only 1 day old)
    assert set(plan.run_id for plan in plans) == {'conv-expired', 'term-expired'}


def test_run_cleanup_apply_conversation_run_guard_rechecks_both_branches(tmp_path) -> None:
    # Regression guard: the re-fetch-before-delete must check BOTH terminal AND
    # conversation eligibility branches. A conversation run that entered eligible
    # state mid-scan must survive.
    now = utc_now()
    eligible_conversation = _run_record(
        'conv-1',
        state=RunState.PAUSED,
        updated_at=now - timedelta(days=60),
    )
    eligible_conversation = eligible_conversation.model_copy(update={'conversation': True})
    runtime_file = tmp_path / 'conv-1' / 'runtime' / 'beaker' / 'cache.bin'
    _write(runtime_file, 100)

    # Simulate a conversation run that "resumed" (state changed) between plan_cleanup
    # and run_cleanup. The stored run is no longer PAUSED but the storage cleanup
    # must skip it if it's no longer eligible.
    store = _FakeStore([eligible_conversation])

    # Make get_run return a run that left eligible state (state changed to something
    # non-terminal and less than conversation_retention_days old)
    resumed_conversation = eligible_conversation.model_copy(
        update={'state': RunState.BEAKER_IMPLEMENTING, 'updated_at': now - timedelta(days=1)}
    )
    store.get_run = lambda run_id: resumed_conversation  # type: ignore[method-assign]

    # With a short retention window, the conversation would no longer be eligible
    # (updated_at is only 1 day ago, and conversation eligibility uses created_at
    # which would also need to be old enough)
    plans = plan_cleanup(
        store=store,
        workspace_root=tmp_path,
        retention_days=14,
        conversation_retention_days=30,
        now=now,
    )
    assert len(plans) == 1, 'planning still reports it as eligible (based on created_at)'

    report = run_cleanup(
        store=store,
        workspace_root=tmp_path,
        retention_days=14,
        conversation_retention_days=30,
        dry_run=False,
        now=now,
    )

    # The re-fetch guard should catch that the run is no longer eligible
    # (updated_at is now only 1 day ago for a non-terminal run)
    assert runtime_file.is_file(), 'the guard must have refused the deletion'
