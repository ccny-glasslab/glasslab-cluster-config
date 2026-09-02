"""Agent runtime backend selection (issue #98).

The orchestrator retains OpenCode as the selected agent runtime; Hermes
remains only as an explicit opt-in rollback backend. These tests lock the
selector contract so the backend flip in the deployment configmap can never
silently regress.
"""

from __future__ import annotations

import json

from app.config import Settings
from app.hermes_runtime import HermesProcessRuntime
from app.main import build_agent_runtime
from app.opencode_runtime import OpenCodeProcessRuntime
from app.schemas import AgentName


def _read_opencode_config(
    workspace, agent: AgentName,
):
    return json.loads(
        (
            workspace.parent
            / 'runtime'
            / agent.value
            / 'config'
            / 'opencode'
            / 'opencode.json'
        ).read_text()
    )


def test_build_agent_runtime_defaults_to_opencode() -> None:
    settings = Settings(agent_runtime_backend='opencode')
    runtime = build_agent_runtime(settings)
    assert isinstance(runtime, OpenCodeProcessRuntime)
    assert not isinstance(runtime, HermesProcessRuntime)


def test_build_agent_runtime_hermes_is_explicit_opt_in() -> None:
    settings = Settings(agent_runtime_backend='hermes')
    runtime = build_agent_runtime(settings)
    assert isinstance(runtime, HermesProcessRuntime)

def test_opencode_runtime_config_uses_per_agent_model(tmp_path: Path) -> None:
    settings = Settings(
        agent_model_honeydew='mlx-community/Qwen3.6-27B-4bit',
        agent_model_beaker='mlx-community/Qwen3-Coder-Next-4bit',
    )
    runtime = OpenCodeProcessRuntime(settings)
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    runtime._write_runtime_config(
        run_id='run-1',
        agent=AgentName.HONEYDEW,
        workspace=workspace,
    )
    config = _read_opencode_config(workspace, AgentName.HONEYDEW)
    assert config['model'] == (
        'exo/mlx-community/Qwen3.6-27B-4bit'
    )
    runtime._write_runtime_config(
        run_id='run-2',
        agent=AgentName.BEAKER,
        workspace=workspace,
    )
    config = _read_opencode_config(workspace, AgentName.BEAKER)
    assert config['model'] == (
        'exo/mlx-community/Qwen3-Coder-Next-4bit'
    )


def test_agent_model_falls_back_to_shared_default(tmp_path: Path) -> None:
    settings = Settings(agent_model_name='mlx-community/Shared-Model-4bit')
    runtime = OpenCodeProcessRuntime(settings)
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    runtime._write_runtime_config(
        run_id='run-1',
        agent=AgentName.HONEYDEW,
        workspace=workspace,
    )
    config = _read_opencode_config(workspace, AgentName.HONEYDEW)
    assert config['model'] == 'exo/mlx-community/Shared-Model-4bit'
