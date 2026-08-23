from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import secrets
import socket
import subprocess
import time
from typing import Any
from uuid import uuid4

import httpx
import yaml
from pydantic import ValidationError

from .config import Settings
from .opencode_runtime import AgentRuntime, RuntimeSession
from .schemas import AgentName, AgentTurnResult, TurnKind


class HermesRuntimeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure_class: str | None = None,
    ) -> None:
        super().__init__(message)
        # Machine-readable classifier so the engine can distinguish
        # malformed output from schema-invalid output from a wrong turn kind
        # in durable normalized events, without string-matching the message.
        self.failure_class = failure_class


@dataclass
class _HermesHandle:
    runtime_id: str
    run_id: str
    agent: AgentName
    workspace: Path
    base_url: str
    api_key: str
    process: subprocess.Popen[str]
    log_handle: Any


def _required_turn_kind(prompt: str) -> TurnKind | None:
    match = re.search(
        r'Set the JSON `kind` field to exactly `([^`]+)`\.',
        prompt,
    )
    if match is None:
        return None
    try:
        return TurnKind(match.group(1))
    except ValueError:
        return None


def _structured_prompt(
    prompt: str,
    *,
    required_kind: TurnKind | None = None,
) -> str:
    schema_object = AgentTurnResult.model_json_schema()
    if required_kind is not None:
        schema_object['properties']['kind'] = {
            'const': required_kind.value,
            'title': 'Kind',
            'type': 'string',
        }
    schema = json.dumps(schema_object, sort_keys=True)
    return (
        prompt
        + '\n\nReturn only one JSON object matching this schema after all '
        'workspace work is complete. Do not wrap it in Markdown fences.\n'
        + schema
    )


def _decode_structured_output(
    output: Any,
    *,
    required_kind: TurnKind | None = None,
) -> AgentTurnResult:
    if not isinstance(output, str):
        raise HermesRuntimeError(
            'Hermes run output was not text',
            failure_class='not_text',
        )
    candidate = output.strip()
    if candidate.startswith('```') and candidate.endswith('```'):
        lines = candidate.splitlines()
        candidate = '\n'.join(lines[1:-1]).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise HermesRuntimeError(
            'Hermes turn did not return a JSON object',
            failure_class='malformed_json',
        ) from exc
    try:
        result = AgentTurnResult.model_validate(payload)
    except ValidationError as exc:
        details = '; '.join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in exc.errors(include_url=False, include_input=False)[:8]
        )
        raise HermesRuntimeError(
            f'Hermes structured output failed validation: {details}',
            failure_class='schema_invalid',
        ) from exc
    if required_kind is not None and result.kind != required_kind:
        raise HermesRuntimeError(
            'Hermes structured output used kind '
            f'{result.kind.value}; expected {required_kind.value}',
            failure_class='wrong_kind',
        )
    return result


class HermesProcessRuntime(AgentRuntime):
    """One profile-scoped, loopback Hermes gateway per run and agent."""

    REQUIRED_CAPABILITIES = {
        'run_submission',
        'run_status',
        'run_stop',
        'session_resources',
    }

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._handles: dict[tuple[str, AgentName], _HermesHandle] = {}
        self._active_runs: dict[tuple[str, AgentName], str] = {}
        prompt_root = Path(__file__).resolve().parents[1] / 'prompts'
        self._system_prompts = {
            AgentName.HONEYDEW: (prompt_root / 'honeydew.md').read_text(),
            AgentName.BEAKER: (prompt_root / 'beaker.md').read_text(),
        }

    def _runtime_port(self) -> int:
        used = {
            int(handle.base_url.rsplit(':', 1)[1])
            for handle in self._handles.values()
            if handle.process.poll() is None
        }
        for port in range(
            self.settings.hermes_start_port,
            self.settings.hermes_start_port + 100,
        ):
            if port in used:
                continue
            with socket.socket() as probe:
                try:
                    probe.bind((self.settings.hermes_server_host, port))
                except OSError:
                    continue
            return port
        raise HermesRuntimeError('no Hermes runtime port is available')

    def _write_runtime_config(
        self,
        *,
        agent: AgentName,
        workspace: Path,
        port: int,
    ) -> Path:
        hermes_home = workspace.parent / 'runtime' / agent.value / 'hermes'
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / '.no-bundled-skills').touch()
        config = {
            'model': {
                'default': self.settings.qwen_model_name,
                'provider': 'custom',
                'base_url': self.settings.qwen_base_url,
                'api_key': '',
            },
            'terminal': {
                'backend': 'local',
                'cwd': str(workspace),
                'home_mode': 'profile',
                'persistent_shell': False,
                'timeout': self.settings.hermes_command_timeout_seconds,
            },
            'memory': {
                'memory_enabled': False,
                'user_profile_enabled': False,
                'write_approval': True,
            },
            'agent': {
                'disabled_toolsets': [
                    'browser',
                    'clarify',
                    'code_execution',
                    'cronjob',
                    'delegation',
                    'image_gen',
                    'memory',
                    'messaging',
                    'session_search',
                    'skills',
                    'tts',
                    'video',
                    'vision',
                    'web',
                ],
            },
            'tools': {
                'api_server': {
                    'enabled': ['file', 'terminal'],
                    'disabled': [],
                },
            },
            'approvals': {
                'mode': 'manual',
                'timeout': 30,
                'deny': [
                    'kubectl *',
                    'ssh *',
                    'scp *',
                    'docker *',
                    'podman *',
                    'pip *',
                    'pip3 *',
                    'python -m pip *',
                    'python3 -m pip *',
                    'uv pip *',
                    'apt *',
                    'apt-get *',
                    'npm *',
                    'npx *',
                    'yarn *',
                    'pnpm *',
                    'conda *',
                    'mamba *',
                    'brew *',
                    'git push*',
                    'gh pr create*',
                    '*secret*',
                ],
            },
            'security': {
                'redact_secrets': True,
            },
            'gateway': {
                'api_server': {
                    'enabled': True,
                    'host': self.settings.hermes_server_host,
                    'port': port,
                    'model_name': f'glasslab-{agent.value}',
                    'max_concurrent_runs': 1,
                },
            },
        }
        (hermes_home / 'config.yaml').write_text(
            yaml.safe_dump(config, sort_keys=False),
            encoding='utf-8',
        )
        (hermes_home / 'SOUL.md').write_text(
            self._system_prompts[agent],
            encoding='utf-8',
        )
        return hermes_home

    def _start_process(
        self,
        *,
        run_id: str,
        agent: AgentName,
        workspace: Path,
    ) -> _HermesHandle:
        key = (run_id, agent)
        existing = self._handles.get(key)
        if existing is not None and existing.process.poll() is None:
            return existing
        port = self._runtime_port()
        api_key = secrets.token_urlsafe(32)
        hermes_home = self._write_runtime_config(
            agent=agent,
            workspace=workspace,
            port=port,
        )
        log_path = hermes_home / 'gateway.log'
        log_handle = log_path.open('a', encoding='utf-8')
        environment = {
            **__import__('os').environ,
            'HERMES_HOME': str(hermes_home),
            'HERMES_WRITE_SAFE_ROOT': str(workspace),
            'HERMES_MAX_ITERATIONS': str(self.settings.hermes_max_iterations),
            'API_SERVER_ENABLED': 'true',
            'API_SERVER_HOST': self.settings.hermes_server_host,
            'API_SERVER_PORT': str(port),
            'API_SERVER_KEY': api_key,
        }
        try:
            process = subprocess.Popen(
                [self.settings.hermes_executable, 'gateway'],
                cwd=workspace,
                env=environment,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception:
            log_handle.close()
            raise
        handle = _HermesHandle(
            runtime_id=f'hermes-{agent.value}-{uuid4().hex[:12]}',
            run_id=run_id,
            agent=agent,
            workspace=workspace,
            base_url=f'http://{self.settings.hermes_server_host}:{port}',
            api_key=api_key,
            process=process,
            log_handle=log_handle,
        )
        self._handles[key] = handle
        deadline = time.monotonic() + self.settings.hermes_start_timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._stop_handle(handle)
                raise HermesRuntimeError(
                    f'Hermes process exited during startup; see {log_path}'
                )
            try:
                with self._client(handle) as client:
                    health = client.get('/health')
                    if health.status_code != 200:
                        time.sleep(0.1)
                        continue
                    capabilities = client.get('/v1/capabilities')
                    capabilities.raise_for_status()
                    features = capabilities.json().get('features', {})
                    missing = sorted(
                        name
                        for name in self.REQUIRED_CAPABILITIES
                        if features.get(name) is not True
                    )
                    if missing:
                        self._stop_handle(handle)
                        raise HermesRuntimeError(
                            'Hermes API lacks required capabilities: '
                            + ', '.join(missing)
                        )
                    return handle
            except httpx.HTTPError:
                time.sleep(0.1)
        self._stop_handle(handle)
        raise HermesRuntimeError('Hermes gateway did not become ready')

    def _client(self, handle: _HermesHandle) -> httpx.Client:
        return httpx.Client(
            base_url=handle.base_url,
            headers={'Authorization': f'Bearer {handle.api_key}'},
            timeout=self.settings.hermes_http_timeout_seconds,
            transport=self._transport,
        )

    def ensure_session(
        self,
        *,
        run_id: str,
        agent: AgentName,
        workspace: Path,
        existing_session_id: str | None,
    ) -> RuntimeSession:
        handle = self._start_process(
            run_id=run_id,
            agent=agent,
            workspace=workspace,
        )
        session_id = existing_session_id or f'glasslab-{agent.value}-{run_id}'
        with self._client(handle) as client:
            response = client.get(f'/api/sessions/{session_id}')
            if response.status_code == 404:
                response = client.post(
                    '/api/sessions',
                    json={
                        'id': session_id,
                        'title': f'Glasslab {agent.value} {run_id}',
                    },
                )
            response.raise_for_status()
        return RuntimeSession(handle.runtime_id, session_id)

    def run_turn(
        self,
        *,
        run_id: str,
        agent: AgentName,
        workspace: Path,
        session_id: str,
        prompt: str,
    ) -> tuple[AgentTurnResult, str | None]:
        handle = self._start_process(
            run_id=run_id,
            agent=agent,
            workspace=workspace,
        )
        required_kind = _required_turn_kind(prompt)
        current_prompt = _structured_prompt(
            prompt,
            required_kind=required_kind,
        )
        attempts = self.settings.hermes_structured_repair_attempts + 1
        for attempt in range(attempts):
            with self._client(handle) as client:
                response = client.post(
                    '/v1/runs',
                    json={
                        'input': current_prompt,
                        'instructions': self._system_prompts[agent],
                        'session_id': session_id,
                        'provider': 'custom',
                        'model': self.settings.qwen_model_name,
                    },
                )
                response.raise_for_status()
                hermes_run_id = str(response.json()['run_id'])
            key = (run_id, agent)
            self._active_runs[key] = hermes_run_id
            try:
                output = self._wait_for_run(handle, hermes_run_id)
            finally:
                self._active_runs.pop(key, None)
            try:
                return _decode_structured_output(
                    output,
                    required_kind=required_kind,
                ), hermes_run_id
            except HermesRuntimeError as exc:
                if attempt + 1 >= attempts:
                    raise
                previous_output = (
                    output
                    if isinstance(output, str)
                    else json.dumps(output, sort_keys=True, default=str)
                )
                current_prompt = _structured_prompt(
                    'Correct only the structured result quoted below from '
                    'your previous completed turn. Do not repeat workspace '
                    'work. Preserve valid content, repair the identified '
                    'error, and return a complete object matching the '
                    'supplied schema.\n\n'
                    f'Validation error: {exc}\n'
                    'BEGIN PREVIOUS OUTPUT\n'
                    f'{previous_output}\n'
                    'END PREVIOUS OUTPUT',
                    required_kind=required_kind,
                )
        raise HermesRuntimeError('Hermes turn ended without a result')

    def _wait_for_run(self, handle: _HermesHandle, hermes_run_id: str) -> Any:
        deadline = time.monotonic() + self.settings.hermes_turn_timeout_seconds
        while time.monotonic() < deadline:
            with self._client(handle) as client:
                response = client.get(f'/v1/runs/{hermes_run_id}')
                response.raise_for_status()
                body = response.json()
            status = str(body.get('status', ''))
            if status == 'completed':
                return body.get('output')
            if status in {'failed', 'cancelled'}:
                raise HermesRuntimeError(
                    f'Hermes run {status}: {body.get("error", "no detail")}'
                )
            if status in {'awaiting_approval', 'approval_required'}:
                self._stop_run(handle, hermes_run_id)
                raise HermesRuntimeError(
                    'Hermes requested an inner tool approval; Glasslab does '
                    'not delegate approval authority to the agent runtime'
                )
            time.sleep(self.settings.hermes_poll_interval_seconds)
        self._stop_run(handle, hermes_run_id)
        raise HermesRuntimeError(
            'Hermes turn exceeded the hard wall-clock limit of '
            f'{self.settings.hermes_turn_timeout_seconds:g} seconds'
        )

    def _stop_run(self, handle: _HermesHandle, hermes_run_id: str) -> None:
        with self._client(handle) as client:
            response = client.post(f'/v1/runs/{hermes_run_id}/stop')
            if response.status_code not in {200, 404, 409}:
                response.raise_for_status()

    def abort(self, *, run_id: str, agent: AgentName, session_id: str) -> None:
        handle = self._handles.get((run_id, agent))
        hermes_run_id = self._active_runs.get((run_id, agent))
        if handle is not None and hermes_run_id is not None:
            self._stop_run(handle, hermes_run_id)

    @staticmethod
    def _stop_handle(handle: _HermesHandle) -> None:
        if handle.process.poll() is None:
            handle.process.terminate()
            try:
                handle.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                handle.process.kill()
                handle.process.wait(timeout=5)
        handle.log_handle.close()

    def release(self, *, run_id: str, agent: AgentName) -> None:
        handle = self._handles.pop((run_id, agent), None)
        self._active_runs.pop((run_id, agent), None)
        if handle is not None:
            self._stop_handle(handle)

    def close(self) -> None:
        for handle in list(self._handles.values()):
            self._stop_handle(handle)
        self._handles.clear()
        self._active_runs.clear()
