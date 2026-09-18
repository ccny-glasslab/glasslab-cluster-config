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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Iterator, Literal
from uuid import uuid4

import httpx
from pydantic import ValidationError

from .config import Settings
from .knowledge_tool import TOOL_NAME, BoundRetrieveEvidenceTool
from .runtime_env import build_agent_environment
from .schemas import AgentName, AgentTurnResult, ProducedFile


ResultPreparer = Callable[[dict[str, Any]], dict[str, Any]]


class OpenCodeRuntimeError(RuntimeError):
    """A surfaced OpenCode runtime failure.

    ``failure_class`` classifies the failure for the engine's bounded retry
    decision: transient classes (startup, turn_timeout, repeated_tool_loop,
    provider, network) are retryable; deterministic classes (validation,
    kind_mismatch) are not.

    ``details`` carries a small, secret-free diagnostic payload (for a repeated
    tool loop: the repeated tool name, its repeat count, and a short digest of
    the identical input fingerprint) so recovery can be corrective without
    persisting raw tool arguments.
    """

    def __init__(
        self,
        message: str,
        *,
        failure_class: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.details = details


# Issue #482: a worktree whose .opencode dependency tree cannot resolve is an
# infrastructure condition, not a model failure. OpenCode surfaces it either
# as a module-resolution failure ("ResolveMessage: Cannot find module
# '@opencode-ai/plugin'") on the message endpoint or as a failed background
# dependency install in its own log. Such a turn cannot succeed in a fresh
# session, so the class is reported distinctly and the engine keeps it out of
# the model-failure retry/rotation machinery and the turn budget.
RUNTIME_DEPENDENCY_UNAVAILABLE_CLASS = 'runtime_dependency_unavailable'
OPENCODE_PLUGIN_MODULE = '@opencode-ai/plugin'
_DEPENDENCY_INSTALL_FAILURE_MARKERS = (
    'background dependency install failed',
)
_MODULE_RESOLUTION_FAILURE_MARKERS = (
    'cannot find module',
    'module not found',
    'resolvemessage',
)
_RUNTIME_LOG_TAIL_BYTES = 64 * 1024
_DEPENDENCY_CACHE_SEARCH_MAX_DIRS = 4000
_DEPENDENCY_CACHE_SEARCH_MAX_DEPTH = 12


def dependency_resolution_failure(text: str) -> bool:
    """True when OpenCode text describes the worktree dependency failure."""
    if not text:
        return False
    normalized = ' '.join(text.lower().split())
    if any(
        marker in normalized for marker in _DEPENDENCY_INSTALL_FAILURE_MARKERS
    ):
        return True
    if not any(
        marker in normalized for marker in _MODULE_RESOLUTION_FAILURE_MARKERS
    ):
        return False
    # Only a resolution failure that names the worktree plugin surface is the
    # infrastructure condition; an unrelated "Cannot find module 'x'" 500 must
    # keep its existing failure classification.
    return OPENCODE_PLUGIN_MODULE in normalized or '.opencode' in normalized


@dataclass(frozen=True)
class DependencyRepairResult:
    """Outcome of one bounded .opencode dependency repair attempt."""

    status: Literal['repaired', 'already_resolved', 'unavailable']
    source: str | None
    detail: str

    @property
    def resolved(self) -> bool:
        return self.status in {'repaired', 'already_resolved'}


def find_cached_plugin_package(cache_root: Path) -> Path | None:
    """Locate @opencode-ai/plugin in the shared OpenCode cache.

    Bounded search: the cache is a regenerable download store, not an index,
    so a miss returns None instead of walking without limit. Handles both a
    materialized node_modules layout and bun's hashed ``plugin@<version>``
    cache entries.
    """
    if not cache_root.is_dir():
        return None
    pending: list[tuple[Path, int]] = [(cache_root, 0)]
    visited = 0
    while pending:
        directory, depth = pending.pop()
        visited += 1
        if (
            visited > _DEPENDENCY_CACHE_SEARCH_MAX_DIRS
            or depth > _DEPENDENCY_CACHE_SEARCH_MAX_DEPTH
        ):
            continue
        scope = directory / '@opencode-ai'
        if scope.is_dir():
            exact = scope / 'plugin'
            if (exact / 'package.json').is_file():
                return exact
            for candidate in sorted(scope.glob('plugin@*')):
                if (candidate / 'package.json').is_file():
                    return candidate
        try:
            children = sorted(
                (child for child in directory.iterdir() if child.is_dir()),
                key=lambda path: path.name,
            )
        except OSError:
            continue
        pending.extend((child, depth + 1) for child in children)
    return None


def dependency_unavailable_error(
    *,
    worktree: Path,
    module: str,
    cache_root: Path,
    detail: str,
) -> OpenCodeRuntimeError:
    """Build the actionable infrastructure error for issue #482."""
    opencode_root = worktree / '.opencode'
    message = (
        'OpenCode cannot resolve the worktree plugin dependency '
        f"'{module}' from {opencode_root} ({detail}). This is an "
        'infrastructure/runtime-availability failure, not a model failure: '
        "the attempt does not consume the run's turn budget and is not "
        'retried in a loop. Repair the worktree dependency tree by '
        f'reinstalling from the shared OpenCode cache root {cache_root}, '
        f'for example: cd {opencode_root} && bun install --offline '
        f'(or re-link node_modules/{module} from that cache), then resume '
        'the run.'
    )
    return OpenCodeRuntimeError(
        message,
        failure_class=RUNTIME_DEPENDENCY_UNAVAILABLE_CLASS,
        details={
            'worktree': str(worktree),
            'module': module,
            'cache_root': str(cache_root),
            'detail': detail,
        },
    )


@dataclass
class _TurnAbort:
    reason: str
    failure_class: str
    details: dict[str, Any] | None = None


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
        model_override: str | None = None,
        base_url_override: str | None = None,
        knowledge_tool: BoundRetrieveEvidenceTool | None = None,
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
        model_override: str | None = None,
        base_url_override: str | None = None,
        knowledge_tool: BoundRetrieveEvidenceTool | None = None,
        result_preparers: Sequence[ResultPreparer] = (),
    ) -> tuple[AgentTurnResult, str | None]:
        raise NotImplementedError

    def session_context_tokens(
        self,
        *,
        run_id: str,
        agent: AgentName,
        session_id: str,
    ) -> int | None:
        """Real cumulative context tokens observed for a live session.

        Backends that receive provider usage (OpenCode) override this so
        rotation bounds the real prompt rather than an estimate over the
        orchestrator's stored turns. ``None`` keeps the estimation fallback.
        """
        del run_id, agent, session_id
        return None

    @abstractmethod
    def abort(self, *, run_id: str, agent: AgentName, session_id: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def release(self, *, run_id: str, agent: AgentName) -> None:
        return None

    def repair_workspace_dependencies(
        self,
        *,
        workspace: Path,
    ) -> DependencyRepairResult:
        """One bounded attempt to restore the worktree dependency tree.

        Runtimes without a local .opencode tree report ``unavailable`` and the
        engine then fails closed with the runtime's actionable error.
        """
        del workspace
        return DependencyRepairResult(
            status='unavailable',
            source=None,
            detail='this agent runtime manages no workspace dependency tree',
        )


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


def message_context_tokens(body: Any) -> int | None:
    """Real context size the next turn must resend, from an assistant message.

    The AI SDK normalizes ``info.tokens.input`` to exclude cached tokens, so the
    full prompt processed is ``input + cache.read + cache.write`` and generated
    tokens (``output + reasoning``) re-enter the next prompt; their sum is the
    cumulative context. Returns ``None`` when no usage is reported.
    """
    if not isinstance(body, dict):
        return None
    info = body.get('info')
    if not isinstance(info, dict):
        return None
    tokens = info.get('tokens')
    if not isinstance(tokens, dict):
        return None

    cache = tokens.get('cache')
    cache = cache if isinstance(cache, dict) else {}

    def _count(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return max(0, int(value))

    measured = (
        _count(tokens.get('input'))
        + _count(cache.get('read'))
        + _count(cache.get('write'))
        + _count(tokens.get('output'))
        + _count(tokens.get('reasoning'))
    )
    if measured > 0:
        return measured
    # Some providers only publish the aggregate total.
    return _count(tokens.get('total')) or None


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


def apply_result_preparers(
    structured: Any,
    preparers: Sequence[ResultPreparer],
) -> Any:
    """Apply orchestrator-owned preparers to a raw structured payload.

    Preparers run before schema validation so orchestrator-established facts
    required by the schema (for example a task asset's verified digest) are
    present when the payload is validated and committed. A preparer raising
    aborts the turn: its failure is a deterministic rejection, not something a
    model repair turn can fix.
    """
    if not isinstance(structured, dict) or not preparers:
        return structured
    for prepare in preparers:
        structured = prepare(structured)
    return structured


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
        # Port allocation is serialized and each picked port is reserved until
        # the child process is registered in _handles, so concurrent starts
        # (recover racing an approval-driven start) cannot pick the same port.
        self._port_lock = threading.Lock()
        self._reserved_ports: set[int] = set()
        # Last real context size observed for a (run, agent) session, captured
        # from each message response so rotation bounds the real prompt even
        # though the engine stores none of OpenCode's own context. The session
        # id is kept alongside so a stale value from a rotated session is never
        # reused; release() drops the entry entirely.
        self._observed_session_tokens: dict[
            tuple[str, AgentName], tuple[str, int]
        ] = {}
        prompt_root = Path(__file__).resolve().parents[1] / 'prompts'
        self._system_prompts = {
            AgentName.HONEYDEW: (prompt_root / 'honeydew.md').read_text(),
            AgentName.BEAKER: (prompt_root / 'beaker.md').read_text(),
        }

    def _runtime_port(self) -> int:
        with self._port_lock:
            used = {
                int(handle.base_url.rsplit(':', 1)[1])
                for handle in self._handles.values()
                if handle.process.poll() is None
            }
            used.update(self._reserved_ports)
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
                    # Reserve while the probe socket still holds the port so a
                    # concurrent allocator cannot observe it as free.
                    self._reserved_ports.add(port)
                    return port
        raise OpenCodeRuntimeError(
            'no OpenCode runtime port is available',
            failure_class='startup',
        )

    def _release_reserved_port(self, port: int) -> None:
        with self._port_lock:
            self._reserved_ports.discard(port)

    def _sync_knowledge_tool_file(
        self,
        *,
        workspace: Path,
        agent: AgentName,
        knowledge_tool: BoundRetrieveEvidenceTool | None,
    ) -> None:
        # Per-agent tool surface: the retrieve_evidence tool file is written
        # into Honeydew's workspace only. Beaker's workspace never receives
        # it (and any stale copy is removed), so the OpenCode process cannot
        # register the tool for Beaker.
        tool_path = workspace / '.opencode' / 'tools' / f'{TOOL_NAME}.js'
        if (
            agent is AgentName.HONEYDEW
            and knowledge_tool is not None
            and self.settings.knowledge_tool_enabled
        ):
            tool_path.parent.mkdir(parents=True, exist_ok=True)
            tool_path.write_text(
                self._knowledge_tool_source(knowledge_tool),
                encoding='utf-8',
            )
        elif tool_path.exists():
            tool_path.unlink()

    def _knowledge_tool_source(
        self,
        tool: BoundRetrieveEvidenceTool,
    ) -> str:
        endpoint = json.dumps(self.settings.knowledge_tool_endpoint_url)
        token = json.dumps(tool.token)
        return (
            '// Generated by the Glasslab orchestrator; do not edit.\n'
            'import { tool } from "@opencode-ai/plugin"\n\n'
            f'const ENDPOINT = {endpoint}\n'
            f'const TOKEN = {token}\n'
            'const MAX_K = '
            f'{self.settings.knowledge_tool_max_k}\n'
            'const DEFAULT_K = '
            f'{self.settings.knowledge_tool_default_k}\n\n'
            'export default tool({\n'
            '  description:\n'
            '    "Search the approved Glasslab knowledge corpus (read-only). "\n'
            '    + "Returns ranked chunks with knowledge:// evidence URIs, "\n'
            '    + "verbatim excerpts, and a verified flag. Use it to gather "\n'
            '    + "citable evidence before making claims.",\n'
            '  args: {\n'
            '    query: tool.schema\n'
            '      .string()\n'
            '      .describe("Natural-language search query"),\n'
            '    k: tool.schema\n'
            '      .number()\n'
            '      .int()\n'
            '      .min(1)\n'
            '      .max(MAX_K)\n'
            '      .optional()\n'
            '      .describe("Number of chunks to return"),\n'
            '  },\n'
            '  async execute(args) {\n'
            '    const response = await fetch(ENDPOINT, {\n'
            '      method: "POST",\n'
            '      headers: {\n'
            '        "content-type": "application/json",\n'
            '        "x-glasslab-tool-token": TOKEN,\n'
            '      },\n'
            '      body: JSON.stringify({\n'
            '        query: args.query,\n'
            '        k: args.k ?? DEFAULT_K,\n'
            '      }),\n'
            '    })\n'
            '    if (!response.ok) {\n'
            '      return `retrieve_evidence failed with HTTP '
            '${response.status}`\n'
            '    }\n'
            '    const payload = await response.json()\n'
            '    if (!payload.context) {\n'
            '      return "No matching knowledge corpus material."\n'
            '    }\n'
            '    return payload.context\n'
            '  },\n'
            '})\n'
        )

    def _permissions(
        self,
        agent: AgentName,
        run_root: Path | None = None,
    ) -> dict[str, Any]:
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
            # Network egress: a prompt-injected agent must not be able to
            # exfiltrate the model-provider API keys present in its env.
            'curl *': 'deny',
            'curl.exe *': 'deny',
            'wget *': 'deny',
            'nc *': 'deny',
            'ncat *': 'deny',
            'fetch *': 'deny',
            'telnet *': 'deny',
            'ftp *': 'deny',
            'socat *': 'deny',
        }
        if agent == AgentName.HONEYDEW:
            denied_shell.update(
                {
                    'git commit*': 'deny',
                    'git checkout*': 'deny',
                    'git switch*': 'deny',
                }
            )
        # external_directory default-denies every path outside the agent's
        # own worktree. Only the SAME run's read-only durable directories
        # (protocol/, shared-artifacts/, reports/, events/) are allowed by
        # absolute-path pattern; runtime/** (OpenCode session databases and
        # secrets), the other agent's worktree, other runs, and arbitrary
        # host paths match no allow pattern and therefore stay denied.
        #
        # ORDER IS LOAD-BEARING: OpenCode evaluates permission rules with
        # last-match-wins over the merged, ordered ruleset, and
        # _write_runtime_config serializes this dict with sort_keys=True.
        # '*' (0x2A) sorts before '/' (0x2F), so the catch-all deny is
        # emitted first and every run-scoped allow after it. The allow is
        # what applies to the permitted directories; swapping that order
        # would make the catch-all deny win for them too.
        external_directories: dict[str, str] = {'*': 'deny'}
        if run_root is not None:
            root = str(run_root)
            for name in ('protocol', 'shared-artifacts', 'reports', 'events'):
                external_directories[f'{root}/{name}/*'] = 'allow'
        return {
            '*': 'allow',
            'doom_loop': 'deny',
            'external_directory': external_directories,
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
        model_override: str | None = None,
        base_url_override: str | None = None,
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
        model_name = model_override or self.settings.agent_model_for(agent)
        base_url = base_url_override or self.settings.base_url_for(agent)
        config = {
            '$schema': 'https://opencode.ai/config.json',
            'model': f'{provider_id}/{model_name}',
            'small_model': f'{provider_id}/{model_name}',
            'default_agent': 'build',
            'share': 'disabled',
            'autoupdate': False,
            'lsp': False,
            'permission': self._permissions(agent, run_root=workspace.parent),
            'agent': {
                'build': {
                    'temperature': 0,
                    'permission': self._permissions(
                        agent, run_root=workspace.parent
                    ),
                },
                'plan': {'disable': True},
            },
        }
        if provider_id == 'exo':
            config['provider'] = {
                'exo': {
                    'npm': '@ai-sdk/openai-compatible',
                    'name': 'Glasslab Exo',
                    'options': {'baseURL': base_url},
                    'models': {
                        model_name: {
                            'name': model_name,
                            'options': {
                                'maxOutputTokens': (
                                    self.settings.agent_model_max_output_tokens
                                ),
                            },
                        }
                    },
                }
            }
        (opencode_config_root / 'opencode.json').write_text(
            json.dumps(config, indent=2, sort_keys=True) + '\n'
        )
        return config_root, data_root, cache_root, state_root, home_root

    def repair_workspace_dependencies(
        self,
        *,
        workspace: Path,
    ) -> DependencyRepairResult:
        """Re-link the pinned plugin from the shared cache (issue #482).

        The per-run worktree is materialized once; a later resume in a newer
        image can leave it with a .opencode tree whose background dependency
        install failed, which makes every turn 500. This is a single bounded
        filesystem repair with no network access and no loop: the engine
        attempts it at most once per failed turn and fails closed when the
        package cannot be resolved from the shared cache.
        """
        target = (
            workspace
            / '.opencode'
            / 'node_modules'
            / '@opencode-ai'
            / 'plugin'
        )
        if (target / 'package.json').is_file():
            return DependencyRepairResult(
                status='already_resolved',
                source=str(target),
                detail='the worktree already resolves the OpenCode plugin',
            )
        cache_root = Path(self.settings.opencode_shared_cache_root)
        source = find_cached_plugin_package(cache_root)
        if source is None:
            return DependencyRepairResult(
                status='unavailable',
                source=None,
                detail=(
                    'no @opencode-ai/plugin package was found under the '
                    f'shared OpenCode cache root {cache_root}'
                ),
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            target.symlink_to(source, target_is_directory=True)
        except OSError as exc:
            return DependencyRepairResult(
                status='unavailable',
                source=str(source),
                detail=f'could not link {source} into {target}: {exc}',
            )
        return DependencyRepairResult(
            status='repaired',
            source=str(source),
            detail=f'linked {source} into {target}',
        )

    def _start_process(
        self,
        *,
        run_id: str,
        agent: AgentName,
        workspace: Path,
        model_override: str | None = None,
        base_url_override: str | None = None,
        knowledge_tool: BoundRetrieveEvidenceTool | None = None,
    ) -> _ProcessHandle:
        key = (run_id, agent)
        self._sync_knowledge_tool_file(
            workspace=workspace,
            agent=agent,
            knowledge_tool=knowledge_tool,
        )
        existing = self._handles.get(key)
        # Recovery hook: if the stored process is still alive it is reused so a
        # transient error above this layer does not tear down a healthy server.
        if existing is not None and existing.process.poll() is None:
            return existing
        port = self._runtime_port()
        try:
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
                model_override=model_override,
                base_url_override=base_url_override,
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
            try:
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
            except Exception:
                log_handle.close()
                raise
        except Exception:
            self._release_reserved_port(port)
            raise
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
        self._release_reserved_port(port)
        deadline = time.monotonic() + self.settings.opencode_start_timeout_seconds
        # Poll the authenticated health endpoint until the server accepts
        # requests; a process that dies during startup is surfaced with the log
        # path so the failure is diagnosable without guessing.
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._stop_handle(handle)
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
        # The blocking turn request must outlive the watchdog deadline by a
        # buffer so the watchdog (which records the abort reason and surfaces a
        # classified turn_timeout failure) always wins the race at the wall
        # clock. Without the buffer, the request's own read timeout can fire
        # first and the raw httpx.ReadTimeout escapes unclassified, which the
        # engine then treats as a generic retryable network error.
        return httpx.Client(
            base_url=handle.base_url,
            auth=('glasslab-orchestrator', handle.password),
            timeout=self.settings.opencode_turn_timeout_seconds + 30.0,
        )

    def ensure_session(
        self,
        *,
        run_id: str,
        agent: AgentName,
        workspace: Path,
        existing_session_id: str | None,
        model_override: str | None = None,
        base_url_override: str | None = None,
        knowledge_tool: BoundRetrieveEvidenceTool | None = None,
    ) -> RuntimeSession:
        handle = self._start_process(
            run_id=run_id,
            agent=agent,
            workspace=workspace,
            model_override=model_override,
            base_url_override=base_url_override,
            knowledge_tool=knowledge_tool,
        )
        params = {'directory': str(workspace)}
        with self._client(handle) as client:
            if existing_session_id:
                # Recovery across process restarts: the persisted session id is
                # re-validated against the live server so a re-attached run
                # continues the same conversation. Only a 404 (deleted or never
                # created session) rotates to a fresh session; any other
                # non-200 status is a transient server failure and must not
                # silently discard conversation continuity.
                response = client.get(
                    f'/session/{existing_session_id}',
                    params=params,
                )
                if response.status_code == 200:
                    return RuntimeSession(handle.runtime_id, existing_session_id)
                if response.status_code != 404:
                    raise OpenCodeRuntimeError(
                        'OpenCode session validation failed with HTTP '
                        f'{response.status_code}',
                        failure_class='network',
                    )
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
        model_override: str | None = None,
        base_url_override: str | None = None,
        knowledge_tool: BoundRetrieveEvidenceTool | None = None,
        result_preparers: Sequence[ResultPreparer] = (),
    ) -> tuple[AgentTurnResult, str | None]:
        handle = self._start_process(
            run_id=run_id,
            agent=agent,
            workspace=workspace,
            model_override=model_override,
            base_url_override=base_url_override,
            knowledge_tool=knowledge_tool,
        )
        stop_watchdog = threading.Event()
        abort_reasons: list[_TurnAbort] = []
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
                model_override=model_override,
                result_preparers=result_preparers,
            )
            if abort_reasons:
                abort = abort_reasons[0]
                raise OpenCodeRuntimeError(
                    abort.reason,
                    failure_class=abort.failure_class,
                    details=abort.details,
                )
            return result
        except Exception as exc:
            if abort_reasons:
                abort = abort_reasons[0]
                raise OpenCodeRuntimeError(
                    abort.reason,
                    failure_class=abort.failure_class,
                    details=abort.details,
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
        model_override: str | None = None,
        result_preparers: Sequence[ResultPreparer] = (),
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
                                model_override
                                or self.settings.agent_model_for(agent)
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
                    if response.status_code >= 500:
                        # Issue #482: a worktree whose .opencode dependency
                        # tree cannot resolve makes every turn 500; surface it
                        # as infrastructure instead of a retryable transport
                        # error that rotates and burns the turn budget.
                        detail = self._dependency_failure_detail(
                            response=response,
                            workspace=workspace,
                            agent=agent,
                        )
                        if detail is not None:
                            raise dependency_unavailable_error(
                                worktree=workspace,
                                module=OPENCODE_PLUGIN_MODULE,
                                cache_root=Path(
                                    self.settings.opencode_shared_cache_root
                                ),
                                detail=detail,
                            )
                    response.raise_for_status()
                    body = response.json()
                    measured_context = message_context_tokens(body)
                    if measured_context is not None:
                        # _run_turn_request_loop has no run_id parameter; the
                        # handle owns it (set by _start_process from run_turn).
                        self._observed_session_tokens[(handle.run_id, agent)] = (
                            session_id,
                            measured_context,
                        )
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
                        # Orchestrator-owned preparers run before validation so
                        # facts the schema requires but the model cannot know
                        # (verified task-asset digests) are populated first.
                        structured = apply_result_preparers(
                            structured,
                            result_preparers,
                        )
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
    def _dependency_marker_line(text: str) -> str:
        for line in text.splitlines():
            normalized = line.lower()
            if (
                'background dependency install failed' in normalized
                or 'cannot find module' in normalized
                or 'module not found' in normalized
                or 'resolvemessage' in normalized
            ):
                return ' '.join(line.split())[:300]
        return 'the OpenCode runtime reports an unresolvable plugin dependency'

    def _dependency_failure_detail(
        self,
        *,
        response: httpx.Response,
        workspace: Path,
        agent: AgentName,
    ) -> str | None:
        # The HTTP body carries the ResolveMessage; OpenCode's own log holds
        # the failed background install when the body is generic.
        if dependency_resolution_failure(response.text):
            return self._dependency_marker_line(response.text)
        log_text = self._runtime_log_tail(workspace=workspace, agent=agent)
        if dependency_resolution_failure(log_text):
            return self._dependency_marker_line(log_text)
        return None

    @staticmethod
    def _runtime_log_tail(*, workspace: Path, agent: AgentName) -> str:
        runtime_root = workspace.parent / 'runtime' / agent.value
        candidates = (
            runtime_root / 'opencode.log',
            runtime_root / 'data' / 'opencode' / 'log' / 'opencode.log',
        )
        chunks: list[str] = []
        for path in candidates:
            try:
                with path.open('rb') as handle:
                    handle.seek(0, os.SEEK_END)
                    size = handle.tell()
                    handle.seek(max(0, size - _RUNTIME_LOG_TAIL_BYTES))
                    chunks.append(handle.read().decode('utf-8', 'replace'))
            except OSError:
                continue
        return '\n'.join(chunks)

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

    @staticmethod
    def _repeated_tool_abort(
        signatures: list[str], limit: int
    ) -> dict[str, Any] | None:
        # Payload for `limit` byte-identical terminal tool signatures. The
        # repeated tool name and count are safe to persist; the input fingerprint
        # may contain secrets so only a short digest travels with the abort.
        if limit <= 1 or len(signatures) < limit:
            return None
        repeated = signatures[-limit:]
        if len(set(repeated)) != 1:
            return None
        signature = repeated[0]
        try:
            parsed = json.loads(signature)
        except (TypeError, ValueError):
            parsed = {}
        tool = parsed.get('tool') if isinstance(parsed, dict) else None
        return {
            'tool': tool if isinstance(tool, str) else None,
            'count': len(repeated),
            'input_digest': hashlib.sha256(
                signature.encode('utf-8')
            ).hexdigest()[:16],
        }

    @staticmethod
    def _turn_step_count(messages: list[dict[str, Any]]) -> int:
        # Per-turn loop-step proxy. OpenCode's session loop increments its own
        # `step` counter once per assistant message it creates for the current
        # user prompt (the `step=N` it logs), so the number of assistant
        # messages created after the latest user message equals the live step
        # count. Counting terminal tool parts undercounts text/reasoning-only
        # steps and overcounts parallel calls, so the assistant-message count is
        # the more robust proxy; step-start markers and terminal-tool counts are
        # fallbacks for payloads that omit message-role metadata.
        last_user_index = -1
        for index, message in enumerate(messages):
            info = message.get('info') if isinstance(message, dict) else None
            if isinstance(info, dict) and info.get('role') == 'user':
                last_user_index = index
        if last_user_index >= 0:
            return sum(
                1
                for message in messages[last_user_index + 1 :]
                if isinstance(message, dict)
                and isinstance(message.get('info'), dict)
                and message['info'].get('role') == 'assistant'
            )
        step_markers = sum(
            1
            for message in messages
            if isinstance(message, dict)
            for part in message.get('parts', [])
            if isinstance(part, dict) and part.get('type') == 'step-start'
        )
        if step_markers:
            return step_markers
        return len(OpenCodeProcessRuntime._terminal_tool_signatures(messages))

    def _watch_turn(
        self,
        *,
        handle: _ProcessHandle,
        session_id: str,
        workspace: Path,
        stop: threading.Event,
        abort_reasons: list[_TurnAbort],
    ) -> None:
        deadline = time.monotonic() + self.settings.opencode_turn_timeout_seconds
        while not stop.wait(2):
            reason: str | None = None
            failure_class: str | None = None
            details: dict[str, Any] | None = None
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
                        details = self._repeated_tool_abort(signatures, limit)
                        if details is not None:
                            reason = (
                                'OpenCode turn aborted after '
                                f'{limit} identical terminal tool calls'
                            )
                            failure_class = 'repeated_tool_loop'
                        else:
                            step_limit = self.settings.opencode_turn_step_limit
                            step_count = self._turn_step_count(messages)
                            # A varied-call runaway evades the identical-tool
                            # guard above; the step budget stops it before the
                            # wall clock does. 0 disables the budget.
                            if step_limit > 0 and step_count > step_limit:
                                reason = (
                                    'OpenCode turn exceeded the step budget of '
                                    f'{step_limit} steps ({step_count} steps) '
                                    'without returning a result'
                                )
                                failure_class = 'step_budget_exceeded'
                                details = {
                                    'step_count': step_count,
                                    'step_limit': step_limit,
                                }
                except (httpx.HTTPError, ValueError):
                    continue
            if reason is None:
                continue
            abort_reasons.append(
                _TurnAbort(
                    reason=reason,
                    failure_class=failure_class or 'turn_timeout',
                    details=details,
                )
            )
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

    def session_context_tokens(
        self,
        *,
        run_id: str,
        agent: AgentName,
        session_id: str,
    ) -> int | None:
        observed = self._observed_session_tokens.get((run_id, agent))
        if observed is None or observed[0] != session_id:
            return None
        return observed[1]

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
        self._observed_session_tokens.clear()
        with self._port_lock:
            self._reserved_ports.clear()

    def release(self, *, run_id: str, agent: AgentName) -> None:
        # Used for short-lived compiler sessions; terminating the process also
        # drops the isolated session state so no half-finished agent context
        # survives.
        self._observed_session_tokens.pop((run_id, agent), None)
        handle = self._handles.pop((run_id, agent), None)
        if handle is not None:
            self._stop_handle(handle)
