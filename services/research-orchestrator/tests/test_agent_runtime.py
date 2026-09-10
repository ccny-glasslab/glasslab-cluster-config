"""Agent runtime backend selection (issue #98).

The orchestrator retains OpenCode as the selected agent runtime; Hermes
remains only as an explicit opt-in rollback backend. These tests lock the
selector contract so the backend flip in the deployment configmap can never
silently regress.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.hermes_runtime import HermesProcessRuntime
from app.main import build_agent_runtime
from app.opencode_runtime import OpenCodeProcessRuntime, OpenCodeRuntimeError
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


def test_opencode_runtime_config_uses_per_agent_base_url(tmp_path: Path) -> None:
    settings = Settings(
        agent_base_url_honeydew='http://192.168.1.18:52416/v1',
        agent_base_url_beaker='http://192.168.1.17:52416/v1',
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
    provider = config['provider']['exo']
    assert provider['options']['baseURL'] == 'http://192.168.1.18:52416/v1'
    runtime._write_runtime_config(
        run_id='run-2',
        agent=AgentName.BEAKER,
        workspace=workspace,
    )
    config = _read_opencode_config(workspace, AgentName.BEAKER)
    provider = config['provider']['exo']
    assert provider['options']['baseURL'] == 'http://192.168.1.17:52416/v1'


def test_opencode_runtime_config_base_url_falls_back_to_shared(tmp_path: Path) -> None:
    settings = Settings(qwen_base_url='http://192.168.1.17:52415/v1')
    runtime = OpenCodeProcessRuntime(settings)
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    runtime._write_runtime_config(
        run_id='run-1',
        agent=AgentName.HONEYDEW,
        workspace=workspace,
    )
    config = _read_opencode_config(workspace, AgentName.HONEYDEW)
    provider = config['provider']['exo']
    assert provider['options']['baseURL'] == 'http://192.168.1.17:52415/v1'


def test_opencode_runtime_config_sets_max_output_tokens(tmp_path: Path) -> None:
    settings = Settings(agent_model_max_output_tokens=8192)
    runtime = OpenCodeProcessRuntime(settings)
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    runtime._write_runtime_config(
        run_id='run-1',
        agent=AgentName.HONEYDEW,
        workspace=workspace,
    )
    config = _read_opencode_config(workspace, AgentName.HONEYDEW)
    provider = config['provider']['exo']
    model_cfg = next(iter(provider['models'].values()))
    assert model_cfg['options']['maxOutputTokens'] == 8192


class _ExitedProcess:
    """A process that has already exited when startup first polls it."""

    returncode = 1

    def poll(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def test_opencode_crashed_startup_stops_the_leaked_handle(
    tmp_path, monkeypatch,
) -> None:
    runtime = OpenCodeProcessRuntime(
        Settings(opencode_shared_cache_root=str(tmp_path / 'shared-cache'))
    )
    workspace = tmp_path / 'run-1' / 'honeydew-worktree'
    workspace.mkdir(parents=True)
    stopped: list[object] = []
    real_stop = runtime._stop_handle

    def record_stop(handle):
        stopped.append(handle)
        real_stop(handle)

    monkeypatch.setattr(runtime, '_stop_handle', record_stop)
    monkeypatch.setattr(
        'app.opencode_runtime.subprocess.Popen',
        lambda *args, **kwargs: _ExitedProcess(),
    )

    with pytest.raises(OpenCodeRuntimeError, match='exited during startup'):
        runtime._start_process(
            run_id='run-1',
            agent=AgentName.HONEYDEW,
            workspace=workspace,
        )

    assert len(stopped) == 1
    assert stopped[0].log_handle.closed is True


def test_opencode_ensure_session_raises_on_validation_5xx(
    tmp_path, monkeypatch,
) -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    runtime = OpenCodeProcessRuntime(Settings())
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    handle = SimpleNamespace(
        base_url='http://opencode.test',
        password='password',
        runtime_id='runtime-1',
    )
    monkeypatch.setattr(runtime, '_start_process', lambda **_: handle)
    monkeypatch.setattr(
        runtime,
        '_client',
        lambda _: httpx.Client(
            base_url=handle.base_url,
            transport=httpx.MockTransport(respond),
        ),
    )

    with pytest.raises(OpenCodeRuntimeError) as excinfo:
        runtime.ensure_session(
            run_id='run-1',
            agent=AgentName.HONEYDEW,
            workspace=workspace,
            existing_session_id='session-1',
        )
    assert excinfo.value.failure_class == 'network'


def test_opencode_ensure_session_rotates_only_on_404(
    tmp_path, monkeypatch,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == 'GET':
            return httpx.Response(404)
        return httpx.Response(200, json={'id': 'session-new'})

    runtime = OpenCodeProcessRuntime(Settings())
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    handle = SimpleNamespace(
        base_url='http://opencode.test',
        password='password',
        runtime_id='runtime-1',
    )
    monkeypatch.setattr(runtime, '_start_process', lambda **_: handle)
    monkeypatch.setattr(
        runtime,
        '_client',
        lambda _: httpx.Client(
            base_url=handle.base_url,
            transport=httpx.MockTransport(respond),
        ),
    )

    session = runtime.ensure_session(
        run_id='run-1',
        agent=AgentName.HONEYDEW,
        workspace=workspace,
        existing_session_id='session-1',
    )

    assert session.session_id == 'session-new'
    assert [request.method for request in requests] == ['GET', 'POST']
