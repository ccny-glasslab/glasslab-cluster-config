"""RED tests for L2: report materialisation and de-stranding (T9 target).

These tests reproduce the live incident where run 4be2976 SUCCEEDED a real
Kubernetes Job, reached HONEYDEW_WRITING_REPORT, and then stranded: Honeydew
declared purpose='report' pointing at 'reports/report.md' without creating a
real file in its own workspace, so WorkspaceManager.copy_agent_output refused
to hand over bytes it did not produce ('agent output is not a real file: ...').
The watcher only LOGGED the resulting job.reconciliation_failed event, the run
stayed in HONEYDEW_WRITING_REPORT, and it burned its way to the 20-turn cap.

L2 (T9) must:
  * convert that non-file report hand-off into a BOUNDED corrective retry that
    carries the deterministic reason, then PAUSE the run in a resumable state;
  * stop looping when reconcile keeps failing for the same run (pause instead);
  * still deliver the report.md/report.pdf/report.docx bundle after acceptance;
  * keep the anti-escape invariant intact: a report path that is a symlink
    resolving to a real file INSIDE the agent workspace is accepted (resolve
    semantics), while an escaping symlink stays rejected.

All four tests are RED at the deployed revision and marked xfail(strict=True);
T9 implements the behavior and removes the markers.
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path

import pytest

from app.mock_runtime import ScriptedMockRuntime
from app.schemas import (
    AgentName,
    AgentTurnResult,
    ApprovalStatus,
    ProducedFile,
    RunCreateRequest,
    RunState,
    TurnKind,
)
from app.watcher import JobWatcher
from app.workspaces import WorkspaceError

INCIDENT_REPORT_PATH = 'reports/report.md'


class _ReportPhaseRuntime(ScriptedMockRuntime):
    """Intercepts every Honeydew report prompt, initial or corrective retry.

    The report phase is the last Honeydew phase and its prompt is the only one
    that names report.md, so the first such prompt marks the phase and every
    later Honeydew prompt is a retry, whatever wording T9 uses for it.
    """

    def __init__(self, *, runner_image: str) -> None:
        super().__init__(runner_image=runner_image)
        self.report_prompts: list[str] = []
        self.declared_paths: list[str] = []
        self._in_report_phase = False

    def run_turn(self, **kwargs):
        if kwargs['agent'] == AgentName.HONEYDEW and (
            self._in_report_phase or 'report.md' in kwargs['prompt']
        ):
            self._in_report_phase = True
            return self._report_turn(kwargs['workspace'], kwargs['prompt'])
        return super().run_turn(**kwargs)

    def _report_turn(self, workspace: Path, prompt: str):
        self.report_prompts.append(prompt)
        declared_path = self._materialise(workspace)
        self.declared_paths.append(declared_path)
        return (
            AgentTurnResult(
                kind=TurnKind.FINAL_REPORT,
                summary='Prepared the report for human acceptance.',
                produced_files=[
                    ProducedFile(path=declared_path, purpose='report')
                ],
                recommended_next_state=RunState.AWAITING_FINAL_ACCEPTANCE,
                done=True,
            ),
            f'mock-report-{len(self.report_prompts)}',
        )

    def _materialise(self, workspace: Path) -> str:
        raise NotImplementedError


class NonFileReportRuntime(_ReportPhaseRuntime):
    """Honeydew never hands over a real file; reproduces run 4be2976."""

    def __init__(self, *, runner_image: str, shape: str) -> None:
        super().__init__(runner_image=runner_image)
        self.shape = shape

    def _materialise(self, workspace: Path) -> str:
        target = workspace / INCIDENT_REPORT_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.shape == 'directory':
            target.mkdir(exist_ok=True)
        elif self.shape == 'symlink' and not target.is_symlink():
            target.symlink_to('missing-report-target.md')
        # 'missing': deliberately create nothing at the declared path.
        return INCIDENT_REPORT_PATH


class RepairingReportRuntime(_ReportPhaseRuntime):
    """Fails the first report hand-off, then materialises a real workspace file.

    retry_shape='plain' writes the report file directly; 'symlink-inside'
    writes the same real file and declares a symlink that resolves to it.
    """

    def __init__(self, *, runner_image: str, retry_shape: str) -> None:
        super().__init__(runner_image=runner_image)
        self.retry_shape = retry_shape

    def _materialise(self, workspace: Path) -> str:
        if len(self.report_prompts) == 1:
            return INCIDENT_REPORT_PATH
        target = workspace / 'report.md'
        target.write_text(
            '# Report\n\nMaterialised after one bounded corrective retry.\n',
            encoding='utf-8',
        )
        if self.retry_shape == 'symlink-inside':
            link = workspace / 'report-link.md'
            link.unlink(missing_ok=True)
            link.symlink_to(target.name)
            return 'report-link.md'
        return 'report.md'


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
            objective='Exercise report materialisation with fake evidence.'
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


@pytest.mark.parametrize('shape', ['missing', 'directory', 'symlink'])
@pytest.mark.xfail(
    strict=True,
    reason=(
        'L2/T9: a non-file final report must trigger one bounded corrective '
        'retry carrying the deterministic reason, then a resumable PAUSED'
    ),
)
def test_final_report_non_file_triggers_bounded_corrective_retry_then_pause(
    orchestrator_bundle, shape: str
) -> None:
    settings, store, cluster, _, engine = orchestrator_bundle
    runtime = NonFileReportRuntime(
        runner_image=settings.permitted_job_images[0],
        shape=shape,
    )
    engine.runtime = runtime
    run = _advance_to_jobs(engine, store)

    _complete_jobs(engine, store, cluster, run.run_id)

    paused = store.get_run(run.run_id)
    # The strand bug left the run in HONEYDEW_WRITING_REPORT forever; L2 must
    # move it to a recoverable pause instead of letting it burn to the cap.
    assert paused.state == RunState.PAUSED
    assert paused.resume_state == RunState.HONEYDEW_WRITING_REPORT
    # Exactly one bounded corrective retry: the initial turn plus one repair.
    assert len(runtime.report_prompts) == 2
    assert (
        f'agent output is not a real file: {INCIDENT_REPORT_PATH}'
        in runtime.report_prompts[-1]
    )
    # A paused run must not have manufactured an acceptance gate.
    assert not [
        action
        for action in store.list_actions(run.run_id)
        if action.type == 'accept_final_report'
        and action.approval_status == ApprovalStatus.PENDING
    ]


@pytest.mark.xfail(
    strict=True,
    reason=(
        'L2/T9: after 3 consecutive job.reconciliation_failed events the '
        'watcher must pause the run (recoverable) instead of looping'
    ),
)
def test_repeated_reconciliation_failure_pauses_run(orchestrator_bundle) -> None:
    _, store, _, _, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(
            objective='A repeatedly failing reconcile must pause the run.'
        )
    )
    store.replace_run(
        run.model_copy(update={'state': RunState.JOB_QUEUED}),
        expected_version=run.version,
    )

    reconcile_attempts: list[str] = []

    def failing_reconcile(run_id: str):
        reconcile_attempts.append(run_id)
        raise RuntimeError('cluster unreachable')

    engine.reconcile_run = failing_reconcile

    pause_calls: list[str] = []
    original_pause = engine.pause_run

    def recording_pause(run_id: str, **kwargs):
        pause_calls.append(run_id)
        return original_pause(run_id, **kwargs)

    engine.pause_run = recording_pause
    watcher = JobWatcher(engine, poll_interval_seconds=0.01)

    async def drive() -> None:
        task = asyncio.create_task(watcher.run())
        for _ in range(300):
            await asyncio.sleep(0.01)
            if store.get_run(run.run_id).state == RunState.PAUSED:
                break
        # Keep polling: a paused run must not be reconciled again.
        for _ in range(20):
            await asyncio.sleep(0.01)
        watcher.stop()
        await task

    asyncio.run(drive())

    paused = store.get_run(run.run_id)
    assert paused.state == RunState.PAUSED
    assert paused.resume_state == RunState.JOB_QUEUED
    # Three consecutive failures, then pause: not an endless reconcile loop.
    assert len(reconcile_attempts) == 3
    assert pause_calls == [run.run_id]
    failure_events = [
        event
        for event in store.list_events(run.run_id)
        if event.event_type == 'job.reconciliation_failed'
    ]
    assert len(failure_events) == 3


@pytest.mark.xfail(
    strict=True,
    reason=(
        'L2/T9: the incident report hand-off must be repaired by one bounded '
        'retry before the accepted run registers the md/pdf/docx bundle'
    ),
)
def test_completed_run_registers_pdf_and_docx_artifacts(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, _, engine = orchestrator_bundle
    runtime = RepairingReportRuntime(
        runner_image=settings.permitted_job_images[0],
        retry_shape='plain',
    )
    engine.runtime = runtime
    run = _advance_to_jobs(engine, store)

    _complete_jobs(engine, store, cluster, run.run_id)
    awaiting = store.get_run(run.run_id)
    assert awaiting.state == RunState.AWAITING_FINAL_ACCEPTANCE
    assert runtime.declared_paths == [INCIDENT_REPORT_PATH, 'report.md']

    action = _pending_action(store, run.run_id, 'accept_final_report')
    engine.approve_action(
        action.action_id,
        reviewer='test-human',
        reason='Report accepted.',
    )

    completed = store.get_run(run.run_id)
    assert completed.state == RunState.COMPLETE
    artifacts = {
        artifact.type: artifact
        for artifact in store.list_artifacts(run.run_id)
    }
    assert {'report', 'report.pdf', 'report.docx'} <= set(artifacts)
    assert artifacts['report'].uri == (
        f'artifact://{run.run_id}/reports/report.md'
    )
    for artifact_type in ('report', 'report.pdf', 'report.docx'):
        artifact = artifacts[artifact_type]
        path = Path(artifact.metadata['path'])
        assert path.is_file()
        assert artifact.sha256 == sha256(path.read_bytes()).hexdigest()


@pytest.mark.xfail(
    strict=True,
    reason=(
        'L2/T9: a report symlink that resolves inside the workspace must be '
        'accepted through the bounded retry, without weakening the invariant'
    ),
)
def test_report_symlink_resolving_inside_workspace_is_accepted(
    orchestrator_bundle, tmp_path
) -> None:
    settings, store, cluster, _, engine = orchestrator_bundle
    runtime = RepairingReportRuntime(
        runner_image=settings.permitted_job_images[0],
        retry_shape='symlink-inside',
    )
    engine.runtime = runtime
    run = _advance_to_jobs(engine, store)

    _complete_jobs(engine, store, cluster, run.run_id)
    accepted = store.get_run(run.run_id)
    assert accepted.state == RunState.AWAITING_FINAL_ACCEPTANCE
    assert runtime.declared_paths == [INCIDENT_REPORT_PATH, 'report-link.md']
    assert len(runtime.report_prompts) == 2

    workspace = Path(accepted.honeydew_workspace)
    target = workspace / 'report.md'
    link = workspace / 'report-link.md'
    assert link.is_symlink()
    assert link.resolve() == target.resolve()
    assert link.resolve().is_relative_to(workspace.resolve())
    # The authoritative copy is the target's real bytes, digest included.
    report = [
        artifact
        for artifact in store.list_artifacts(run.run_id)
        if artifact.type == 'report'
    ][-1]
    assert report.sha256 == sha256(target.read_bytes()).hexdigest()
    assert Path(report.metadata['path']).read_bytes() == target.read_bytes()

    # Pin the invariant so T9 cannot relax it: an escaping symlink is rejected.
    outside = tmp_path / 'outside-report.md'
    outside.write_text('outside the isolated workspace\n', encoding='utf-8')
    escape = workspace / 'escape-link.md'
    escape.unlink(missing_ok=True)
    escape.symlink_to(outside)
    with pytest.raises(
        WorkspaceError,
        match='agent output escapes isolated workspace',
    ):
        engine.workspaces.copy_agent_output(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            relative_path='escape-link.md',
            destination_kind='report',
        )
