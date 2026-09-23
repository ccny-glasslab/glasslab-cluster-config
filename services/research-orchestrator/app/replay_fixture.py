"""Fixture format and recorder for engine-level runtime replay.

A replay fixture is a JSON document keyed by ``<agent>:<turn_kind>`` whose
values are ordered recorded turns. ``ReplayRecorder`` captures typed
``AgentTurnResult`` objects (optionally with produced-file contents) into that
format, and ``RecordingRuntime`` wraps any real ``AgentRuntime`` to record
every completed turn. ``ReplayAgentRuntime`` in ``replay_runtime`` consumes the
same format.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .opencode_runtime import AgentRuntime, ResultPreparer, RuntimeSession
from .schemas import AgentName, AgentTurnResult, TurnKind


REPLAY_SCHEMA_VERSION = 'glasslab-runtime-replay-v1'


class ReplayRuntimeError(RuntimeError):
    """A replay was requested for a turn the fixture never recorded."""


@dataclass(frozen=True, slots=True)
class ReplayTurn:
    agent: AgentName
    turn_kind: TurnKind
    result: AgentTurnResult
    files: Mapping[str, str] = field(default_factory=dict)
    prompt_marker: str | None = None


def turn_key(agent: AgentName, turn_kind: TurnKind) -> str:
    return f'{agent.value}:{turn_kind.value}'


def parse_turn_key(key: str) -> tuple[AgentName, TurnKind]:
    agent_value, separator, kind_value = key.partition(':')
    if not separator:
        raise ReplayRuntimeError(f'malformed replay turn key: {key!r}')
    try:
        return AgentName(agent_value), TurnKind(kind_value)
    except ValueError as exc:
        raise ReplayRuntimeError(f'unknown replay turn key: {key!r}') from exc


def parse_turns(
    payload: Mapping[str, Any],
) -> dict[tuple[AgentName, TurnKind], list[ReplayTurn]]:
    if payload.get('schema_version') != REPLAY_SCHEMA_VERSION:
        raise ReplayRuntimeError(
            'unsupported replay fixture schema: '
            f'{payload.get("schema_version")!r}'
        )
    raw_turns = payload.get('turns')
    if not isinstance(raw_turns, Mapping):
        raise ReplayRuntimeError('replay fixture has no turns mapping')
    turns: dict[tuple[AgentName, TurnKind], list[ReplayTurn]] = {}
    for key, raw_records in raw_turns.items():
        agent, turn_kind = parse_turn_key(str(key))
        records: list[ReplayTurn] = []
        for raw in raw_records:
            result = AgentTurnResult.model_validate(raw['result'])
            if result.kind != turn_kind:
                raise ReplayRuntimeError(
                    f'recorded turn {key!r} carries kind {result.kind.value!r}'
                )
            records.append(
                ReplayTurn(
                    agent=agent,
                    turn_kind=turn_kind,
                    result=result,
                    files=dict(raw.get('files') or {}),
                    prompt_marker=raw.get('prompt_marker'),
                )
            )
        if records:
            turns[(agent, turn_kind)] = records
    return turns


def load_fixture(path: Path | str) -> Mapping[str, Any]:
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, Mapping):
        raise ReplayRuntimeError('replay fixture must be a JSON object')
    return payload


class ReplayRecorder:
    """Capture ``AgentTurnResult`` objects into the replay fixture format."""

    def __init__(self) -> None:
        self._turns: dict[str, list[dict[str, Any]]] = {}

    def capture(
        self,
        *,
        agent: AgentName,
        turn_kind: TurnKind,
        result: AgentTurnResult,
        files: Mapping[str, str] | None = None,
        prompt_marker: str | None = None,
    ) -> None:
        if result.kind != turn_kind:
            raise ReplayRuntimeError(
                f'cannot record {result.kind.value!r} as {turn_kind.value!r}'
            )
        record: dict[str, Any] = {'result': result.model_dump(mode='json')}
        if files:
            record['files'] = dict(files)
        if prompt_marker:
            record['prompt_marker'] = prompt_marker
        self._turns.setdefault(turn_key(agent, turn_kind), []).append(record)

    def to_fixture(self) -> dict[str, Any]:
        return {
            'schema_version': REPLAY_SCHEMA_VERSION,
            'provenance': {'generated_by': 'ReplayRecorder'},
            'turns': {
                key: list(records) for key, records in self._turns.items()
            },
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_fixture(), indent=2, ensure_ascii=False) + '\n'
        )


class RecordingRuntime(AgentRuntime):
    """Delegate to a real runtime and record every completed turn."""

    def __init__(
        self,
        *,
        inner: AgentRuntime,
        recorder: ReplayRecorder | None = None,
    ) -> None:
        self.inner = inner
        self.recorder = recorder or ReplayRecorder()

    def ensure_session(
        self,
        *,
        run_id: str,
        agent: AgentName,
        workspace: Path,
        existing_session_id: str | None,
        model_override: str | None = None,
        base_url_override: str | None = None,
        knowledge_tool: object | None = None,
    ) -> RuntimeSession:
        return self.inner.ensure_session(
            run_id=run_id,
            agent=agent,
            workspace=workspace,
            existing_session_id=existing_session_id,
            model_override=model_override,
            base_url_override=base_url_override,
            knowledge_tool=knowledge_tool,
        )

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
        knowledge_tool: object | None = None,
        result_preparers: Sequence[ResultPreparer] = (),
    ) -> tuple[AgentTurnResult, str | None]:
        result, message_id = self.inner.run_turn(
            run_id=run_id,
            agent=agent,
            workspace=workspace,
            session_id=session_id,
            prompt=prompt,
            model_override=model_override,
            base_url_override=base_url_override,
            knowledge_tool=knowledge_tool,
            result_preparers=result_preparers,
        )
        files: dict[str, str] = {}
        for produced in result.produced_files:
            path = workspace / produced.path
            if path.is_file():
                files[produced.path] = path.read_text()
        self.recorder.capture(
            agent=agent,
            turn_kind=result.kind,
            result=result,
            files=files or None,
        )
        return result, message_id

    def session_context_tokens(
        self,
        *,
        run_id: str,
        agent: AgentName,
        session_id: str,
    ) -> int | None:
        return self.inner.session_context_tokens(
            run_id=run_id, agent=agent, session_id=session_id
        )

    def abort(self, *, run_id: str, agent: AgentName, session_id: str) -> None:
        self.inner.abort(run_id=run_id, agent=agent, session_id=session_id)

    def release(self, *, run_id: str, agent: AgentName) -> None:
        self.inner.release(run_id=run_id, agent=agent)

    def close(self) -> None:
        self.inner.close()
