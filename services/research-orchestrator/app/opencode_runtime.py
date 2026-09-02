"""Headless OpenCode runtime adapter.

Owns one isolated OpenCode server process per (run_id, agent) pair and drives
structured agent turns through the local HTTP API. Session ids are persisted by
the engine so a restarted process can re-attach to the same conversation;
runtime ids are ephemeral process instances and are only used to rebuild the
session handle after recovery. The model's free text is never trusted: every
turn must validate against the AgentTurnResult JSON schema, and declared
write_file actions are applied by the orchestrator only after a strict
workspace and purpose check.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
from typing import Any, Iterator
from uuid import uuid4

import httpx
from pydantic import ValidationError

from .config import Settings
from .runtime_env import build_agent_environment
from .schemas import AgentName, AgentTurnResult, ProducedFile


class OpenCodeRuntimeError(RuntimeError):
    """A surfaced OpenCode runtime failure.

    ``failure_class`` classifies the failure for the engine's bounded retry
    decision: transient classes (startup, turn_timeout, repeated_tool_loop,
    provider, network) are retryable; deterministic classes (validation,
    kind_mismatch) are not.
    """

    def __init__(self, message: str, *, failure_class: str | None = None) -> None:
        super().__init__(message)
        self.failure_class = failure_class


@dataclass
class RuntimeSession:
    runtime_id: str
    session_id: str


class AgentRuntime(ABC):
    @abstractmethod
    def ensure_session(
        self,
        *,
        run_id: str,
        agent: AgentName,
        workspace: Path,
        existing_session_id: str | None,
    ) -> RuntimeSession:
        raise NotImplementedError

    @abstractmethod
    def run_turn(
        self,
        *,
        run_id: str,
        agent: AgentName,
        workspace: Path,
        session_id: str,
        prompt: str,
    ) -> tuple[AgentTurnResult, str | None]:
        raise NotImplementedError

    @abstractmethod
    def abort(self, *, run_id: str, agent: AgentName, session_id: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def release(self, *, run_id: str, agent: AgentName) -> None:
        return None


NORMALIZED_EVENT_TYPES = {
    # SSE stream is noisy; only the states that matter to the event log are
    # projected. Failures (`tool.error`, `session.error`) collapse onto the
    # same generic types as their successful counterparts because the run
    # outcome is decided by structured-output validation, not stream events.
    'message.part.delta': 'agent.output_updated',
    'tool.pending': 'agent.tool_started',
    'tool.running': 'agent.tool_started',
    'tool.completed': 'agent.tool_completed',
    'tool.error': 'agent.tool_completed',
    'permission.asked': 'agent.permission_requested',
    'session.idle': 'agent.turn_completed',
    'session.error': 'agent.turn_completed',
}


def normalize_opencode_event(
    raw: dict[str, Any],
    *,
    run_id: str,
    agent: AgentName,
) -> tuple[str, dict[str, Any]] | None:
    raw_type = str(raw.get('type', ''))
    normalized = NORMALIZED_EVENT_TYPES.get(raw_type)
    if normalized is None:
        # Unknown event types are deliberately dropped rather than recorded:
        # the log must stay bounded and only reflect workflow-relevant states.
        return None
    properties = raw.get('properties')
    if not isinstance(properties, dict):
        properties = {}
    return normalized, {
        'run_id': run_id,
        'agent': agent.value,
        'runtime_event_type': raw_type,
        'properties': properties,
    }


def extract_structured_output(body: dict[str, Any]) -> Any | None:
    """Accept the current OpenCode field and the older SDK spelling."""
    info = body.get('info')
    if not isinstance(info, dict):
        return None
    structured = info.get('structured')
    if structured is not None:
        return structured
    return info.get('structured_output')


def parse_json_text(raw: str) -> Any:
    """Parse a JSON-only response, tolerating one Markdown JSON fence."""
    candidate = raw.strip()
    if candidate.startswith('```') and candidate.endswith('```'):
        lines = candidate.splitlines()
        if len(lines) >= 3 and lines[0].strip() in {'```', '```json'}:
            candidate = '\n'.join(lines[1:-1]).strip()
    return json.loads(candidate)


def provider_error_message(body: dict[str, Any]) -> str | None:
    info = body.get('info')
    if not isinstance(info, dict):
        return None
    error = info.get('error')
    if not isinstance(error, dict):
        return None
    data = error.get('data')
    if isinstance(data, dict) and data.get('message'):
        return str(data['message'])
    if error.get('name'):
        return str(error['name'])
    return 'unknown provider error'


def _decode_json_field(value: Any, expected_type: type) -> Any:
    # Qwen-family models sometimes emit a nested object/array as a JSON string
    # instead of a real JSON value; decode it only when the result is exactly
    # the expected container type so a stray string is never coerced.
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    return decoded if isinstance(decoded, expected_type) else value


def normalize_structured_output(structured: Any) -> Any:
    """Adapt known Qwen/OpenCode JSON-schema encoding quirks."""
    if not isinstance(structured, dict):
        return structured
    normalized = dict(structured)
    for field, expected_type in (
        ('evaluation_contract_proposal', dict),
        ('task_spec_proposal', dict),
        ('claims', list),
        ('requested_actions', list),
        ('produced_files', list),
    ):
        if field in normalized:
            normalized[field] = _decode_json_field(
                normalized[field],
                expected_type,
            )

    proposal = normalized.get('evaluation_contract_proposal')
    if isinstance(proposal, dict):
        proposal = dict(proposal)
        primary = proposal.get('primary_metric')
        if isinstance(primary, str):
            # The schema requires a ProposedMetric object; accept the flattened
            # string spelling some checkpoints emit and back-fill defaults for
            # the two companion fields from their legacy positions.
            proposal['primary_metric'] = {
                'name': primary,
                'direction': proposal.pop(
                    'primary_metric_direction',
                    'maximize',
                ),
                'minimum_effect': proposal.pop('minimum_effect', 0.0),
            }
        normalized['evaluation_contract_proposal'] = proposal
    return normalized


def materialize_declared_workspace_files(
    *,
    structured: dict[str, Any],
    workspace: Path,
    agent: AgentName,
) -> dict[str, Any]:
    """Handle bounded local file requests emitted through structured output."""
    actions = structured.get('requested_actions')
    produced = structured.get('produced_files')
    if not isinstance(actions, list) or not isinstance(produced, list):
        return structured

    # A write_file request is honoured only when it is backed by a declared
    # produced file. Honeydew may only materialize protocol/report/analysis
    # material; Beaker may only materialize implementation material. Anything
    # else (including transition bookkeeping) is left for the engine to see.
    declared: dict[str, ProducedFile] = {}
    for item in produced:
        try:
            parsed = ProducedFile.model_validate(item)
        except ValidationError:
            continue
        declared[parsed.path] = parsed

    allowed_purposes = (
        {'protocol', 'report', 'analysis', 'other'}
        if agent == AgentName.HONEYDEW
        else {'implementation', 'analysis', 'other'}
    )
    root = workspace.resolve()
    remaining: list[Any] = []
    for action in actions:
        if not isinstance(action, dict):
            remaining.append(action)
            continue
        action_type = action.get('type')
        if action_type == 'transition':
            continue
        if action_type != 'write_file':
            remaining.append(action)
            continue
        arguments = action.get('arguments')
        if not isinstance(arguments, dict):
            remaining.append(action)
            continue
        relative_path = arguments.get('path')
        content = arguments.get('content')
        declaration = declared.get(relative_path)
        if (
            declaration is None
            or declaration.purpose not in allowed_purposes
            or not isinstance(content, str)
        ):
            remaining.append(action)
            continue
        destination = workspace / declaration.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        parent = destination.parent.resolve()
        # Re-resolve the parent so a symlinked intermediate directory cannot
        # redirect the write outside the workspace; symlink final components
        # are rejected outright rather than overwritten.
        if not parent.is_relative_to(root) or destination.is_symlink():
            remaining.append(action)
            continue
        destination.write_text(content, encoding='utf-8')
    normalized = dict(structured)
    # Applied write_file actions are removed from the result so the engine and
    # the event log only ever see the declarative remainder.
    normalized['requested_actions'] = remaining
    return normalized


def _validation_summary(exc: ValidationError) -> str:
    # Cap the first few errors only: this text is embedded in a repair prompt
    # and a failure message, so it must stay small and bounded.
    details: list[str] = []
    for error in exc.errors(include_url=False, include_input=False)[:8]:
        location = '.'.join(str(part) for part in error['loc'])
        details.append(f"{location}: {error['msg']}")
    return '; '.join(details)


@dataclass
class _ProcessHandle:
    runtime_id: str
    run_id: str
    agent: AgentName
    workspace: Path
    base_url: str
    password: str
    process: subprocess.Popen[str]
    log_handle: Any


class OpenCodeProcessRuntime(AgentRuntime):
    """One authenticated headless OpenCode process per run and agent."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Keyed by (run_id, agent): sessions of two agents never share a
        # process, and different runs never share agent context.
        self._handles: dict[tuple[str, AgentName], _ProcessHandle] = {}
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
            self.settings.opencode_start_port,
            self.settings.opencode_start_port + 100,
        ):
            if port in used:
                continue
            with socket.socket() as probe:
                try:
                    probe.bind((self.settings.opencode_server_host, port))
                except OSError:
                    continue
            return port
        raise OpenCodeRuntimeError(
            'no OpenCode runtime port is available',
            failure_class='startup',
        )

    def _permissions(self, agent: AgentName) -> dict[str, Any]:
        # Deny-list for the agent's bash tool. Cluster mutation, network
        # egress to the cluster, image publication, and git push/PR creation
        # are off limits for both agents; Honeydew additionally cannot mutate
        # the repository at all while drafting and reviewing.
        denied_shell = {
            '*': 'allow',
            'kubectl *': 'deny',
            'ssh *': 'deny',
            'scp *': 'deny',
            'docker *': 'deny',
            'podman *': 'deny',
            'git push*': 'deny',
            'gh pr create*': 'deny',
            '*secret*': 'deny',
        }
        if agent == AgentName.HONEYDEW:
            denied_shell.update(
                {
                    'git commit*': 'deny',
                    'git checkout*': 'deny',
                    'git switch*': 'deny',
                }
            )
        return {
            '*': 'allow',
            'doom_loop': 'deny',
            'external_directory': 'deny',
            'lsp': 'deny',
            'question': 'deny',
            'skill': 'deny',
            'task': 'deny',
            'webfetch': 'deny',
            'websearch': 'deny',
            'bash': denied_shell,
        }

    def _write_runtime_config(
        self,
        *,
        run_id: str,
        agent: AgentName,
        workspace: Path,
    ) -> tuple[Path, Path, Path, Path, Path]:
        runtime_root = workspace.parent / 'runtime' / agent.value
        config_root = runtime_root / 'config'
        data_root = runtime_root / 'data'
        # XDG_CACHE_HOME is intentionally NOT under runtime_root: it holds
        # OpenCode's own package/model download cache, which is the same
        # regenerable content for every run and both agents (same OpenCode
        # version, same plugin set). Sharing one location here is what stops
        # it from being copied into every run directory (see config.py's
        # opencode_shared_cache_root and issue #99). XDG_DATA_HOME (session
        # auth), XDG_STATE_HOME (logs/history), and HOME stay per-run: those
        # are run-specific state, not cache.
        cache_root = Path(self.settings.opencode_shared_cache_root)
        state_root = runtime_root / 'state'
        home_root = runtime_root / 'home'
        opencode_config_root = config_root / 'opencode'
        opencode_config_root.mkdir(parents=True, exist_ok=True)
        for path in (data_root, cache_root, state_root, home_root):
            path.mkdir(parents=True, exist_ok=True)
        provider_id = self.settings.agent_model_provider_id
        model_name = self.settings.agent_model_for(agent)
        config = {
            '$schema': 'https://opencode.ai/config.json',
            'model': f'{provider_id}/{model_name}',
            'small_model': f'{provider_id}/{model_name}',
            'default_agent': 'build',
            'share': 'disabled',
            'autoupdate': False,
            'lsp': False,
            'permission': self._permissions(agent),
            'agent': {
                'build': {
                    'temperature': 0,
                    'permission': self._permissions(agent),
                },
                'plan': {'disable': True},
            },
        }
        if provider_id == 'exo':
            config['provider'] = {
                'exo': {
                    'npm': '@ai-sdk/openai-compatible',
                    'name': 'Glasslab Exo',
                    'options': {'baseURL': self.settings.qwen_base_url},
                    'models': {
                        model_name: {
                            'name': model_name,
                        }
                    },
                }
            }
        (opencode_config_root / 'opencode.json').write_text(
            json.dumps(config, indent=2, sort_keys=True) + '\n'
        )
        return config_root, data_root, cache_root, state_root, home_root

    def _start_process(
        self,
        *,
        run_id: str,
        agent: AgentName,
        workspace: Path,
    ) -> _ProcessHandle:
        key = (run_id, agent)
        existing = self._handles.get(key)
        # Recovery hook: if the stored process is still alive it is reused so a
        # transient error above this layer does not tear down a healthy server.
        if existing is not None and existing.process.poll() is None:
            return existing
        port = self._runtime_port()
        (
            config_root,
            data_root,
            cache_root,
            state_root,
            home_root,
        ) = self._write_runtime_config(
            run_id=run_id,
            agent=agent,
            workspace=workspace,
        )
        runtime_root = config_root.parent
        log_path = runtime_root / 'opencode.log'
        log_handle = log_path.open('a', encoding='utf-8')
        password = secrets.token_urlsafe(32)
        # XDG roots are isolated per run and agent under the workspace parent,
        # so no conversation state leaks between runs. The random server
        # password is held only in this in-memory handle and is never written
        # to disk or the log. The child environment is an explicit allowlist
        # (see app/runtime_env.py): orchestrator control-plane secrets never
        # reach the agent shell.
        environment = build_agent_environment(
            runtime_vars={
                'XDG_CONFIG_HOME': str(config_root),
                'XDG_DATA_HOME': str(data_root),
                'XDG_CACHE_HOME': str(cache_root),
                'XDG_STATE_HOME': str(state_root),
                'HOME': str(home_root),
                'OPENCODE_SERVER_USERNAME': 'glasslab-orchestrator',
                'OPENCODE_SERVER_PASSWORD': password,
            },
        )
        process = subprocess.Popen(
            [
                self.settings.opencode_executable,
                'serve',
                '--hostname',
                self.settings.opencode_server_host,
                '--port',
                str(port),
            ],
            cwd=workspace,
            env=environment,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        handle = _ProcessHandle(
            runtime_id=f'opencode-{agent.value}-{uuid4().hex[:12]}',
            run_id=run_id,
            agent=agent,
            workspace=workspace,
            base_url=f'http://{self.settings.opencode_server_host}:{port}',
            password=password,
            process=process,
            log_handle=log_handle,
        )
        self._handles[key] = handle
        deadline = time.monotonic() + self.settings.opencode_start_timeout_seconds
        # Poll the authenticated health endpoint until the server accepts
        # requests; a process that dies during startup is surfaced with the log
        # path so the failure is diagnosable without guessing.
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise OpenCodeRuntimeError(
                    f'OpenCode process exited during startup; see {log_path}',
                    failure_class='startup',
                )
            try:
                response = httpx.get(
                    f'{handle.base_url}/global/health',
                    auth=('glasslab-orchestrator', password),
                    timeout=1,
                )
                if response.status_code == 200:
                    return handle
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        self._stop_handle(handle)
        raise OpenCodeRuntimeError(
            'OpenCode server did not become healthy',
            failure_class='startup',
        )

    def _client(self, handle: _ProcessHandle) -> httpx.Client:
        return httpx.Client(
            base_url=handle.base_url,
            auth=('glasslab-orchestrator', handle.password),
            timeout=self.settings.opencode_turn_timeout_seconds,
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
        params = {'directory': str(workspace)}
        with self._client(handle) as client:
            if existing_session_id:
                # Recovery across process restarts: the persisted session id is
                # re-validated against the live server so a re-attached run
                # continues the same conversation. A fresh process without the
                # persisted id (or a deleted session) falls through to a new
                # session instead.
                response = client.get(
                    f'/session/{existing_session_id}',
                    params=params,
                )
                if response.status_code == 200:
                    return RuntimeSession(handle.runtime_id, existing_session_id)
            response = client.post(
                '/session',
                params=params,
                json={'title': f'Glasslab {agent.value} {run_id}'},
            )
            response.raise_for_status()
            session_id = str(response.json()['id'])
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
        stop_watchdog = threading.Event()
        abort_reasons: list[tuple[str, str]] = []
        # The watchdog polls the transcript and enforces both the hard
        # wall-clock deadline and the identical-terminal-tool-call guard. It
        # records why the turn was aborted so run_turn surfaces a meaningful
        # error rather than a generic HTTP failure.
        watchdog = threading.Thread(
            target=self._watch_turn,
            kwargs={
                'handle': handle,
                'session_id': session_id,
                'workspace': workspace,
                'stop': stop_watchdog,
                'abort_reasons': abort_reasons,
            },
            daemon=True,
            name=f'opencode-watchdog-{agent.value}-{run_id[:8]}',
        )
        watchdog.start()
        try:
            result = self._run_turn_request_loop(
                handle=handle,
                agent=agent,
                workspace=workspace,
                session_id=session_id,
                prompt=prompt,
            )
            if abort_reasons:
                reason, failure_class = abort_reasons[0]
                raise OpenCodeRuntimeError(
                    reason, failure_class=failure_class
                )
            return result
        except Exception as exc:
            if abort_reasons:
                reason, failure_class = abort_reasons[0]
                raise OpenCodeRuntimeError(
                    reason, failure_class=failure_class
                ) from exc
            raise
        finally:
            stop_watchdog.set()
            watchdog.join(timeout=3)

    def _run_turn_request_loop(
        self,
        *,
        handle: _ProcessHandle,
        agent: AgentName,
        workspace: Path,
        session_id: str,
        prompt: str,
    ) -> tuple[AgentTurnResult, str | None]:
        message_id: str | None = None
        current_prompt = prompt
        with self._client(handle) as client:
            try:
                # The model is asked to emit an AgentTurnResult-shaped object
                # under a JSON-schema format; validation failures and missing
                # structured output trigger a bounded repair turn that may only
                # correct the result, never do new workspace work.
                for attempt in range(
                    self.settings.opencode_structured_repair_attempts + 1
                ):
                    payload = {
                        'model': {
                            'providerID': self.settings.agent_model_provider_id,
                            'modelID': (
                                self.settings.agent_model_for(agent)
                            ),
                        },
                        'agent': 'build',
                        'system': self._system_prompts[agent],
                        'parts': [{'type': 'text', 'text': current_prompt}],
                    }
                    output_format = {
                        'type': 'json_schema',
                        'schema': AgentTurnResult.model_json_schema(),
                        'retryCount': 2,
                    }
                    if (
                        self.settings.opencode_structured_output_mode
                        == 'json_schema'
                    ):
                        payload['format'] = output_format
                    else:
                        payload['parts'][0]['text'] = (
                            f'{current_prompt}\n\n'
                            'Return only a JSON object, without Markdown '
                            'fences or commentary, matching this JSON Schema:\n'
                            + json.dumps(output_format['schema'])
                        )
                    response = client.post(
                        f'/session/{session_id}/message',
                        params={'directory': str(workspace)},
                        json=payload,
                    )
                    response.raise_for_status()
                    body = response.json()
                    provider_error = provider_error_message(body)
                    if provider_error:
                        raise OpenCodeRuntimeError(
                            f'OpenCode provider error: {provider_error}',
                            failure_class='provider',
                        )
                    info = body.get('info', {})
                    message_id = info.get('id')
                    structured = extract_structured_output(body)
                    if structured is None:
                        # Some checkpoints return plain prose instead of the
                        # structured field; fall back to parsing the text parts
                        # before giving up.
                        text_parts = [
                            str(part.get('text', ''))
                            for part in body.get('parts', [])
                            if part.get('type') == 'text'
                        ]
                        raw = ''.join(text_parts).strip()
                        try:
                            structured = parse_json_text(raw)
                        except json.JSONDecodeError as exc:
                            if (
                                attempt
                                >= self.settings.opencode_structured_repair_attempts
                            ):
                                raise OpenCodeRuntimeError(
                                    'OpenCode turn did not return structured output',
                                    failure_class='validation',
                                ) from exc
                            current_prompt = (
                                'Return only the structured result for your '
                                'previous completed turn. Do not repeat the '
                                'implementation or perform additional workspace '
                                'work. Return a complete object matching the '
                                'supplied JSON schema. Nested objects and arrays '
                                'must be JSON values, not JSON-encoded strings. '
                                'The previous response did not contain a valid '
                                'structured object.'
                            )
                            continue
                    structured = normalize_structured_output(structured)
                    if isinstance(structured, dict):
                        structured = materialize_declared_workspace_files(
                            structured=structured,
                            workspace=workspace,
                            agent=agent,
                        )
                    try:
                        return (
                            AgentTurnResult.model_validate(structured),
                            message_id,
                        )
                    except ValidationError as exc:
                        if (
                            attempt
                            >= self.settings.opencode_structured_repair_attempts
                        ):
                            raise
                        current_prompt = (
                            'Correct only the structured result from your '
                            'previous response. You may use a workspace file '
                            'tool only when a declared produced file is missing. '
                            'Return a complete object matching the supplied '
                            'JSON schema. Nested objects and arrays must be JSON '
                            'values, not JSON-encoded strings. Remove local '
                            'write_file and transition requests after applying '
                            'them. The independent '
                            f'validator reported:\n{exc}'
                        )
            except ValidationError as exc:
                raise OpenCodeRuntimeError(
                    'OpenCode structured output remained invalid after '
                    f'{self.settings.opencode_structured_repair_attempts} '
                    f'repair attempt(s): {_validation_summary(exc)}',
                    failure_class='validation',
                ) from exc
        raise OpenCodeRuntimeError(
            'OpenCode turn ended without a result',
            failure_class='validation',
        )

    @staticmethod
    def _terminal_tool_signatures(messages: list[dict[str, Any]]) -> list[str]:
        # Canonical fingerprint (tool + input) of tool parts that already
        # finished, so identical repeated calls are detectable as a loop.
        signatures: list[str] = []
        for message in messages:
            for part in message.get('parts', []):
                if part.get('type') != 'tool':
                    continue
                state = part.get('state')
                if (
                    not isinstance(state, dict)
                    or state.get('status') not in {'completed', 'error'}
                ):
                    continue
                signatures.append(
                    json.dumps(
                        {
                            'tool': part.get('tool'),
                            'input': state.get('input'),
                        },
                        sort_keys=True,
                        separators=(',', ':'),
                    )
                )
        return signatures

    def _watch_turn(
        self,
        *,
        handle: _ProcessHandle,
        session_id: str,
        workspace: Path,
        stop: threading.Event,
        abort_reasons: list[tuple[str, str]],
    ) -> None:
        deadline = time.monotonic() + self.settings.opencode_turn_timeout_seconds
        while not stop.wait(2):
            reason: str | None = None
            failure_class: str | None = None
            if time.monotonic() >= deadline:
                reason = (
                    'OpenCode turn exceeded the hard wall-clock limit of '
                    f'{self.settings.opencode_turn_timeout_seconds:g} seconds'
                )
            else:
                try:
                    with httpx.Client(
                        base_url=handle.base_url,
                        auth=('glasslab-orchestrator', handle.password),
                        timeout=5,
                    ) as client:
                        response = client.get(
                            f'/session/{session_id}/message',
                            params={'directory': str(workspace)},
                        )
                        response.raise_for_status()
                        messages = response.json()
                    if isinstance(messages, list):
                        signatures = self._terminal_tool_signatures(messages)
                        limit = self.settings.opencode_repeated_tool_limit
                        # Guard against retry loops: once `limit` consecutive
                        # terminal tool calls are byte-identical (same tool and
                        # same input) the turn is stuck and is aborted.
                        if (
                            limit > 1
                            and len(signatures) >= limit
                            and len(set(signatures[-limit:])) == 1
                        ):
                            reason = (
                                'OpenCode turn aborted after '
                                f'{limit} identical terminal tool calls'
                            )
                            failure_class = 'repeated_tool_loop'
                except (httpx.HTTPError, ValueError):
                    continue
            if reason is None:
                continue
            abort_reasons.append((reason, failure_class or 'turn_timeout'))
            try:
                with httpx.Client(
                    base_url=handle.base_url,
                    auth=('glasslab-orchestrator', handle.password),
                    timeout=5,
                ) as client:
                    response = client.post(
                        f'/session/{session_id}/abort',
                        params={'directory': str(workspace)},
                    )
                    if response.status_code not in {200, 404}:
                        response.raise_for_status()
            except httpx.HTTPError:
                pass
            return

    def abort(self, *, run_id: str, agent: AgentName, session_id: str) -> None:
        handle = self._handles.get((run_id, agent))
        if handle is None or handle.process.poll() is not None:
            return
        with self._client(handle) as client:
            response = client.post(
                f'/session/{session_id}/abort',
                params={'directory': str(handle.workspace)},
            )
            if response.status_code not in {200, 404}:
                response.raise_for_status()

    def iter_normalized_events(
        self,
        *,
        run_id: str,
        agent: AgentName,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        handle = self._handles[(run_id, agent)]
        with self._client(handle).stream('GET', '/event') as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith('data:'):
                    continue
                raw = json.loads(line.removeprefix('data:').strip())
                normalized = normalize_opencode_event(
                    raw,
                    run_id=run_id,
                    agent=agent,
                )
                if normalized is not None:
                    yield normalized

    def _stop_handle(self, handle: _ProcessHandle) -> None:
        if handle.process.poll() is None:
            handle.process.terminate()
            try:
                handle.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                handle.process.kill()
                handle.process.wait(timeout=5)
        handle.log_handle.close()

    def close(self) -> None:
        for handle in list(self._handles.values()):
            self._stop_handle(handle)
        self._handles.clear()

    def release(self, *, run_id: str, agent: AgentName) -> None:
        # Used for short-lived compiler sessions; terminating the process also
        # drops the isolated session state so no half-finished agent context
        # survives.
        handle = self._handles.pop((run_id, agent), None)
        if handle is not None:
            self._stop_handle(handle)
