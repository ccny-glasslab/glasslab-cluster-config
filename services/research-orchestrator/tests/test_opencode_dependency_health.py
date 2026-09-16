"""Worktree ``.opencode`` dependency health (issue #482).

A long-lived run's per-run worktree can end up with a stale ``.opencode``
dependency tree: OpenCode's background dependency install fails, the plugin
cannot resolve (``ResolveMessage: Cannot find module '@opencode-ai/plugin'``),
and every ``POST /session/{id}/message`` returns HTTP 500. The raw transport
error is retryable, so the engine rotated the session and resumed until the
run burned ``maximum_turns`` and ended ``TIMED_OUT``.

These tests lock the fix at both layers:

- the runtime detects the module-resolution / failed-install condition and
  reports it as ``runtime_dependency_unavailable`` with an actionable message
  (worktree path, module, shared cache root, offline reinstall remedy);
- the engine treats the class as infrastructure -- it attempts one bounded
  repair from the shared cache, retries the turn at most once, never consumes
  the turn budget, never loops, and keeps genuine model failures unchanged.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app import opencode_runtime
from app.config import Settings
from app.opencode_runtime import OpenCodeProcessRuntime, OpenCodeRuntimeError
from app.schemas import AgentName, RunCreateRequest, RunState, TurnKind

PLUGIN_MODULE = '@opencode-ai/plugin'

_RUNTIME_ERROR_BODY = (
    'failed error="ResolveMessage: Cannot find module '
    f"'{PLUGIN_MODULE}' from "
    "'/mnt/runs/run-1/honeydew-worktree/.opencode/node_modules'\""
)


def test_dependency_resolution_failure_markers_are_specific() -> None:
    classify = opencode_runtime.dependency_resolution_failure
    assert classify(_RUNTIME_ERROR_BODY)
    assert classify(
        'level=WARN message="background dependency install failed" '
        'dir=/mnt/runs/run-1/honeydew-worktree/.opencode'
    )
    # A generic 500 or an unrelated missing module is not the worktree
    # dependency condition and must keep its existing classification.
    assert not classify('Internal Server Error')
    assert not classify("Cannot find module 'left-pad'")
    assert not classify('')


def _runtime_with_transport_response(
    monkeypatch,
    tmp_path,
    *,
    status_code: int,
    text: str,
):
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=text)

    cache_root = tmp_path / 'shared-cache'
    runtime = OpenCodeProcessRuntime(
        Settings(opencode_shared_cache_root=str(cache_root))
    )
    workspace = tmp_path / 'honeydew-worktree'
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
    # The watchdog polls the (unreachable) test host; the detection under test
    # happens in the request loop, so the poll is disabled.
    monkeypatch.setattr(runtime, '_watch_turn', lambda **_: None)
    return runtime, workspace, cache_root


def _run_failing_turn(runtime, workspace):
    return runtime.run_turn(
        run_id='run-1',
        agent=AgentName.HONEYDEW,
        workspace=workspace,
        session_id='session-1',
        prompt='Draft a protocol.',
    )


def test_run_turn_classifies_module_resolution_failure(
    tmp_path, monkeypatch,
) -> None:
    runtime, workspace, cache_root = _runtime_with_transport_response(
        monkeypatch,
        tmp_path,
        status_code=500,
        text=_RUNTIME_ERROR_BODY,
    )

    with pytest.raises(OpenCodeRuntimeError) as excinfo:
        _run_failing_turn(runtime, workspace)

    error = excinfo.value
    assert error.failure_class == 'runtime_dependency_unavailable'
    message = str(error)
    assert str(workspace) in message
    assert PLUGIN_MODULE in message
    assert str(cache_root) in message
    assert 'bun install --offline' in message
    assert error.details == {
        'worktree': str(workspace),
        'module': PLUGIN_MODULE,
        'cache_root': str(cache_root),
        'detail': error.details['detail'],
    }


def test_run_turn_classifies_background_install_failure_from_log(
    tmp_path, monkeypatch,
) -> None:
    runtime, workspace, _ = _runtime_with_transport_response(
        monkeypatch,
        tmp_path,
        status_code=500,
        text='Internal Server Error',
    )
    log_dir = workspace.parent / 'runtime' / 'honeydew' / 'data' / (
        'opencode'
    ) / 'log'
    log_dir.mkdir(parents=True)
    (log_dir / 'opencode.log').write_text(
        'level=WARN message="background dependency install failed" '
        f'dir={workspace / ".opencode"}\n',
        encoding='utf-8',
    )

    with pytest.raises(OpenCodeRuntimeError) as excinfo:
        _run_failing_turn(runtime, workspace)

    assert excinfo.value.failure_class == 'runtime_dependency_unavailable'


def test_unrelated_500_keeps_its_transport_error(tmp_path, monkeypatch) -> None:
    runtime, workspace, _ = _runtime_with_transport_response(
        monkeypatch,
        tmp_path,
        status_code=500,
        text='Internal Server Error',
    )

    with pytest.raises(httpx.HTTPStatusError):
        _run_failing_turn(runtime, workspace)


def test_repair_workspace_dependencies_links_cached_plugin(tmp_path) -> None:
    cache_root = tmp_path / 'shared-cache'
    cached_plugin = (
        cache_root
        / 'bun'
        / 'install'
        / 'cache'
        / '@opencode-ai'
        / 'plugin@1.4.0'
    )
    cached_plugin.mkdir(parents=True)
    (cached_plugin / 'package.json').write_text(
        json.dumps({'name': PLUGIN_MODULE, 'version': '1.4.0'}),
        encoding='utf-8',
    )
    workspace = tmp_path / 'honeydew-worktree'
    (workspace / '.opencode').mkdir(parents=True)
    runtime = OpenCodeProcessRuntime(
        Settings(opencode_shared_cache_root=str(cache_root))
    )

    first = runtime.repair_workspace_dependencies(workspace=workspace)

    assert first.status == 'repaired'
    assert first.source == str(cached_plugin)
    target = (
        workspace
        / '.opencode'
        / 'node_modules'
        / '@opencode-ai'
        / 'plugin'
    )
    assert (target / 'package.json').is_file()
    # A second repair is a no-op, never a duplicate or a loop.
    second = runtime.repair_workspace_dependencies(workspace=workspace)
    assert second.status == 'already_resolved'


def test_repair_workspace_dependencies_unavailable_without_cached_plugin(
    tmp_path,
) -> None:
    cache_root = tmp_path / 'shared-cache'
    cache_root.mkdir()
    workspace = tmp_path / 'honeydew-worktree'
    (workspace / '.opencode').mkdir(parents=True)
    runtime = OpenCodeProcessRuntime(
        Settings(opencode_shared_cache_root=str(cache_root))
    )

    result = runtime.repair_workspace_dependencies(workspace=workspace)

    assert result.status == 'unavailable'
    assert result.source is None
    assert str(cache_root) in result.detail


class DependencyUnavailableRuntime:
    """Reports the issue #482 runtime failure class to the engine."""

    def __init__(self, inner, *, repair_result, fail_calls=1000) -> None:
        self.inner = inner
        self.repair_result = repair_result
        self.fail_calls = fail_calls
        self.attempts = 0
        self.repair_calls = 0
        self.release_calls = 0

    def ensure_session(self, **kwargs):
        return self.inner.ensure_session(**kwargs)

    def run_turn(self, **kwargs):
        self.attempts += 1
        if self.attempts <= self.fail_calls:
            raise OpenCodeRuntimeError(
                'OpenCode cannot resolve the worktree plugin dependency '
                f"'{PLUGIN_MODULE}' from "
                f'{kwargs["workspace"]}/.opencode; reinstall it from the '
                'shared cache and resume.',
                failure_class='runtime_dependency_unavailable',
                details={
                    'worktree': str(kwargs['workspace']),
                    'module': PLUGIN_MODULE,
                    'cache_root': '/shared/opencode-cache',
                    'detail': 'background dependency install failed',
                },
            )
        return self.inner.run_turn(**kwargs)

    def repair_workspace_dependencies(self, *, workspace):
        self.repair_calls += 1
        return self.repair_result

    def abort(self, **kwargs):
        return self.inner.abort(**kwargs)

    def release(self, **kwargs):
        self.release_calls += 1
        return self.inner.release(**kwargs)

    def close(self):
        return self.inner.close()


class FailingTurnRuntime:
    """Fails every turn with one fixed failure class."""

    def __init__(self, inner, *, failure_class, details=None) -> None:
        self.inner = inner
        self.failure_class = failure_class
        self.details = details
        self.attempts = 0
        self.repair_calls = 0

    def ensure_session(self, **kwargs):
        return self.inner.ensure_session(**kwargs)

    def run_turn(self, **kwargs):
        self.attempts += 1
        raise OpenCodeRuntimeError(
            f'OpenCode turn failed with {self.failure_class}',
            failure_class=self.failure_class,
            details=self.details,
        )

    def repair_workspace_dependencies(self, *, workspace):
        self.repair_calls += 1
        return opencode_runtime.DependencyRepairResult(
            status='unavailable',
            source=None,
            detail='normal failures must never repair dependencies',
        )

    def abort(self, **kwargs):
        return self.inner.abort(**kwargs)

    def release(self, **kwargs):
        return self.inner.release(**kwargs)

    def close(self):
        return self.inner.close()


def _draft_prompt() -> str:
    return (
        'Draft a concrete program.md for this objective: dependency health\n\n'
        'Evaluation contract: contract://generic-task-integrity-v1/1.0.0'
    )


def _failed_turn_events(store, run_id, event_type):
    return [
        event
        for event in store.list_events(run_id)
        if event.event_type == event_type
    ]


def test_runtime_dependency_failure_does_not_consume_turn_budget(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Stale worktree dependency objective.')
    )
    before = store.get_run(run.run_id).turn_number
    engine.runtime = DependencyUnavailableRuntime(
        runtime,
        repair_result=opencode_runtime.DependencyRepairResult(
            status='unavailable',
            source=None,
            detail='no cached plugin package',
        ),
    )

    for _ in range(3):
        with pytest.raises(OpenCodeRuntimeError) as excinfo:
            engine._run_agent_turn(
                run_id=run.run_id,
                agent=AgentName.HONEYDEW,
                prompt=_draft_prompt(),
                expected_kind=TurnKind.PROTOCOL_DRAFT,
                input_event={'objective': 'stale worktree dependency'},
            )
        assert excinfo.value.failure_class == 'runtime_dependency_unavailable'

    final = store.get_run(run.run_id)
    # The failed attempts are infrastructure, not model turns: the counter
    # that _check_turn_budget gates on never advanced toward maximum_turns.
    assert final.turn_number == before
    assert final.state != RunState.TIMED_OUT
    # One runtime call and one bounded repair per attempt: no internal loop.
    assert engine.runtime.attempts == 3
    assert engine.runtime.repair_calls == 3
    # The broken process is released so the next attempt resolves deps fresh.
    assert engine.runtime.release_calls == 3

    unavailable_events = _failed_turn_events(
        store, run.run_id, 'agent.runtime_dependency_unavailable'
    )
    assert len(unavailable_events) == 3
    payload = unavailable_events[-1].payload
    assert payload['turn_budget_consumed'] is False
    assert payload['worktree'] == final.honeydew_workspace
    assert payload['module'] == PLUGIN_MODULE
    assert payload['cache_root'] == '/shared/opencode-cache'
    # An infrastructure failure must not rotate the session: rotation is the
    # retry-and-checkpoint machinery reserved for model failures.
    assert not _failed_turn_events(
        store, run.run_id, 'agent.session_rotated'
    )


def test_runtime_dependency_repair_is_attempted_once_then_fails_closed(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Bounded dependency repair objective.')
    )
    before = store.get_run(run.run_id).turn_number
    engine.runtime = DependencyUnavailableRuntime(
        runtime,
        repair_result=opencode_runtime.DependencyRepairResult(
            status='repaired',
            source='/shared/opencode-cache/@opencode-ai/plugin@1.4.0',
            detail='linked the cached plugin into the worktree',
        ),
    )

    with pytest.raises(OpenCodeRuntimeError) as excinfo:
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={'objective': 'bounded dependency repair'},
        )

    assert excinfo.value.failure_class == 'runtime_dependency_unavailable'
    # The original attempt plus exactly one retry after the repair; never a
    # second repair attempt.
    assert engine.runtime.attempts == 2
    assert engine.runtime.repair_calls == 1
    final = store.get_run(run.run_id)
    assert final.turn_number == before
    assert final.state != RunState.TIMED_OUT

    repair_events = _failed_turn_events(
        store, run.run_id, 'agent.runtime_dependency_repair_attempted'
    )
    assert len(repair_events) == 1
    assert repair_events[0].payload['status'] == 'repaired'
    assert repair_events[0].payload['worktree'] == final.honeydew_workspace
    assert len(
        _failed_turn_events(
            store, run.run_id, 'agent.runtime_dependency_unavailable'
        )
    ) == 1


def test_runtime_dependency_repair_self_heals_the_turn(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Self-healed dependency objective.')
    )
    before = store.get_run(run.run_id).turn_number
    engine.runtime = DependencyUnavailableRuntime(
        runtime,
        repair_result=opencode_runtime.DependencyRepairResult(
            status='repaired',
            source='/shared/opencode-cache/@opencode-ai/plugin@1.4.0',
            detail='linked the cached plugin into the worktree',
        ),
        fail_calls=1,
    )

    _, result = engine._run_agent_turn(
        run_id=run.run_id,
        agent=AgentName.HONEYDEW,
        prompt=_draft_prompt(),
        expected_kind=TurnKind.PROTOCOL_DRAFT,
        input_event={'objective': 'self-healed dependency'},
    )

    assert result.kind == TurnKind.PROTOCOL_DRAFT
    assert engine.runtime.attempts == 2
    assert engine.runtime.repair_calls == 1
    # Exactly the one successful turn counts against the budget.
    assert store.get_run(run.run_id).turn_number == before + 1
    assert not _failed_turn_events(
        store, run.run_id, 'agent.runtime_dependency_unavailable'
    )


def test_step_budget_failure_keeps_pre_fix_behavior(
    orchestrator_bundle,
) -> None:
    settings, store, cluster, runtime, engine = orchestrator_bundle
    run = engine.create_run(
        RunCreateRequest(objective='Step budget regression objective.')
    )
    before = store.get_run(run.run_id).turn_number
    engine.runtime = FailingTurnRuntime(
        runtime,
        failure_class='step_budget_exceeded',
        details={'step_count': 300, 'step_limit': 250},
    )

    with pytest.raises(OpenCodeRuntimeError):
        engine._run_agent_turn(
            run_id=run.run_id,
            agent=AgentName.HONEYDEW,
            prompt=_draft_prompt(),
            expected_kind=TurnKind.PROTOCOL_DRAFT,
            input_event={'objective': 'step budget regression'},
        )

    # Unchanged model-failure semantics: one attempt, session rotation, and
    # the failed model turn still counts against the turn budget.
    assert engine.runtime.attempts == 1
    assert engine.runtime.repair_calls == 0
    final = store.get_run(run.run_id)
    assert final.turn_number == before + 1
    assert _failed_turn_events(store, run.run_id, 'agent.session_rotated')
    assert not _failed_turn_events(
        store, run.run_id, 'agent.runtime_dependency_unavailable'
    )
