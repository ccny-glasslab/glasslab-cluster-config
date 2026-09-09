"""Shared fixtures for the research-orchestrator test suite.

Builds a production-wired engine against a temp git repo and fake runtimes:
real SqliteStore, FakeClusterExecutor, ScriptedMockRuntime, and a disabled
Discord adapter, so no test ever touches a live cluster or model. The
orchestrator_bundle fixture returns (settings, store, cluster, runtime,
engine) in that fixed order.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

import pytest


# Env vars are set before the app imports below because app.config reads
# these at import time; setdefault keeps a real environment's overrides.
_IMPORT_ROOT = Path(tempfile.mkdtemp(prefix='glasslab-orchestrator-import-'))
for _name, _relative in {
    'DATABASE_PATH': 'orchestrator.db',
    'WORKSPACE_ROOT': 'runs',
    'ARTIFACT_ROOT': 'artifacts',
    'PROMOTED_CONTRACT_ROOT': 'trusted-contracts/bundles',
    'SEALED_CONTRACT_CANDIDATE_ROOT': 'contract-candidates',
    'TRUSTED_CONTRACT_CATALOG_PATH': 'trusted-contracts/catalog.json',
    'SHARED_MOUNT_ROOT': '.',
    'TASK_BUNDLE_ROOT': 'task-bundles',
    'TASK_ASSET_ROOT': 'task-assets',
    'DATASET_UPLOAD_ROOT': 'dataset-uploads',
    'BENCHMARK_DATASET_CATALOG_PATH': 'datasets/catalog.json',
}.items():
    os.environ.setdefault(
        f'GLASSLAB_ORCHESTRATOR_{_name}',
        str((_IMPORT_ROOT / _relative).resolve()),
    )

from app.cluster import FakeClusterExecutor
from app.config import SERVICE_ROOT, Settings
from app.contract_candidates import ContractCandidateManager
from app.contracts import EvaluationContractResolver
from app.discord_adapter import DisabledDiscordAdapter
from app.engine import ResearchOrchestrator
from app.mock_runtime import ScriptedMockRuntime
from app.policy import ActionPolicy
from app.storage import SqliteStore
from app.workspaces import WorkspaceManager


RUNNER_IMAGE = 'ghcr.io/ccny-glasslab/glasslab-test-runner:test'


def create_test_repo(root: Path) -> Path:
    # Every fixture workspace needs a real git repository with a committed
    # baseline config: WorkspaceManager clones it and stages worktrees from it.
    repo = root / 'repo'
    repo.mkdir()
    subprocess.run(
        ['git', 'init', '-b', 'main'],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ['git', 'config', 'user.email', 'test@glasslab.local'],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ['git', 'config', 'user.name', 'Glasslab Test'],
        cwd=repo,
        check=True,
    )
    (repo / 'README.md').write_text('# Test repository\n')
    (repo / 'configs').mkdir()
    (repo / 'configs' / 'baseline.yaml').write_text('learning_rate: 0.0001\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(
        ['git', 'commit', '-m', 'Initialize'],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
def orchestrator_bundle(tmp_path):
    repo = create_test_repo(tmp_path)
    settings = Settings(
        database_path=str(tmp_path / 'orchestrator.db'),
        workspace_root=str(tmp_path / 'runs'),
        artifact_root=str(tmp_path / 'artifacts'),
        approved_repo_path=str(repo),
        approved_repo_ref='main',
        evaluation_contract_root=str(SERVICE_ROOT / 'evaluation-contracts'),
        permitted_job_images=[RUNNER_IMAGE],
        cluster_execution_mode='fake',
        promoted_contract_root=str(tmp_path / 'trusted-contracts'),
        sealed_contract_candidate_root=str(tmp_path / 'contract-candidates'),
        trusted_contract_catalog_path=str(
            tmp_path / 'trusted-contracts' / 'catalog.json'
        ),
        shared_mount_root=str(tmp_path),
        task_bundle_root=str(tmp_path / 'task-bundles'),
        task_asset_root=str(tmp_path / 'task-assets'),
        dataset_upload_root=str(tmp_path / 'dataset-uploads'),
        benchmark_dataset_catalog_path=str(tmp_path / 'datasets' / 'catalog.json'),
        one_active_run=False,
        maximum_parallel_jobs=2,
        # The test harness is a trusted local process; the fail-closed default
        # (require_operator_auth=True) applies to real deployments.
        require_operator_auth=False,
    )
    store = SqliteStore(settings.database_path)
    cluster = FakeClusterExecutor()
    runtime = ScriptedMockRuntime(runner_image=RUNNER_IMAGE)
    engine = ResearchOrchestrator(
        settings=settings,
        store=store,
        runtime=runtime,
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
    return settings, store, cluster, runtime, engine
