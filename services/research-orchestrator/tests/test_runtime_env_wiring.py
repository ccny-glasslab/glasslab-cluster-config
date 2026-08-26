"""Agent subprocess environment allowlist wiring tests.

Prove both agent runtimes construct their child environment through
:mod:`app.runtime_env` and never pass orchestrator secrets to Popen.
Captures the env dict by stubbing ``subprocess.Popen`` to record it and
raise before the runtime's health-poll loop starts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.hermes_runtime import HermesProcessRuntime
from app.opencode_runtime import OpenCodeProcessRuntime
from app.schemas import AgentName

SECRET_VARS = {
    'GLASSLAB_ORCHESTRATOR_DISCORD_BOT_TOKEN': 'discord-token',
    'GLASSLAB_ORCHESTRATOR_OPERATOR_API_TOKEN': 'operator-token',
    'GLASSLAB_WORKFLOW_API_TOKEN': 'workflow-token',
    'GLASSLAB_ORCHESTRATOR_DISCORD_WEBHOOK_URL': 'https://hook.invalid',
}


@pytest.fixture()
def secret_parent_env(monkeypatch) -> None:
    for name, value in SECRET_VARS.items():
        monkeypatch.setenv(name, value)


def _captured_popen(monkeypatch) -> dict[str, str]:
    captured: dict[str, str] = {}

    class _RecordingPopen:
        def __init__(self, argv, *, cwd, env, stdout, stderr, text):
            captured.update(env)
            raise RuntimeError('env captured')

    monkeypatch.setattr(
        'app.opencode_runtime.subprocess.Popen',
        _RecordingPopen,
    )
    monkeypatch.setattr(
        'app.hermes_runtime.subprocess.Popen',
        _RecordingPopen,
    )
    return captured


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=str(tmp_path / 'orchestrator.db'),
        workspace_root=str(tmp_path / 'runs'),
        artifact_root=str(tmp_path / 'artifacts'),
        approved_repo_path=str(tmp_path / 'repo'),
        approved_repo_ref='main',
        evaluation_contract_root=str(
            Path(__file__).resolve().parents[1] / 'evaluation-contracts'
        ),
        permitted_job_images=['glasslab-runner:test'],
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
        knowledge_root=str(tmp_path / 'knowledge'),
        one_active_run=False,
        maximum_parallel_jobs=2,
    )


def test_opencode_child_env_contains_no_secrets(
    tmp_path: Path, monkeypatch, secret_parent_env
) -> None:
    monkeypatch.setenv('OPENCODE_API_KEY', 'model-key')
    captured = _captured_popen(monkeypatch)
    workspace = tmp_path / 'runs' / 'run-1' / 'honeydew'
    workspace.mkdir(parents=True, exist_ok=True)

    runtime = OpenCodeProcessRuntime(_settings(tmp_path))
    with pytest.raises(RuntimeError, match='env captured'):
        runtime._start_process(
            run_id='run-1',
            agent=AgentName.HONEYDEW,
            workspace=workspace,
        )

    for leaked in SECRET_VARS:
        assert leaked not in captured, f'secret {leaked} reached OpenCode Popen'
    assert captured.get('OPENCODE_API_KEY') == 'model-key'
    assert captured.get('HOME')  # per-run HOME is set
    assert captured.get('OPENCODE_SERVER_USERNAME') == 'glasslab-orchestrator'


def test_hermes_child_env_contains_no_secrets(
    tmp_path: Path, monkeypatch, secret_parent_env
) -> None:
    monkeypatch.setenv('CUSTOM_API_KEY', 'model-key')
    captured = _captured_popen(monkeypatch)
    workspace = tmp_path / 'runs' / 'run-1' / 'beaker'
    workspace.mkdir(parents=True, exist_ok=True)

    runtime = HermesProcessRuntime(_settings(tmp_path))
    with pytest.raises(RuntimeError, match='env captured'):
        runtime._start_process(
            run_id='run-1',
            agent=AgentName.BEAKER,
            workspace=workspace,
        )

    for leaked in SECRET_VARS:
        assert leaked not in captured, f'secret {leaked} reached Hermes Popen'
    assert captured.get('CUSTOM_API_KEY') == 'model-key'
    assert captured.get('HERMES_HOME')
    assert captured.get('API_SERVER_ENABLED') == 'true'