#!/usr/bin/env python3
"""Rehearse the full research flow against REAL agent models with FAKE cluster.

Smoke tests use a scripted mock runtime whose output is idealized, so they
cannot catch the failure classes that only appear with real model output
(contract id reuse, structured-envelope drift, per-turn model routing,
evaluator_type derivation). This harness drives the same bounded pipeline
through the REAL OpenCode runtime pointed at the live mlx_lm servers, with a
fake cluster executor so no Kubernetes job is ever submitted.

Usage (on the provisioner or a host with opencode + access to .17/.18):
    python3 -m app.rehearse_research_flow

Each stage is reported pass/fail with the exact error; the JSON summary is
CI-consumable. A failing stage means a fresh user run would fail the same way.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

from app.cluster import FakeClusterExecutor
from app.config import SERVICE_ROOT, Settings
from app.contract_candidates import ContractCandidateManager
from app.contracts import EvaluationContractResolver
from app.discord_adapter import DisabledDiscordAdapter
from app.engine import ResearchOrchestrator
from app.opencode_runtime import OpenCodeProcessRuntime
from app.policy import ActionPolicy
from app.schemas import (
    ApprovalStatus,
    JobStatus,
    RunCreateRequest,
    RunState,
    SourceType,
)
from app.storage import SqliteStore
from app.workspaces import WorkspaceManager

RUNNER_IMAGE = 'ghcr.io/ccny-glasslab/glasslab-smoke-runner:test'

# The live model servers (split-model serving, verified 2026-09-07).
HONEYDEW_REASONING_MODEL = 'mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit'
HONEYDEW_REASONING_URL = 'http://192.168.1.18:52417/v1'
HONEYDEW_STRUCTURED_MODEL = 'mlx-community/Qwen3-Coder-Next-4bit'
HONEYDEW_STRUCTURED_URL = 'http://192.168.1.17:52417/v1'
BEAKER_MODEL = 'mlx-community/Qwen3-Coder-Next-4bit'
BEAKER_URL = 'http://192.168.1.17:52417/v1'
TASK_COMPILER_MODEL = 'mlx-community/Qwen3-Coder-Next-4bit'
TASK_COMPILER_URL = 'http://192.168.1.17:52417/v1'


def _create_repo(root: Path) -> Path:
    repo = root / 'approved-repo'
    if repo.exists():
        # Rehearsal resume: a prior attempt already initialized this
        # repository; reuse it rather than failing on the existing dir.
        return repo
    repo.mkdir()
    subprocess.run(['git', 'init', '-b', 'main'], cwd=repo, check=True)
    subprocess.run(
        ['git', 'config', 'user.email', 'rehearse@glasslab.local'],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ['git', 'config', 'user.name', 'Glasslab Rehearsal'],
        cwd=repo,
        check=True,
    )
    (repo / 'README.md').write_text('# Rehearsal repository\n')
    (repo / 'configs').mkdir()
    (repo / 'configs' / 'baseline.yaml').write_text('learning_rate: 0.0001\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(
        ['git', 'commit', '-m', 'Initialize rehearsal repository'],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


_stages: dict[str, object] = {}
REHEARSE_ROOT = Path(
    os.environ.get('REHEARSE_ROOT', '/tmp/glasslab-rehearse-root')
)


def _stage_progress(stages: dict[str, object]) -> None:
    print('STAGE_PROGRESS ' + json.dumps(stages, sort_keys=True), flush=True)
    (REHEARSE_ROOT / 'checkpoint.json').write_text(
        json.dumps(stages, sort_keys=True)
    )


def _build_engine(
    root: Path,
    *,
    runtime_factory=OpenCodeProcessRuntime,
):
    repo = _create_repo(root)
    approved = root / 'knowledge-approved'
    approved.mkdir(exist_ok=True)
    (approved / 'technique-card.md').write_text(
        'Technique card: metric-search over GPU clusters. Prefer cosine '
        'similarity for embedding retrieval and report verified metrics '
        'with a fixed seed.'
    )
    settings = Settings(
        database_path=str(root / 'orchestrator.db'),
        workspace_root=str(root / 'runs'),
        artifact_root=str(root / 'artifacts'),
        approved_repo_path=str(repo),
        approved_repo_ref='main',
        evaluation_contract_root=str(
            SERVICE_ROOT / 'evaluation-contracts'
        ),
        permitted_job_images=[RUNNER_IMAGE],
        cluster_execution_mode='fake',
        promoted_contract_root=str(root / 'trusted-contracts'),
        sealed_contract_candidate_root=str(root / 'contract-candidates'),
        trusted_contract_catalog_path=str(
            root / 'trusted-contracts' / 'catalog.json'
        ),
        shared_mount_root=str(root),
        task_bundle_root=str(root / 'task-bundles'),
        task_asset_root=str(root / 'task-assets'),
        dataset_upload_root=str(root / 'dataset-uploads'),
        benchmark_dataset_catalog_path=str(
            root / 'datasets' / 'catalog.json'
        ),
        knowledge_root=str(root / 'knowledge'),
        knowledge_allowlist_roots=[str(approved)],
        one_active_run=True,
        maximum_parallel_jobs=2,
        # The split-model routing: structured long-context turns on
        # Coder-Next (.17), verification on Thinking (.18), Beaker on
        # Coder-Next (.17), task compiler on Coder-Next (.17).
        agent_model_provider_id='exo',
        agent_model_honeydew=HONEYDEW_REASONING_MODEL,
        agent_base_url_honeydew=HONEYDEW_REASONING_URL,
        agent_model_beaker=BEAKER_MODEL,
        agent_base_url_beaker=BEAKER_URL,
        honeydew_structured_agent_model=HONEYDEW_STRUCTURED_MODEL,
        honeydew_structured_agent_base_url=HONEYDEW_STRUCTURED_URL,
        honeydew_reasoning_agent_model=HONEYDEW_REASONING_MODEL,
        honeydew_reasoning_agent_base_url=HONEYDEW_REASONING_URL,
        task_compiler_agent_model=TASK_COMPILER_MODEL,
        task_compiler_agent_base_url=TASK_COMPILER_URL,
        # Real-model turns are slow; widen the budget so a rehearsal turn
        # is not killed mid-reasoning on the Thinking model. Long-context
        # protocol drafts have exceeded 3600s live, so allow 2h per turn.
        opencode_turn_timeout_seconds=7200.0,
        hermes_turn_timeout_seconds=3600.0,
    )
    store = SqliteStore(settings.database_path)
    cluster = FakeClusterExecutor()
    engine = ResearchOrchestrator(
        settings=settings,
        store=store,
        runtime=runtime_factory(settings),
        workspaces=WorkspaceManager(
            workspace_root=settings.workspace_root,
            approved_repo_path=settings.approved_repo_path,
            approved_repo_ref=settings.approved_repo_ref,
        ),
        contracts=EvaluationContractResolver(
            settings.promoted_contract_root,
            fallback_roots=[settings.evaluation_contract_root],
        ),
        contract_candidates=ContractCandidateManager(
            sealed_root=settings.sealed_contract_candidate_root,
            promoted_root=settings.promoted_contract_root,
            catalog_path=settings.trusted_contract_catalog_path,
            shared_mount_root=settings.shared_mount_root,
        ),
        policy=ActionPolicy(
            permitted_images=settings.permitted_job_images,
            maximum_cpu=settings.maximum_cpu,
            maximum_memory_gib=settings.maximum_memory_gib,
            maximum_gpus=settings.maximum_gpus,
            maximum_parallel_jobs=settings.maximum_parallel_jobs,
        ),
        cluster=cluster,
        discord=DisabledDiscordAdapter(),
    )
    return settings, store, cluster, engine


# Gate action type for each human-wait run state. A state outside this map is
# either terminal (handled above), paused (handled above), or a transient
# agent state that should never be observed between driver iterations.
_GATE_FOR_STATE = {
    RunState.AWAITING_PROTOCOL_APPROVAL: 'approve_protocol',
    RunState.AWAITING_CONTRACT_PROMOTION: 'propose_evaluation_contract',
    RunState.AWAITING_EXECUTION_APPROVAL: 'submit_experiment_matrix',
    RunState.AWAITING_FINAL_ACCEPTANCE: 'accept_final_report',
}

# Agent states the engine can recover from with a fresh session; if an
# exception leaves the run in one of these (rather than PAUSED), a relaunch
# still resumes from the correct phase via engine.recover().
_RESUMABLE_AGENT_STATES = frozenset(
    {
        RunState.HONEYDEW_DRAFTING_PROTOCOL,
        RunState.BEAKER_DRAFTING_CONTRACT,
        RunState.HONEYDEW_REVIEWING_CONTRACT,
        RunState.BEAKER_PLANNING,
        RunState.BEAKER_IMPLEMENTING,
        RunState.BEAKER_FINALIZING,
        RunState.HONEYDEW_REVIEWING,
        RunState.BEAKER_REVISING,
        RunState.BEAKER_ANALYZING,
        RunState.HONEYDEW_VERIFYING,
        RunState.HONEYDEW_WRITING_REPORT,
    }
)

_TERMINAL_RUN_STATES = frozenset(
    {RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT}
)


def _default_snapshot_root(root: Path) -> Path:
    """Resolve where snapshots live: env override, else a root SIBLING.

    The default is a sibling of the rehearsal root (never inside it) so a
    snapshot copy can never recurse into itself.
    """
    override = os.environ.get('REHEARSE_SNAPSHOT_ROOT')
    if override:
        return Path(override)
    root = Path(root)
    return root.parent / (root.name + '.snapshots')


def _snapshot_git_commit() -> str | None:
    """The commit of the checkout this harness runs from, or None if absent."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=SERVICE_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return None
    commit = result.stdout.strip()
    return commit or None


def _timestamp_name() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _current_run_state(root: Path) -> tuple[str | None, str | None]:
    """Read (run_id, state) of the latest run in root without building an engine."""
    db_path = Path(root) / 'orchestrator.db'
    if not db_path.exists():
        return None, None
    store = SqliteStore(str(db_path))
    runs = store.list_runs()
    if not runs:
        return None, None
    run = runs[-1]
    return run.run_id, run.state.value


def _backup_sqlite(source_path: Path, dest_path: Path) -> None:
    """Write a transaction-consistent copy of a SQLite DB via the backup API.

    A plain file copy races the live WAL: SQLite deletes -wal/-shm when the
    last connection closes, and a .db copied mid-checkpoint can miss committed
    rows. sqlite3.Connection.backup() yields one coherent snapshot file.
    """
    source = sqlite3.connect(str(source_path), timeout=30)
    try:
        destination = sqlite3.connect(str(dest_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def _copy_root_snapshot(root: Path, state_dir: Path) -> None:
    """Copy root into state_dir, replacing each SQLite DB with a clean backup."""
    state_dir.mkdir(parents=True, exist_ok=True)
    skip: set[str] = set()
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if entry.is_file() and entry.suffix == '.db':
            skip.update({entry.name, entry.name + '-wal', entry.name + '-shm'})
            _backup_sqlite(entry, state_dir / entry.name)
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if entry.name in skip:
            continue
        target = state_dir / entry.name
        if entry.is_symlink():
            os.symlink(os.readlink(entry), target)
        elif entry.is_dir():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target)


def create_snapshot(
    root: Path,
    snapshot_root: Path,
    name: str,
    *,
    state_value: str | None = None,
    run_id: str | None = None,
) -> Path:
    """Copy the whole rehearsal root into <snapshot_root>/<name>/state.

    Layout is exactly <snapshot-root>/<name>/state/ (full copy of root) plus
    <snapshot-root>/<name>/meta.json. state_value/run_id default to whatever
    the latest run in root records, so a caller can snapshot without racing
    the store open.
    """
    root = Path(root)
    snapshot_root = Path(snapshot_root)
    if state_value is None or run_id is None:
        detected_run, detected_state = _current_run_state(root)
        if run_id is None:
            run_id = detected_run
        if state_value is None:
            state_value = detected_state
    dest = snapshot_root / name
    state_dir = dest / 'state'
    resolved_root = root.resolve()
    resolved_state = state_dir.resolve()
    if resolved_root == resolved_state or resolved_root in resolved_state.parents:
        raise ValueError(
            'snapshot state directory must not live inside the rehearsal root'
        )
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _copy_root_snapshot(root, state_dir)
    meta: dict[str, object] = {
        'name': name,
        'state': state_value,
        'run_id': run_id,
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'git_commit': _snapshot_git_commit(),
    }
    (dest / 'meta.json').write_text(
        json.dumps(meta, indent=2, sort_keys=True) + '\n'
    )
    return dest


def restore_snapshot(root: Path, snapshot_root: Path, name: str) -> Path:
    """Restore <snapshot_root>/<name>/state/* back into root.

    A non-empty root is first moved aside to <root>.prev (overwriting any
    previous .prev) so a restore is recoverable.
    """
    root = Path(root)
    snapshot_root = Path(snapshot_root)
    state_dir = snapshot_root / name / 'state'
    if not state_dir.is_dir():
        raise FileNotFoundError(
            f'snapshot {name!r} has no state directory: {state_dir}'
        )
    prev = Path(str(root) + '.prev')
    if root.exists():
        if prev.is_dir():
            shutil.rmtree(prev)
        elif prev.exists():
            prev.unlink()
        root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root), str(prev))
    root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(state_dir, root)
    return root


def read_snapshot_meta(snapshot_root: Path, name: str) -> dict[str, object] | None:
    meta_path = Path(snapshot_root) / name / 'meta.json'
    if not meta_path.is_file():
        return None
    loaded = json.loads(meta_path.read_text())
    return loaded if isinstance(loaded, dict) else None


def list_snapshots(snapshot_root: Path) -> list[dict[str, object]]:
    base = Path(snapshot_root)
    if not base.is_dir():
        return []
    entries: list[dict[str, object]] = []
    for child in sorted(base.iterdir(), key=lambda path: path.name):
        if not child.is_dir():
            continue
        meta = read_snapshot_meta(base, child.name)
        if meta is not None:
            entries.append(meta)
    return entries


def _latest_run_state(store: 'object') -> RunState | None:
    runs = store.list_runs()
    if not runs:
        return None
    return runs[-1].state


def _load_checkpoint(stages: dict[str, object]) -> None:
    checkpoint = REHEARSE_ROOT / 'checkpoint.json'
    if not checkpoint.exists():
        return
    loaded = json.loads(checkpoint.read_text())
    if isinstance(loaded, dict):
        stages.update(loaded)


def _record_failure(
    stages: dict[str, object],
    store: 'object',
    run_id: str,
    exc: Exception,
) -> None:
    """Capture the failure and the durable action state for the summary."""
    try:
        actions = store.list_actions(run_id)
    except Exception:
        actions = []
    stages['last_failure'] = {
        'error': str(exc),
        'failure_class': str(getattr(exc, 'failure_class', '') or ''),
    }
    stages['state_at_failure'] = store.get_run(run_id).state.value
    stages['actions_at_failure'] = [
        f"{a.type}/{a.approval_status.value}" for a in actions
    ]
    rejection_reasons = [
        str(a.reason)
        for a in actions
        if a.approval_status == ApprovalStatus.REJECTED
        and a.type
        in {
            'propose_evaluation_contract',
            'submit_experiment_matrix',
        }
    ]
    if rejection_reasons:
        stages['rejection_reasons'] = rejection_reasons


def _complete_active_jobs(
    cluster: object,
    store: 'object',
    run_id: str,
) -> None:
    """Mark every fake-cluster job terminal so reconcile can advance the run.

    The fake executor never runs the workload; this injects the digest-carrying
    metrics artifact the rest of the deterministic pipeline consumes. Already
    terminal jobs are left untouched so retries are idempotent.
    """
    for job in store.list_jobs(run_id):
        if job.status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
            assert job.external_run_id is not None
            cluster.complete(
                job.external_run_id,
                metrics={
                    'score': 0.8 if job.variant_name == 'candidate' else 0.6
                },
            )


def run_rehearsal(
    root: Path | None = None,
    *,
    max_gates: int | None = None,
    stop_at_state: RunState | None = None,
    runtime_factory=OpenCodeProcessRuntime,
    auto_snapshot: bool = False,
    snapshot_root: Path | None = None,
) -> dict[str, object]:
    """Drive one rehearsal run to COMPLETE, pausing cleanly on turn walls.

    With max_gates None (the default) every gate is approved and the run is
    driven to COMPLETE - the end-to-end behavior this harness has always had.
    With max_gates set, exactly that many human-wait gate approvals are
    consumed and the driver then stops at the next human-wait state (or
    stop_at_state, or a terminal state), returning result SEGMENT_DONE with
    entry_state / gates_consumed / reached_state. Job states between gates are
    advanced against the fake cluster and never count as gate consumption.

    The run state is the source of truth, not the presence of a PENDING action:
    after a crash the driver relaunches, engine.recover() re-advances any
    already-approved gate, and a PAUSED run is resumed via engine.resume_run().
    A wall-clock turn timeout pauses the run and returns result RESUMABLE with
    exit code 0 (the durable checkpoint survives); a deterministic failure
    returns result FAIL with exit code 1 so CI still fails loudly.
    """
    global _stages, REHEARSE_ROOT
    if root is not None:
        REHEARSE_ROOT = Path(root)
    root = REHEARSE_ROOT
    if snapshot_root is None:
        snapshot_root = _default_snapshot_root(root)
    snapshot_root = Path(snapshot_root)
    stages: dict[str, object] = {}
    _stages = stages
    root.mkdir(parents=True, exist_ok=True)
    fresh = not (root / 'orchestrator.db').exists()
    settings, store, cluster, engine = _build_engine(
        root, runtime_factory=runtime_factory
    )
    segment_mode = max_gates is not None or stop_at_state is not None
    gates_consumed = 0
    snapshotted: set[str] = set()
    if not fresh:
        _load_checkpoint(stages)
        # Resumes already-APPROVED-but-interrupted gates and any run left in a
        # resumable agent state by a previous driver crash. PAUSED runs are not
        # active and are resumed explicitly below.
        engine.recover()
    else:
        engine.knowledge.ingest_source(
            source_type=SourceType.TECHNIQUE_CARD,
            path=str(root / 'knowledge-approved' / 'technique-card.md'),
            title='GPU metric-search technique card',
        )
        run = engine.create_run(
            RunCreateRequest(
                objective=(
                    'Rehearse the complete research flow with real models: '
                    'compare three bounded methods and report which performs '
                    'best on the technique-card metric.'
                )
            )
        )
        stages['run_created'] = run.state.value
        _stage_progress(stages)

    # The segment summary reports where this invocation entered the run so a
    # restore-and-run caller can see the exact span it covered.
    entry_state = _latest_run_state(store)

    # Driver loop: every branch advances the run deterministically or blocks on
    # a real-model turn inside the engine call. Approved gates are consumed in
    # order; each engine call chains forward until the next human-wait state.
    for _ in range(40):
        runs = store.list_runs()
        if not runs:
            raise RuntimeError('rehearsal store has no run')
        run_id = runs[-1].run_id
        run = store.get_run(run_id)
        state = run.state

        if state is RunState.COMPLETE:
            stages['final_state'] = state.value
            try:
                stages['context_packets'] = len(
                    store.list_context_packets(run_id)
                )
            except Exception:
                pass
            stages['result'] = 'PASS'
            _stage_progress(stages)
            return stages

        if state in _TERMINAL_RUN_STATES:
            stages['final_state'] = state.value
            stages['result'] = 'FAIL'
            stages['reason'] = 'run reached terminal state without acceptance'
            _stage_progress(stages)
            return stages

        if (
            auto_snapshot
            and state in _GATE_FOR_STATE
            and state.value not in snapshotted
        ):
            create_snapshot(
                root=root,
                snapshot_root=snapshot_root,
                name=state.value,
                state_value=state.value,
                run_id=run_id,
            )
            snapshotted.add(state.value)

        if segment_mode:
            # A segment ends at the next human-wait state once max_gates
            # approvals have been consumed, or as soon as stop_at_state is
            # reached. Job states are advanced below without counting.
            at_approved_gate = (
                state in _GATE_FOR_STATE
                and max_gates is not None
                and gates_consumed >= max_gates
            )
            at_stop_state = (
                stop_at_state is not None and state == stop_at_state
            )
            if at_approved_gate or at_stop_state:
                stages['result'] = 'SEGMENT_DONE'
                if entry_state is not None:
                    stages['entry_state'] = entry_state.value
                stages['gates_consumed'] = gates_consumed
                stages['reached_state'] = state.value
                _stage_progress(stages)
                return stages

        try:
            if state is RunState.PAUSED:
                if run.resume_state is None:
                    stages['result'] = 'FAIL'
                    stages['reason'] = 'run is paused without a resumable phase'
                    _stage_progress(stages)
                    return stages
                engine.resume_run(
                    run_id,
                    requested_by='rehearse-human',
                    reason='Rehearsal resume after a paused model turn.',
                )
                continue

            if state in {RunState.JOB_QUEUED, RunState.JOB_RUNNING}:
                _complete_active_jobs(cluster, store, run_id)
                # reconcile records artifacts, then advances to Beaker analysis
                # (a real-model turn) once no job is active.
                engine.reconcile_run(run_id)
                continue

            gate = _GATE_FOR_STATE.get(state)
            if gate is None:
                # A transient agent state that persisted across an engine call
                # boundary means the previous call did not advance as expected.
                stages['result'] = 'FAIL'
                stages['reason'] = (
                    f'unexpected run state {state.value}; expected an awaiting '
                    'gate or a resumable phase'
                )
                stages['actions_at_failure'] = [
                    f"{a.type}/{a.approval_status.value}"
                    for a in store.list_actions(run_id)
                ]
                _stage_progress(stages)
                return stages

            pending = [
                action
                for action in store.list_actions(run_id)
                if action.type == gate
                and action.approval_status == ApprovalStatus.PENDING
            ]
            if not pending:
                stages['result'] = 'FAIL'
                stages['reason'] = (
                    f'{gate} gate has no pending action while the run is in '
                    f'{state.value}'
                )
                stages['actions_at_failure'] = [
                    f"{a.type}/{a.approval_status.value}"
                    for a in store.list_actions(run_id)
                ]
                _stage_progress(stages)
                return stages
            engine.approve_action(
                pending[-1].action_id,
                reviewer='rehearse-human',
                reason=f'Rehearsal {gate} approved.',
            )
            stages.setdefault('approved_gates', []).append(gate)
            gates_consumed += 1
            _stage_progress(stages)
        except Exception as exc:
            # A turn wall-clock timeout (or any transient agent failure) pauses
            # the run inside the engine; that is a clean, resumable stop, not a
            # rehearsal failure. Deterministic failures keep the old behavior:
            # loud traceback + nonzero exit.
            _record_failure(stages, store, run_id, exc)
            after = store.get_run(run_id)
            if (
                after.state is RunState.PAUSED
                or after.state in _RESUMABLE_AGENT_STATES
            ):
                stages['result'] = 'RESUMABLE'
                stages['resume_state'] = (
                    after.resume_state or after.state
                ).value
                stages['next_step'] = (
                    'The run is paused or recoverable after an interrupted '
                    'model turn. Relaunch this command to resume from the '
                    'durable checkpoint.'
                )
                _stage_progress(stages)
                return stages
            raise

    stages['result'] = 'FAIL'
    stages['reason'] = 'rehearsal driver iteration budget exceeded'
    _stage_progress(stages)
    return stages


def _proposal_evaluator_type(store, run_id: str) -> str | None:
    for artifact in store.list_artifacts(run_id):
        if artifact.type == 'evaluation_contract_proposal':
            proposal = artifact.metadata.get('proposal')
            if isinstance(proposal, dict):
                return str(proposal.get('evaluator_type'))
    return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='python3 -m app.rehearse_research_flow',
        description=(
            'Rehearse the research flow end-to-end, or snapshot and drive one '
            'segment (one gate approval) at a time from a saved state.'
        ),
    )
    parser.add_argument(
        '--root',
        default=None,
        help='Rehearsal state root (default $REHEARSE_ROOT).',
    )
    parser.add_argument(
        '--snapshot-root',
        default=None,
        help=(
            'Where snapshots live (default $REHEARSE_SNAPSHOT_ROOT, else '
            '<root>.snapshots, a sibling of root).'
        ),
    )
    parser.add_argument(
        '--full',
        action='store_true',
        help='Drive the run to COMPLETE (the default when no mode is given).',
    )
    parser.add_argument(
        '--no-snapshots',
        action='store_true',
        help='Disable the automatic gate snapshots taken during --full.',
    )
    parser.add_argument(
        '--snapshot',
        nargs='?',
        default=None,
        const='',
        metavar='NAME',
        help=(
            'Copy the current root into a snapshot and exit. NAME defaults to '
            'the current run state (fallback: a UTC timestamp).'
        ),
    )
    parser.add_argument(
        '--list',
        dest='list_snapshots',
        action='store_true',
        help='List available snapshots and exit.',
    )
    parser.add_argument(
        '--from',
        dest='from_snapshot',
        default=None,
        metavar='NAME',
        help='Restore snapshot NAME into --root, then run the segment(s).',
    )
    parser.add_argument(
        '--segments',
        type=int,
        default=None,
        metavar='N',
        help='Gate approvals to consume before stopping (default 1).',
    )
    parser.add_argument(
        '--until',
        default=None,
        choices=[state.value for state in RunState],
        metavar='STATE',
        help='Also stop once the run reaches STATE.',
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    global REHEARSE_ROOT
    root = Path(args.root) if args.root else REHEARSE_ROOT
    REHEARSE_ROOT = root
    snapshot_root = (
        Path(args.snapshot_root)
        if args.snapshot_root
        else _default_snapshot_root(root)
    )

    if args.list_snapshots:
        print(
            json.dumps(list_snapshots(snapshot_root), indent=2, sort_keys=True)
        )
        return 0

    if args.snapshot is not None:
        run_id, state_value = _current_run_state(root)
        name = args.snapshot or state_value or _timestamp_name()
        dest = create_snapshot(
            root,
            snapshot_root,
            name,
            state_value=state_value,
            run_id=run_id,
        )
        meta = read_snapshot_meta(snapshot_root, name) or {}
        print(
            json.dumps(
                {'snapshot': name, 'path': str(dest), 'meta': meta},
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.from_snapshot is not None:
        restore_snapshot(root, snapshot_root, args.from_snapshot)

    if args.segments is not None:
        max_gates = args.segments
    elif args.from_snapshot is not None:
        max_gates = 1
    else:
        max_gates = None
    stop_at_state = RunState(args.until) if args.until else None
    segment_mode = max_gates is not None or stop_at_state is not None
    # The default (no mode) --full run keeps today's end-to-end behavior; only
    # it takes automatic gate snapshots.
    auto_snapshot = not segment_mode and not args.no_snapshots

    try:
        summary = run_rehearsal(
            root=root,
            max_gates=max_gates,
            stop_at_state=stop_at_state,
            auto_snapshot=auto_snapshot,
            snapshot_root=snapshot_root,
        )
    except Exception:
        import traceback

        traceback.print_exc()
        print('STAGES_ON_FAILURE ' + json.dumps(_stages, indent=2, sort_keys=True))
        raise
    print(json.dumps(summary, indent=2, sort_keys=True))
    result = str(summary.get('result', ''))
    if result == 'RESUMABLE':
        print(
            'REHEARSAL_RESUMABLE: relaunch this command to resume the paused '
            'run from its durable checkpoint.',
            flush=True,
        )
    if result == 'FAIL':
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
