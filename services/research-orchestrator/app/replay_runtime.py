"""Deterministic engine replay over recorded ``AgentTurnResult`` turns.

A ``ReplayAgentRuntime`` implements the exact ``AgentRuntime`` surface the
engine consumes (``ensure_session`` / ``run_turn`` / ``abort`` / ``release``),
but serves pre-recorded turns from a JSON fixture instead of launching an
OpenCode subprocess or calling a model. The engine already tells the runtime
which result variant it requires through the authoritative structured-output
contract suffix in the prompt, so a replay turn is keyed by
``(agent, turn_kind)`` and is fully deterministic: no clock, no uuid, no
network.

This is an *engine* replay: it feeds the real ``ResearchOrchestrator`` state
machine (store, policy, gates, workspace materialization) so a multi-gate
workflow can be exercised in seconds. It is deliberately distinct from
``runtime_replay.py``, the standalone benchmark rig that shells out to the
real CLI and never touches the store or engine.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from .opencode_runtime import AgentRuntime, ResultPreparer, RuntimeSession
from .replay_fixture import (
    ReplayRecorder,
    ReplayRuntimeError,
    ReplayTurn,
    load_fixture,
    parse_turns,
    turn_key,
)
from .schemas import AgentName, AgentTurnResult, TurnKind


__all__ = [
    'ReplayAgentRuntime',
    'ReplayRecorder',
    'ReplayRuntimeError',
    'ReplayTurn',
]

# The engine appends this exact contract line to every turn it drives (see
# ResearchOrchestrator._required_turn_kind_instruction); parsing it is how the
# replay runtime learns which recorded variant this call must serve.
_KIND_CONTRACT_RE = re.compile(r'`kind` field to exactly `([a-z_]+)`')


class ReplayAgentRuntime(AgentRuntime):
    """Serve recorded turns to the engine with no subprocess and no network."""

    def __init__(
        self,
        *,
        fixture_path: Path | str,
        runner_image: str | None = None,
    ) -> None:
        self.runner_image = runner_image
        self.sessions: dict[tuple[str, AgentName], RuntimeSession] = {}
        self.turn_counts: defaultdict[AgentName, int] = defaultdict(int)
        self.aborted: list[tuple[str, AgentName, str]] = []
        self.released: list[tuple[str, AgentName]] = []
        self.prompts: list[tuple[AgentName, str]] = []
        self.replayed: list[tuple[AgentName, TurnKind]] = []
        self.repeated: list[tuple[AgentName, TurnKind]] = []
        self._turns = parse_turns(load_fixture(fixture_path))
        self._cursors: defaultdict[tuple[AgentName, TurnKind], int] = (
            defaultdict(int)
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
        knowledge_tool: object | None = None,
    ) -> RuntimeSession:
        del workspace, model_override, base_url_override, knowledge_tool
        key = (run_id, agent)
        session = self.sessions.get(key)
        if session is None:
            # Deterministic ids (no uuid): two fresh runtimes replaying the
            # same fixture must produce byte-identical sessions.
            session = RuntimeSession(
                runtime_id=f'replay-runtime-{agent.value}-{run_id[:8]}',
                session_id=existing_session_id
                or f'replay-session-{agent.value}-{run_id[:12]}',
            )
            self.sessions[key] = session
        return session

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
        # result_preparers normalizes raw runtime payloads; replay already holds
        # typed AgentTurnResults, so there is nothing to transform.
        del run_id, session_id, model_override, base_url_override
        del knowledge_tool, result_preparers
        self.turn_counts[agent] += 1
        self.prompts.append((agent, prompt))
        expected = self._expected_kind(agent, prompt)
        records = self._turns.get((agent, expected))
        if not records:
            raise ReplayRuntimeError(
                f'no recorded replay turn for {turn_key(agent, expected)}'
            )
        cursor = self._cursors[(agent, expected)]
        if cursor < len(records):
            record = records[cursor]
            self._cursors[(agent, expected)] = cursor + 1
        else:
            # Revisions and repairs legitimately repeat a turn kind; replay the
            # last recorded instance rather than failing the deterministic flow.
            record = records[-1]
            self.repeated.append((agent, expected))
        self._materialize(workspace, record)
        self.replayed.append((agent, expected))
        message_id = f'replay-message-{agent.value}-{self.turn_counts[agent]:04d}'
        return record.result, message_id

    def session_context_tokens(
        self,
        *,
        run_id: str,
        agent: AgentName,
        session_id: str,
    ) -> int | None:
        del run_id, agent, session_id
        return None

    def abort(self, *, run_id: str, agent: AgentName, session_id: str) -> None:
        self.aborted.append((run_id, agent, session_id))

    def release(self, *, run_id: str, agent: AgentName) -> None:
        self.released.append((run_id, agent))
        self.sessions.pop((run_id, agent), None)

    def _expected_kind(self, agent: AgentName, prompt: str) -> TurnKind:
        match = _KIND_CONTRACT_RE.search(prompt)
        if match is not None:
            try:
                return TurnKind(match.group(1))
            except ValueError as exc:
                raise ReplayRuntimeError(
                    f'prompt requires unrecorded turn kind {match.group(1)!r}'
                ) from exc
        for (recorded_agent, kind), records in self._turns.items():
            if recorded_agent != agent:
                continue
            if any(
                record.prompt_marker and record.prompt_marker in prompt
                for record in records
            ):
                return kind
        raise ReplayRuntimeError(
            f'cannot determine expected turn kind for {agent.value}'
        )

    @staticmethod
    def _materialize(workspace: Path, record: ReplayTurn) -> None:
        # The engine checks that declared produced files exist on disk, so a
        # replay must write the recorded bytes (or an empty placeholder for a
        # turn recorded without file contents).
        contents = dict(record.files)
        for produced in record.result.produced_files:
            contents.setdefault(produced.path, '')
        for relative, content in contents.items():
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
