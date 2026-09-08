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


def run_rehearsal() -> dict[str, object]:
    global _stages
    stages: dict[str, object] = {}
    _stages = stages
    with tempfile.TemporaryDirectory(prefix='glasslab-orchestrator-rehearse-') as raw:
        root = Path(raw)
        repo = _create_repo(root)
        approved = root / 'knowledge-approved'
        approved.mkdir()
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
        engine.knowledge.ingest_source(
            source_type=SourceType.TECHNIQUE_CARD,
            path=str(approved / 'technique-card.md'),
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

        # Stage 1: protocol draft on the structured model (Coder-Next .17).
        try:
            protocol_action = next(
                action
                for action in store.list_actions(run.run_id)
                if action.type == 'approve_protocol'
                and action.approval_status == ApprovalStatus.PENDING
            )
            stages['protocol_draft'] = 'ok'
            engine.approve_action(
                protocol_action.action_id,
                reviewer='rehearse-human',
                reason='Rehearsal protocol approved.',
            )
        except Exception as exc:
            stages['protocol_draft'] = f'FAILED: {exc}'
            stages['state_at_failure'] = store.get_run(run.run_id).state.value
            stages['rejection_reasons'] = [
                str(a.reason)
                for a in store.list_actions(run.run_id)
                if a.type == 'propose_evaluation_contract'
                and a.approval_status == ApprovalStatus.REJECTED
            ]
            raise
        run = store.get_run(run.run_id)
        stages['after_protocol'] = run.state.value
        stages['proposed_evaluator_type'] = _proposal_evaluator_type(
            store, run.run_id
        )

        # Stage 2: contract candidate on Beaker (Coder-Next .17), then
        # promotion/bind. The evaluator_type must be task-specific, not a
        # generic template id, or promotion fails scientific compatibility.
        try:
            contract_action = next(
                action
                for action in store.list_actions(run.run_id)
                if action.type == 'propose_evaluation_contract'
                and action.approval_status == ApprovalStatus.PENDING
            )
            stages['contract_draft'] = 'ok'
            engine.approve_action(
                contract_action.action_id,
                reviewer='rehearse-human',
                reason='Rehearsal contract approved.',
            )
        except Exception as exc:
            stages['contract_draft'] = f'FAILED: {exc}'
            raise
        run = store.get_run(run.run_id)
        stages['after_contract'] = run.state.value
        stages['bound_contract_id'] = run.evaluation_contract_id

        # Stage 3: Beaker plan + implementation, then matrix approval.
        try:
            execution_action = next(
                action
                for action in store.list_actions(run.run_id)
                if action.type == 'submit_experiment_matrix'
                and action.approval_status == ApprovalStatus.PENDING
            )
            stages['implementation'] = 'ok'
            engine.approve_action(
                execution_action.action_id,
                reviewer='rehearse-human',
                reason='Rehearsal execution approved.',
            )
        except Exception as exc:
            stages['implementation'] = f'FAILED: {exc}'
            stages['state_at_failure'] = store.get_run(run.run_id).state.value
            stages['actions_at_failure'] = [
                f"{a.type}/{a.approval_status.value}"
                for a in store.list_actions(run.run_id)
            ]
            stages['rejection_reasons'] = [
                str(a.reason)
                for a in store.list_actions(run.run_id)
                if a.type == 'propose_evaluation_contract'
                and a.approval_status == ApprovalStatus.REJECTED
            ]
            stages['matrix_rejection_reasons'] = [
                str(a.reason)
                for a in store.list_actions(run.run_id)
                if a.type == 'submit_experiment_matrix'
                and a.approval_status == ApprovalStatus.REJECTED
            ]
            raise

        # Stage 4: fake cluster jobs complete.
        for job in store.list_jobs(run.run_id):
            assert job.external_run_id is not None
            cluster.complete(
                job.external_run_id,
                metrics={
                    'score': 0.8 if job.variant_name == 'candidate' else 0.6
                },
            )
        engine.reconcile_run(run.run_id)
        run = store.get_run(run.run_id)
        stages['jobs_executed'] = run.state.value

        # Stage 5: verification on the reasoning model (Thinking .18).
        try:
            report_action = next(
                action
                for action in store.list_actions(run.run_id)
                if action.type == 'accept_final_report'
                and action.approval_status == ApprovalStatus.PENDING
            )
            stages['verification_report'] = 'ok'
            engine.approve_action(
                report_action.action_id,
                reviewer='rehearse-human',
                reason='Rehearsal report accepted.',
            )
        except Exception as exc:
            stages['verification_report'] = f'FAILED: {exc}'
            raise
        run = store.get_run(run.run_id)
        stages['final_state'] = run.state.value

        packets = store.list_context_packets(run.run_id)
        stages['context_packets'] = len(packets)

        stages['result'] = 'PASS' if run.state is RunState.COMPLETE else 'FAIL'
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
        summary = _stages
        print('STAGES_ON_FAILURE ' + json.dumps(summary, indent=2, sort_keys=True))
        raise
    print(json.dumps(summary, indent=2, sort_keys=True))