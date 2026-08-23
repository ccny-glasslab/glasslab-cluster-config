from __future__ import annotations

import io
import json
from hashlib import sha256
from pathlib import Path
import sqlite3
import subprocess
import shutil
import zipfile

import pytest

from app.discord_adapter import DiscordAdapter
from app.engine import WorkflowError
from app.schemas import AgentName, ApprovalStatus, EventRecord, RequestedAction, RunCreateRequest, RunState, TerminalRetryRequest


class RecordingDiscord(DiscordAdapter):
    def __init__(self) -> None:
        self.created: list[str] = []
        self.published: list[tuple[str | None, EventRecord]] = []

    def create_thread(self, *, run_id: str, objective: str) -> str | None:
        self.created.append(run_id)
        return f'thread-{len(self.created)}'

    def publish(self, *, thread_id: str | None, status_message_id: str | None, event: EventRecord) -> str | None:
        self.published.append((thread_id, event))
        return status_message_id


class FlakyDiscord(RecordingDiscord):
    def __init__(self, *, fail_creates: int) -> None:
        super().__init__()
        self.fail_creates = fail_creates

    def create_thread(self, *, run_id: str, objective: str) -> str | None:
        if self.fail_creates:
            self.fail_creates -= 1
            raise RuntimeError('Discord unavailable')
        return super().create_thread(run_id=run_id, objective=objective)


def _terminal_parent(engine, store):
    parent = engine.create_run(RunCreateRequest(objective='Verify terminal retry checkpoint isolation.'))
    protocol_action = next(
        action for action in store.list_actions(parent.run_id)
        if action.type == 'approve_protocol'
    )
    engine.approve_action(
        protocol_action.action_id,
        reviewer='test-reviewer',
        reason='Approved checkpoint for terminal retry test.',
    )
    store.transition_run(store.get_run(parent.run_id).run_id, RunState.FAILED)
    return store.get_run(parent.run_id)


def _task_archive() -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr('task/problem.md', '# Task\n')
        archive.writestr('task/eval_agent_prompt.md', '# Evaluator\n')
    return content.getvalue()


def _terminal_task_parent(engine, store):
    task = engine.import_task_bundle(filename='task.zip', content=_task_archive())
    engine.policy.permitted_images.add(task.runner_image)
    parent = engine.create_run(
        RunCreateRequest(
            objective='Retry a verified task-bound research run.',
            task_id=task.task_id,
            task_bundle_digest=task.digest,
        )
    )
    approval = next(action for action in store.list_actions(parent.run_id) if action.type == 'approve_protocol')
    engine.approve_action(approval.action_id, reviewer='test-reviewer', reason='Approve task protocol.')
    parent = store.get_run(parent.run_id)
    store.replace_run(parent.model_copy(update={'state': RunState.FAILED}), expected_version=parent.version)
    return task, store.get_run(parent.run_id)


def test_terminal_retry_creates_fresh_child_and_renews_protocol_approval(orchestrator_bundle):
    settings, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    before = parent.model_dump(mode='json')

    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())

    assert child.parent_run_id == parent.run_id
    assert child.state == RunState.AWAITING_PROTOCOL_APPROVAL
    assert child.turn_number == 0
    assert child.beaker_session_id is None and child.honeydew_session_id is None
    assert child.maximum_turns == settings.maximum_turns
    assert not store.list_jobs(child.run_id)
    actions = store.list_actions(child.run_id)
    assert [(a.type, a.approval_status) for a in actions] == [
        ('approve_protocol', ApprovalStatus.PENDING)
    ]
    assert store.get_run(parent.run_id).model_dump(mode='json') == before
    assert any(e.event_type == 'run.retry_created' for e in store.list_events(parent.run_id))
    assert any(e.event_type == 'run.retry_created' for e in store.list_events(child.run_id))


def test_terminal_retry_is_idempotent_and_recovery_is_safe(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    first = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    second = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    assert second.run_id == first.run_id
    action_count = len(store.list_actions(first.run_id))
    assert first.run_id in engine.recover()
    assert len(store.list_actions(first.run_id)) == action_count


def _force_run_state(store, run_id: str, state: RunState) -> None:
    current = store.get_run(run_id)
    store.replace_run(current.model_copy(update={'state': state}), expected_version=current.version)


def test_retry_after_cancelled_child_creates_fresh_sibling(orchestrator_bundle):
    settings, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    first = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    _force_run_state(store, first.run_id, RunState.CANCELLED)
    first_before = store.get_run(first.run_id).model_dump(mode='json')
    first_events_before = [e.event_type for e in store.list_events(first.run_id)]
    parent_retries_before = [
        e for e in store.list_events(parent.run_id) if e.event_type == 'run.retry_created'
    ]

    sibling = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())

    assert sibling.run_id != first.run_id
    assert sibling.parent_run_id == parent.run_id
    assert sibling.state == RunState.AWAITING_PROTOCOL_APPROVAL
    assert sibling.turn_number == 0
    assert sibling.beaker_session_id is None and sibling.honeydew_session_id is None
    assert sibling.maximum_turns == settings.maximum_turns
    assert not store.list_jobs(sibling.run_id)
    assert [(a.type, a.approval_status) for a in store.list_actions(sibling.run_id)] == [
        ('approve_protocol', ApprovalStatus.PENDING)
    ]
    assert store.get_run(first.run_id).model_dump(mode='json') == first_before
    first_event_types = [e.event_type for e in store.list_events(first.run_id)]
    assert len(first_event_types) == len(first_events_before) + 1
    assert first_event_types[-1] == 'run.retry_superseded'
    parent_retries_after = [
        e for e in store.list_events(parent.run_id) if e.event_type == 'run.retry_created'
    ]
    assert len(parent_retries_after) == len(parent_retries_before) + 1
    assert parent_retries_after[-1].payload['child_run_id'] == sibling.run_id
    assert any(
        event.event_type == 'run.retry_created' for event in store.list_events(sibling.run_id)
    )


def test_failed_resume_child_can_be_superseded(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    first = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    _force_run_state(store, first.run_id, RunState.FAILED)

    sibling = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())

    assert sibling.run_id != first.run_id
    assert sibling.state == RunState.AWAITING_PROTOCOL_APPROVAL
    assert store.get_terminal_retry_child(parent.run_id).run_id == sibling.run_id
    superseded = [
        e for e in store.list_events(first.run_id) if e.event_type == 'run.retry_superseded'
    ]
    assert len(superseded) == 1
    assert superseded[-1].payload['superseded_by'] == sibling.run_id


@pytest.mark.parametrize('state', [RunState.CREATED, RunState.CANCELLED, RunState.COMPLETE])
def test_terminal_retry_rejects_ineligible_source(orchestrator_bundle, state):
    _, store, _, _, engine = orchestrator_bundle
    parent = engine.create_run(RunCreateRequest(objective='Reject nonterminal retry source state.'))
    if state == RunState.COMPLETE:
        store.replace_run(parent.model_copy(update={'state': state}), expected_version=parent.version)
    elif state != RunState.CREATED:
        store.transition_run(parent.run_id, state)
    with pytest.raises(WorkflowError):
        engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())


@pytest.mark.parametrize('tamper', ['protocol', 'manifest'])
def test_terminal_retry_fails_closed_on_tampered_checkpoint(orchestrator_bundle, tamper):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    if tamper == 'protocol':
        protocol = Path(parent.protocol_path)
        protocol.chmod(protocol.stat().st_mode | 0o200)
        protocol.write_text('tampered\n', encoding='utf-8')
        with pytest.raises(WorkflowError, match='approved protocol artifact'):
            engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    else:
        child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
        checkpoint = Path(child.beaker_workspace).parent / 'events' / 'terminal-retry-checkpoint.json'
        checkpoint.write_text('{}\n', encoding='utf-8')
        current = store.get_run(child.run_id)
        store.replace_run(current.model_copy(update={'state': RunState.PREPARING}), expected_version=current.version)
        with pytest.raises(Exception, match='checkpoint'):
            engine._resume_terminal_retry(child.run_id)


def test_terminal_retry_does_not_inherit_pending_actions_or_jobs(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    assert store.list_actions(parent.run_id)
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    assert {a.type for a in store.list_actions(child.run_id)} == {'approve_protocol'}
    assert store.list_jobs(child.run_id) == []


def test_terminal_retry_requires_parent_protocol_approval(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    parent = engine.create_run(RunCreateRequest(objective='Require fresh approval for retry source protocol.'))
    store.replace_run(
        parent.model_copy(update={'state': RunState.FAILED}),
        expected_version=parent.version,
    )
    with pytest.raises(WorkflowError, match='human-approved'):
        engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())


def test_discord_failure_does_not_lose_retry_state(orchestrator_bundle, monkeypatch):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    monkeypatch.setattr(engine.discord, 'publish', lambda **_: (_ for _ in ()).throw(RuntimeError('offline')))
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    assert store.get_run(child.run_id).state == RunState.AWAITING_PROTOCOL_APPROVAL
    assert any(event.event_type == 'run.retry_created' for event in store.list_events(parent.run_id))


def test_retry_recovery_does_not_duplicate_action_or_event(orchestrator_bundle, monkeypatch):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    current = store.get_run(child.run_id)
    store.replace_run(current.model_copy(update={'state': RunState.PREPARING}), expected_version=current.version)
    before_events = len(store.list_events(child.run_id))
    engine.recover()
    assert len(store.list_actions(child.run_id)) == 1
    assert len(store.list_events(child.run_id)) == before_events + 1  # state transition only


def test_task_bound_retry_copies_real_delta_and_allows_fresh_approval(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    task = engine.import_task_bundle(filename='task.zip', content=_task_archive())
    engine.policy.permitted_images.add(task.runner_image)
    parent = engine.create_run(
        RunCreateRequest(
            objective='Retry a task-bound protocol with implementation files.',
            task_id=task.task_id,
            task_bundle_digest=task.digest,
        )
    )
    approval = next(action for action in store.list_actions(parent.run_id) if action.type == 'approve_protocol')
    engine.approve_action(approval.action_id, reviewer='test-reviewer', reason='Use the task-bound protocol.')
    parent = store.get_run(parent.run_id)
    source = Path(parent.beaker_workspace) / 'implementation' / 'train.py'
    source.parent.mkdir()
    source.write_text('print("retry me")\n', encoding='utf-8')
    store.replace_run(
        parent.model_copy(update={
            'state': RunState.FAILED,
            'task_definition': {'obsolete': True},
            'task_bundle_path': '/obsolete/task.zip',
        }),
        expected_version=parent.version,
    )

    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    assert (Path(child.beaker_workspace) / 'benchmark-task' / 'problem.md').is_file()
    assert (Path(child.beaker_workspace) / 'implementation' / 'train.py').read_text() == 'print("retry me")\n'
    assert child.task_definition == task.model_dump(mode='json')
    assert child.task_bundle_path == task.archive_path
    child_approval = next(action for action in store.list_actions(child.run_id) if action.type == 'approve_protocol')
    engine.approve_action(child_approval.action_id, reviewer='test-reviewer', reason='Fresh retry approval.')
    assert Path(child.beaker_workspace, 'program.md').stat().st_mode & 0o200 == 0


def test_task_bound_retry_rejects_tampered_archive(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    task, parent = _terminal_task_parent(engine, store)
    archive = Path(task.archive_path)
    archive.chmod(archive.stat().st_mode | 0o200)
    archive.write_bytes(archive.read_bytes() + b'tampered')

    with pytest.raises(WorkflowError, match='preflight'):
        engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())


def test_retry_rejects_legacy_parent_without_recorded_base_commit(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    store.replace_run(
        parent.model_copy(update={'workspace_base_commit': None}),
        expected_version=parent.version,
    )
    with pytest.raises(WorkflowError, match='durably recorded'):
        engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())


def test_task_bound_retry_rejects_missing_verified_asset(orchestrator_bundle):
    settings, store, _, _, engine = orchestrator_bundle
    task, parent = _terminal_task_parent(engine, store)
    metadata = Path(task.archive_path).with_name('task.json')
    metadata.chmod(metadata.stat().st_mode | 0o200)
    payload = task.model_dump(mode='json')
    payload['datasets'] = [{
        'name': 'required-data',
        'uri': 's3://artifacts/missing.csv',
        'sha256': sha256(b'expected').hexdigest(),
        'role': 'train',
        'contains_labels': True,
    }]
    metadata.write_text(json.dumps(payload), encoding='utf-8')

    with pytest.raises(WorkflowError, match='preflight'):
        engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())


def test_task_bound_retry_rebinds_current_runner_metadata(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    task, parent = _terminal_task_parent(engine, store)
    metadata = Path(task.archive_path).with_name('task.json')
    metadata.chmod(metadata.stat().st_mode | 0o200)
    payload = task.model_dump(mode='json')
    payload['runner_image'] = 'ghcr.io/ccny-glasslab/obsolete-runner:old'
    metadata.write_text(json.dumps(payload), encoding='utf-8')
    rebound = engine.task_bundles.get(task.task_id, task.digest)
    engine.policy.permitted_images.add(rebound.runner_image)

    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    assert child.task_definition['runner_image'] == rebound.runner_image
    assert child.task_definition['runner_image'] != payload['runner_image']


def test_task_bound_retry_rejects_mismatched_task_identifier(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    task, parent = _terminal_task_parent(engine, store)
    metadata = Path(task.archive_path).with_name('task.json')
    metadata.chmod(metadata.stat().st_mode | 0o200)
    payload = task.model_dump(mode='json')
    payload['task_id'] = 'task-other-identifier'
    metadata.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(WorkflowError, match='binding checksum'):
        engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())


def test_retry_uses_recorded_historical_base_commit(orchestrator_bundle):
    settings, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    original_base = parent.workspace_base_commit
    repository = Path(settings.approved_repo_path)
    (repository / 'README.md').write_text('# Advanced main\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'README.md'], cwd=repository, check=True)
    subprocess.run(['git', 'commit', '-m', 'Advance main'], cwd=repository, check=True)

    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    assert child.workspace_base_commit == original_base
    assert engine.workspaces.worktree_base_commit(child.run_id) == original_base


def test_retry_selects_the_current_approved_protocol_after_revision(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    parent = engine.create_run(
        RunCreateRequest(objective='Retry the current protocol after a revision.')
    )
    first = next(action for action in store.list_actions(parent.run_id) if action.type == 'approve_protocol')
    engine.reject_action(first.action_id, reviewer='test-reviewer', reason='Revise the controls.')
    second = next(action for action in store.list_actions(parent.run_id) if action.type == 'approve_protocol' and action.action_id != first.action_id)
    engine.approve_action(second.action_id, reviewer='test-reviewer', reason='Approve the revised protocol.')
    parent = store.get_run(parent.run_id)
    store.replace_run(parent.model_copy(update={'state': RunState.FAILED}), expected_version=parent.version)

    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    assert child.protocol_version == 2
    assert child.state == RunState.AWAITING_PROTOCOL_APPROVAL


def test_retry_creates_a_fresh_discord_thread_and_projects_approval(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    discord = RecordingDiscord()
    engine.discord = discord
    parent = _terminal_parent(engine, store)
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())

    assert parent.discord_thread_id == 'thread-1'
    assert child.discord_thread_id == 'thread-2'
    approval = next(action for action in store.list_actions(child.run_id) if action.type == 'approve_protocol')
    assert any(
        thread_id == 'thread-2'
        and event.event_type == 'action.proposed'
        and event.payload.get('action_id') == approval.action_id
        for thread_id, event in discord.published
    )


def test_retry_recovers_missing_child_discord_thread(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    discord = FlakyDiscord(fail_creates=1)
    engine.discord = discord

    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    assert child.discord_thread_id is None
    assert store.get_run(child.run_id).state == RunState.AWAITING_PROTOCOL_APPROVAL

    engine.recover()
    recovered = store.get_run(child.run_id)
    assert recovered.discord_thread_id == 'thread-1'
    approval = next(action for action in store.list_actions(child.run_id) if action.type == 'approve_protocol')
    assert any(
        thread_id == 'thread-1'
        and event.payload.get('action_id') == approval.action_id
        for thread_id, event in discord.published
    )


def test_retry_recovery_rejects_extra_child_worktree_delta(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    (Path(child.beaker_workspace) / 'unexpected.py').write_text('x = 1\n')
    current = store.get_run(child.run_id)
    store.replace_run(current.model_copy(update={'state': RunState.PREPARING}), expected_version=current.version)
    with pytest.raises(Exception, match='delta'):
        engine._resume_terminal_retry(child.run_id)


def test_taskless_retry_rejects_benchmark_task_content(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    injected = Path(child.beaker_workspace) / 'benchmark-task' / 'unmanifested.txt'
    injected.parent.mkdir()
    injected.write_text('unexpected\n')
    current = store.get_run(child.run_id)
    store.replace_run(current.model_copy(update={'state': RunState.PREPARING}), expected_version=current.version)
    with pytest.raises(Exception, match='taskless'):
        engine._resume_terminal_retry(child.run_id)


@pytest.mark.parametrize('mutation, expected', [
    ('extra', 'unexpected or missing'),
    ('missing', 'unexpected or missing'),
    ('changed', 'task input checksum'),
])
def test_task_bound_retry_verifies_exact_managed_task_inputs(
    orchestrator_bundle, mutation, expected,
):
    _, store, _, _, engine = orchestrator_bundle
    _, parent = _terminal_task_parent(engine, store)
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    task_root = Path(child.beaker_workspace) / 'benchmark-task'
    task_root.chmod(0o755)
    if mutation == 'extra':
        (task_root / 'extra.txt').write_text('unexpected\n')
    elif mutation == 'missing':
        (task_root / 'problem.md').chmod(0o644)
        (task_root / 'problem.md').unlink()
    else:
        source = task_root / 'problem.md'
        source.chmod(0o644)
        source.write_text('tampered\n')
    current = store.get_run(child.run_id)
    store.replace_run(current.model_copy(update={'state': RunState.PREPARING}), expected_version=current.version)
    with pytest.raises(Exception, match=expected):
        engine._resume_terminal_retry(child.run_id)


def test_retry_recovery_rejects_worktree_protocol_not_matching_authority(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    protocol = Path(child.beaker_workspace) / 'program.md'
    protocol.write_text('not authoritative\n')
    current = store.get_run(child.run_id)
    store.replace_run(current.model_copy(update={'state': RunState.PREPARING}), expected_version=current.version)
    with pytest.raises(Exception, match='protocol does not match'):
        engine._resume_terminal_retry(child.run_id)


def test_retry_recovery_accepts_matching_worktree_protocol(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    source = Path(child.protocol_path)
    target = Path(child.beaker_workspace) / 'program.md'
    target.write_bytes(source.read_bytes())
    current = store.get_run(child.run_id)
    store.replace_run(current.model_copy(update={'state': RunState.PREPARING}), expected_version=current.version)
    engine._resume_terminal_retry(child.run_id)
    assert store.get_run(child.run_id).state == RunState.AWAITING_PROTOCOL_APPROVAL


@pytest.mark.parametrize('workspace_name', ['beaker_workspace', 'honeydew_workspace'])
@pytest.mark.parametrize('target_exists', [False, True])
def test_retry_rejects_live_and_dangling_worktree_protocol_symlinks(
    orchestrator_bundle, tmp_path, workspace_name, target_exists,
):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    outside = tmp_path / f'outside-{workspace_name}-{target_exists}.md'
    sentinel = 'outside sentinel\n'
    if target_exists:
        outside.write_text(sentinel)
    target = Path(getattr(child, workspace_name)) / 'program.md'
    target.symlink_to(outside)
    current = store.get_run(child.run_id)
    store.replace_run(current.model_copy(update={'state': RunState.PREPARING}), expected_version=current.version)
    with pytest.raises(Exception, match='must not be a symlink'):
        engine._resume_terminal_retry(child.run_id)
    # Defense in depth: approval must also reject the link rather than making
    # copy2 follow it outside the isolated worktree.
    with pytest.raises(Exception, match='must not be a symlink'):
        engine.workspaces.freeze_protocol(child.run_id)
    if target_exists:
        assert outside.read_text() == sentinel
    else:
        assert not outside.exists()


@pytest.mark.parametrize('task_bound', [False, True])
@pytest.mark.parametrize('target_exists', [False, True])
def test_retry_rejects_live_and_dangling_benchmark_task_root_symlinks(
    orchestrator_bundle, tmp_path, task_bound, target_exists,
):
    _, store, _, _, engine = orchestrator_bundle
    if task_bound:
        _, parent = _terminal_task_parent(engine, store)
    else:
        parent = _terminal_parent(engine, store)
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    outside = tmp_path / f'outside-task-{task_bound}-{target_exists}'
    if target_exists:
        outside.mkdir()
        (outside / 'sentinel.txt').write_text('outside\n')
    for workspace_name in ('beaker_workspace', 'honeydew_workspace'):
        task_root = Path(getattr(child, workspace_name)) / 'benchmark-task'
        if task_root.exists():
            task_root.chmod(0o755)
            shutil.rmtree(task_root)
        task_root.symlink_to(outside, target_is_directory=True)
    current = store.get_run(child.run_id)
    store.replace_run(current.model_copy(update={'state': RunState.PREPARING}), expected_version=current.version)
    with pytest.raises(Exception, match='benchmark-task root must not be a symlink'):
        engine._resume_terminal_retry(child.run_id)


@pytest.mark.parametrize('mutation, expected', [
    ('changed', 'checksum'),
    ('missing', 'checksum'),
    ('head', 'base commit'),
])
def test_retry_recovery_rejects_changed_or_missing_checkpoint_material(
    orchestrator_bundle, mutation, expected,
):
    _, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    source = Path(parent.beaker_workspace) / 'checkpoint.py'
    source.write_text('value = 1\n')
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    target = Path(child.beaker_workspace) / 'checkpoint.py'
    if mutation == 'changed':
        target.write_text('value = 2\n')
    elif mutation == 'missing':
        target.unlink()
    else:
        subprocess.run(['git', 'add', 'checkpoint.py'], cwd=child.beaker_workspace, check=True)
        subprocess.run(['git', 'commit', '-m', 'Alter retry checkpoint'], cwd=child.beaker_workspace, check=True)
    current = store.get_run(child.run_id)
    store.replace_run(current.model_copy(update={'state': RunState.PREPARING}), expected_version=current.version)
    with pytest.raises(Exception, match=expected):
        engine._resume_terminal_retry(child.run_id)


def test_legacy_action_without_event_is_repaired_with_persisted_identity(orchestrator_bundle):
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(RunCreateRequest(objective='Repair a legacy action proposal event.'))
    # The real creator will use this turn-local ordinal after this manually
    # persisted legacy row becomes the second action.
    legacy = engine.policy.build_record(
        run_id=run.run_id,
        proposed_by=AgentName.ORCHESTRATOR,
        action=RequestedAction(type='accept_final_report', arguments={}, reason='Legacy action.'),
        ordinal=run.turn_number * 1000 + 2,
    )
    store.save_action(legacy)
    repaired = engine._create_human_action(
        run_id=run.run_id, action_type='accept_final_report', reason='Legacy action.',
    )
    assert repaired.action_id == legacy.action_id
    proposals = [event for event in store.list_events(run.run_id) if event.event_type == 'action.proposed']
    assert any(event.payload.get('action_id') == legacy.action_id for event in proposals)


def test_preparing_retry_repair_restores_one_missing_protocol_proposal_event(orchestrator_bundle):
    settings, store, _, _, engine = orchestrator_bundle
    parent = _terminal_parent(engine, store)
    child = engine.retry_terminal_run(parent.run_id, TerminalRetryRequest())
    action = next(action for action in store.list_actions(child.run_id) if action.type == 'approve_protocol')
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "DELETE FROM events WHERE run_id = ? AND event_type = 'action.proposed'",
            (child.run_id,),
        )
    current = store.get_run(child.run_id)
    store.replace_run(current.model_copy(update={'state': RunState.PREPARING}), expected_version=current.version)

    engine.recover()
    proposals = [
        event for event in store.list_events(child.run_id)
        if event.event_type == 'action.proposed'
    ]
    assert [event.payload.get('action_id') for event in proposals] == [action.action_id]
    engine.recover()
    proposals = [
        event for event in store.list_events(child.run_id)
        if event.event_type == 'action.proposed'
    ]
    assert [event.payload.get('action_id') for event in proposals] == [action.action_id]
