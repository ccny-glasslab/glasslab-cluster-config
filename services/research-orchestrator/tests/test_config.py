"""Per-agent model routing characterization tests (issue #319).

The per-agent model override feature (#319) was merged behavior-neutral with
no test coverage. These tests lock the routing contract: each agent's turns
run against its own model on the shared endpoint when the per-agent override
is set, otherwise the shared effective model applies to both agents.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml

from app.config import Settings
from app.hermes_runtime import HermesProcessRuntime, _HermesHandle
from app.opencode_runtime import OpenCodeProcessRuntime
from app.schemas import AgentName


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


def _handle(tmp_path: Path) -> _HermesHandle:
    workspace = tmp_path / 'honeydew-worktree'
    workspace.mkdir()
    return _HermesHandle(
        runtime_id='hermes-honeydew-test',
        run_id='run-test',
        agent=AgentName.HONEYDEW,
        workspace=workspace,
        base_url='http://hermes.test',
        api_key='test-api-key',
        process=FakeProcess(),  # type: ignore[arg-type]
        log_handle=(tmp_path / 'hermes.log').open('a'),
    )


def _read_opencode_config(workspace: Path, agent: AgentName) -> dict:
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


def _write_hermes_config(
    settings: Settings, workspace: Path, agent: AgentName
) -> dict:
    runtime = HermesProcessRuntime(settings)
    hermes_home = runtime._write_runtime_config(
        agent=agent,
        workspace=workspace,
        port=4310,
    )
    return yaml.safe_load((hermes_home / 'config.yaml').read_text())


def test_honeydew_uses_agent_model_honeydew_override() -> None:
    settings = Settings(
        agent_model_honeydew='mlx-community/Qwen3.6-27B-4bit',
        agent_model_beaker='mlx-community/Qwen3-Coder-Next-4bit',
    )
    assert settings.agent_model_for(AgentName.HONEYDEW) == (
        'mlx-community/Qwen3.6-27B-4bit'
    )


def test_beaker_uses_agent_model_beaker_override() -> None:
    settings = Settings(
        agent_model_honeydew='mlx-community/Qwen3.6-27B-4bit',
        agent_model_beaker='mlx-community/Qwen3-Coder-Next-4bit',
    )
    assert settings.agent_model_for(AgentName.BEAKER) == (
        'mlx-community/Qwen3-Coder-Next-4bit'
    )


def test_agent_model_falls_back_to_agent_model_name() -> None:
    settings = Settings(agent_model_name='mlx-community/Shared-Model-4bit')
    assert settings.agent_model_for(AgentName.HONEYDEW) == (
        'mlx-community/Shared-Model-4bit'
    )
    assert settings.agent_model_for(AgentName.BEAKER) == (
        'mlx-community/Shared-Model-4bit'
    )


def test_agent_model_falls_back_to_qwen_model_name_when_unset() -> None:
    settings = Settings(
        agent_model_name=None,
        qwen_model_name='mlx-community/Qwen3-Coder-Next-4bit',
    )
    assert settings.agent_model_for(AgentName.HONEYDEW) == (
        'mlx-community/Qwen3-Coder-Next-4bit'
    )
    assert settings.agent_model_for(AgentName.BEAKER) == (
        'mlx-community/Qwen3-Coder-Next-4bit'
    )


def test_per_agent_override_wins_over_shared_agent_model_name() -> None:
    settings = Settings(
        agent_model_name='mlx-community/Shared-Model-4bit',
        agent_model_honeydew='mlx-community/Qwen3.6-27B-4bit',
        agent_model_beaker='mlx-community/Qwen3-Coder-Next-4bit',
    )
    assert settings.agent_model_for(AgentName.HONEYDEW) == (
        'mlx-community/Qwen3.6-27B-4bit'
    )
    assert settings.agent_model_for(AgentName.BEAKER) == (
        'mlx-community/Qwen3-Coder-Next-4bit'
    )


def test_opencode_runtime_config_passes_per_agent_model(tmp_path: Path) -> None:
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
    assert config['model'] == 'exo/mlx-community/Qwen3.6-27B-4bit'
    runtime._write_runtime_config(
        run_id='run-2',
        agent=AgentName.BEAKER,
        workspace=workspace,
    )
    config = _read_opencode_config(workspace, AgentName.BEAKER)
    assert config['model'] == 'exo/mlx-community/Qwen3-Coder-Next-4bit'


def test_hermes_runtime_config_passes_per_agent_model(tmp_path: Path) -> None:
    settings = Settings(
        agent_model_honeydew='mlx-community/Qwen3.6-27B-4bit',
        agent_model_beaker='mlx-community/Qwen3-Coder-Next-4bit',
    )
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    honeydew_config = _write_hermes_config(
        settings, workspace, AgentName.HONEYDEW
    )
    assert honeydew_config['model']['default'] == (
        'mlx-community/Qwen3.6-27B-4bit'
    )
    beaker_config = _write_hermes_config(settings, workspace, AgentName.BEAKER)
    assert beaker_config['model']['default'] == (
        'mlx-community/Qwen3-Coder-Next-4bit'
    )


def test_hermes_turn_payload_passes_per_agent_model(tmp_path: Path) -> None:
    submitted_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == 'POST' and request.url.path == '/v1/runs':
            submitted_models.append(json.loads(request.content)['model'])
            return httpx.Response(200, json={'run_id': 'hermes-run-1'})
        if request.method == 'GET' and request.url.path == (
            '/v1/runs/hermes-run-1'
        ):
            return httpx.Response(
                200,
                json={
                    'status': 'completed',
                    'output': json.dumps(
                        {
                            'kind': 'protocol_draft',
                            'summary': 'Drafted the protocol.',
                            'produced_files': [],
                        }
                    ),
                },
            )
        raise AssertionError(f'unexpected request: {request.method} {request.url}')

    settings = Settings(
        agent_model_honeydew='mlx-community/Qwen3.6-27B-4bit',
        agent_model_beaker='mlx-community/Qwen3-Coder-Next-4bit',
        hermes_poll_interval_seconds=0,
        hermes_structured_repair_attempts=0,
    )
    runtime = HermesProcessRuntime(
        settings,
        transport=httpx.MockTransport(handler),
    )
    handle = _handle(tmp_path)
    runtime._start_process = lambda **_kwargs: handle  # type: ignore[method-assign]

    runtime.run_turn(
        run_id='run-test',
        agent=AgentName.HONEYDEW,
        workspace=handle.workspace,
        session_id='glasslab-honeydew-run-test',
        prompt='Draft program.md.',
    )

    assert submitted_models == ['mlx-community/Qwen3.6-27B-4bit']