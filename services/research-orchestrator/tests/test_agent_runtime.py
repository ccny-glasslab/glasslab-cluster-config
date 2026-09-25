"""Agent runtime backend selection (issue #98).

The orchestrator retains OpenCode as the selected agent runtime; Hermes
remains only as an explicit opt-in rollback backend. These tests lock the
selector contract so the backend flip in the deployment configmap can never
silently regress.
"""

from __future__ import annotations

from hashlib import sha256
import json
from types import SimpleNamespace
import threading
import time

import httpx
import pytest

from app import opencode_runtime
from app.config import Settings
from app.hermes_runtime import HermesProcessRuntime, _decode_structured_output
from app.main import build_agent_runtime
from app.opencode_runtime import OpenCodeProcessRuntime, OpenCodeRuntimeError
from app.schemas import AgentName
from app.task_bundles import TaskBundleError


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


def test_opencode_runtime_links_hosted_provider_auth(tmp_path: Path) -> None:
    auth_source = tmp_path / 'mounted-auth.json'
    auth_source.write_text('{"opencode-go": {"type": "api", "key": "secret"}}')
    settings = Settings(
        agent_model_provider_id='opencode-go',
        agent_model_name='deepseek-v4.1-flash',
        opencode_auth_json_path=str(auth_source),
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
    assert config['model'] == 'opencode-go/deepseek-v4.1-flash'
    assert 'provider' not in config
    link = (
        workspace.parent
        / 'runtime'
        / AgentName.HONEYDEW.value
        / 'data'
        / 'opencode'
        / 'auth.json'
    )
    assert link.is_symlink()
    assert link.resolve() == auth_source.resolve()


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


def test_opencode_runtime_port_is_reserved_until_handle_registered(
    tmp_path,
) -> None:
    runtime = OpenCodeProcessRuntime(
        Settings(opencode_shared_cache_root=str(tmp_path / 'shared-cache'))
    )
    first = runtime._runtime_port()
    second = runtime._runtime_port()
    assert first != second


def _turn_structured_with_source_url_asset() -> dict:
    return {
        'kind': 'protocol_draft',
        'summary': 'Drafted a protocol over URL-declared task assets.',
        'task_spec_proposal': {
            'schema_version': 'glasslab-task-spec-v1',
            'display_name': 'URL-asset protocol task',
            'runtime_profile': 'cpu-ml-standard-v1',
            'assets': [
                {
                    'name': 'titanic_train',
                    'role': 'training data',
                    'source_url': (
                        'https://example.com/kaggle-titanic/train.csv'
                    ),
                    'contains_labels': True,
                }
            ],
            'rationale': 'The problem declares these bytes by public URL.',
        },
    }


def _mock_opencode_runtime(monkeypatch, tmp_path, structured: dict):
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'info': {
                    'id': 'message-source-url',
                    'structured': structured,
                }
            },
        )

    runtime = OpenCodeProcessRuntime(Settings())
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    handle = SimpleNamespace(
        base_url='http://opencode.test',
        password='password',
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
    return runtime, workspace


def test_opencode_turn_preparers_establish_source_url_checksum(
    tmp_path, monkeypatch,
) -> None:
    # The model cannot know the checksum of a remote URL, so the engine passes
    # a result preparer that fetches the bytes and populates expected_sha256
    # before AgentTurnResult validation runs. The prepared digest is the hash
    # of the bytes actually served.
    served = b'PassengerId,Pclass,Survived\n1,3,0\n'
    runtime, workspace = _mock_opencode_runtime(
        monkeypatch, tmp_path, _turn_structured_with_source_url_asset()
    )
    observed: list[dict] = []

    def preparer(structured: dict) -> dict:
        observed.append(structured)
        proposal = structured['task_spec_proposal']
        assert proposal['assets'][0].get('expected_sha256') is None
        return {
            **structured,
            'task_spec_proposal': {
                **proposal,
                'assets': [
                    {
                        **proposal['assets'][0],
                        'expected_sha256': sha256(served).hexdigest(),
                    }
                ],
            },
        }

    result, message_id = runtime.run_turn(
        run_id='run-1',
        agent=AgentName.HONEYDEW,
        workspace=workspace,
        session_id='session-1',
        prompt='Draft a protocol.',
        result_preparers=(preparer,),
    )

    assert message_id == 'message-source-url'
    assert observed, 'the preparer must see the raw structured payload'
    asset = result.task_spec_proposal.assets[0]
    assert asset.expected_sha256 == sha256(served).hexdigest()


def test_opencode_turn_without_preparer_still_rejects_unverified_source_url(
    tmp_path, monkeypatch,
) -> None:
    # C6 guard: the rule is not weakened. Without the fetch path there is no
    # verified digest, and the raw proposal must still fail validation.
    runtime, workspace = _mock_opencode_runtime(
        monkeypatch, tmp_path, _turn_structured_with_source_url_asset()
    )

    with pytest.raises(OpenCodeRuntimeError) as excinfo:
        runtime.run_turn(
            run_id='run-1',
            agent=AgentName.HONEYDEW,
            workspace=workspace,
            session_id='session-1',
            prompt='Draft a protocol.',
        )

    assert excinfo.value.failure_class == 'validation'
    assert 'expected_sha256' in str(excinfo.value)


def test_opencode_turn_preparer_failure_is_actionable_not_a_validation_error(
    tmp_path, monkeypatch,
) -> None:
    # A URL that cannot be fetched and verified is deterministic: the model
    # cannot repair it, so the preparer's own actionable error must propagate
    # instead of being collapsed into a generic validation failure.
    runtime, workspace = _mock_opencode_runtime(
        monkeypatch, tmp_path, _turn_structured_with_source_url_asset()
    )

    def preparer(_: dict) -> dict:
        raise TaskBundleError(
            'source_url asset `titanic_train` could not be fetched and '
            'verified: URL host resolves to a non-public address: 127.0.0.1'
        )

    with pytest.raises(TaskBundleError, match='titanic_train') as excinfo:
        runtime.run_turn(
            run_id='run-1',
            agent=AgentName.HONEYDEW,
            workspace=workspace,
            session_id='session-1',
            prompt='Draft a protocol.',
            result_preparers=(preparer,),
        )

    assert 'non-public' in str(excinfo.value)


def test_hermes_decode_applies_result_preparers_before_validation() -> None:
    # The rollback backend shares the same result-prepare seam: a preparer
    # populates the digest before the payload is validated against the schema.
    payload = _turn_structured_with_source_url_asset()

    def preparer(structured: dict) -> dict:
        proposal = structured['task_spec_proposal']
        return {
            **structured,
            'task_spec_proposal': {
                **proposal,
                'assets': [
                    {
                        **proposal['assets'][0],
                        'expected_sha256': 'a' * 64,
                    }
                ],
            },
        }

    result = _decode_structured_output(
        json.dumps(payload),
        result_preparers=(preparer,),
    )

    assert result.task_spec_proposal.assets[0].expected_sha256 == 'a' * 64


class _AbortPostRecorder:
    def __init__(self, posts: list[tuple[str, dict]]) -> None:
        self._posts = posts

    def __enter__(self) -> '_AbortPostRecorder':
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def post(self, url: str, **kwargs: object) -> SimpleNamespace:
        self._posts.append((url, kwargs))
        return SimpleNamespace(status_code=200)


def _runtime_with_blocked_turn(
    monkeypatch, tmp_path, release: threading.Event,
):
    runtime = OpenCodeProcessRuntime(
        Settings(opencode_turn_timeout_seconds=0.1)
    )
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    handle = SimpleNamespace(
        base_url='http://opencode.test',
        password='password',
    )
    monkeypatch.setattr(runtime, '_start_process', lambda **_: handle)
    monkeypatch.setattr(runtime, '_run_turn_request_loop', lambda **_: release.wait())
    monkeypatch.setattr(
        opencode_runtime, '_TURN_TIMEOUT_BUFFER_SECONDS', 0.1
    )
    return runtime, workspace


def test_opencode_run_turn_hard_wall_clock_raises_turn_timeout(
    tmp_path, monkeypatch,
) -> None:
    # Given a turn request that never returns, When the hard wall-clock budget
    # expires, Then run_turn raises the classified timeout, attempts the
    # session abort, and unwinds without waiting for the stuck worker.
    release = threading.Event()
    runtime, workspace = _runtime_with_blocked_turn(
        monkeypatch, tmp_path, release
    )
    abort_posts: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        httpx, 'Client', lambda **_: _AbortPostRecorder(abort_posts)
    )

    started = time.monotonic()
    try:
        with pytest.raises(OpenCodeRuntimeError) as excinfo:
            runtime.run_turn(
                run_id='run-1',
                agent=AgentName.BEAKER,
                workspace=workspace,
                session_id='session-1',
                prompt='Implement the experiment.',
            )
    finally:
        release.set()

    assert time.monotonic() - started < 5
    assert excinfo.value.failure_class == 'turn_timeout'
    assert 'hard wall-clock limit' in str(excinfo.value)
    assert [
        url for url, _ in abort_posts if url.endswith('/session/session-1/abort')
    ]


def test_opencode_run_turn_propagates_worker_error_unchanged(
    tmp_path, monkeypatch,
) -> None:
    runtime = OpenCodeProcessRuntime(Settings())
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    handle = SimpleNamespace(
        base_url='http://opencode.test',
        password='password',
    )
    monkeypatch.setattr(runtime, '_start_process', lambda **_: handle)
    failure = OpenCodeRuntimeError(
        'OpenCode provider error: model overloaded',
        failure_class='provider',
        details={'provider': 'exo'},
    )

    def _raise(**_: object) -> None:
        raise failure

    monkeypatch.setattr(runtime, '_run_turn_request_loop', _raise)

    with pytest.raises(OpenCodeRuntimeError) as excinfo:
        runtime.run_turn(
            run_id='run-1',
            agent=AgentName.BEAKER,
            workspace=workspace,
            session_id='session-1',
            prompt='Run.',
        )

    assert excinfo.value is failure
    assert excinfo.value.failure_class == 'provider'
    assert excinfo.value.details == {'provider': 'exo'}


def test_opencode_run_turn_classifies_raw_http_timeout_as_turn_timeout(
    tmp_path, monkeypatch,
) -> None:
    # The request's own read timeout can win the race with the join deadline on
    # a fully stalled socket; it must surface as a classified turn_timeout (so
    # the engine auto-resumes) rather than a generic retryable httpx error.
    runtime = OpenCodeProcessRuntime(Settings())
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    handle = SimpleNamespace(
        base_url='http://opencode.test',
        password='password',
    )
    monkeypatch.setattr(runtime, '_start_process', lambda **_: handle)

    def _timeout(**_: object) -> None:
        raise httpx.ReadTimeout('stalled')

    monkeypatch.setattr(runtime, '_run_turn_request_loop', _timeout)

    with pytest.raises(OpenCodeRuntimeError) as excinfo:
        runtime.run_turn(
            run_id='run-1',
            agent=AgentName.BEAKER,
            workspace=workspace,
            session_id='session-1',
            prompt='Run.',
        )

    assert excinfo.value.failure_class == 'turn_timeout'
    assert excinfo.value.details == {'timeout_class': 'ReadTimeout'}


def test_opencode_run_turn_surfaces_watchdog_abort_while_blocked(
    tmp_path, monkeypatch,
) -> None:
    # The watchdog records a classified abort while the request is still
    # blocked; the hard timeout must surface that classification instead of
    # synthesizing a generic turn_timeout.
    release = threading.Event()
    runtime, workspace = _runtime_with_blocked_turn(
        monkeypatch, tmp_path, release
    )

    def _abort_from_watchdog(*, abort_reasons: list, **_: object) -> None:
        abort_reasons.append(
            SimpleNamespace(
                reason=(
                    'OpenCode turn aborted after 6 identical terminal tool calls'
                ),
                failure_class='repeated_tool_loop',
                details={'tool': 'bash', 'count': 6},
            )
        )

    monkeypatch.setattr(runtime, '_watch_turn', _abort_from_watchdog)
    monkeypatch.setattr(httpx, 'Client', lambda **_: _AbortPostRecorder([]))

    try:
        with pytest.raises(OpenCodeRuntimeError) as excinfo:
            runtime.run_turn(
                run_id='run-1',
                agent=AgentName.BEAKER,
                workspace=workspace,
                session_id='session-1',
                prompt='Run.',
            )
    finally:
        release.set()

    assert excinfo.value.failure_class == 'repeated_tool_loop'
    assert 'identical terminal tool calls' in str(excinfo.value)
    assert excinfo.value.details == {'tool': 'bash', 'count': 6}
