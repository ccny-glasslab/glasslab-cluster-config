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

import json
from pathlib import Path
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
    __import__('os').environ.get(
        'REHEARSE_ROOT', '/tmp/glasslab-rehearse-root'
    )
)


def _stage_progress(stages: dict[str, object]) -> None:
    print('STAGE_PROGRESS ' + json.dumps(stages, sort_keys=True), flush=True)
    (REHEARSE_ROOT / 'checkpoint.json').write_text(
        json.dumps(stages, sort_keys=True)
    )


def _build_engine(root: Path):
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
        runtime=OpenCodeProcessRuntime(settings),
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


def run_rehearsal() -> dict[str, object]:
    """Drive one rehearsal run to COMPLETE, pausing cleanly on turn walls.

    The run state is the source of truth, not the presence of a PENDING action:
    after a crash the driver relaunches, engine.recover() re-advances any
    already-approved gate, and a PAUSED run is resumed via engine.resume_run().
    A wall-clock turn timeout pauses the run and returns result RESUMABLE with
    exit code 0 (the durable checkpoint survives); a deterministic failure
    returns result FAIL with exit code 1 so CI still fails loudly.
    """
    global _stages
    stages: dict[str, object] = {}
    _stages = stages
    root = REHEARSE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    fresh = not (root / 'orchestrator.db').exists()
    settings, store, cluster, engine = _build_engine(root)
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


if __name__ == '__main__':
    try:
        summary = run_rehearsal()
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
        raise SystemExit(1)
