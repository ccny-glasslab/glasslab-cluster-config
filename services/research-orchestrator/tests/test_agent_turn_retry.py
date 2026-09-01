"""Bounded retry of retryable agent-turn failures (issue #229).

A protocol-draft turn that burns the full wall-clock budget (or a transient
provider/startup failure) must not terminally fail the run: the engine should
retry the turn with a rotated fresh session, bounded by a configured retry
count. Deterministic failures (structured-output validation, kind mismatch)
must never be retried.
"""

from __future__ import annotations

import pytest

from app.engine import ResearchOrchestrator
from app.mock_runtime import ScriptedMockRuntime
from app.opencode_runtime import AgentRuntime, OpenCodeRuntimeError
from app.schemas import AgentName, RunCreateRequest, TurnKind


class FlakyTurnRuntime:
    """Delegates to a real mock runtime but fails the first N run_turn calls."""

    def __init__(
        self,
        inner: AgentRuntime,
        *,
        fail_calls: int,
        failure_class: str,
    ) -> None:
        self.inner = inner
        self.fail_calls = fail_calls
        self.failure_class = failure_class
        self.attempts = 0

    def ensure_session(self, **kwargs):
        return self.inner.ensure_session(**kwargs)

    def run_turn(self, **kwargs):
        self.attempts += 1
        if self.attempts <= self.fail_calls:
            raise OpenCodeRuntimeError(
                "OpenCode turn exceeded the hard wall-clock limit of 1800 seconds",
                failure_class=self.failure_class,
            )
        return self.inner.run_turn(**kwargs)

    def abort(self, **kwargs):
        return self.inner.abort(**kwargs)

    def close(self):
        return self.inner.close()

    def release(self, **kwargs):
        return self.inner.release(**kwargs)


def _make_run(engine: ResearchOrchestrator, objective: str):
    return engine.create_run(RunCreateRequest(objective=objective))


def _draft_prompt() -> str:
    return (
        "Draft a concrete program.md for this objective: retry test\n\n"
        "Evaluation contract: contract://generic-task-integrity-v1/1.0.0"
    )


def test_retryable_turn_failure_is_retried_with_fresh_session(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Retry test objective for the agent-turn retry path.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=1, failure_class="turn_timeout"
    )
    _, result = engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={"objective": "retry test"},
    )
    assert result.kind == TurnKind.PROTOCOL_DRAFT
    assert engine.runtime.attempts == 2
    events = store.list_events(run.run_id)
    assert any(e.event_type == "agent.session_rotated" for e in events)
    final = store.get_run(run.run_id)
    assert final.honeydew_session_id is not None


def test_non_retryable_turn_failure_is_not_retried(orchestrator_bundle) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Non-retryable failure objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=10, failure_class="validation"
    )
    with pytest.raises(OpenCodeRuntimeError):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={"objective": "retry test"},
        )
    assert engine.runtime.attempts == 1


def test_retry_bound_is_respected(orchestrator_bundle) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Bounded retry objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=10, failure_class="turn_timeout"
    )
    with pytest.raises(OpenCodeRuntimeError):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={"objective": "retry test"},
        )
    assert engine.runtime.attempts == 1 + settings.agent_turn_max_retries


def test_provider_failure_is_retryable(orchestrator_bundle) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Provider failure objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=1, failure_class="provider"
    )
    _, result = engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={"objective": "retry test"},
    )
    assert result.kind == TurnKind.PROTOCOL_DRAFT
    assert engine.runtime.attempts == 2


def test_startup_failure_is_retryable(orchestrator_bundle) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = _make_run(engine, "Startup failure objective.")
    engine.runtime = FlakyTurnRuntime(
        runtime, fail_calls=1, failure_class="startup"
    )
    _, result = engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={"objective": "retry test"},
    )
    assert result.kind == TurnKind.PROTOCOL_DRAFT
    assert engine.runtime.attempts == 2